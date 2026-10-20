"""Generate the Grafana dashboards (OBS-05) as JSON files in this directory.

    uv run python observability/dashboards/build.py

The JSON is what Grafana loads (via the sidecar); this script is the readable source.
Conventions (from the dataviz method): one y-axis per panel, 2px lines, fixed colour per
agent (never cycled), status colours reserved for states (red = blocked/killed), headline
numbers as stat tiles, legends on every multi-series panel. Screens stay minimal: logs and
traces are one click away in Explore, not on the dashboards.
"""

import json
from pathlib import Path

OUT = Path(__file__).parent

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}

# Categorical slots 1-4 (dark-mode steps of the reference palette), fixed per agent
AGENT_COLORS = {"researcher": "#3987e5", "executor": "#d95926", "probe": "#199e70", "demo-user": "#c98500"}
# Status colours, reserved for states
GOOD, WARN, CRIT, NEUTRAL = "#1a9e5c", "#c98500", "#e5484d", "#8e8c84"

TOKEN_BURN_PER_MIN = 50000  # keep in sync with observability/alerts/killswitch.yaml (D-037)


class Layout:
    """Places panels left to right on Grafana's 24-column grid."""

    def __init__(self):
        self.x, self.y, self.row_h, self.next_id = 0, 0, 0, 1

    def place(self, panel: dict, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        panel["gridPos"] = {"x": self.x, "y": self.y, "w": w, "h": h}
        panel["id"] = self.next_id
        self.next_id += 1
        self.x += w
        self.row_h = max(self.row_h, h)
        return panel

    def row(self, title: str) -> dict:
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        panel = {"type": "row", "title": title, "collapsed": False, "panels": []}
        panel["gridPos"] = {"x": 0, "y": self.y, "w": 24, "h": 1}
        panel["id"] = self.next_id
        self.next_id += 1
        self.y += 1
        return panel


def prom(expr: str, legend: str = "", ref: str = "A", instant: bool = False) -> dict:
    t = {"datasource": PROM, "expr": expr, "legendFormat": legend or "__auto", "refId": ref}
    if instant:
        t.update(instant=True, range=False)
    return t


def agent_overrides(label_values=AGENT_COLORS) -> list:
    return [
        {
            "matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}],
        }
        for name, color in label_values.items()
    ]


def named_color_overrides(colors: dict) -> list:
    return agent_overrides(colors)


def timeseries(title, targets, unit="short", overrides=None, description="", stack=False, thresholds=None):
    custom = {
        "lineWidth": 2,
        "fillOpacity": 8 if not stack else 60,
        "showPoints": "never",
        "spanNulls": True,
        "axisSoftMin": 0,
        "stacking": {"mode": "normal" if stack else "none"},
        "drawStyle": "line" if not stack else "bars",
        "barAlignment": 0,
    }
    defaults = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"}}
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
        custom["thresholdsStyle"] = {"mode": "dashed"}
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": PROM,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def stat(title, expr, unit="short", color=NEUTRAL, thresholds=None, description="", decimals=None):
    steps = thresholds or [{"color": color, "value": None}]
    defaults = {"unit": unit, "color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": steps}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": PROM,
        "targets": [prom(expr, instant=True)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "none",
            "textMode": "value",
            "justifyMode": "center",
        },
    }


def dashboard(uid, title, description, panels, annotations=None, tags=("ai-gateway-demo",)):
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": list(tags),
        "timezone": "browser",
        "editable": False,
        "graphTooltip": 1,  # shared crosshair
        "refresh": "5s",  # OBS-05
        "time": {"from": "now-15m", "to": "now"},
        "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m"]},
        "schemaVersion": 39,
        "version": 1,
        "annotations": {"list": annotations or []},
        "links": [
            {"title": t, "type": "link", "url": f"/d/{u}", "targetBlank": False}
            for u, t in [
                ("ai-gateway-overview", "AI Gateway Overview"),
                ("agent-behaviour", "Agent Behaviour"),
                ("incident", "Incident"),
            ]
            if u != uid
        ],
        "panels": panels,
    }


