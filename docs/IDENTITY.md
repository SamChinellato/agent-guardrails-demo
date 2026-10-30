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
| 3 | Identity panel: the user's token and each agent's OBO token | after phase 1 |
| 4 | Agents exchange and send OBO tokens; the guard checks them and decides on groups | after phase 1 |
| 5 | NetworkPolicy egress to the tenant, Secret, decision trail shows the user | with 2 and 4 |

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

Open `<CONSOLE>/console/`. You sign in to OpenShift first, then the console shows its sign-in page: **Sign in with IBM Verify**, as claire or dave. Sign out is in the user menu at the top right. Once that works, remove `http://localhost:8765/callback` from the `aigw-console` application (step 3.4).

## Rollback

Set `AUTH_MODE=none` on the console, or revert the D-072 commits. The anonymous `demo-user` path is unchanged.
