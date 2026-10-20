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
    analyst = User("You")

    with Cluster("This machine", graph_attr=host):
        llm = Rack("LLM API")

        with Cluster("Kubernetes cluster", graph_attr=k8s):
            console = Client("Chat console")

            with Cluster("AI gateway"):
                gateway = Router("Gateway")
                with Cluster("Guard"):
                    guard = Decision("Policy engine")
                    classifier = PredefinedProcess("ML injection classifier")
                killswitch = Firewall("Kill switch")

            with Cluster("Agents"):
                researcher = Server("Researcher agent")
                executor = Server("Executor agent")

            with Cluster("Tools"):
                docs = MultipleDocuments("Documents tool")
                actions = Document("Actions tool")

            with Cluster("Observability"):
                collector = Storage("Collector")
                tempo = Tempo("Tempo")
                loki = Loki("Loki")
                prometheus = Prometheus("Prometheus")
                grafana = Grafana("Grafana")

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
