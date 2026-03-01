# Keycloak Auth Setup — OAS MCP Server

This guide walks through deploying Keycloak, wiring it into the OAS MCP server, and connecting Claude.ai or ChatGPT so every request is authenticated.

## Table of Contents

1. [Why auth matters](#1-why-auth-matters)
2. [Deploy Keycloak on Railway](#2-deploy-keycloak-on-railway)
3. [Create a realm](#3-create-a-realm)
4. [Create the `oas-mcp` client](#4-create-the-oas-mcp-client)
5. [Create the `mcp:tools` scope](#5-create-the-mcptools-scope)
6. [Configure the OAS server](#6-configure-the-oas-server)
7. [Get a test token](#7-get-a-test-token)
8. [Connect Claude Desktop with auth](#8-connect-claude-desktop-with-auth)
9. [Connect Claude.ai (remote MCP)](#9-connect-claudeai-remote-mcp)
10. [Connect ChatGPT via Custom Actions](#10-connect-chatgpt-via-custom-actions)
11. [Verify auth end-to-end](#11-verify-auth-end-to-end)
12. [Troubleshooting](#12-troubleshooting)

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
3. Set **Realm name** to `oas`
4. Leave **Enabled** on
5. Click **Create**

Your realm URL is now:
```
https://<your-railway-url>/realms/oas
```
This is the value you will set as `KEYCLOAK_ISSUER_URL`.

---

## 4. Create the `oas-mcp` client

Clients are applications that request tokens. Create one client that the OAS server (and your AI client) will use.

1. In the `oas` realm sidebar, go to **Clients** → **Create client**

2. **General settings:**
   - Client type: `OpenID Connect`
   - Client ID: `oas-mcp`
   - Name: `OAS MCP Server` *(optional)*
   - Click **Next**

3. **Capability config:**
   - **Client authentication**: ON — makes this a "confidential" client with a secret
   - **Authorization**: OFF *(not needed)*
   - Authentication flow — check only **Service accounts roles**
     - This enables the `client_credentials` grant (machine-to-machine, no user login)
   - Click **Next**

4. **Login settings:**
   - Valid redirect URIs: `http://localhost:8000/*`
   - Web origins: `http://localhost:8000` *(or `*` for testing)*
   - Click **Save**

5. Go to the **Credentials** tab → copy the **Client secret** — you will need it shortly.

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

## 6. Configure the OAS server

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

KEYCLOAK_ISSUER_URL=https://<your-railway-url>/realms/oas
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
OAS MCP — HTTP transport  |  auth: Keycloak (https://<your-railway-url>/realms/oas)
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

## 7. Get a test token

```bash
# Set your values:
KC=https://<your-railway-url>/realms/oas
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

## 8. Connect Claude Desktop with auth

Claude Desktop (≥ 0.10) supports remote HTTP MCP servers directly via a `"url"`
key in the config — no `mcp-remote` bridge needed. This is the recommended
approach and mirrors how cloud deployments work.

### Step 1 — Fetch a Bearer token

The Bearer token is a **JWT access token** — not the client secret. Fetch one
with the `client_credentials` grant. Source your `.env` first so the values
are available automatically:

```bash
# Run from the repo root (WSL or Linux/macOS terminal)
source <(sed 's/\r//' .env)

TOKEN=$(curl -s -X POST "$KEYCLOAK_ISSUER_URL/protocol/openid-connect/token" \
  -d "grant_type=client_credentials" \
  -d "client_id=$KEYCLOAK_CLIENT_ID" \
  -d "client_secret=$KEYCLOAK_CLIENT_SECRET" \
  -d "scope=mcp:tools" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "$TOKEN"   # copy this — it's a long eyJ... string
```

> **Tip:** Tokens expire after 5 minutes by default. For local dev, increase
> the lifetime in Keycloak: **Realm settings → Tokens → Access token lifespan**
> → set to `1 day`. Then re-fetch and update the config. The client secret
> itself never expires.

### Step 2 — Edit `claude_desktop_config.json`

Location:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

**Windows (WSL + mcp-remote) — with auth:**

Node.js on Windows often has PATH or spaces-in-path issues when invoked
directly from Claude Desktop. The reliable approach is to run `npx mcp-remote`
inside WSL. Adjust the `PATH` export to match `which npx` in your WSL terminal.

```json
{
  "mcpServers": {
    "openaerostruct": {
      "command": "wsl",
      "args": [
        "bash", "-c",
        "export PATH=/home/alex/.nvm/versions/node/v24.12.0/bin:$PATH; exec npx -y mcp-remote http://localhost:8000/mcp --header 'Authorization: Bearer <paste-eyJ...-token-here>'"
      ]
    }
  }
}
```

**Windows (WSL + mcp-remote) — no auth:**

```json
{
  "mcpServers": {
    "openaerostruct": {
      "command": "wsl",
      "args": [
        "bash", "-c",
        "export PATH=/home/alex/.nvm/versions/node/v24.12.0/bin:$PATH; exec npx -y mcp-remote http://localhost:8000/mcp"
      ]
    }
  }
}
```

**macOS / Linux — with auth:**

```json
{
  "mcpServers": {
    "openaerostruct": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8000/mcp",
               "--header", "Authorization: Bearer <paste-eyJ...-token-here>"]
    }
  }
}
```

Replace `<paste-eyJ...-token-here>` with the token printed in step 1.

### Step 3 — Restart Claude Desktop

The server should appear in the tool panel within a few seconds of restart.

---

## 9. Connect Claude.ai (remote MCP)

Claude.ai supports connecting remote MCP servers directly — no bridge needed. The server must be:
- **Publicly reachable** (a hostname, not `localhost`)
- **HTTPS** (Claude.ai requires TLS; Railway, Render, and Fly all provide this automatically)
- **OAuth 2.0 authenticated** (Claude.ai initiates an OAuth2 authorization code flow)

### 9a. Make the server public

Deploy the Docker container on Railway, Render, or Fly. Use the public URL (e.g. `https://oas-mcp.up.railway.app`) as `RESOURCE_SERVER_URL`.

### 9b. Set up an OAuth2 Authorization Code client in Keycloak

The `client_credentials` client you created in step 4 is for machine-to-machine flows. For Claude.ai, you need a client that supports the **Authorization Code + PKCE** flow so the user can log in interactively.

1. In the `oas` realm → **Clients** → **Create client**
2. Client ID: `oas-mcp-interactive`
3. Client authentication: **OFF** (public client — no secret needed for PKCE)
4. Authentication flow: check **Standard flow** (Authorization Code)
5. Valid redirect URIs: add the redirect URI that Claude.ai specifies — it will show this to you during the connection wizard. It is typically of the form:
   ```
   https://claude.ai/oauth/callback
   ```
6. Web origins: `https://claude.ai`
7. Click **Save**
8. Assign the `mcp:tools` scope (same as step 5b) to this client

### 9c. Add the server in Claude.ai

1. Open [claude.ai](https://claude.ai) → click your profile → **Settings**
2. Find **Integrations** or **MCP Servers** → **Add server**
3. Enter your server URL: `https://oas-mcp.up.railway.app/mcp`
4. Claude.ai will redirect you to your Keycloak login page
5. Log in (or create a user in the `oas` realm — sidebar → **Users** → **Create user**)
6. After login, Claude.ai receives a token and the server appears as connected

> **If Claude.ai asks for client ID / client secret during setup:** use `oas-mcp-interactive` as the client ID and leave the secret blank (it's a public/PKCE client).

---

## 10. Connect ChatGPT via Custom Actions

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
| Authorization URL | `https://<your-kc>/realms/oas/protocol/openid-connect/auth` |
| Token URL | `https://<your-kc>/realms/oas/protocol/openid-connect/token` |
| Client ID | `oas-mcp-interactive` |
| Client Secret | *(leave blank for public client)* |
| Scope | `mcp:tools openid` |

### Option B — MCP-to-OpenAPI bridge

Projects like [`mcp-proxy`](https://github.com/sparfenyuk/mcp-proxy) can expose an MCP server as an OpenAPI endpoint automatically. Deploy the bridge alongside the OAS server and point ChatGPT at the bridge URL.

---

## 11. Verify auth end-to-end

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

---

## 12. Troubleshooting

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

Default access token lifetime in Keycloak is 5 minutes. For Claude Desktop with a static token header, increase it:

**Realm settings** → **Tokens** → **Access token lifespan** → set to `1 day` (or longer for local dev).

For production, implement token refresh in the wrapper script instead.

---

### HTTPS required for Claude.ai

Claude.ai will refuse to connect to `http://` endpoints. Options:
- Deploy on Railway / Render / Fly — all provide automatic HTTPS
- Use a Cloudflare Tunnel to expose localhost over HTTPS: `cloudflared tunnel --url http://localhost:8000`
- Use `ngrok`: `ngrok http 8000`
