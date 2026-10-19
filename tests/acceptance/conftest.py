"""Shared fixtures: gateway client, consumer keys, Admin API through a port-forward."""

import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SECRETS = ROOT / ".secrets"
KUBECONFIG = os.environ.get("KUBECONFIG_DEMO") or str(ROOT / ".kube" / "config")
KUBECTL = str(ROOT / ".tools" / "bin" / "kubectl") if (ROOT / ".tools/bin/kubectl").exists() else "kubectl"
GATEWAY = f"http://127.0.0.1:{os.environ.get('HOST_PORT_GATEWAY') or '8080'}"


def secret(name: str) -> str:
    return (SECRETS / name).read_text().strip()


def poll(fn: Callable[[], bool], timeout: float, interval: float = 0.05) -> float:
    """Call fn until it returns True; return elapsed seconds or fail at the deadline."""
    start = time.monotonic()
    while True:
        if fn():
            return time.monotonic() - start
        if time.monotonic() - start > timeout:
            pytest.fail(f"condition not met within {timeout}s")
        time.sleep(interval)


@pytest.fixture(scope="session")
def gateway() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=GATEWAY, timeout=30) as client:
        yield client


@pytest.fixture(scope="session")
def key() -> Callable[[str], str]:
    return lambda consumer: secret(f"consumer-{consumer}")


class Admin:
    """Thin APISIX Admin API client."""

    def __init__(self, base: str, api_key: str):
        self.http = httpx.Client(base_url=f"{base}/apisix/admin", headers={"X-API-KEY": api_key}, timeout=10)

    def get(self, path: str) -> dict:
        r = self.http.get(path)
        r.raise_for_status()
        return r.json()["value"]

    def put(self, path: str, body: dict) -> None:
        self.http.put(path, json=body).raise_for_status()

    def patch(self, path: str, body: dict) -> None:
        self.http.patch(path, json=body).raise_for_status()


@pytest.fixture(scope="session")
def admin() -> Iterator[Admin]:
    """Deliberate port-forward to the Admin API (rule 6), torn down after the session."""
    proc = subprocess.Popen(
        [KUBECTL, "--kubeconfig", KUBECONFIG, "-n", "gateway", "port-forward", "svc/apisix-admin", ":9180"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        line = proc.stdout.readline()
        m = re.search(r"127\.0\.0\.1:(\d+)", line)
        assert m, f"port-forward failed: {line}"
        client = Admin(f"http://127.0.0.1:{m.group(1)}", secret("apisix-admin-key"))
        poll(lambda: _reachable(client), timeout=15)
        yield client
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _reachable(client: Admin) -> bool:
    try:
        return client.http.get("/routes").status_code == 200
    except httpx.HTTPError:
        return False
