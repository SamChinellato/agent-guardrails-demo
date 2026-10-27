"""Architecture diagram of the deployed demo, drawn by hand on a grid (make diagram, D-070).

The layout follows the AWS architecture-icon guidelines: one main flow left to right, straight
square-cornered arrows with no labels on them, numbered steps explained in a legend under the
picture, few nested groups and short names. Every icon and arrow has a fixed position, so adding a
component means choosing where it goes. The icons are in icons/ (see icons/README.md); the
products keep their own logos, everything else is an IBM Carbon pictogram, the MCP gateway the
Model Context Protocol logo and the agents AWS's Generic Application icon.

Black arrows are requests, red ones control (the circuit breaker), dashed ones telemetry and dotted
ones deployment. The gateway is one deployment, drawn as the two gateways its routes form (D-068).

TARGET=k3d (default) draws the laptop demo: k3d, the LLM on the host, no sign-in. TARGET=openshift
draws the same demo on OpenShift (D-062): user authentication in front, the LLM in the cluster and
Argo CD deploying it all from git. Everything from the console onwards is the same in both.

Writes an SVG to /out; scripts/diagram.sh turns it into the PNG.
"""

import base64
import os
import re

OPENSHIFT = os.environ.get("TARGET", "k3d") == "openshift"
ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")

INK, GREY, RED, TEAL, MUTED = "#16191f", "#7d8998", "#c8322f", "#00a4a6", "#5f6b7a"
FONT = "Liberation Sans, Arial, Helvetica, sans-serif"
W, H = 2420, 1500
ICON = 80
Y = 430   # the main row: console → API gateway → agents → MCP gateway → guard → tools
out = []


def svg_body(name):
    """An icon's SVG, as its viewBox and the elements inside it."""
    src = open(os.path.join(ICONS, name)).read()
    vb = re.search(r'viewBox="([^"]+)"', src).group(1)
    inner = re.sub(r"^.*?<svg[^>]*>|</svg>\s*$", "", src, flags=re.S)
    return vb, re.sub(r"<title>.*?</title>", "", inner, flags=re.S)


def icon(name, x, y, label, sub=None):
    """An icon centred on (x, y), its name underneath and an optional detail line."""
    if name.endswith(".svg"):   # monochrome icons take the ink colour
        vb, inner = svg_body(name)
        out.append(f'<svg x="{x-ICON/2}" y="{y-ICON/2}" width="{ICON}" height="{ICON}" viewBox="{vb}" '
                   f'fill="{INK}">{inner}</svg>')
    else:
        with open(os.path.join(ICONS, name), "rb") as f:
            data = base64.b64encode(f.read()).decode()
        out.append(f'<image x="{x-ICON/2}" y="{y-ICON/2}" width="{ICON}" height="{ICON}" '
                   f'preserveAspectRatio="xMidYMid meet" href="data:image/png;base64,{data}"/>')
    out.append(f'<text x="{x}" y="{y+ICON/2+24}" text-anchor="middle" font-size="19" fill="{INK}">{label}</text>')
    if sub:
        out.append(f'<text x="{x}" y="{y+ICON/2+46}" text-anchor="middle" font-size="15" fill="{MUTED}">{sub}</text>')


def group(x1, y1, x2, y2, title, color=INK, dash=None, logo=None, bold=False):
    """An outlined group. With a logo, an AWS-style corner tab: the logo in white on the group's colour."""
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out.append(f'<rect x="{x1}" y="{y1}" width="{x2-x1}" height="{y2-y1}" fill="none" stroke="{color}" '
               f'stroke-width="2"{d}/>')
    tx = x1 + 14
    if logo:
        vb, inner = svg_body(logo)
        out.append(f'<rect x="{x1}" y="{y1}" width="44" height="44" fill="{color}"/>'
                   f'<svg x="{x1+7}" y="{y1+7}" width="30" height="30" viewBox="{vb}" fill="white">'
                   f'{inner.replace("fill=", "data-fill=")}</svg>')
        tx = x1 + 58
    w = ' font-weight="bold"' if bold else ""
    out.append(f'<text x="{tx}" y="{y1+30}" font-size="21" fill="{INK}"{w}>{title}</text>')