# Shared expressions -----------------------------------------------------------------------
TOKENS_PER_MIN_BY_AGENT = (
    "sum by (consumer) (rate(apisix_llm_prompt_tokens[1m]) + rate(apisix_llm_completion_tokens[1m])) * 60"
)
GUARD_BLOCK_ANNOTATION = {
    "name": "Guard blocks",
    "datasource": PROM,
    "enable": True,
    "iconColor": CRIT,
    "expr": 'sum by (agent, check) (increase(guard_decisions_total{verdict="block"}[15s])) > 0',
    "step": "15s",
    "titleFormat": "Guard block",
    "textFormat": "{{agent}}: {{check}}",
    "useValueForTime": False,
}
KILL_ANNOTATION = {
    "name": "Kill-switch events",
    "datasource": LOKI,
    "enable": True,
    "iconColor": CRIT,
    "expr": '{service_name="killswitch"} | json | event_type=~"trip|reset"',
    "titleFormat": "Kill switch {{event_type}}",
    "textFormat": "{{scope}} by {{actor}} ({{source}})",
    "tagKeys": "scope,source",
}


def overview() -> dict:
    lay = Layout()
    p = [
        lay.place(stat("Requests / s", "sum(rate(apisix_http_status[1m]))", unit="reqps", decimals=1), 6, 4),
        lay.place(
            stat("Blocked (15 min)", 'sum(increase(apisix_http_status{code=~"400|403"}[15m])) or vector(0)',
                 thresholds=[{"color": NEUTRAL, "value": None}, {"color": CRIT, "value": 1}], decimals=0),
            6, 4,
        ),
        lay.place(
            stat("Kill switches on", "sum(killswitch_active) or vector(0)",
                 thresholds=[{"color": GOOD, "value": None}, {"color": CRIT, "value": 1}], decimals=0),
            6, 4,
        ),
        lay.place(stat("LLM tokens / min", f"sum({TOKENS_PER_MIN_BY_AGENT}) or vector(0)", decimals=0), 6, 4),
        lay.place(
            timeseries("Requests by agent",
                       [prom('sum by (consumer) (rate(apisix_http_status{consumer!=""}[1m]))', "{{consumer}}")],
                       unit="reqps", overrides=agent_overrides()),
            12, 10,
        ),
        lay.place(
            timeseries(
                "Allowed · blocked · killed",
                [
                    prom('sum(rate(apisix_http_status{code=~"2.."}[1m]))', "allowed"),
                    prom('sum(rate(apisix_http_status{code=~"400|403"}[1m]))', "blocked", "B"),
                    prom('sum(rate(apisix_http_status{code="503"}[1m]))', "killed", "C"),
                ],
                unit="reqps",
                overrides=named_color_overrides({"allowed": GOOD, "blocked": WARN, "killed": CRIT}),
            ),
            12, 10,
        ),
    ]
    return dashboard("ai-gateway-overview", "AI Gateway Overview", "", p,
                     annotations=[GUARD_BLOCK_ANNOTATION, KILL_ANNOTATION])


def agent_behaviour() -> dict:
    lay = Layout()
    p = [
        lay.place(
            timeseries("Tokens / min", [prom(TOKENS_PER_MIN_BY_AGENT, "{{consumer}}")], overrides=agent_overrides(),
                       thresholds=[{"color": "transparent", "value": None}, {"color": CRIT, "value": TOKEN_BURN_PER_MIN}]),
            12, 11,
        ),
        lay.place(
            timeseries("Tool calls / min",
                       [prom('sum by (consumer) (rate(apisix_http_status{route=~"mcp-.*"}[1m])) * 60', "{{consumer}}")],
                       overrides=agent_overrides()),
            12, 11,
        ),
        lay.place(
            timeseries("Guard blocks / min",
                       [prom('sum by (agent) (rate(guard_decisions_total{verdict="block"}[1m])) * 60', "{{agent}}")],
                       overrides=agent_overrides()),
            24, 9,
        ),
    ]
    return dashboard("agent-behaviour", "Agent Behaviour", "", p,
                     annotations=[GUARD_BLOCK_ANNOTATION, KILL_ANNOTATION])


