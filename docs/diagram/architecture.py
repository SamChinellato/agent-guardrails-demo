"""Architecture diagram of the deployed demo, rendered with `diagrams` (make diagram).

The gateway and the models are named by capability (D-045); the platform pieces (Kubernetes,
Prometheus, Loki, Tempo, Grafana) by name (D-048). The gateway is one deployment, drawn as the two
gateways its routes form (D-068): an API gateway for the model and the agents (/llm, /agents)
and an MCP gateway for the tools (/mcp), each checking its traffic with the same guard.
Bold blue: requests, all through a gateway. Red: control (the policy engine stopping agents or tools). Dashed grey: telemetry.

TARGET=k3d (default) draws the laptop demo: k3d, the LLM on the host. TARGET=openshift draws
the same demo on OpenShift (D-062): the router and OpenShift login in front, llama.cpp in the
cluster, the operator-managed telemetry, and Argo CD deploying it all from git. The agents,
gateway, guard, tools and every arrow between them are the same in both.
"""

import os

from diagrams import Cluster, Diagram, Edge
from diagrams.generic.compute import Rack
from diagrams.generic.network import Firewall, Router
from diagrams.generic.storage import Storage
from diagrams.onprem.auth import Oauth2Proxy
from diagrams.onprem.client import Client, User
from diagrams.onprem.compute import Server
from diagrams.onprem.gitops import Argocd
from diagrams.onprem.logging import Loki
from diagrams.onprem.monitoring import Grafana, Prometheus
from diagrams.onprem.network import Haproxy
from diagrams.onprem.tracing import Tempo
from diagrams.programming.flowchart import Decision, Document, MultipleDocuments, PredefinedProcess

OPENSHIFT = os.environ.get("TARGET", "k3d") == "openshift"

REQUEST = dict(color="#2f5aa8", penwidth="2.6", fontcolor="#2f5aa8")
CONTROL = dict(color="#c8322f", penwidth="2", fontcolor="#c8322f")
TELEMETRY = dict(color="#8e8c84", style="dashed", fontcolor="#6b7484")
DEPLOY = dict(color="#8e8c84", style="dotted", fontcolor="#6b7484")

graph = {"fontname": "Helvetica", "fontsize": "24", "pad": "0.5", "nodesep": "0.6", "ranksep": "1.1",
         "splines": "spline", "newrank": "true", "labeljust": "l"}
node = {"fontname": "Helvetica", "fontsize": "13"}
edge = {"fontname": "Helvetica", "fontsize": "12"}
host = {"bgcolor": "#f6f7f9", "fontsize": "16"}
k8s = {"bgcolor": "#eaf0fb", "fontsize": "16", "pencolor": "#326ce5"}
ocp = {"bgcolor": "#fbecec", "fontsize": "16", "pencolor": "#ee0000"}   # OpenShift red
api_box = {"bgcolor": "#dde7f7", "fontsize": "15", "pencolor": "#2f5aa8", "penwidth": "2"}
mcp_box = {"bgcolor": "#e2f1ea", "fontsize": "15", "pencolor": "#2e7d5b", "penwidth": "2"}