STYLE = {"req": (INK, None, "a"), "ctl": (RED, None, "r"), "tel": (GREY, "8 6", "g"), "dep": (GREY, "2 5", "g")}


def arrow(*pts, kind="req", both=False):
    color, dash, m = STYLE[kind]
    d = f' stroke-dasharray="{dash}"' if dash else ""
    s = f' marker-start="url(#{m}s)"' if both else ""
    p = " ".join(f"{x},{y}" for x, y in pts)
    out.append(f'<polyline points="{p}" fill="none" stroke="{color}" stroke-width="2.4"{d}{s} '
               f'marker-end="url(#{m})"/>')


def badge(x, y, n):
    out.append(f'<circle cx="{x}" cy="{y}" r="19" fill="{INK}"/>'
               f'<text x="{x}" y="{y+7}" text-anchor="middle" font-size="20" font-weight="bold" fill="white">{n}</text>')


# Groups first, so icons and arrows draw over them
if OPENSHIFT:
    group(220, 70, 2380, 1160, "OpenShift cluster", color="#ee0000", logo="openshift.svg")
    group(690, 115, 2350, 1130, "Demo namespaces (aigw-*)", color=TEAL, dash="10 7")
else:
    group(220, 70, 2380, 1160, "This machine", color=GREY, dash="10 7")
    group(690, 115, 2350, 1130, "Kubernetes cluster (k3d)", color="#326ce5", logo="kubernetes.svg")
group(1130, 360, 1510, 560, "Agents", bold=True)
group(1770, 140, 2040, 560, "Guard", bold=True)
group(2100, 360, 2320, 740, "Tools", bold=True)
group(760, 880, 2320, 1100, "Observability", bold=True)

# Arrows
if OPENSHIFT:
    arrow((150, Y), (382, Y))                                   # user → authentication
    arrow((478, Y), (752, Y))                                   # authentication → console
    arrow((430, Y+75), (430, 990), (812, 990), kind="tel")      # authentication → Grafana
    arrow((470, 200), (682, 200), kind="dep")                   # Argo CD → everything in the namespaces
    arrow((1000, Y-40), (1000, 308))                            # API gateway → LLM
else:
    arrow((150, Y), (752, Y))                                   # user → console
    arrow((110, Y+75), (110, 990), (812, 990), kind="tel")      # user → Grafana
    arrow((1000, Y-40), (1000, 200), (478, 200))                # API gateway → LLM on the host
arrow((850, Y), (962, Y))                                       # console → API gateway
arrow((1030, Y-40), (1030, 330), (1762, 330))                   # prompt check → guard
arrow((1058, Y), (1122, Y), both=True)                          # API gateway ⇄ agents
arrow((1510, Y), (1602, Y))                                     # agents → MCP gateway
arrow((1698, Y), (1852, Y))                                     # MCP gateway → policy engine
arrow((1905, Y-40), (1905, 318))                                # policy engine → classifier
arrow((1958, Y), (2092, Y))                                     # policy engine → tools
arrow((990, Y+96), (990, 872), kind="tel")                      # API gateway → observability
arrow((1630, Y+96), (1630, 872), kind="tel")                    # MCP gateway → observability
arrow((1310, 690), (1310, 640), (1030, 640), (1030, Y+104), kind="ctl")   # breaker stops agents
arrow((1350, 690), (1350, 640), (1670, 640), (1670, Y+104), kind="ctl")   # breaker stops tools
arrow((1860, 560), (1860, 730), (1378, 730), kind="ctl")        # guard auto-trip
arrow((1330, 950), (1330, 828), kind="ctl")                     # Prometheus alerts

