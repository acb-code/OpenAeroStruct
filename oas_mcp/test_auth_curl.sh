#!/usr/bin/env bash
# test_auth_curl.sh — end-to-end curl tests for OAS MCP HTTP transport + Keycloak auth
#
# Sources .env automatically, then runs the verification tests from keycloak_auth_setup.md.
#
# Usage:
#   ./oas_mcp/test_auth_curl.sh               # uses defaults from .env
#   OAS_PORT=9000 ./oas_mcp/test_auth_curl.sh # override port inline
#
# Exit codes: 0 = all tests passed, 1 = one or more tests failed.

set -euo pipefail

# ---------------------------------------------------------------------------
# Load .env from the repo root (two levels up from this script)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$REPO_ROOT/.env"

if [[ -f "$ENV_FILE" ]]; then
    # Strip Windows CR characters before sourcing — .env is gitignored so
    # .gitattributes can't enforce LF, and editors on Windows often write CRLF.
    # shellcheck disable=SC1090
    set -a; source <(sed 's/\r//' "$ENV_FILE"); set +a
    echo "Loaded $ENV_FILE"
else
    echo "Warning: $ENV_FILE not found — using environment variables as-is"
fi

# ---------------------------------------------------------------------------
# Config (env vars override .env values; .env overrides these defaults)
# ---------------------------------------------------------------------------
OAS_HOST="${OAS_HOST:-127.0.0.1}"
OAS_PORT="${OAS_PORT:-8000}"
# OAS_HOST may be a bind-all address (0.0.0.0 / ::) — map to 127.0.0.1 for curl
_CONNECT_HOST="$OAS_HOST"
[[ "$_CONNECT_HOST" == "0.0.0.0" || "$_CONNECT_HOST" == "::" ]] && _CONNECT_HOST="127.0.0.1"
SERVER_URL="http://${_CONNECT_HOST}:${OAS_PORT}/mcp"
SERVER_URL_BASE="http://${_CONNECT_HOST}:${OAS_PORT}"

KC="${KEYCLOAK_ISSUER_URL:-}"
CLIENT_ID="${KEYCLOAK_CLIENT_ID:-oas-mcp}"
CLIENT_SECRET="${KEYCLOAK_CLIENT_SECRET:-}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

assert_http() {
    local label="$1"
    local expected="$2"
    local actual="$3"
    if [[ "$actual" == "$expected" ]]; then
        green "  PASS  $label — got $actual"
        ((PASS++)) || true
    else
        red "  FAIL  $label — expected $expected, got $actual"
        ((FAIL++)) || true
    fi
}

assert_contains() {
    local label="$1"
    local needle="$2"
    local haystack="$3"
    if echo "$haystack" | grep -q "$needle"; then
        green "  PASS  $label — response contains '$needle'"
        ((PASS++)) || true
    else
        red "  FAIL  $label — response does not contain '$needle'"
        red "         Response: ${haystack:0:200}"
        ((FAIL++)) || true
    fi
}

MCP_INIT_BODY='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
MCP_HEADERS=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")

# ---------------------------------------------------------------------------
# Check server is reachable
# ---------------------------------------------------------------------------
bold ""
bold "=== OAS MCP curl test suite ==="
bold "Server: $SERVER_URL"
[[ -n "$KC" ]] && bold "Keycloak: $KC" || bold "Auth: disabled (no KEYCLOAK_ISSUER_URL)"
bold ""

echo "Checking server is up..."
if ! curl -sf -o /dev/null --max-time 5 -X POST "${MCP_HEADERS[@]}" -d "$MCP_INIT_BODY" "$SERVER_URL" 2>/dev/null; then
    # A 401 is fine here — it means the server is up but rejecting unauthenticated requests.
    # Only abort if we can't connect at all.
    HTTP_CHECK=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
        -X POST "${MCP_HEADERS[@]}" -d "$MCP_INIT_BODY" "$SERVER_URL" 2>/dev/null || echo "000")
    if [[ "$HTTP_CHECK" == "000" ]]; then
        red "Cannot reach $SERVER_URL — is the server running?"
        exit 1
    fi
