# User identity with IBM Verify (OpenShift only)

The console signs users in with IBM Verify, and each agent swaps the user's token for one of its own, *on behalf of* that user (OAuth 2.0 token exchange, RFC 8693). The guard checks that token on every call, so an agent can never do more than the person it is working for. That is the confused-deputy problem, closed at the gateway. Why it is built this way is in [DECISIONS.md D-072](DECISIONS.md).

This runs on OpenShift only; k3d keeps consumer keys and the anonymous demo user. Everything is switched by `AUTH_MODE` on the console (`none`, the default, or `verify`), so turning it off is one setting.

```
user ──sign in──▶ IBM Verify ──user token──▶ console backend (tokens stay server-side; the browser gets a session cookie)
console ──user token──▶ researcher ──token exchange──▶ Verify ──▶ OBO token  (sub = the user, aud = researcher)
researcher ──OBO token + its key──▶ gateways ──▶ guard: checks signature, issuer, audience; decides on the user's groups
researcher ──hand-off──▶ executor ──token exchange──▶ OBO token  (sub = the user, aud = executor)
```

## Status

| Phase | What | State |
|---|---|---|
| 0 | Verify tenant: users, groups, three apps (below) | done (`samcverify.verify.ibm.com`, 2026-10-30) |
| 1 | Smoke test: `scripts/openshift/verify-obo-smoke.py` shows what the tokens carry | passed for claire and dave; findings below |
| 2 | Console sign-in (`AUTH_MODE=verify`) | live on syd05 (2026-10-30) |
| 3 | Sign-in page, user menu, and an **Identity** panel showing the user's decoded Verify token | sign-in and menu live; panel deploying (2026-10-30) |
| 4 | Agents exchange and send OBO tokens; the guard checks them and decides on groups | live on syd05 (2026-10-30) |
| 5 | NetworkPolicy egress to the tenant, Secret, decision trail shows the user | egress and Secret done; the user is on every decision record |

## How the agents act for a person

1. **The console sends the user's token.** Every request from the signed-in console to the researcher carries the person's access token as `X-Subject-Token`, including story prompts. The token stays server-side; the browser never sees it.
2. **The researcher swaps it for its own token.** It exchanges the token at Verify with its own client credentials (`agents/obo.py`). The result is a token whose subject is the person and whose audience is the researcher.
3. **The token goes on tool calls only.** The researcher sends that token as `X-Obo-Token` on its tool calls. It never goes to the LLM, the MCP servers or a log line; the gateway logs no request headers.
4. **The hand-off passes it on.** The researcher's OBO token goes to the executor as `X-Subject-Token`. The executor exchanges it in turn, getting a token for the same person with the executor as audience.
5. **The guard checks each tool call** (`guard/obo.py`, `mcp_proxy.py`). It verifies the token's signature against Verify's JWKS, and its issuer, expiry and audience. The audience must be the agent the gateway authenticated by its key, so an agent can't present another agent's token. The guard then runs three checks, in order:
   1. `mcp.tool_allowlist`: what the agent may do.
   2. `mcp.argument_rules`: the agent's argument rules.
   3. `mcp.user_entitlement`: what the person may do, by their groups in the policy's `users.groups`.

   The agent's own policy comes first, so a critical breach, such as the customer export in beat 4, still trips the circuit breaker whoever the agent acts for.
6. **Every decision names the person.** Decision records carry `user`, and so do the spans' `guard.decision` events. Each agent reports the claims of the token it used, never the token, as `identity`. The console keeps them under `acting_for_you` in `/api/me`.

| | `analyst-readonly` (claire) | `analyst-actions` (dave) |
|---|---|---|
| list and read documents | ✓ | ✓ |
| update a record, send email | ✗ the executor is refused | ✓ within the executor's own argument rules |
| export customer data | ✗ for everyone (`deny_tools_all`, trips the circuit breaker) | ✗ |

A call with no token is anonymous and skips `mcp.user_entitlement`, as on k3d or with `AUTH_MODE=none`. A token that fails verification blocks the call.

## Showing it

- **Identity** (header button, next to Monitoring; signed-in only): the person's IBM Verify token, decoded. It shows `sub`, `username`, `groups` (highlighted: the permissions come from it), `aud` and `expires`, and **Show all claims** gives the full set. The token itself never reaches the browser; the console decodes it server-side for `/api/me`.
- **Claire against Dave:** send *Put Blue Ridge on watch and raise a corrective action with quality*. Any message mentioning Blue Ridge plays the baseline scenario (`s0:scripted`): the executor updates Blue Ridge's record and emails the internal quality team. Both actions are within the executor's own rules, so only the person decides:
  - As claire, both are refused (`mcp.user_entitlement`, "claire may not …").
  - As dave, both go through.
  - Press **New chat** between runs.
