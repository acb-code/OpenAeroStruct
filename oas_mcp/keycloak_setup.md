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
OIDC_CLIENT_SECRET=          # fill after step 4 below
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

Wait for Keycloak to become healthy (~30s on first start):

```bash
docker compose -f docker-compose.prod.yml logs keycloak -f
```

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

1. **Client scopes → Create client scope**
2. Name: `mcp:tools`
3. Type: **Default**
4. Protocol: **OpenID Connect**
5. Click **Save**

Then assign it:
1. Go back to **Clients → oas-mcp → Client scopes**
2. Click **Add client scope** → select `mcp:tools` → Add as **Default**

### 4e. Enable Dynamic Client Registration (DCR)

1. **Realm settings → Client registration → Client registration policies**
2. Under **Anonymous access policies**, verify it exists (Keycloak enables anonymous DCR by default for the realm)

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

1. **Client scopes → mcp:tools → Mappers → Configure a new mapper**
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
# OIDC discovery
curl -s https://auth.lakesideai.dev/realms/oas/.well-known/openid-configuration | jq '{issuer, registration_endpoint, authorization_endpoint, token_endpoint}'

# Protected resource metadata
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

### claude.ai

Add as a remote MCP server in claude.ai settings:
- URL: `https://mcp.lakesideai.dev/mcp`
- claude.ai will discover the auth server via `/.well-known/oauth-protected-resource`,
  register via DCR, and present the Keycloak login page.
