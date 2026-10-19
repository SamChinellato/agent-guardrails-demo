--
-- agent-kill-switch: per-agent kill (KS-01 scope agent:<name>), attached to a consumer.
--
-- Why not fault-injection on the consumer, as the spec suggests: every route already
-- carries a (disabled) fault-injection from its plugin config for the global kill (GW-03).
-- When APISIX merges consumer plugins into the route it only re-runs plugins the route
-- does not already have, so a consumer fault-injection never fires on 3.19 (DECISIONS D-013).
-- This plugin's name is used on no route, so it always runs after authentication.
--
-- The response matches the global kill for the route type (KS-06): JSON-RPC error on
-- MCP routes, OpenAI-style error elsewhere. All logic stays here; the killswitch only
-- adds or removes {"agent-kill-switch": {...}} on the consumer.
--
local core = require("apisix.core")

local plugin_name = "agent-kill-switch"

local schema = {
    type = "object",
    properties = {
        http_status = { type = "integer", minimum = 400, maximum = 599, default = 503 },
        incident_id = { type = "string", maxLength = 64 },
        message = {
            type = "string",
            maxLength = 200,
            default = "Agent access suspended by incident response",
        },
    },
}

local _M = {
    version = 0.1,
    -- Below consumer-restriction (2400) so a disallowed consumer still gets 403, and above
    -- every AI plugin (ai-prompt-guard 1072, ai-proxy 1040) so nothing reaches an upstream.
    priority = 2390,
    name = plugin_name,
    schema = schema,
}


function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end


local function is_mcp_route(ctx)
    local route = ctx.matched_route and ctx.matched_route.value
    return route ~= nil and route.plugin_config_id == "pc-mcp"
end


function _M.access(conf, ctx)
    core.response.set_header("Content-Type", "application/json")
    if is_mcp_route(ctx) then
        return conf.http_status, {
            jsonrpc = "2.0",
            id = ngx.null,
            error = {
                code = -32003,
                message = "kill_switch_engaged: " .. conf.message,
                data = { incident_id = conf.incident_id },
            },
        }
    end
    return conf.http_status, {
        error = {
            type = "kill_switch_engaged",
            message = conf.message,
            incident_id = conf.incident_id,
        },
    }
end


return _M
