# Keycloak Setup for OAS MCP Server

Keycloak provides OAuth2/OIDC authentication with native Dynamic Client
Registration (DCR), which Claude Code and claude.ai require.

## 1. Environment variables

Add these to `.env` on the VPS:

```bash
# Keycloak admin (only used on first start)
KEYCLOAK_ADMIN=admin
KEYCLOAK_ADMIN_PASSWORD=<strong-random-password>

# Keycloak Postgres
KC_DB_PASSWORD=<strong-random-password>

# Public hostname (must match your Caddy/DNS config)
KC_HOSTNAME=auth.lakesideai.dev

# OAS MCP auth
OIDC_ISSUER_URL=https://auth.lakesideai.dev/realms/oas
OIDC_CLIENT_ID=oas-mcp
OIDC_CLIENT_SECRET=          # fill after step 4b below
RESOURCE_SERVER_URL=https://mcp.lakesideai.dev
```

## 2. Caddy config

Add a Keycloak entry to your Caddyfile (alongside the existing MCP entry):

```
auth.lakesideai.dev {
    reverse_proxy localhost:8080
}
```

Then `sudo systemctl reload caddy`.

## 3. Start the stack

```bash
docker compose -f docker-compose.prod.yml up -d
```

Wait for Keycloak to become healthy (~30–45s on first start):

```bash
docker compose -f docker-compose.prod.yml logs keycloak -f
```

Look for: `Keycloak 26.x.x on JVM ... started in XXs. Listening on: http://0.0.0.0:8080`

## 4. Configure Keycloak

Open **https://auth.lakesideai.dev** and log in with your admin credentials.

### 4a. Create realm

1. Click the realm dropdown (top-left, says "Keycloak") → **Create realm**
2. Name: `oas`
3. Click **Create**

### 4b. Create client

1. **Clients → Create client**
2. Client ID: `oas-mcp`
3. Client authentication: **ON** (confidential)
4. Authentication flow: check **Standard flow** + **Service accounts roles**
5. Click **Next**, then **Save**
6. Go to **Credentials** tab → copy the **Client secret** → paste into `.env` as `OIDC_CLIENT_SECRET`

### 4c. Configure redirect URIs

In the client's **Settings** tab, add these **Valid redirect URIs**:

```
http://localhost:*
http://127.0.0.1:*
https://claude.ai/api/mcp/auth_callback
https://claude.com/api/mcp/auth_callback
```

Set **Valid post logout redirect URIs** to `+` (same as redirect URIs).
Set **Web origins** to `+`.

### 4d. Create scope

1. **Client scopes** (left sidebar) → **Create client scope**
2. Name: `mcp:tools`
3. Type: **Default**
4. Protocol: **OpenID Connect**
5. **Include in token scope**: **ON** (critical — without this the scope won't
   appear in the access token's `scope` claim, and the MCP server will reject
   the token)
6. Click **Save**

Then assign it to the `oas-mcp` client:
1. Go to **Clients → oas-mcp → Client scopes** tab
2. Click **Add client scope** → select `mcp:tools` → Add as **Default**

### 4e. Remove Trusted Hosts DCR policy

Keycloak's default "Trusted Hosts" policy blocks DCR requests from
Claude Code and claude.ai because their requests don't originate from
a whitelisted host.

1. **Clients** (left sidebar) → **Client registration** tab (top of the page)
2. Under **Anonymous access policies**, find **Trusted Hosts**
3. **Delete it** (click the trash icon)

> Keycloak enables anonymous DCR by default for new realms. Removing this
> policy allows any client to register. This is safe because the MCP server
> validates every token independently — DCR registration alone grants no
> access.

Verify DCR works:

```bash
curl -s https://auth.lakesideai.dev/realms/oas/.well-known/openid-configuration | jq .registration_endpoint
```

Should return: `https://auth.lakesideai.dev/realms/oas/clients-registrations/openid-connect`

### 4f. Create user accounts

1. **Users → Add user**
2. Fill in username, email, first/last name
3. Click **Create**
4. Go to **Credentials** tab → **Set password** → uncheck "Temporary"

### 4g. Audience mapper (important!)

By default, Keycloak doesn't include the `oas-mcp` client ID in the token's
`aud` claim. Without this, JWT audience validation fails.

1. **Client scopes** (left sidebar) → **mcp:tools** → **Mappers** tab → **Configure a new mapper**
2. Choose **Audience**
3. Name: `oas-mcp-audience`
4. Included Client Audience: `oas-mcp`
5. Add to access token: **ON**
6. Click **Save**

## 5. Restart OAS MCP