- **On the real model:** for a signed-in person, live chat (Granite) may hand work to the executor (the console sends `handoff`), so the identity check also happens unscripted. Try *Hand this to the executor: email logistics@demo.local that Northwind's late shipments in week 32 are resolved.* As claire the console shows **Insufficient permissions**; as dave the email is sent. Granite's wording, and whether it hands off at all, vary.
- **Decision details:** every guard decision names the person the agent acted for.

## What the smoke test found

Run on 2026-10-30 against `samcverify.verify.ibm.com`, as claire and as dave:

- **The user's tokens.** The ID token and the access token carry `groups` as a list, for example `["analyst-readonly", "allUsers"]`, along with `preferred_username` and a stable `sub` (claire is `6470002ITF`).
- **The OBO tokens.** Every exchange worked, including the executor exchanging the researcher's OBO token, so the hand-off can pass that token on. Each OBO token keeps the user's `sub` and `preferred_username`. Its `aud` and `client_id` are the agent's own client ID.
- **`groups` in the OBO tokens.** They have it, but as a **string** when there is one group (`"analyst-readonly"`, without `allUsers`). The guard and the console accept a string or a list.
- **No `act` claim.** The OBO token names the agent only through `aud` and `client_id`. So the guard pairs the two: a call made with the researcher's gateway key must carry an OBO token whose audience is the researcher. That stops one agent replaying another's token.
- **Expiry.** An OBO token expires when the user's access token does, an hour after sign-in. An agent can never act for someone for longer than that person's own sign-in lasts.
- **Client authentication.** The apps use `client_secret_basic` (the secret in an HTTP Basic header). The scripts and the console send it that way, and fall back to sending it in the form.
- **Testing either user.** Verify keeps you signed in, so the smoke test sends `prompt=login` to always show the sign-in page. Browsing from another machine, the final `localhost:8765` tab is refused; paste its address into the script.

## Setting up the Verify tenant

Sign in to the admin console at `https://<TENANT>.verify.ibm.com/ui/admin` as an administrator. You will create:

- two users, Claire (read-only) and Dave (may let the executor act)
- two groups, one for each
- three OpenID Connect applications: one for the console, and one for each agent

Throughout, `<CONSOLE>` means the console's address on the cluster. On syd05 it is:

```
https://aigw-console.apps.itz-0x5jw5.infra01-lb.syd05.techzone.ibm.com
```

On another cluster, it is `https://aigw-console.<ingress domain>`; `oc get ingresses.config cluster -o jsonpath='{.spec.domain}'` prints the domain.

### 1. Users

#### Claire (read-only)
1. Navigate to **Directory** → **Users & groups** → **Users** tab
2. Click **Add user**
3. Configure:
   - Identity provider: `Cloud Directory`
   - User name: `claire`
   - Given name / Surname: `Claire` / `Analyst`
   - Preferred email: `<your-email>` *(Verify sends the first password here!)*
4. Click **Save**

#### Dave (may let the executor act)
Repeat the steps for Claire, with user name `dave` and given name `Dave`.

> **Tip:** you can give both users the same email address. Sign in as each one once in a private window before the demo, to set their passwords; Verify asks for a new password at the first sign-in.

### 2. Groups

