--
-- ai-injection-guard (GW-09): a thin client of the guard service. All decision logic lives
-- in the guard (7.3); this plugin only extracts the messages, asks, and enforces the answer.
--
--   access phase, priority 1060: after ai-prompt-guard (1072), before ai-proxy (1040)
--   POST {messages, stage} to the guard with traceparent and X-Agent-Id, timeout 3 s
--   verdict "block"   -> 403, OpenAI-style error with decision_id
--   guard unreachable -> 503 (fail closed), unless fail_open
--
-- stage "llm" inspects LLM requests (F2); stage "agent_ingress" inspects what a person
-- sends to an agent before the agent sees it (DEMO-02). For agent /task bodies the
-- "instruction" field is inspected as a user message.
--
local core = require("apisix.core")
local http = require("resty.http")

local plugin_name = "ai-injection-guard"

local schema = {
    type = "object",
    properties = {
        endpoint = {
            type = "string",
            default = "http://guard.ai.svc.cluster.local:8000/v1/inspect/llm",
        },
        stage = { type = "string", enum = { "llm", "agent_ingress" }, default = "llm" },
        timeout_ms = { type = "integer", minimum = 100, maximum = 10000, default = 3000 },
        fail_open = { type = "boolean", default = false },
    },
}

local _M = {
    version = 0.1,
    priority = 1060,
    name = plugin_name,
    schema = schema,
}


function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end


local function reject(status, err_type, message, decision_id, detail)
    core.response.set_header("Content-Type", "application/json")
    local err = { type = err_type, message = message, decision_id = decision_id }
    -- the guard's own explanation (check, score, reason), so a client can tell its user why
    for k, v in pairs(detail or {}) do
        err[k] = v
    end
    return status, { error = err }
end


local function messages_of(body)
    if type(body.messages) == "table" then
        return body.messages
    end
    if type(body.instruction) == "string" then
        return { { role = "user", content = body.instruction } }
    end
    return nil
end


function _M.access(conf, ctx)
    local body = core.request.get_json_request_body_table()
    if type(body) ~= "table" then
        return reject(400, "invalid_request_error", "request body must be JSON")
    end
    local messages = messages_of(body)
    if not messages then
        return  -- nothing to inspect
    end

    local traceparent = ngx.var.opentelemetry_context_traceparent
    if traceparent == nil or traceparent == "" then
        traceparent = core.request.header(ctx, "traceparent")
    end

    local httpc = http.new()
    httpc:set_timeout(conf.timeout_ms)
    local res, err = httpc:request_uri(conf.endpoint, {
        method = "POST",
        body = core.json.encode({ messages = messages, stage = conf.stage }),
        headers = {
            ["Content-Type"] = "application/json",
            ["X-Agent-Id"] = ctx.consumer_name or "",
            ["traceparent"] = traceparent,
        },
        keepalive = true,
    })

    if not res or res.status ~= 200 then
        core.log.error("ai-injection-guard: guard unavailable: ", err or (res and res.status))
        if conf.fail_open then
            return
        end
        return reject(503, "guard_unavailable", "Request inspection is unavailable")
    end

    local result = core.json.decode(res.body)
    if type(result) ~= "table" then
        core.log.error("ai-injection-guard: invalid guard response")
        if conf.fail_open then
            return
        end
        return reject(503, "guard_unavailable", "Request inspection is unavailable")
    end

    if result.verdict == "block" then
        return reject(403, "prompt_injection_blocked",
                      "Blocked by AI gateway policy (" .. (result.check or "injection") .. ")",
                      result.decision_id,
                      { check = result.check, score = result.score, reason = result.reason })
    end
    if result.verdict == "flag" and result.decision_id then
        core.response.set_header("X-Guard-Flag", result.decision_id)
    end
end


return _M
