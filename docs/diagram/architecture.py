"""Architecture diagram of the deployed demo, rendered with `diagrams` (make diagram).

The gateway and the models are named by capability (D-045); the platform pieces (Kubernetes,
Prometheus, Loki, Tempo, Grafana) by name (D-048). Bold blue: requests, all through the
gateway. Red: control (kill switch). Dashed grey: telemetry.
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.generic.compute import Rack
from diagrams.generic.network import Firewall, Router
from diagrams.generic.storage import Storage
from diagrams.onprem.client import Client, User
from diagrams.onprem.compute import Server
from diagrams.onprem.logging import Loki
from diagrams.onprem.monitoring import Grafana, Prometheus
from diagrams.onprem.tracing import Tempo
from diagrams.programming.flowchart import Decision, Document, MultipleDocuments, PredefinedProcess

REQUEST = dict(color="#2f5aa8", penwidth="2.6", fontcolor="#2f5aa8")
CONTROL = dict(color="#c8322f", penwidth="2", fontcolor="#c8322f")
TELEMETRY = dict(color="#8e8c84", style="dashed", fontcolor="#6b7484")

graph = {"fontname": "Helvetica", "fontsize": "24", "pad": "0.5", "nodesep": "0.6", "ranksep": "1.1",
         "splines": "spline", "newrank": "true", "labeljust": "l"}
node = {"fontname": "Helvetica", "fontsize": "13"}
edge = {"fontname": "Helvetica", "fontsize": "12"}
host = {"bgcolor": "#f6f7f9", "fontsize": "16"}
k8s = {"bgcolor": "#eaf0fb", "fontsize": "16", "pencolor": "#326ce5"}

with Diagram("", filename="/out/architecture", show=False,
             direction="LR", outformat="png", graph_attr=graph, node_attr=node, edge_attr=edge):
    analyst = User("Procurement analyst\n(you)")

    with Cluster("This machine", graph_attr=host):
        llm = Rack("LLM API\nlocal language model\n(runs on the host)")

        with Cluster("Kubernetes cluster", graph_attr=k8s):
            console = Client("Chat console")

            with Cluster("AI gateway: every call passes here"):
                gateway = Router("Gateway\nidentity · prompt filters\nLLM proxy · tool routing")
                with Cluster("Guard"):
                    guard = Decision("Policy engine\ntool allowlists\nargument rules")
                    classifier = PredefinedProcess("ML injection classifier\nruns inside the guard, on CPU\nscores text 0 to 1")
                killswitch = Firewall("Kill switch\n+ control panel")

            with Cluster("Agents (can only reach the gateway)"):
                researcher = Server("Researcher agent\nreviews supplier documents")
                executor = Server("Executor agent\nupdates records, sends email")

            with Cluster("Tools (reached only through the guard)"):
                docs = MultipleDocuments("MCP tool\nsupplier documents")
                actions = Document("MCP tool\nrecords, email, export")

            with Cluster("Observability"):
                collector = Storage("Telemetry collector")
                tempo = Tempo("Tempo\ntraces")
                loki = Loki("Loki\nlogs · decision records")
                prometheus = Prometheus("Prometheus\nmetrics · alerts")
                grafana = Grafana("Grafana\ndashboards · decision trail")

    analyst >> Edge(label="chat", **REQUEST) >> console >> Edge(label="as demo-user", **REQUEST) >> gateway
    gateway >> Edge(label="every call checked", **REQUEST) >> guard
    guard >> Edge(label="text from people and tools", style="bold", color="#2f5aa8") >> classifier
    gateway >> Edge(label="model calls", **REQUEST) >> llm
    guard >> Edge(**REQUEST) >> docs
    guard >> Edge(**REQUEST) >> actions
    # Agents only talk to the gateway: requests in, their LLM and tool calls out
    gateway >> Edge(label="your request · its LLM and tool calls", dir="both", **REQUEST) >> researcher
    gateway >> Edge(label="hand-off (via the gateway) · its tool calls", dir="both", **REQUEST) >> executor

    gateway >> Edge(label="traces · logs", **TELEMETRY) >> collector
    collector >> Edge(**TELEMETRY) >> tempo
    collector >> Edge(**TELEMETRY) >> loki
    gateway >> Edge(label="metrics", **TELEMETRY) >> prometheus
    [tempo, loki, prometheus] >> Edge(**TELEMETRY) >> grafana

    prometheus >> Edge(label="anomaly alerts", **CONTROL) >> killswitch
    guard >> Edge(label="auto-trip", **CONTROL) >> killswitch
    killswitch >> Edge(label="stops an agent or everything", constraint="false", **CONTROL) >> gateway