# Icons
icon("user.svg", 110, Y, "User")
if OPENSHIFT:
    icon("authentication.svg", 430, Y, "User authentication")
    icon("argocd.png", 430, 200, "Argo CD", "deploys from git")
    icon("llm.svg", 1000, 200, "LLM API", "Granite 4.0 Micro")
else:
    icon("llm.svg", 430, 200, "LLM API", "runs on the host")
icon("console.svg", 800, Y, "Chat console")
icon("api-gateway.svg", 1010, Y, "API gateway", "/llm · /agents")
icon("agent.svg", 1230, Y+15, "Researcher")
icon("agent.svg", 1410, Y+15, "Executor")
icon("mcp-gateway.svg", 1650, Y, "MCP gateway", "/mcp")
icon("classifier.svg", 1905, 240, "Injection classifier")
icon("policy-engine.svg", 1905, Y, "Policy engine")
icon("documents.svg", 2210, Y+15, "Documents")
icon("records.svg", 2210, 625, "Records · email")
icon("circuit-breaker.svg", 1330, 730, "Circuit breaker")
icon("grafana.png", 860, 990, "Grafana")
icon("loki.png", 1095, 990, "LokiStack" if OPENSHIFT else "Loki", "logs")
icon("prometheus.png", 1330, 990, "Prometheus", "metrics · alerts")
icon("tempo.png", 1700, 990, "Tempo", "traces")
icon("collector.svg", 2080, 990, "Collector", "OpenTelemetry")

# Numbered steps, at the thing that does each one
for n, (x, y) in enumerate([(482, Y-48) if OPENSHIFT else (160, Y-48), (860, Y-48), (1400, 330), (1495, 375),
                            (1710, Y-48), (2020, 158), (2300, 900), (1395, 690)], start=1):
    badge(x, y, n)

steps = [
    "The user signs in through user authentication." if OPENSHIFT else "The user opens the chat console.",
    "The chat console sends the user's request to the API gateway.",
    "The guard checks the prompt for injection before anything runs.",
    "The API gateway hands the request to an agent; its model calls go back through the gateway.",
    "Agents reach tools only through the MCP gateway, each with its own key.",
    "The policy engine checks every tool call; the classifier checks what tools return.",
    "Both gateways send traces, logs and metrics to observability.",
    "An anomaly alert or a guard auto-trip opens the circuit breaker: it stops an agent or tools.",
]
for i, s in enumerate(steps):
    x, y = (240 if i < 4 else 1300), 1225 + (i % 4) * 56
    badge(x, y, i + 1)
    out.append(f'<text x="{x+34}" y="{y+7}" font-size="19" fill="{INK}">{s}</text>')

key = [("req", "request"), ("ctl", "control"), ("tel", "telemetry")] + ([("dep", "deployment")] if OPENSHIFT else [])
for i, (k, name) in enumerate(key):
    x = 2380 - (len(key) - i) * 170
    color, dash, _ = STYLE[k]
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out.append(f'<line x1="{x}" y1="1180" x2="{x+50}" y2="1180" stroke="{color}" stroke-width="2.4"{d}/>'
               f'<text x="{x+60}" y="1186" font-size="16" fill="{MUTED}">{name}</text>')

defs = "".join(
    f'<marker id="{m}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto">'
    f'<path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
    f'<marker id="{m}s" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="5" markerHeight="5" orient="auto">'
    f'<path d="M10,0 L0,5 L10,10 z" fill="{c}"/></marker>' for m, c in [("a", INK), ("r", RED), ("g", GREY)])
svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}">'
       f'<defs>{defs}</defs><rect width="100%" height="100%" fill="white"/>' + "".join(out) + "</svg>")
with open("/out/architecture-openshift.svg" if OPENSHIFT else "/out/architecture.svg", "w") as f:
    f.write(svg)
