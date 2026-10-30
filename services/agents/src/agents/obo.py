"""Acting on a person's behalf (D-072, docs/IDENTITY.md).

A task may arrive with the signed-in person's token (X-Subject-Token): from the console for
the researcher, or from the researcher's own OBO token for the executor. The agent swaps it at
IBM Verify for a token of its own (OAuth 2.0 token exchange, RFC 8693) with its own client
credentials, and sends that on its tool calls as X-Obo-Token, where the guard checks it.

The tokens never reach the LLM, the MCP servers or a log line. Off unless this agent has a
Verify app (VERIFY_CLIENT_ID); a subject token is then ignored and the agent acts as itself.
"""

import base64
import contextvars
import hashlib
import os
import time

import httpx
import jwt

from demo_common import get_logger

log = get_logger("agents.obo")
TENANT = os.environ.get("VERIFY_TENANT", "").removeprefix("https://").rstrip("/")
CLIENT_ID = os.environ.get("VERIFY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("VERIFY_CLIENT_SECRET", "")
GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"

# This task's OBO token, for the hand-off to pass on (the researcher's local tool)
current: contextvars.ContextVar[str | None] = contextvars.ContextVar("obo_token", default=None)
_cache: dict[str, tuple[str, float]] = {}   # sha256(subject token) -> (OBO token, expiry)


class ExchangeRefused(Exception):
    """IBM Verify wouldn't issue this agent a token for the person."""


def enabled() -> bool:
    return bool(TENANT and CLIENT_ID and CLIENT_SECRET)


async def exchange(subject: str) -> str:
    """The person's token (or the previous agent's OBO token) -> this agent's OBO token."""
    key = hashlib.sha256(subject.encode()).hexdigest()
    now = time.time()
    if (hit := _cache.get(key)) and hit[1] - 60 > now:
        return hit[0]
    form = {"grant_type": GRANT, "subject_token": subject, "subject_token_type": ACCESS_TOKEN,
            "requested_token_type": ACCESS_TOKEN, "scope": "openid"}
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    url = f"https://{TENANT}/oauth2/token"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, data=form, headers={"Authorization": f"Basic {basic}"})
        if r.status_code == 401 and "client_secret_post" in r.text:   # an app set up that way
            r = await client.post(url, data={**form, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    if r.status_code != 200 or "access_token" not in r.json():
        detail = r.json().get("error_description") or r.json().get("error") if r.headers.get(
            "content-type", "").startswith("application/json") else r.text[:120]
        log.warning("token exchange refused", extra={"status": r.status_code, "detail": detail})
        raise ExchangeRefused(f"IBM Verify refused the token exchange ({r.status_code}): {detail}")
    token = r.json()["access_token"]
    exp = float(claims(token).get("exp") or now + 300)
    for k in [k for k, (_, e) in _cache.items() if e < now]:
        del _cache[k]
    _cache[key] = (token, exp)
    return token


def claims(token: str) -> dict:
    """Unverified claims, for the summary below; the guard is what verifies the token."""
    return jwt.decode(token, options={"verify_signature": False})


def summary(agent: str, token: str) -> dict:
    """What the console shows about an OBO token: whose it is, for whom, until when, and every
    claim IBM Verify put in it. Never the token itself."""
    c = claims(token)
    groups = c.get("groups") or []
    return {"agent": agent, "user": c.get("preferred_username") or c.get("sub"), "sub": c.get("sub"),
            "aud": c.get("aud"), "client_id": c.get("client_id"), "grant_type": c.get("grant_type"),
            "groups": [groups] if isinstance(groups, str) else groups, "iat": c.get("iat"), "exp": c.get("exp"),
            "claims": c}
