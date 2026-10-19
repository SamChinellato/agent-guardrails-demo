"""NetworkPolicy requirements (GW-07, NFR-08, 7.3): who may talk to whom inside the cluster."""

import subprocess
import time
import uuid

import pytest

from conftest import KUBECONFIG, KUBECTL

PROBE_IMAGE = "docker.io/curlimages/curl:8.11.1"


def kubectl(*args, check=True, timeout=120):
    return subprocess.run([KUBECTL, "--kubeconfig", KUBECONFIG, *args], capture_output=True, text=True,
                          check=check, timeout=timeout)


@pytest.fixture(scope="module")
def probe_pod():
    """probe_pod(namespace, labels) -> exec(url) returning the HTTP code ('000' when blocked)."""
    created = []

    def make(namespace: str, labels: dict[str, str]):
        name = f"np-{uuid.uuid4().hex[:6]}"
        label_arg = ",".join(f"{k}={v}" for k, v in labels.items())
        kubectl("-n", namespace, "run", name, f"--image={PROBE_IMAGE}", f"--labels={label_arg}",
                "--restart=Never", "--command", "--", "sleep", "600")
        created.append((namespace, name))
        kubectl("-n", namespace, "wait", "--for=condition=Ready", f"pod/{name}", "--timeout=90s")

        def fetch(url: str) -> str:
            r = kubectl("-n", namespace, "exec", name, "--", "curl", "-s", "-o", "/dev/null", "-m", "4",
                        "-w", "%{http_code}", url, check=False)
            return r.stdout.strip() or "000"

        return fetch

    yield make
    for ns, name in created:
        kubectl("-n", ns, "delete", "pod", name, "--now", "--wait=false", check=False)


ADMIN = "http://apisix-admin.gateway.svc.cluster.local:9180/apisix/admin/routes"


def test_admin_api_unreachable_from_ai_namespace(probe_pod):
    assert probe_pod("ai", {"app": "nettest"})(ADMIN) == "000"


def test_admin_api_unreachable_from_other_gateway_pods(probe_pod):
    assert probe_pod("gateway", {"app": "nettest"})(ADMIN) == "000"


def eventually(fetch, url, want, timeout=15):
    """Allow rules for a brand-new pod can take a few seconds to be programmed."""
    deadline = time.monotonic() + timeout
    while (got := fetch(url)) != want and time.monotonic() < deadline:
        time.sleep(1)
    return got


def test_admin_api_reachable_from_killswitch_label(probe_pod):
    """Positive control: the rule admits the killswitch (401 = reached APISIX, no key)."""
    assert eventually(probe_pod("gateway", {"app": "killswitch"}), ADMIN, "401") == "401"


def test_ai_namespace_has_no_internet(probe_pod):
    assert probe_pod("ai", {"app": "nettest"})("https://1.1.1.1") == "000"


def test_mcp_servers_only_reachable_through_guard(probe_pod):
    fetch = probe_pod("ai", {"app": "nettest"})
    assert fetch("http://mcp-actions.ai.svc.cluster.local:8000/healthz") == "000"
    assert fetch("http://guard.ai.svc.cluster.local:8000/healthz") == "000"  # only APISIX may call the guard


def test_agents_reach_only_the_gateway(probe_pod):
    fetch = probe_pod("ai", {"app": "researcher"})
    gateway = "http://apisix-gateway.gateway.svc.cluster.local/llm/v1/chat/completions"
    assert eventually(fetch, gateway, "404") == "404"  # reached APISIX (GET has no route)
    assert fetch("http://mcp-docs.ai.svc.cluster.local:8000/healthz") == "000"
    assert fetch("https://1.1.1.1") == "000"