```bash
docker compose -f docker-compose.prod.yml restart oas-mcp
```

Check the logs — you should see:

```
OAS MCP — HTTP transport  |  auth: OIDC (https://auth.lakesideai.dev/realms/oas)
```

## 6. Verify

```bash
# OIDC discovery (check issuer, DCR endpoint, token endpoint)
curl -s https://auth.lakesideai.dev/realms/oas/.well-known/openid-configuration \
  | jq '{issuer, registration_endpoint, authorization_endpoint, token_endpoint}'

# Protected resource metadata (served by the MCP server)
curl -s https://mcp.lakesideai.dev/.well-known/oauth-protected-resource | jq .

# Unauthenticated request → 401
curl -s -o /dev/null -w '%{http_code}' -X POST https://mcp.lakesideai.dev/mcp
```

## 7. Connect clients

### Claude Code

```bash
claude mcp add --transport http oas-mcp https://mcp.lakesideai.dev/mcp
```

Start a session — Claude Code will open your browser for Keycloak login.
After authenticating, the MCP server's tools will be available.

### claude.ai

Add as a remote MCP server in claude.ai settings:
- URL: `https://mcp.lakesideai.dev/mcp`
- claude.ai will discover the auth server via `/.well-known/oauth-protected-resource`,
  register via DCR, and present the Keycloak login page.

### OpenAI Codex (CLI)

```bash
codex mcp add oas-mcp --url https://mcp.lakesideai.dev/mcp
codex mcp login oas-mcp --scopes mcp:tools
```

Your browser will open for Keycloak login. After authenticating, start a
Codex session and the OAS tools will be available.

Alternatively, add to `~/.codex/config.toml`:

```toml
[mcp_servers.oas-mcp]
url = "https://mcp.lakesideai.dev/mcp"
scopes = ["mcp:tools"]
```

Then run `codex mcp login oas-mcp` to authenticate.

## 8. Adding new users

### Admin steps

1. Open **https://auth.lakesideai.dev** → log in as admin → switch to the **oas** realm
2. **Users → Add user** → fill in username, email, first/last name → **Create**
3. **Credentials** tab → **Set password** → uncheck "Temporary" → **Save**
4. Send the new user their username, temporary password, and the instructions below

### Instructions to send new users

> **OpenAeroStruct MCP Server — Getting Started**
>
> You've been given access to the OAS MCP server. Here's how to connect:
>
> **Your credentials**
> - Login URL: https://auth.lakesideai.dev
> - Username: `<their-username>`
> - Password: `<their-password>`
>
> **Option 1: Claude Code (CLI)**
>
> 1. Install Claude Code if you haven't: `npm install -g @anthropic-ai/claude-code`
> 2. Add the MCP server:
>    ```bash
>    claude mcp add --transport http oas-mcp https://mcp.lakesideai.dev/mcp
>    ```
> 3. Start a Claude Code session. The first time you use an OAS tool,
>    your browser will open for login. Sign in with your credentials above.
> 4. After login you can ask Claude to run aerostructural analyses,
>    optimizations, and more — all tools are available automatically.
>
> **Option 2: claude.ai (web)**
>
> 1. Go to https://claude.ai → **Settings → Integrations → Add MCP Server**
> 2. Enter URL: `https://mcp.lakesideai.dev/mcp`
> 3. You'll be redirected to sign in with your credentials above.
> 4. The OAS tools will appear in your Claude conversation.
>
> **Option 3: OpenAI Codex (CLI)**
>
> 1. Install Codex if you haven't: `npm install -g @openai/codex`
> 2. Add the MCP server:
>    ```bash
>    codex mcp add oas-mcp --url https://mcp.lakesideai.dev/mcp
>    ```
> 3. Log in:
>    ```bash
>    codex mcp login oas-mcp --scopes mcp:tools
>    ```
> 4. Your browser will open for login. Sign in with your credentials above.
> 5. Start a Codex session — the OAS tools will be available automatically.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Token rejected: missing required scope 'mcp:tools'` | Scope not in token | Check **Include in token scope** is ON in the `mcp:tools` client scope settings (step 4d) |
| `Policy 'Trusted Hosts' rejected request` | DCR blocked | Delete the Trusted Hosts policy (step 4e) |
| JWT audience validation fails | Missing audience mapper | Add the audience mapper to the `mcp:tools` scope (step 4g) |
| `password authentication failed` on Keycloak start | Stale Postgres volume | `docker volume rm <project>_postgres_data` and restart |
| Keycloak health check fails | Port 9000 not exposed | Compose uses port 8080 check; verify `--health-enabled=true` in command |