fi
green "Server is reachable."
echo ""

# ---------------------------------------------------------------------------
# Test 0 — OAuth metadata endpoints (only when auth is enabled)
# ---------------------------------------------------------------------------
if [[ -n "$KC" ]]; then
    bold "--- Test 0: OAuth metadata endpoints ---"

    # a) Verify 401 response includes resource_metadata in WWW-Authenticate header
    HEADERS=$(curl -sD - -o /dev/null --max-time 10 \
        -X POST "${MCP_HEADERS[@]}" -d "$MCP_INIT_BODY" "$SERVER_URL")
    assert_contains "401 includes resource_metadata" "resource_metadata=" "$HEADERS"

    # b) Fetch protected resource metadata document
    PRM=$(curl -s --max-time 10 "$SERVER_URL_BASE/.well-known/oauth-protected-resource")
    assert_contains "PRM has authorization_servers" "$KC" "$PRM"
    assert_contains "PRM has scopes_supported: mcp:tools" "mcp:tools" "$PRM"

    echo ""
fi

# ---------------------------------------------------------------------------
# Test 1 — unauthenticated request
# ---------------------------------------------------------------------------
bold "--- Test 1: unauthenticated request ---"
if [[ -n "$KC" ]]; then
    # Auth enabled — expect 401
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
        -X POST "${MCP_HEADERS[@]}" -d "$MCP_INIT_BODY" "$SERVER_URL")
    assert_http "unauthenticated → 401" "401" "$STATUS"
else
    # No auth — expect a 200-level response with serverInfo
    BODY=$(curl -s --max-time 10 -X POST "${MCP_HEADERS[@]}" -d "$MCP_INIT_BODY" "$SERVER_URL")
    assert_contains "no-auth server responds with serverInfo" "OpenAeroStruct" "$BODY"
fi

# ---------------------------------------------------------------------------
# Test 2 — invalid token → 401  (only when auth is enabled)
# ---------------------------------------------------------------------------
if [[ -n "$KC" ]]; then
    bold ""
    bold "--- Test 2: invalid token ---"
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
        -X POST "${MCP_HEADERS[@]}" \
        -H "Authorization: Bearer not.a.real.token" \
        -d "$MCP_INIT_BODY" "$SERVER_URL")
    assert_http "invalid token → 401" "401" "$STATUS"
fi

# ---------------------------------------------------------------------------
# Test 3 — fetch a real token and make an authenticated request
# ---------------------------------------------------------------------------
if [[ -n "$KC" && -n "$CLIENT_SECRET" ]]; then
    bold ""
    bold "--- Test 3: client_credentials token → authenticated request ---"

    echo "  Fetching token from $KC/protocol/openid-connect/token ..."
    TOKEN_RESPONSE=$(curl -s --max-time 15 \
        -X POST "$KC/protocol/openid-connect/token" \
        -d "grant_type=client_credentials" \
        -d "client_id=$CLIENT_ID" \
        -d "client_secret=$CLIENT_SECRET" \
        -d "scope=mcp:tools")

    TOKEN=$(echo "$TOKEN_RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
except json.JSONDecodeError as e:
    print(f'ERROR: response was not JSON: {e}', file=sys.stderr)
    sys.exit(1)
if 'access_token' in data:
    print(data['access_token'])
else:
    # Print every field so it's clear what Keycloak rejected
    error        = data.get('error', 'unknown')
    description  = data.get('error_description', '(no error_description)')
    print(f'ERROR: {error} — {description}', file=sys.stderr)
    print(f'Full response: {json.dumps(data, indent=2)}', file=sys.stderr)
    sys.exit(1)
" 2>&1) || {
        red "  FAIL  Could not fetch token: $TOKEN"
        red "  Keycloak URL: $KC/protocol/openid-connect/token"
        red "  client_id:    $CLIENT_ID"
        red "  Common causes:"
        red "    • 'Service accounts roles' not enabled on the client (enables client_credentials grant)"
        red "    • Wrong client_secret in .env"
        red "    • Client does not exist in this realm"
        red "    • Realm name is wrong in KEYCLOAK_ISSUER_URL"
        ((FAIL++)) || true
        TOKEN=""
    }

    if [[ -n "$TOKEN" && "$TOKEN" != ERROR* ]]; then
        echo "  Token: ${TOKEN:0:60}..."
        # Decode and show the relevant JWT claims for quick diagnosis
        python3 -c "
