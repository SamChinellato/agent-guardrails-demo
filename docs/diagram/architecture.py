"""Architecture diagram of the deployed demo, rendered with `diagrams` (make diagram).

Capabilities, not product names (D-045). Bold blue: requests, all through the gateway.
Red: control (kill switch). Dashed grey: telemetry.
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.generic.compute import Rack
from diagrams.generic.network import Firewall, Router
from diagrams.generic.storage import Storage
from diagrams.onprem.client import Client, User
from diagrams.onprem.compute import Server
from diagrams.programming.flowchart import Decision, Document, MultipleDocuments

REQUEST = dict(color="#2f5aa8", penwidth="2.6", fontcolor="#2f5aa8")
CONTROL = dict(color="#c8322f", penwidth="2", fontcolor="#c8322f")
TELEMETRY = dict(color="#8e8c84", style="dashed", fontcolor="#6b7484")

graph = {"fontname": "Helvetica", "fontsize": "24", "pad": "0.5", "nodesep": "0.7", "ranksep": "1.3",
         "splines": "spline", "newrank": "true", "labeljust": "l"}
node = {"fontname": "Helvetica", "fontsize": "13"}
edge = {"fontname": "Helvetica", "fontsize": "12"}

with Diagram("Acme procurement assistant behind an AI gateway", filename="/out/architecture", show=False,
             direction="LR", outformat="png", graph_attr=graph, node_attr=node, edge_attr=edge):
    analyst = User("Procurement analyst\n(you)")
    console = Client("Chat console")

    with Cluster("AI gateway: every call passes here"):
        gateway = Router("Gateway\nidentity · prompt filters\nLLM proxy · tool routing")
        guard = Decision("Guard\ninjection classifier\ntool policy")

    with Cluster("Agents (can only reach the gateway)"):
        researcher = Server("Researcher agent\nreviews supplier documents")
        executor = Server("Executor agent\nupdates records, sends email")

    with Cluster("Behind the gateway"):
        llm = Rack("LLM API\n(local model)")
        docs = MultipleDocuments("MCP tool\nsupplier documents")
        actions = Document("MCP tool\nrecords, email, export")

    with Cluster("Incident response"):
        telemetry = Storage("Traces · logs ·\ndecision records · metrics")
        alerts = Client("Dashboards + alerts")
        killswitch = Firewall("Kill switch\n+ control panel")

    analyst >> Edge(label="chat", **REQUEST) >> console >> Edge(label="as demo-user", **REQUEST) >> gateway
    gateway >> Edge(label="every call checked", **REQUEST) >> guard
    guard >> Edge(**REQUEST) >> llm
    guard >> Edge(**REQUEST) >> docs
    guard >> Edge(**REQUEST) >> actions
    # Agents only talk to the gateway: requests in, their LLM and tool calls out
    gateway >> Edge(label="your request · its LLM and tool calls", dir="both", **REQUEST) >> researcher
    gateway >> Edge(label="hand-off (via the gateway) · its tool calls", dir="both", **REQUEST) >> executor

    gateway >> Edge(label="everything reports here", **TELEMETRY) >> telemetry >> Edge(**TELEMETRY) >> alerts
    alerts >> Edge(label="anomaly alerts", **CONTROL) >> killswitch
    guard >> Edge(label="auto-trip", **CONTROL) >> killswitch
    killswitch >> Edge(label="stops an agent or everything", constraint="false", **CONTROL) >> gateway
