"""Console sign-in with IBM Verify (D-072, OpenShift only; docs/IDENTITY.md).

AUTH_MODE=none (the default, and k3d) keeps the anonymous demo-user console. AUTH_MODE=verify
signs people in with OIDC authorization code + PKCE. The tokens stay in this process; the
browser only ever holds an opaque session cookie, so a token can't leak from the page.

The app's paths are /auth/login, /auth/callback, /auth/logout and /api/me; the browser sees
them under /console/ (APISIX strips the prefix), which is why the cookie and the redirects
use that prefix.
"""

import base64
import contextvars
import hashlib
import os
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from demo_common import get_logger

log = get_logger("console.identity")
MODE = os.environ.get("AUTH_MODE", "none")
TENANT = os.environ.get("VERIFY_TENANT", "").removeprefix("https://").rstrip("/")
CLIENT_ID = os.environ.get("VERIFY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("VERIFY_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("VERIFY_REDIRECT_URI", "")   # <console>/console/auth/callback
SCOPE = "openid profile email"
PREFIX = "/console/"           # where the browser sees this app
COOKIE = "aigw_session"
LOGIN_TTL = 600                # seconds to finish signing in
SESSION_TTL = 8 * 3600         # the demo day; the token's own expiry ends it sooner


@dataclass
class Session:
    claims: dict                       # from the verified ID token
    access_token: str                  # the user's token, sent to the researcher to exchange
    id_token: str
    expires: float
    obo: dict = field(default_factory=dict)   # agent -> its OBO token's claims, as it reported them


# This request's signed-in session, so story prompts (scenarios.py) send the same token
current: contextvars.ContextVar["Session | None"] = contextvars.ContextVar("session", default=None)
_pending: dict[str, tuple[str, str, float]] = {}   # state -> (PKCE verifier, nonce, deadline)
_sessions: dict[str, Session] = {}
_oidc: dict = {}
_jwks: jwt.PyJWKClient | None = None


def enabled() -> bool:
    return MODE == "verify"


async def _discovery() -> dict:
    """Verify's endpoints, from its discovery document (fetched once)."""
    global _jwks
    if not _oidc:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://{TENANT}/oauth2/.well-known/openid-configuration")
            r.raise_for_status()
            _oidc.update(r.json())
        _jwks = jwt.PyJWKClient(_oidc["jwks_uri"], cache_keys=True)
    return _oidc


def session(request: Request) -> Session | None:
    s = _sessions.get(request.cookies.get(COOKIE, ""))
    if s and s.expires > time.time():
        return s
    return None


def agent_headers() -> dict[str, str]:
    """For a call to an agent: the signed-in person's token, which the agent exchanges for its
    own on-behalf-of token (D-072). Empty when nobody is signed in."""
    s = current.get()
    return {"X-Subject-Token": s.access_token} if s else {}


def remember(result: dict) -> None:
    """Keep the OBO token claims an agent reported, for /api/me (never the tokens)."""
    if (s := current.get()) is not None:
        for i in result.get("identity") or []:
            if i.get("agent"):
                s.obo[i["agent"]] = i


def user(request: Request) -> dict | None:
    """Who is signed in, as the page and the decision trail show it; None if nobody is."""
    if (s := session(request)) is None:
        return None
    c = s.claims
    return {"sub": c.get("sub"), "username": c.get("preferred_username") or c.get("email") or c.get("sub"),
            "name": c.get("name") or c.get("preferred_username"), "groups": _groups(c)}


def _groups(c: dict) -> list[str]:
    g = c.get("groups") or c.get("groupIds") or []
    return [g] if isinstance(g, str) else list(g)


async def login(request: Request) -> Response:
    oidc = await _discovery()
    verifier, state, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    now = time.time()
    for k in [k for k, (_, _, t) in _pending.items() if t < now]:
        del _pending[k]
    _pending[state] = (verifier, nonce, now + LOGIN_TTL)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return RedirectResponse(oidc["authorization_endpoint"] + "?" + urlencode({
        "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI, "scope": SCOPE,
        "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}))


async def callback(request: Request) -> Response:
    q = request.query_params
    if q.get("error"):
        log.warning("sign-in refused", extra={"error": q.get("error")})
        return _problem(f"IBM Verify refused the sign-in: {q.get('error_description') or q.get('error')}")
    pending = _pending.pop(q.get("state", ""), None)
    if not pending or pending[2] < time.time() or not q.get("code"):
        return _problem("That sign-in link has expired. Sign in again.")
    verifier, nonce, _ = pending
    oidc = await _discovery()
    r = await token_request(oidc["token_endpoint"], CLIENT_ID, CLIENT_SECRET, {
        "grant_type": "authorization_code", "code": q["code"], "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier})
    if r.status_code != 200:
        log.warning("code exchange failed", extra={"status": r.status_code})
        return _problem("IBM Verify didn't accept the sign-in. Sign in again.")
    tok = r.json()
    try:
        claims = _verify_id_token(tok.get("id_token", ""), oidc["issuer"])
    except jwt.PyJWTError as e:
        log.warning("ID token rejected", extra={"error": str(e)})
        return _problem("The sign-in couldn't be verified. Sign in again.")
    if claims.get("nonce") != nonce:
        return _problem("The sign-in couldn't be verified. Sign in again.")

    sid = secrets.token_urlsafe(32)
    _sessions[sid] = Session(claims=claims, access_token=tok["access_token"], id_token=tok["id_token"],
                             expires=min(time.time() + SESSION_TTL, claims.get("exp", 0) or time.time() + SESSION_TTL))
    log.info("signed in", extra={"user": claims.get("preferred_username") or claims.get("sub"),
                                 "groups": ",".join(_groups(claims))})
    resp = RedirectResponse(PREFIX, 303)
    resp.set_cookie(COOKIE, sid, httponly=True, secure=True, samesite="lax", path=PREFIX, max_age=SESSION_TTL)
    return resp


async def token_request(url: str, client_id: str, secret: str, form: dict) -> httpx.Response:
    """Call a token endpoint as this client: HTTP Basic (client_secret_basic, Verify's default),
    or the secret in the form (client_secret_post) if the app is set up that way."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, data=form, auth=(client_id, secret))
        if r.status_code == 401 and "client_secret_post" in r.text:
            r = await client.post(url, data={**form, "client_id": client_id, "client_secret": secret})
    return r


def _verify_id_token(token: str, issuer: str) -> dict:
    assert _jwks is not None
    key = _jwks.get_signing_key_from_jwt(token)
    return jwt.decode(token, key.key, algorithms=["RS256"], audience=CLIENT_ID, issuer=issuer, leeway=30)


async def logout(request: Request) -> Response:
    s = _sessions.pop(request.cookies.get(COOKIE, ""), None)
    target = PREFIX
    end = (await _discovery()).get("end_session_endpoint")
    if s and end:   # end the Verify session too, or the next sign-in skips the password
        target = end + "?" + urlencode({"id_token_hint": s.id_token,
                                        "post_logout_redirect_uri": REDIRECT_URI.rsplit("/auth/", 1)[0] + "/"})
    resp = RedirectResponse(target, 303)
    resp.delete_cookie(COOKIE, path=PREFIX)
    return resp


def me(request: Request) -> JSONResponse:
    """The Identity panel: who is signed in, and the claims of each token held for them."""
    if not enabled():
        return JSONResponse({"mode": MODE})
    if (s := session(request)) is None:
        return JSONResponse({"mode": MODE, "user": None}, 401)
    return JSONResponse({"mode": MODE, "user": user(request),
                         "tokens": {"id_token": s.claims, "access_token": _claims(s.access_token)},
                         "acting_for_you": s.obo})


def _claims(token: str) -> dict | None:
    """A token's claims for display only; the guard is what verifies the tokens agents send."""
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None


def _problem(text: str) -> Response:
    """Back to the sign-in page, which says what went wrong."""
    return RedirectResponse(PREFIX + "signin?" + urlencode({"error": text}), 303)
