"""On-behalf-of tokens (D-072, docs/IDENTITY.md): who an agent is acting for.

An agent signed in for a person sends its OBO token (IBM Verify, RFC 8693) on each tool call
as X-Obo-Token. The guard checks its signature against Verify's JWKS, its issuer and expiry,
and that its audience is the calling agent's own Verify app: Verify puts no act claim in the
token, so the audience is what ties it to one agent, and pairing it with the agent the
gateway authenticated (X-Agent-Id, from its key) stops one agent replaying another's token.

Off (every call anonymous) unless VERIFY_TENANT is set, as on k3d.
"""

import os

import jwt

from demo_common import get_logger

log = get_logger("guard.obo")
TENANT = os.environ.get("VERIFY_TENANT", "").removeprefix("https://").rstrip("/")
ISSUER = f"https://{TENANT}/oauth2"
# Each agent's Verify client ID, which is its OBO tokens' audience
AUDIENCES = {agent: os.environ.get(f"VERIFY_AUDIENCE_{agent.upper()}", "") for agent in ("researcher", "executor")}
_jwks = jwt.PyJWKClient(f"{ISSUER}/jwks", cache_keys=True, lifespan=3600) if TENANT else None


def enabled() -> bool:
    return _jwks is not None


def verify(token: str, agent: str) -> tuple[dict | None, str | None]:
    """(claims, None) for a valid token for this agent, or (None, why not). Blocking: the
    first call per signing key fetches the JWKS, so call it off the event loop."""
    if _jwks is None:
        return None, "IBM Verify is not configured on the guard"
    audience = AUDIENCES.get(agent)
    if not audience:
        return None, f"the {agent or 'caller'} has no IBM Verify app, so it can't act for a person"
    try:
        key = _jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=audience, issuer=ISSUER,
                            leeway=30, options={"require": ["exp", "sub", "aud"]})
    except jwt.InvalidAudienceError:
        return None, f"it was issued to another app, not the {agent}"
    except jwt.ExpiredSignatureError:
        return None, "it has expired"
    except (jwt.PyJWTError, jwt.PyJWKClientError) as e:
        return None, f"it isn't valid ({type(e).__name__})"
    return claims, None


def user_of(claims: dict) -> tuple[str, list[str]]:
    """The person's name for the decision trail, and their groups (a string when there is
    only one, in Verify's OBO tokens)."""
    groups = claims.get("groups") or []
    groups = [groups] if isinstance(groups, str) else [g for g in groups if g != "allUsers"]
    return claims.get("preferred_username") or claims["sub"], groups