import base64, json, sys
payload = sys.argv[1].split('.')[1]
payload += '=' * (4 - len(payload) % 4)
c = json.loads(base64.b64decode(payload))
for k in ['iss','aud','azp','scope','exp']:
    if k in c: print(f'  {k}: {c[k]}')
" "$TOKEN"

        BODY=$(curl -s --max-time 15 \
            -X POST "${MCP_HEADERS[@]}" \
            -H "Authorization: Bearer $TOKEN" \
            -d "$MCP_INIT_BODY" "$SERVER_URL")

        if ! echo "$BODY" | grep -q "OpenAeroStruct"; then
            red "  FAIL  valid token → serverInfo in response — response does not contain 'OpenAeroStruct'"
            red "         Response: $(echo "$BODY" | head -c 200)"
            red "  Diagnosis:"
            python3 -c "
import base64, json, sys
payload = sys.argv[1].split('.')[1]
payload += '=' * (4 - len(payload) % 4)
c = json.loads(base64.b64decode(payload))
aud       = c.get('aud', '(missing)')
iss       = c.get('iss', '(missing)')
client_id = sys.argv[2]
kc_url    = sys.argv[3].rstrip('/')
RED, RST  = '\033[31m', '\033[0m'

if aud != client_id and (not isinstance(aud, list) or client_id not in aud):
    print(f'{RED}  aud claim is {aud!r} but server expects {client_id!r}{RST}')
    print(f'{RED}  Fix: Keycloak → Clients → {client_id} → Client scopes{RST}')
    print(f'{RED}       → {client_id}-dedicated → Add mapper → Audience{RST}')
    print(f'{RED}       → Included Client Audience = {client_id}{RST}')

if iss != kc_url:
    print(f'{RED}  iss claim is {iss!r}{RST}')
    print(f'{RED}  but KEYCLOAK_ISSUER_URL is {kc_url!r}{RST}')
    print(f'{RED}  The server was likely started before .env was updated — restart it.{RST}')
" "$TOKEN" "$CLIENT_ID" "$KC"
            ((FAIL++)) || true
        else
            green "  PASS  valid token → serverInfo in response"
            ((PASS++)) || true
        fi

        # Also verify a clearly expired/tampered token gets 401
        TAMPERED="${TOKEN}tampered"
        STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
            -X POST "${MCP_HEADERS[@]}" \
            -H "Authorization: Bearer $TAMPERED" \
            -d "$MCP_INIT_BODY" "$SERVER_URL")
        assert_http "tampered token → 401" "401" "$STATUS"
    fi
elif [[ -n "$KC" && -z "$CLIENT_SECRET" ]]; then
    echo "  Skipping token test — KEYCLOAK_CLIENT_SECRET is not set in .env"
fi

# ---------------------------------------------------------------------------
# Test 4 — no-auth mode: server responds without any header
# ---------------------------------------------------------------------------
if [[ -z "$KC" ]]; then
    bold ""
    bold "--- Test 4: no-auth mode — confirm ⚠ warning was printed (manual check) ---"
    echo "  (Check server startup output for the NO AUTHENTICATION ENABLED box)"
    echo "  Running a second call to confirm server stays up..."
    BODY=$(curl -s --max-time 10 -X POST "${MCP_HEADERS[@]}" -d "$MCP_INIT_BODY" "$SERVER_URL")
    assert_contains "second call succeeds" "OpenAeroStruct" "$BODY"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
bold ""
bold "=== Results ==="
TOTAL=$((PASS + FAIL))
if [[ $FAIL -eq 0 ]]; then
    green "All $TOTAL tests passed."
    exit 0
else
    red "$FAIL/$TOTAL tests failed."
    exit 1
fi
