# Keycloak Auth Setup — OAS MCP Server

This guide walks through deploying Keycloak, wiring it into the OAS MCP server, and connecting Claude Desktop or Claude.ai so every request is authenticated.

## Table of Contents

1. [Why auth matters](#1-why-auth-matters)
2. [Deploy Keycloak on Railway](#2-deploy-keycloak-on-railway)
3. [Create a realm](#3-create-a-realm)
4. [Create the `oas-mcp` client](#4-create-the-oas-mcp-client)
5. [Create the `mcp:tools` scope](#5-create-the-mcptools-scope)
6. [Create a test user](#6-create-a-test-user)
7. [Configure the OAS server](#7-configure-the-oas-server)
8. [Get a test token](#8-get-a-test-token)
9. [Connect Claude Desktop](#9-connect-claude-desktop)
10. [Connect Claude.ai via ngrok](#10-connect-claudeai-via-ngrok)
11. [Connect ChatGPT via Custom Actions](#11-connect-chatgpt-via-custom-actions)
12. [Verify auth end-to-end](#12-verify-auth-end-to-end)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Why auth matters

When `OAS_TRANSPORT=http` is set and `KEYCLOAK_ISSUER_URL` is **not** set, the server accepts every request with no validation. Anyone who can reach the port can:
- Call any analysis tool
- Trigger long-running optimizations consuming CPU
- Read and delete all saved artifacts

Set `KEYCLOAK_ISSUER_URL` before exposing the server to any network. The server will then reject every request that does not carry a valid signed JWT, returning `401 Unauthorized`.

---

## 2. Deploy Keycloak on Railway

Railway is the easiest way to get a public Keycloak instance for free (or low cost).

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy a Template**
2. Search for **Keycloak** and select the official template
3. Click **Deploy** — Railway provisions a Postgres database and the Keycloak service automatically
4. In the Keycloak service settings, add these environment variables:

   | Variable | Value |
   |----------|-------|
   | `KEYCLOAK_ADMIN` | `admin` |
   | `KEYCLOAK_ADMIN_PASSWORD` | *(choose a strong password)* |

5. Under **Settings → Networking**, click **Generate Domain** to get a public URL
   - Example: `https://keycloak-production-a1b2.up.railway.app`

6. Wait for the deploy to finish (the health check turns green), then open:
   ```
   https://<your-railway-url>/admin
   ```
   Log in with the admin credentials you set above.

> **Self-hosted alternative:** Any publicly reachable Keycloak instance works. The only requirement is that the OAS MCP server can reach `<KEYCLOAK_ISSUER_URL>/protocol/openid-connect/certs` to fetch signing keys.

---

## 3. Create a realm

Realms in Keycloak are isolated namespaces for users, clients, and roles. Create a dedicated realm for OAS so it stays separate from any other projects.

1. In the Keycloak admin console, click the realm dropdown in the top-left (shows **master** by default)
2. Click **Create realm**
3. Set **Realm name** to `mcp-realm`
4. Leave **Enabled** on
5. Click **Create**

Your realm URL is now:
```
https://<your-railway-url>/realms/mcp-realm
```
This is the value you will set as `KEYCLOAK_ISSUER_URL`.

---

## 4. Create the `oas-mcp` client

Clients are applications that request tokens. Create one client that the OAS server (and your AI client) will use.

1. In the `mcp-realm` sidebar, go to **Clients** → **Create client**

2. **General settings:**
   - Client type: `OpenID Connect`
   - Client ID: `oas-mcp`
   - Name: `OAS MCP Server` *(optional)*
   - Click **Next**

3. **Capability config:**
   - **Client authentication**: ON — makes this a "confidential" client with a secret
   - **Authorization**: OFF *(not needed)*
   - Authentication flow — check **Standard flow** AND **Service accounts roles**:
     - **Standard flow** enables Authorization Code + PKCE (interactive browser login — needed for Claude Desktop with mcp-remote and for Claude.ai)
     - **Service accounts roles** enables the `client_credentials` grant (machine-to-machine, no user login — needed for the test script)
   - Click **Next**

4. **Login settings:**
   - Valid redirect URIs:
     - `http://localhost:*` (for mcp-remote OAuth callback)
     - `https://*.ngrok-free.app/*` (for Claude.ai via ngrok)
     - `https://claude.ai/api/mcp/auth_callback` (Claude.ai MCP connector callback)
   - Web origins: `+` (allows CORS from all redirect URIs)
   - Click **Save**

5. Go to the **Credentials** tab → copy the **Client secret** — you will need it shortly.

### 4a. Add the audience mapper (required)

By default Keycloak tokens do **not** include the client ID in the `aud` claim.
The OAS MCP server's JWT verifier requires `aud: "oas-mcp"` in every token.
Without this mapper, all requests return `401` even with a valid token.

1. Still in the `oas-mcp` client → go to the **Client scopes** tab
2. Click **oas-mcp-dedicated** (the auto-created dedicated scope)
3. Click **Add mapper** → **By configuration** → **Audience**
4. Fill in:
   - Name: `oas-mcp-audience`
   - Included Client Audience: `oas-mcp`
   - Add to access token: **ON**
5. Click **Save**

All tokens issued to `oas-mcp` will now include `"aud": "oas-mcp"`.

---

## 5. Create the `mcp:tools` scope

Scopes control what permissions a token grants. The OAS server requires the `mcp:tools` scope.

### 5a. Create the scope

1. Sidebar → **Client scopes** → **Create client scope**
2. Name: `mcp:tools`
3. Type: **Default** *(automatically included in every token from this realm)*
4. Protocol: `OpenID Connect`
5. Click **Save**

### 5b. Assign the scope to the client

1. Sidebar → **Clients** → click `oas-mcp`
2. Go to the **Client scopes** tab
3. Click **Add client scope**
4. Select `mcp:tools` → click **Add** → choose **Default**

Now every token issued to `oas-mcp` will contain `scope: "mcp:tools openid"`.

---

## 6. Create a test user

Create a user in the realm for interactive OAuth login (needed for Claude Desktop via mcp-remote and for Claude.ai).

1. Sidebar → **Users** → **Add user**
2. Set **Username** (e.g. `testuser`)
3. **Email verified**: ON
4. Click **Create**
5. Go to the **Credentials** tab → **Set password**
6. Enter a password and set **Temporary** to **OFF**
7. Click **Save password**

This user will be prompted to log in when MCP clients redirect to Keycloak for authorization.

---

## 7. Configure the OAS server

Copy `.env.example` to `.env` and fill in your values. `.env` is gitignored so
secrets never end up in version control.

```bash
cp .env.example .env
```

Edit `.env`:

```dotenv
OAS_TRANSPORT=http
OAS_HOST=127.0.0.1
OAS_PORT=8000
OAS_DATA_DIR=./oas_data/artifacts

KEYCLOAK_ISSUER_URL=https://<your-railway-url>/realms/mcp-realm
KEYCLOAK_CLIENT_ID=oas-mcp
KEYCLOAK_CLIENT_SECRET=<paste-secret-from-step-4>
RESOURCE_SERVER_URL=http://localhost:8000
```

The server uses `python-dotenv` to load `.env` automatically on startup — no
`source` or `export` needed.

### Local (no Docker)

```bash
oas-mcp --transport http
```

The server reads `.env` from the current working directory (or any parent).
The startup line should read:

```
OAS MCP — HTTP transport  |  auth: Keycloak (https://<your-railway-url>/realms/mcp-realm)
```

If you see the `⚠ NO AUTHENTICATION ENABLED ⚠` box instead, `KEYCLOAK_ISSUER_URL`
was not picked up — check that `.env` is in the directory you launched from and
that the value is not empty.

### Docker

`docker-compose.yml` uses `env_file` to load `.env` automatically. No edits to
the Compose file are needed — just place a populated `.env` in the same directory
and start the stack:

```bash
cp .env.example .env          # if you haven't already
# edit .env with your Keycloak values
docker compose build && docker compose up -d
docker compose logs -f        # watch for the auth confirmation line
```

The `environment:` block in `docker-compose.yml` sets Docker-specific overrides
(`OAS_TRANSPORT=http`, `OAS_HOST=0.0.0.0`, `OAS_DATA_DIR=/data/artifacts`) that
take precedence over whatever is in `.env`, so you don't need to change those.

---

## 8. Get a test token

```bash
# Set your values:
KC=https://<your-railway-url>/realms/mcp-realm
CLIENT_ID=oas-mcp
CLIENT_SECRET=<your-secret>

# Request a token via client_credentials grant:
TOKEN=$(curl -s -X POST "$KC/protocol/openid-connect/token" \
  -d "grant_type=client_credentials" \
  -d "client_id=$CLIENT_ID" \
  -d "client_secret=$CLIENT_SECRET" \
  -d "scope=mcp:tools" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "Token: ${TOKEN:0:60}..."
```

Test the server with the token:

```bash
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
# Expected: SSE stream containing serverInfo: {name: "OpenAeroStruct"}
```

Test rejection of unauthenticated requests:

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
# Expected: 401
```

---

## 9. Connect Claude Desktop

### Option A — stdio (recommended for local dev)

**stdio is local and trusted — no auth is needed.** Claude Desktop launches the
server as a subprocess over stdin/stdout. This is the simplest and most reliable
approach for local development.

Edit `claude_desktop_config.json`:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

**Windows (WSL):**

```json
{
  "mcpServers": {
    "openaerostruct": {
      "command": "wsl.exe",
      "args": [
        "bash", "-c",
        "cd /home/alex/coding/OpenAeroStruct && /home/alex/coding/OpenAeroStruct/.venv/bin/python3.11 -m oas_mcp.server"
      ]
    }
  }
}
```

**macOS / Linux:**

```json
{
  "mcpServers": {
    "openaerostruct": {
      "command": "/home/alex/coding/OpenAeroStruct/.venv/bin/python",
      "args": ["-m", "oas_mcp.server"]
    }
  }
}
```

Restart Claude Desktop after saving. The server should appear in the MCP panel within a few seconds.

---

### Option B — mcp-remote with automatic OAuth (for testing HTTP + auth)

FastMCP automatically serves the OAuth 2.1 metadata endpoints required by the
[RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) spec:

- `GET /.well-known/oauth-protected-resource` — points MCP clients to Keycloak
- `401` responses include `WWW-Authenticate: Bearer resource_metadata="..."` so
  clients know where to fetch credentials

MCP clients that support OAuth discovery (including recent `mcp-remote` builds)
can handle the entire OAuth flow automatically — no `--header` flag needed. The
client discovers Keycloak, opens a browser for login, gets a token, and uses it.

**Requirements before trying this option:**
- Keycloak `Standard flow` must be enabled on `oas-mcp` (step 4 above)
- A test user must exist in the realm (step 6 above)
- The HTTP server must be running (`oas-mcp --transport http`)

**Windows (WSL + mcp-remote, automatic OAuth):**

```json
{
  "mcpServers": {
    "openaerostruct": {
      "command": "wsl.exe",
      "args": [
        "bash", "-c",
        "/home/alex/.nvm/versions/node/v24.12.0/bin/npx -y mcp-remote http://localhost:8000/mcp"
      ]
    }
  }
}
```

> **Why full path?** Avoid `export PATH=.../bin:$PATH` in WSL — the inherited Windows `PATH` contains entries like `/mnt/c/Program Files/nodejs` where the spaces cause bash to misparse the `export` arguments. Use the absolute path to `npx` instead, the same way the stdio config uses the absolute path to python.

**macOS / Linux:**

```json
{
  "mcpServers": {
    "openaerostruct": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8000/mcp"]
    }
  }
}
```

If mcp-remote opens a browser and you log in with the test user from step 6,
the server will appear as connected with full auth.

---

### Option B fallback — dynamic token fetch

If mcp-remote doesn't support automatic OAuth discovery for your server version,
fetch a `client_credentials` token inline in the bash command:

**Windows (WSL):**

```json
{
  "mcpServers": {
    "openaerostruct": {
      "command": "wsl.exe",
      "args": [
        "bash", "-c",
        "cd /home/alex/coding/OpenAeroStruct && . <(sed 's/\\r//' .env) && TOKEN=$(curl -s \"$KEYCLOAK_ISSUER_URL/protocol/openid-connect/token\" -d grant_type=client_credentials -d \"client_id=$KEYCLOAK_CLIENT_ID\" -d \"client_secret=$KEYCLOAK_CLIENT_SECRET\" -d scope=mcp:tools | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"access_token\"])') && /home/alex/.nvm/versions/node/v24.12.0/bin/npx -y mcp-remote http://localhost:8000/mcp --header \"Authorization: Bearer $TOKEN\""
      ]
    }
  }
}
```

**macOS / Linux:**

```json
{
  "mcpServers": {
    "openaerostruct": {
      "command": "bash",
      "args": [
        "-c",
        "cd /path/to/OpenAeroStruct && . .env && TOKEN=$(curl -s \"$KEYCLOAK_ISSUER_URL/protocol/openid-connect/token\" -d grant_type=client_credentials -d \"client_id=$KEYCLOAK_CLIENT_ID\" -d \"client_secret=$KEYCLOAK_CLIENT_SECRET\" -d scope=mcp:tools | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"access_token\"])') && exec npx -y mcp-remote http://localhost:8000/mcp --header \"Authorization: Bearer $TOKEN\""
      ]
    }
  }
}
```

This fetches a fresh `client_credentials` token every time Claude Desktop starts.

> **Token lifetime:** Default Keycloak access token lifetime is 5 minutes. For local dev, increase it in Keycloak: **Realm settings → Tokens → Access token lifespan** → set to `1 day`.

---

## 10. Connect Claude.ai via ngrok

Claude.ai requires a **public HTTPS URL**. ngrok creates a secure tunnel from a
public URL to your local server in one command.

### Step 1 — Install ngrok

```bash
# Linux / WSL:
curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok-v3-stable-linux-amd64.tgz \
  | sudo tar xvz -C /usr/local/bin

# One-time setup — create a free account at ngrok.com and run:
ngrok authtoken <your-ngrok-token>
```

### Step 2 — Start the MCP server

```bash
cd /home/alex/coding/OpenAeroStruct
# Ensure .env has OAS_HOST=0.0.0.0  (required for ngrok — see note below)
.venv/bin/python3.11 -m oas_mcp.server --transport http
```

> **Why `OAS_HOST=0.0.0.0`?**
> FastMCP reads `OAS_HOST` at startup and passes it to its own constructor.
> When the host is `127.0.0.1` (the default), FastMCP **automatically enables
> DNS rebinding protection** with `allowed_hosts=["127.0.0.1:*", "localhost:*"]`.
> ngrok forwards requests with `Host: xxxx.ngrok-free.dev`, which fails this
> check and returns **421 Misdirected Request** — Claude.ai reports this as
> "There was an error connecting to the MCP server."
>
> Setting `OAS_HOST=0.0.0.0` tells FastMCP the server is listening on all
> interfaces, so it skips the localhost-only restriction and accepts any
> `Host` header. ngrok still tunnels to localhost internally — no extra
> network exposure beyond the ngrok tunnel itself.

### Step 3 — Expose the server with ngrok

In a second terminal:

```bash
ngrok http 8000
```

Note the HTTPS forwarding URL — something like:
```
https://xxxx-xx-xx-xx-xx.ngrok-free.app
```

### Step 4 — Update `RESOURCE_SERVER_URL`

The server's `RESOURCE_SERVER_URL` must match the public ngrok URL so that:
- `/.well-known/oauth-protected-resource` has the correct `resource` field
- The `aud` claim in JWT tokens matches what Claude.ai expects

Edit `.env`:

```dotenv
RESOURCE_SERVER_URL=https://xxxx-xx-xx-xx-xx.ngrok-free.app
```

Restart the MCP server (Ctrl+C and rerun step 2) to pick up the new value.

### Step 5 — Verify the OAuth metadata endpoint

```bash
curl -s https://xxxx-xx-xx-xx-xx.ngrok-free.app/.well-known/oauth-protected-resource \
  | python3 -m json.tool
```

Expected output:
```json
{
  "resource": "https://xxxx-xx-xx-xx-xx.ngrok-free.app",
  "authorization_servers": ["https://<your-railway-url>/realms/mcp-realm"],
  "scopes_supported": ["mcp:tools"]
}
```

### Step 6 — Add the connector in Claude.ai

1. Go to [claude.ai](https://claude.ai) → click your profile → **Settings**
2. Go to **Integrations** → **Add Integration**
3. Integration type: **Remote MCP Server**
4. Fill in:
   - **Name**: `OpenAeroStruct`
   - **Remote MCP Server URL**: `https://xxxx-xx-xx-xx-xx.ngrok-free.app/mcp`
   - **OAuth Client ID**: `oas-mcp`
   - **OAuth Client Secret**: *(the client secret from step 4)*
5. Click **Save**

Claude.ai will:
1. Connect to the ngrok URL → receive `401` with `resource_metadata` URL
2. Fetch `/.well-known/oauth-protected-resource` → discover Keycloak
3. Fetch Keycloak's OIDC discovery → get `authorize` and `token` endpoints
4. Do authorization_code + PKCE → open a browser window for Keycloak login
5. You log in as the test user from step 6 → Claude.ai receives a token
6. Claude.ai makes authenticated MCP requests → tools are available

### Step 7 — Test the connection

In a Claude.ai conversation, type:
```
Use the OpenAeroStruct tool to create a simple rectangular wing and run an aerodynamic analysis at alpha=5 degrees.
```

The tool panel should show the OpenAeroStruct tools are connected and the analysis should return CL, CD, and CM values.

> **ngrok URL changes every restart.** Each time you run `ngrok http 8000`, you get a new URL. Remember to update `RESOURCE_SERVER_URL` in `.env`, restart the server, and update the connector URL in Claude.ai. A paid ngrok plan gives you a stable custom domain.

---

## 11. Connect ChatGPT via Custom Actions

ChatGPT does not speak MCP natively. The integration path is to wrap the MCP tools in an **OpenAPI spec** and register it as a **Custom GPT Action**. The OAS MCP server does not expose OpenAPI directly, but the tools map naturally.

### Option A — OpenAPI wrapper (recommended)

Write a thin FastAPI/Flask proxy that:
1. Accepts HTTP requests from ChatGPT (using the OpenAPI schema you define)
2. Calls the OAS MCP server over HTTP with a `client_credentials` token
3. Returns the result as JSON

The proxy handles token refresh automatically using `httpx` with `httpx-oauth`:

```python
# Minimal sketch — not production code
import httpx, os
from httpx_oauth.clients.openid import OpenID

kc = OpenID(
    client_id=os.environ["KEYCLOAK_CLIENT_ID"],
    client_secret=os.environ["KEYCLOAK_CLIENT_SECRET"],
    openid_configuration_endpoint=os.environ["KEYCLOAK_ISSUER_URL"] + "/.well-known/openid-configuration",
)

async def call_oas_tool(tool_name: str, params: dict) -> dict:
    token = await kc.get_access_token()
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{os.environ['OAS_SERVER_URL']}/mcp",
            json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": tool_name, "arguments": params}},
            headers={"Authorization": f"Bearer {token.access_token}"},
        )
    return r.json()
```

In the Custom GPT **Actions** tab, paste your OpenAPI spec (YAML or JSON). Under **Authentication**, choose **OAuth** and fill in:

| Field | Value |
|-------|-------|
| Authorization URL | `https://<your-kc>/realms/mcp-realm/protocol/openid-connect/auth` |
| Token URL | `https://<your-kc>/realms/mcp-realm/protocol/openid-connect/token` |
| Client ID | `oas-mcp` |
| Client Secret | *(the client secret)* |
| Scope | `mcp:tools openid` |

### Option B — MCP-to-OpenAPI bridge

Projects like [`mcp-proxy`](https://github.com/sparfenyuk/mcp-proxy) can expose an MCP server as an OpenAPI endpoint automatically. Deploy the bridge alongside the OAS server and point ChatGPT at the bridge URL.

---

## 12. Verify auth end-to-end

### Unauthenticated → 401

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
# 401
```

### Invalid token → 401

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer not.a.real.token" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
# 401
```

### Valid token → 200 SSE stream

```bash
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
# event: message
# data: {"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"OpenAeroStruct",...}}}
```

### OAuth metadata endpoints

```bash
# a) Check 401 includes resource_metadata in WWW-Authenticate header
curl -sv -X POST \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}' \
  http://localhost:8000/mcp 2>&1 | grep -i www-authenticate
# Expected: WWW-Authenticate: Bearer error="invalid_token", resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource"

# b) Fetch the protected resource metadata
curl -s http://localhost:8000/.well-known/oauth-protected-resource | python3 -m json.tool
# Expected: { "resource": "http://localhost:8000", "authorization_servers": ["https://..."], "scopes_supported": ["mcp:tools"] }
```

### Run the automated test suite (`test_auth_curl.sh`)

`oas_mcp/test_auth_curl.sh` sources `.env` automatically and runs curl-based
end-to-end tests against the live server. Run it from the repo root with the
server already started:

```bash
# Terminal 1 — start the server
cd /home/alex/coding/OpenAeroStruct
.venv/bin/python3.11 -m oas_mcp.server --transport http

# Terminal 2 — run the tests
bash oas_mcp/test_auth_curl.sh
```

Override port inline if needed:

```bash
OAS_PORT=9000 bash oas_mcp/test_auth_curl.sh
```

**What each test does:**

| Test | Auth required | What it checks |
|------|---------------|----------------|
| **Test 0** | Yes (`KC` set) | `/.well-known/oauth-protected-resource` is served; `401` response includes `resource_metadata=` in `WWW-Authenticate` header; PRM document contains the Keycloak issuer URL and `mcp:tools` scope |
| **Test 1** | Yes | Unauthenticated POST → `401` |
| **Test 2** | Yes | Invalid Bearer token → `401` |
| **Test 3** | Yes + `CLIENT_SECRET` set | Fetches a real `client_credentials` token, checks JWT claims (`iss`, `aud`, `scope`), makes authenticated request → `200` with `OpenAeroStruct` in response; also checks tampered token → `401` |
| **Test 4** | No auth (no `KC` set) | Server responds normally without any token |

**Expected output (auth enabled, all passing):**

```
=== OAS MCP curl test suite ===
Server: http://127.0.0.1:8000/mcp
Keycloak: https://<your-railway-url>/realms/mcp-realm

Checking server is up...
Server is reachable.

--- Test 0: OAuth metadata endpoints ---
  PASS  401 includes resource_metadata — response contains 'resource_metadata='
  PASS  PRM has authorization_servers — response contains 'https://<kc>/realms/mcp-realm'
  PASS  PRM has scopes_supported: mcp:tools — response contains 'mcp:tools'

--- Test 1: unauthenticated request ---
  PASS  unauthenticated → 401 — got 401

--- Test 2: invalid token ---
  PASS  invalid token → 401 — got 401

--- Test 3: client_credentials token → authenticated request ---
  Fetching token from .../protocol/openid-connect/token ...
  Token: eyJhbGciOiJSUzI1NiIsInR5cCI...
  iss: https://<kc>/realms/mcp-realm
  aud: oas-mcp
  scope: mcp:tools openid ...
  PASS  valid token → serverInfo in response
  PASS  tampered token → 401 — got 401

=== Results ===
All 6 tests passed.
```

If any test fails, the script prints the expected vs. actual value and (for token
failures) a diagnosis with the specific Keycloak fix required.

---

## 13. Troubleshooting

### Server prints the `⚠ NO AUTHENTICATION ENABLED ⚠` box despite setting env vars

The module-level constants in `auth.py` are read at import time, which happens
after `python-dotenv` loads `.env` in `main()`. The most common causes:

1. **`.env` is in the wrong directory** — `python-dotenv` searches the current
   working directory and its parents. Run `oas-mcp` from the repo root where
   `.env` lives, or use an absolute path:
   ```bash
   cd /path/to/OpenAeroStruct && oas-mcp --transport http
   ```

2. **The value is empty** — check `grep KEYCLOAK_ISSUER_URL .env` returns a
   non-empty value.

3. **Env var already set to empty in the shell** — `python-dotenv` does not
   override variables already present in the process environment. Unset it first:
   ```bash
   unset KEYCLOAK_ISSUER_URL && oas-mcp --transport http
   ```

Or pass values inline to bypass `.env` entirely:

```bash
KEYCLOAK_ISSUER_URL=https://... KEYCLOAK_CLIENT_ID=oas-mcp KEYCLOAK_CLIENT_SECRET=... oas-mcp --transport http
```

---

### `401` even with a token

1. **Check token expiry:** `python3 -c "import base64,json,sys; parts=sys.argv[1].split('.'); print(json.loads(base64.b64decode(parts[1]+'==')))" "$TOKEN"` — look at the `exp` field.
2. **Check audience:** The `aud` claim in the token must match `KEYCLOAK_CLIENT_ID`. In Keycloak: **Clients** → `oas-mcp` → **Client scopes** → verify `oas-mcp-dedicated` scope includes an audience mapper.
3. **Check issuer:** The `iss` claim must exactly match `KEYCLOAK_ISSUER_URL` (no trailing slash).
4. **Check server logs:** the `KeycloakTokenVerifier.verify_token` method logs nothing on failure — add a `print` call temporarily if needed.

---

### Audience mismatch (most common Keycloak gotcha)

By default Keycloak tokens issued for a confidential client do **not** include the client ID in the `aud` claim unless you add an audience mapper.

1. **Clients** → `oas-mcp` → **Client scopes** tab
2. Click `oas-mcp-dedicated` → **Add mapper** → **By configuration** → **Audience**
3. Name: `oas-mcp-audience`
4. Included Client Audience: `oas-mcp`
5. Add to access token: ON
6. Click **Save**

Now new tokens will include `"aud": "oas-mcp"` and the server's `jwt.decode(..., audience="oas-mcp")` call will succeed.

---

### Token lifetime too short for Claude Desktop

Default access token lifetime in Keycloak is 5 minutes. For local dev, increase it:

**Realm settings** → **Tokens** → **Access token lifespan** → set to `1 day` (or longer for local dev).

For production, implement token refresh in the wrapper script instead.

---

### HTTPS required for Claude.ai

Claude.ai will refuse to connect to `http://` endpoints. Options:
- Use ngrok: `ngrok http 8000` (see [step 10](#10-connect-claudeai-via-ngrok) above)
- Deploy on Railway / Render / Fly — all provide automatic HTTPS
- Use a Cloudflare Tunnel: `cloudflared tunnel --url http://localhost:8000`

---

### ngrok URL changes on every restart

Each `ngrok http 8000` invocation gives a new random URL (on the free plan).
After restarting ngrok you must:
1. Update `RESOURCE_SERVER_URL` in `.env`
2. Restart the MCP server
3. Update the connector URL in Claude.ai Settings → Integrations

A paid ngrok plan provides a stable custom domain that never changes.

---

### WSL: `export: 'Files/nodejs:/mnt/c/Program': not a valid identifier`

This error appears when `export PATH=.../bin:$PATH` is used in a WSL bash
command launched by Claude Desktop. The inherited Windows `PATH` contains entries
like `/mnt/c/Program Files/nodejs` where the **spaces in "Program Files"** cause
bash to misparse the space-separated fragments as separate `export` arguments.

**Fix:** Use the absolute path to `npx` instead of manipulating `$PATH`:

```json
{
  "mcpServers": {
    "openaerostruct": {
      "command": "wsl.exe",
      "args": [
        "bash", "-c",
        "/home/alex/.nvm/versions/node/v24.12.0/bin/npx -y mcp-remote http://localhost:8000/mcp"
      ]
    }
  }
}
```

Find the correct absolute path with `which npx` in your WSL terminal. This is the same pattern the working stdio config uses for python.

---

### Claude.ai: "There was an error connecting to the MCP server … handles auth correctly"

This error appears when Claude.ai tries to connect during integration setup. Work
through these checks in order:

**Check 1 — DNS rebinding protection (most common cause with ngrok)**

The server passes `OAS_HOST` to the `FastMCP()` constructor at startup. When
`OAS_HOST=127.0.0.1`, FastMCP auto-enables host-header validation with
`allowed_hosts=["127.0.0.1:*", "localhost:*"]`. ngrok sends
`Host: xxxx.ngrok-free.dev`, which doesn't match and returns **421 Misdirected
Request** before auth middleware even runs. Claude.ai reports this as a
connection error.

Fix: set `OAS_HOST=0.0.0.0` in `.env` and **restart the server**. With
`0.0.0.0`, FastMCP skips the localhost-only restriction entirely.

**Check 2 — Server not restarted after `.env` change**

Run:
```bash
curl -s https://<ngrok-url>/.well-known/oauth-protected-resource | python3 -m json.tool
```
The `"resource"` field must be the ngrok URL. If it shows `http://localhost:8000`
the server is still using old env vars — restart it.

**Check 3 — Audience mapper missing**

Run the diagnostic commands from [step 12](#12-verify-auth-end-to-end) with a
real token. If the token has `aud: "account"` or no `aud` field, the audience
mapper hasn't been added. Follow step [4a](#4a-add-the-audience-mapper-required).

**Check 4 — ngrok tunnel not running / URL expired**

```bash
curl -sv https://<ngrok-url>/mcp 2>&1 | grep "< HTTP"
# Should return: HTTP/2 401 (or HTTP/2 421 if OAS_HOST is still 127.0.0.1)
# If you get a connection error: start ngrok (ngrok http 8000)
```

**Check 5 — Keycloak unreachable**

```bash
curl -s https://<keycloak>/realms/mcp-realm/.well-known/openid-configuration | python3 -m json.tool
# Should return Keycloak's OIDC discovery document
```

---

### Claude.ai: `invalid_parameter: redirect_uri`

Keycloak rejects the authorization request because the redirect URI sent by
Claude.ai is not in the `oas-mcp` client's allowed list.

**Fix:** Add Claude.ai's callback URI to the `oas-mcp` client in Keycloak:

1. Keycloak admin → `mcp-realm` → **Clients** → `oas-mcp` → **Settings**
2. Under **Valid redirect URIs**, add: `https://claude.ai/api/mcp/auth_callback`
3. Click **Save**

**You do not need a separate client for Claude.ai.** The existing `oas-mcp`
confidential client supports both `client_credentials` (for the test script) and
`authorization_code` (for Claude.ai) simultaneously — you just need all expected
redirect URIs registered.