with Diagram("", filename="/out/architecture-openshift" if OPENSHIFT else "/out/architecture", show=False,
             direction="LR", outformat="png", graph_attr=graph, node_attr=node, edge_attr=edge):
    analyst = User("Procurement analyst\n(you)")

    if OPENSHIFT:
        outer = Cluster("OpenShift cluster", graph_attr=ocp)
    else:
        outer = Cluster("This machine", graph_attr=host)
    with outer:
        if OPENSHIFT:
            router = Haproxy("OpenShift router\nHTTPS routes")
            login = Oauth2Proxy("OpenShift login\nconsole · Grafana · policy engine")
            argocd = Argocd("Argo CD\n(OpenShift GitOps)\ndeploys everything from git")
        else:
            llm = Rack("LLM API\nlocal language model\n(runs on the host)")

        with Cluster("Demo namespaces (aigw-*)" if OPENSHIFT else "Kubernetes cluster", graph_attr=k8s):
            console = Client("Chat console")
            if OPENSHIFT:
                llm = Rack("LLM API\nllama.cpp · Granite 4.0 Micro\n4-bit, on CPU, in the cluster")

            with Cluster("AI gateway: every call passes here"):
                with Cluster("API gateway", graph_attr=api_box):
                    api_gw = Router("/llm · /agents\nidentity\nprompt filters\nLLM proxy")
                with Cluster("MCP gateway", graph_attr=mcp_box):
                    mcp_gw = Router("/mcp\nper-agent keys\ntool routing")
                with Cluster("Guard"):
                    guard = Decision("Policy rules\ntool allowlists\nargument rules")
                    classifier = PredefinedProcess("ML injection classifier\nruns inside the guard, on CPU\nscores text 0 to 1")
                killswitch = Firewall("Policy engine\nsuspends agents · stops tools\n+ control panel")

            with Cluster("Agents (can only reach the gateways)"):
                researcher = Server("Researcher agent\nreviews supplier documents")
                executor = Server("Executor agent\nupdates records, sends email")

            with Cluster("Tools (reached only through the guard)"):
                docs = MultipleDocuments("MCP tool\nsupplier documents")
                actions = Document("MCP tool\nrecords, email, export")

            with Cluster("Observability (OpenShift operators)" if OPENSHIFT else "Observability"):
                collector = Storage("Telemetry collector\n(OpenTelemetry)" if OPENSHIFT else "Telemetry collector")
                tempo = Tempo("Tempo\ntraces")
                loki = Loki("LokiStack\nlogs · decision records" if OPENSHIFT else "Loki\nlogs · decision records")
                prometheus = Prometheus("Prometheus\nmetrics · alerts")
                grafana = Grafana("Grafana\ndashboards · decision trail")

    if OPENSHIFT:
        analyst >> Edge(label="HTTPS", **REQUEST) >> router >> Edge(**REQUEST) >> login
        login >> Edge(label="chat", **REQUEST) >> console
        login >> Edge(label="dashboards", **TELEMETRY) >> grafana
        argocd >> Edge(label="syncs from git", **DEPLOY) >> api_gw
    else:
        analyst >> Edge(label="chat", **REQUEST) >> console
    console >> Edge(label="as demo-user", **REQUEST) >> api_gw
    api_gw >> Edge(label="prompts checked", **REQUEST) >> guard
    mcp_gw >> Edge(label="every tool call checked", **REQUEST) >> guard
    guard >> Edge(label="text from people and tools", style="bold", color="#2f5aa8") >> classifier
    api_gw >> Edge(label="model calls", **REQUEST) >> llm
    guard >> Edge(**REQUEST) >> docs
    guard >> Edge(**REQUEST) >> actions
    # Agents only talk to the gateways: requests and model calls through the API gateway,
    # tool calls through the MCP gateway
    api_gw >> Edge(label="your request · its LLM calls", dir="both", **REQUEST) >> researcher
    api_gw >> Edge(label="hand-off (via the gateway) · its LLM calls", dir="both", **REQUEST) >> executor
    researcher >> Edge(label="tool calls", **REQUEST) >> mcp_gw
    executor >> Edge(label="tool calls", **REQUEST) >> mcp_gw

    [api_gw, mcp_gw] >> Edge(label="traces · logs", **TELEMETRY) >> collector
    collector >> Edge(**TELEMETRY) >> tempo
    collector >> Edge(**TELEMETRY) >> loki
    [api_gw, mcp_gw] >> Edge(label="metrics", **TELEMETRY) >> prometheus
    [tempo, loki, prometheus] >> Edge(**TELEMETRY) >> grafana

    prometheus >> Edge(label="anomaly alerts", **CONTROL) >> killswitch
    guard >> Edge(label="auto-trip", **CONTROL) >> killswitch
    killswitch >> Edge(label="stops an agent or everything", constraint="false", **CONTROL) >> api_gw
    killswitch >> Edge(label="stops tools", constraint="false", **CONTROL) >> mcp_gw
