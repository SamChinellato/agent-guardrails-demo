#!/usr/bin/env python3
"""Smoke-test IBM Verify login and on-behalf-of token exchange before the demo uses them (D-072).

Signs one user in through the aigw-console app (authorization code + PKCE, in a browser),
then exchanges that user's token the way the agents will:

  1. user token            -> OBO token for the researcher   (researcher's client credentials)
  2. researcher's OBO token -> OBO token for the executor     (executor's client credentials)
  3. user token            -> OBO token for the executor      (the fallback if 2 is refused)

and prints each token's claims (never the token itself), so we can see whether the OBO tokens
carry groups, an act claim, and the right audience. Standard library only.

  python3 scripts/openshift/verify-obo-smoke.py            # reads .secrets/verify.env

The aigw-console app needs http://localhost:8765/callback as an extra redirect URI
(docs/IDENTITY.md, step 3).
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PORT = 8765
REDIRECT = f"http://localhost:{PORT}/callback"
EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"
# Claims worth seeing; anything else is listed by name only
SHOWN = ["iss", "sub", "aud", "azp", "client_id", "act", "may_act", "groups", "groupIds",
         "preferred_username", "name", "email", "scope", "grant_type", "exp", "iat"]


def env() -> dict[str, str]:
    path = ROOT / ".secrets" / "verify.env"
    if not path.is_file():
        sys.exit(f"missing {path} (docs/IDENTITY.md, step 4)")
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    need = ["VERIFY_TENANT", "VERIFY_CONSOLE_CLIENT_ID", "VERIFY_CONSOLE_CLIENT_SECRET",
            "VERIFY_RESEARCHER_CLIENT_ID", "VERIFY_RESEARCHER_CLIENT_SECRET",
            "VERIFY_EXECUTOR_CLIENT_ID", "VERIFY_EXECUTOR_CLIENT_SECRET"]
    if missing := [k for k in need if not out.get(k)]:
        sys.exit(f"{path} is missing {', '.join(missing)}")
    return out


def claims(token: str) -> dict:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def show(title: str, token: str | None) -> None:
    print(f"\n== {title}")
    if not token:
        print("   (none)")
        return
    if token.count(".") != 2:
        print(f"   not a JWT ({len(token)} characters): set the app's access token format to JWT")
        return
    c = claims(token)
    for k in SHOWN:
        if k in c:
            print(f"   {k:20} {json.dumps(c[k])}")
    if rest := sorted(set(c) - set(SHOWN)):
        print(f"   {'other claims':20} {', '.join(rest)}")


def post(url: str, form: dict[str, str]) -> tuple[int, dict]:
    """POST to a token endpoint. The client authenticates with HTTP Basic (client_secret_basic,
    Verify's default), or in the form (client_secret_post) if Basic is refused."""
    status, out = _post(url, form, basic=True)
    if status == 401 and "client_secret_post" in str(out.get("error_description", "")):
        status, out = _post(url, form, basic=False)
    return status, out


def _post(url: str, form: dict[str, str], basic: bool) -> tuple[int, dict]:
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    form = dict(form)
    if basic:
        cid, sec = form.pop("client_id"), form.pop("client_secret")
        pair = f"{urllib.parse.quote(cid, safe='')}:{urllib.parse.quote(sec, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"error": body[:300]}


def login(e: dict[str, str], base: str) -> dict:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if "code" in q or "error" in q:
                got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab and go back to the terminal.")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=lambda: [server.handle_request() for _ in range(5) if not got], daemon=True).start()

    def pasted():   # signing in on another machine: its browser can't reach localhost here
        try:
            line = input()
        except EOFError:
            return
        if "?" in line:
            got.update(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(line.strip()).query)))
    threading.Thread(target=pasted, daemon=True).start()
    url = f"{base}/authorize?" + urllib.parse.urlencode({
        "client_id": e["VERIFY_CONSOLE_CLIENT_ID"], "response_type": "code", "redirect_uri": REDIRECT,
        "scope": "openid profile email", "state": state, "prompt": "login",   # always ask: test either user
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print(f"Open this in a browser and sign in as claire or dave:\n\n  {url}\n\n"
          f"Browsing on another machine? Its tab ends on \"connection refused\": paste that tab's\n"
          f"address (http://localhost:{PORT}/callback?code=...) here and press Enter.")
    while not got:
        threading.Event().wait(0.3)
    server.server_close()
    if "error" in got:
        sys.exit(f"login failed: {got.get('error')}: {got.get('error_description', '')}")
    if got.get("state") != state:
        sys.exit("login failed: state mismatch")
    status, tok = post(f"{base}/token", {
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": REDIRECT,
        "code_verifier": verifier, "client_id": e["VERIFY_CONSOLE_CLIENT_ID"],
        "client_secret": e["VERIFY_CONSOLE_CLIENT_SECRET"]})
    if status != 200:
        sys.exit(f"code exchange failed ({status}): {tok}")
    return tok


def exchange(base: str, cid: str, secret: str, subject: str) -> tuple[int, dict]:
    return post(f"{base}/token", {
        "grant_type": EXCHANGE, "client_id": cid, "client_secret": secret,
        "subject_token": subject, "subject_token_type": ACCESS_TOKEN,
        "requested_token_type": ACCESS_TOKEN, "scope": "openid"})


def main() -> None:
    e = env()
    base = f"https://{e['VERIFY_TENANT'].removeprefix('https://').rstrip('/')}/oauth2"
    tok = login(e, base)
    show("ID token (console)", tok.get("id_token"))
    show("User access token (console)", tok.get("access_token"))
    user = tok["access_token"]

    results = {}
    for name, cid, sec, subject in [
        ("researcher, from the user's token", e["VERIFY_RESEARCHER_CLIENT_ID"], e["VERIFY_RESEARCHER_CLIENT_SECRET"], user),
        ("executor, from the researcher's OBO token", e["VERIFY_EXECUTOR_CLIENT_ID"], e["VERIFY_EXECUTOR_CLIENT_SECRET"], None),
        ("executor, from the user's token", e["VERIFY_EXECUTOR_CLIENT_ID"], e["VERIFY_EXECUTOR_CLIENT_SECRET"], user),
    ]:
        if subject is None:
            subject = results.get("researcher, from the user's token")
            if not subject:
                print(f"\n== OBO token: {name}\n   skipped: the researcher's exchange failed")
                continue
        status, out = exchange(base, cid, sec, subject)
        if status == 200 and out.get("access_token"):
            results[name] = out["access_token"]
            show(f"OBO token: {name}", out["access_token"])
            print(f"   {'issued_token_type':20} {out.get('issued_token_type')}")
        else:
            print(f"\n== OBO token: {name}\n   refused ({status}): {json.dumps(out)}")

    print("\nCopy everything above (it has no tokens in it) back into the chat.")


if __name__ == "__main__":
    main()