def incident() -> dict:
    lay = Layout()
    state = {
        "type": "state-timeline",
        "title": "Kill switch",
        "datasource": PROM,
        "targets": [prom("max by (scope) (killswitch_active)", "{{scope}}")],
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": [{"color": GOOD, "value": None}, {"color": CRIT, "value": 1}]},
                "mappings": [
                    {"type": "value", "options": {"0": {"text": "open", "color": GOOD}, "1": {"text": "TRIPPED", "color": CRIT}}}
                ],
                "custom": {"fillOpacity": 85, "lineWidth": 0},
            },
            "overrides": [],
        },
        "options": {"showValue": "auto", "mergeValues": True, "rowHeight": 0.8, "legend": {"showLegend": False}},
    }
    p = [
        lay.place(
            stat("Kill switches on", "sum(killswitch_active) or vector(0)",
                 thresholds=[{"color": GOOD, "value": None}, {"color": CRIT, "value": 1}], decimals=0),
            8, 4,
        ),
        lay.place(
            stat("Alerts firing", 'count(ALERTS{alertstate="firing"}) or vector(0)',
                 thresholds=[{"color": GOOD, "value": None}, {"color": CRIT, "value": 1}], decimals=0),
            8, 4,
        ),
        lay.place(
            stat("Guard blocks (15 min)", 'sum(increase(guard_decisions_total{verdict="block"}[15m])) or vector(0)',
                 thresholds=[{"color": NEUTRAL, "value": None}, {"color": CRIT, "value": 1}], decimals=0),
            8, 4,
        ),
        lay.place(state, 24, 6),
        lay.place(
            timeseries("Cut off by the kill switch",
                       [prom('sum by (consumer) (rate(apisix_http_status{code="503"}[1m])) * 60', "{{consumer}}")],
                       overrides=agent_overrides()),
            24, 9,
        ),
    ]
    return dashboard("incident", "Incident", "", p, annotations=[GUARD_BLOCK_ANNOTATION, KILL_ANNOTATION])


def decision_trail() -> dict:
    """How a decision was reached, for one request (D-045): the guard's decisions in order with
    the score, threshold and rule behind each, then the kill-switch actions, then the trace."""
    lay = Layout()

    def log_panel(title, expr):
        return {
            "type": "logs", "title": title, "datasource": LOKI,
            "targets": [{"datasource": LOKI, "expr": expr, "refId": "A"}],
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Ascending",
                        "enableLogDetails": True, "dedupStrategy": "none", "showLabels": False},
        }

    decisions = log_panel(
        "Guard decisions, in order",
        '{service_name="guard"} | event="guard_decision" | trace_id="$trace_id" '
        '| line_format "{{ .verdict | upper }}   {{ .agent }}   {{ .check }} ({{ .mode }}){{ if .tool }}   tool {{ .tool }}{{ end }}\\n'
        '{{ .reason }}\\n{{ if .rule_id }}rule {{ .rule_id }}   {{ end }}policy {{ .policy_version }}   {{ .decision_id }}"',
    )
    kills = log_panel(
        "Kill-switch actions",
        '{service_name="killswitch"} | trace_id="$trace_id" | event_type=~"trip|reset" '
        '| line_format "{{ .event_type | upper }} {{ .scope }}   source {{ .source }}, actor {{ .actor }}\\n{{ .reason }}\\n'
        'verified={{ .verified }} in {{ .latency_ms }} ms{{ if .incident_id }}   incident {{ .incident_id }}{{ end }}'
        '{{ if .decision_id }}   decision {{ .decision_id }}{{ end }}"',
    )
    trace = {
        "type": "traces", "title": "The request, end to end", "datasource": {"type": "tempo", "uid": "tempo"},
        "targets": [{"datasource": {"type": "tempo", "uid": "tempo"}, "queryType": "traceql", "query": "$trace_id", "refId": "A"}],
    }
    p = [lay.place(decisions, 24, 18), lay.place(kills, 24, 5), lay.place(trace, 24, 18)]
    d = dashboard("decision-trail", "Decision trail", "", p)
    d["time"] = {"from": "now-1h", "to": "now"}
    d["refresh"] = ""
    d["templating"] = {"list": [{"name": "trace_id", "label": "Trace", "type": "textbox", "query": "", "current": {"value": ""}}]}
    return d


if __name__ == "__main__":
    for d in (overview(), agent_behaviour(), incident(), decision_trail()):
        path = OUT / f"{d['uid']}.json"
        path.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
        print(f"wrote {path.name} ({len(d['panels'])} panels)")