#### `analyst-readonly`
1. Navigate to **Directory** → **Users & groups** → **Groups** tab
2. Click **Add group**
3. Configure:
   - Group name: `analyst-readonly` *(exactly this: the guard's policy matches the name)*
   - Group members: add **claire**
4. Click **Save**

#### `analyst-actions`
Repeat the steps for `analyst-readonly`, with group name `analyst-actions` and member **dave**.

### 3. Console application (`aigw-console`)

The application people sign in to.

1. Navigate to **Applications** → **Applications** → **Add application**
2. Select **OpenID Connect** → **Add application**
3. **General** tab:
   - Name: `aigw-console`
   - Company name: `IBM`
4. **Sign-on** tab:
   - Application URL: `<CONSOLE>/console/`
   - Grant types: tick **Authorization code** only; untick everything else
   - Redirect URIs, add both:
     - `<CONSOLE>/console/auth/callback`
     - `http://localhost:8765/callback` *(for the smoke test in step 6; remove it afterwards)*
   - **Require proof key for code exchange (PKCE) verification**: **on** *(unlike confused-deputy, which turns it off)*
   - Client authentication method: `Client secret basic` or `Client secret post` *(either works)*
5. Scroll down to **Token settings**:
   - Access token format: `JWT`
   - Audiences: `https://<TENANT>.verify.ibm.com/oauth2/token` *(confused-deputy sets this so the user's token can be exchanged)*
6. Scroll down to **Endpoint configuration**:
   - **ID token and user info** → **Edit** → **Add attribute**:
     - Verify attribute: `groupIds`
     - Target attribute: `groups`
     - ✅ Tick **Update on refresh**
   - **Introspect** → **Edit** → **Add attribute**, the same mapping:
     - Verify attribute: `groupIds`
     - Target attribute: `groups`
     - ✅ Tick **Update on refresh**
7. Scroll down to **Single logout settings**:
   - Post logout redirect URIs: `<CONSOLE>/console/`
8. Click **Save**. The **Sign-on** tab now shows the **Client ID** and **Client secret**: keep them for step 5.
9. **Entitlements** tab:
   - Tick **Automatic access for all users and groups**
10. Click **Save**

### 4. Agent applications (`aigw-researcher`, `aigw-executor`)

One application per agent, so each on-behalf-of token names the agent it was issued to. They have no sign-in page; they only exchange tokens.

#### `aigw-researcher`
1. Navigate to **Applications** → **Applications** → **Add application**
2. Select **OpenID Connect** → **Add application**
3. **General** tab:
   - Name: `aigw-researcher`
   - Company name: `IBM`
4. **Sign-on** tab:
   - Application URL: `http://localhost` *(a placeholder: the agent has no page)*
   - Grant types: tick **Token exchange** only; untick everything else
   - Redirect URIs: `http://not-in-use/callback` *(a placeholder as well)*
5. Click **Save** once, so the **Client ID** appears, then copy it.
6. Scroll down to **Token settings**:
   - Access token format: `JWT`
   - Audiences: the **Client ID** you just copied *(the token is for this agent)*
7. Scroll down to **Endpoint configuration**: add the same `groupIds` → `groups` attribute to **ID token and user info** and to **Introspect**, with ✅ **Update on refresh**, exactly as in step 3.6
8. Click **Save**. Keep the **Client ID** and **Client secret** for step 5.
9. **Entitlements** tab:
   - Tick **Automatic access for all users and groups**
10. Click **Save**

#### `aigw-executor`
Repeat the steps for `aigw-researcher`, with name `aigw-executor`. Its audience is its own Client ID.

### 5. Hand the credentials over

Create `.secrets/verify.env` in the repository. The `.secrets/` folder is gitignored. Never paste these values into a chat, an issue or the workshop repo.

```bash
# IBM Verify tenant (no https://)
VERIFY_TENANT=<TENANT>.verify.ibm.com

# aigw-console (step 3)
VERIFY_CONSOLE_CLIENT_ID=<console-client-id>
VERIFY_CONSOLE_CLIENT_SECRET=<console-client-secret>

# aigw-researcher and aigw-executor (step 4)
VERIFY_RESEARCHER_CLIENT_ID=<researcher-client-id>
VERIFY_RESEARCHER_CLIENT_SECRET=<researcher-client-secret>
VERIFY_EXECUTOR_CLIENT_ID=<executor-client-id>
VERIFY_EXECUTOR_CLIENT_SECRET=<executor-client-secret>
```

### 6. Smoke test

```bash
python3 scripts/openshift/verify-obo-smoke.py
```

1. A browser opens on the Verify sign-in page. Sign in as **claire**.
2. The browser shows "Signed in. You can close this tab".
3. The terminal prints the claims of the user's token and of each on-behalf-of token. It never prints the tokens themselves.
4. Paste the terminal output into the chat.

What to look for:

| In the output | Means |
|---|---|
| `groups ["analyst-readonly"]` under the ID token | the attribute mapping in step 3.6 works |
| `groups` under the **OBO** tokens too | the guard can decide on groups from the OBO token alone; if it's missing, step 4.7 didn't save |
| `aud` under an OBO token is that agent's Client ID | step 4.6 is right |
| `act` under an OBO token | Verify names the agent as the actor as well (nice to have) |
| `refused (400): invalid_grant` or `unauthorized_client` | that app's **Token exchange** grant isn't ticked, or the console app's audience (step 3.5) is wrong |
| `not a JWT` | that app's access token format isn't `JWT` |

The second exchange, the executor exchanging the researcher's OBO token, may be refused by design. That is a finding, not a failure; the third exchange is the fallback.

Run it again as **dave** to check the other group.

### 7. Switch the console over

```bash
oc login <api-url>
scripts/openshift/verify-secret.sh
```

Open `<CONSOLE>/`. The console shows its sign-in page, **Sign in with IBM Verify**: sign in as claire or dave. There is no OpenShift login in front of the console; Verify is its only gate. Sign out is in the user menu at the top right. Once that works, remove `http://localhost:8765/callback` from the `aigw-console` application (step 3.4).

## Rollback

- **Turn off only the user check:** `make policy-set CHECK=mcp.user_entitlement MODE=off`. Users still sign in, and the agents still exchange tokens; the guard just stops deciding on groups.
- **Go back to the OpenShift login:** revert the D-072 commit that removed the console's OAuth proxy, push, and refresh Argo CD. That restores the proxy in front of an anonymous console.
- **There is no `MODE=none`.** With no proxy in front, an anonymous console would be public. On OpenShift the console sets `REQUIRE_SIGN_IN=true`, so without the `verify-oidc` Secret it answers 503.
