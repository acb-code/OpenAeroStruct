# Tier 3 Provenance: Full Conversation-Level Capture

> **Status**: Planning artifact — do not implement yet.
> **Created**: 2026-03-10
> **Context**: Tier 1/2 are operational (`oas_mcp/provenance/`). Tier 3 extends
> the graph to capture the complete **Prompt → Reasoning → ToolCall → Result**
> chain and export it to a TypeDB instance modelled on W3C PROV-AGENT.

---

## Overview

```
Claude Code JSONL  ──┐
                     ├──► session_joiner.py ──► enriched graph ──► typedb/export.py ──► TypeDB
SQLite (Tier 1/2)  ──┘                                         └──► CLI (join / export-typedb)
```

Tier 3 joins Claude Code's per-session conversation logs against the existing
SQLite records to attach `thinking` blocks, human prompts, and full assistant
reasoning to each tool call node.  The enriched graph is then exported to
TypeDB via a W3C PROV-AGENT schema.

---

## T3-1 — Claude Code Log Format  ✅ RESEARCHED

### File location

```
~/.claude/projects/<slug>/<session-uuid>.jsonl
```

Where `<slug>` is the filesystem-safe project path, e.g.:
`-home-alex-coding-OpenAeroStruct`.  Each file is one Claude Code session.

### Line types

| `type` field            | Description |
|-------------------------|-------------|
| `file-history-snapshot` | Snapshot of tracked files at session start / periodic |
| `user`                  | Human turn (text input, or tool_result content blocks) |
| `assistant`             | Model turn (text, thinking, tool_use content blocks) |
| `progress`              | Streaming partial messages (intermediate state, skip) |

### Key top-level fields

```json
{
  "type": "user" | "assistant" | ...,
  "uuid": "<message-uuid>",
  "parentUuid": "<parent-uuid or null>",
  "sessionId": "<session-uuid>",
  "timestamp": "2026-03-10T22:07:32.963Z",
  "isSidechain": false,
  "message": { ... }
}
```

### Content block types (inside `message.content[]`)

| Block type    | Key fields | Notes |
|---------------|-----------|-------|
| `text`        | `text` | Human message or assistant prose |
| `thinking`    | `thinking`, `signature` | Extended thinking — may be empty string in redacted output |
| `tool_use`    | `id`, `name`, `input` | Tool call request; `name` = full MCP tool name |
| `tool_result` | `tool_use_id`, `content`, `is_error` | Response to a tool_use block |

### Example tool_use block

```json
{
  "type": "tool_use",
  "id": "toolu_01XYZ",
  "name": "mcp__claude_ai_OpenAeroStruct__run_aero_analysis",
  "input": { "surface_name": "wing", "velocity": 248.0 }
}
```

### Example tool_result block

```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_01XYZ",
  "content": [{ "type": "text", "text": "{\"schema_version\": \"1.0\", ...}" }],
  "is_error": false
}
```

### PROV-AGENT mapping

| JSONL element | PROV-AGENT entity |
|---------------|-------------------|
| Human `text` block | `campaign` intent / `prompt` |
| `thinking` block | `ai-model-invocation` (reasoning) |
| `tool_use` block | `agent-tool` (activity) |
| `tool_result` content | `domain-data` (result entity) |
| Assistant `text` block | `response-data` (interpretation) |

### ⚠️ Join key gap

`@capture_tool` (capture.py:104-108) injects `_provenance.call_id` into the
**returned dict** from each tool.  However, examination of real JSONL files
confirms that `_provenance` does **not** appear in `tool_result` content blocks
— the MCP serialisation layer strips it before writing to the log.

**Primary join strategy**: `tool_name` + timestamp window (±5 s default).
**Aspirational exact match**: `_provenance.call_id` if a future MCP version
propagates it into `tool_result` metadata.

---

## T3-2 — Conversation Parser

**File**: `oas_mcp/provenance/conversation_parser.py`

### ConversationEvent dataclass

```python
@dataclass
class ConversationEvent:
    type: str                    # 'tool_use' | 'tool_result' | 'thinking' | 'text'
    timestamp: str               # ISO 8601 from top-level JSONL field
    uuid: str                    # message uuid
    parent_uuid: str | None
    session_id: str              # Claude Code session UUID
    tool_name: str | None        # e.g. 'run_aero_analysis' (suffix only)
    tool_call_id: str | None     # tool_use id ('toolu_...')
    tool_input: dict | None
    tool_output: str | None      # raw text from tool_result
    is_error: bool
    content: str | None          # thinking text or prose text
```

### Functions

```python
def parse_claude_log(path: Path) -> list[ConversationEvent]:
    """Stream-parse a JSONL file line-by-line (no full file load)."""

def find_oas_tool_events(
    events: list[ConversationEvent],
    prefix: str = "mcp__claude_ai_OpenAeroStruct__",
) -> list[ConversationEvent]:
    """Filter to only OAS MCP tool_use / tool_result events."""

def extract_oas_tool_name(full_name: str, prefix: str = "mcp__claude_ai_OpenAeroStruct__") -> str:
    """Strip MCP prefix: 'mcp__claude_ai_OpenAeroStruct__run_aero_analysis' → 'run_aero_analysis'."""

def auto_discover_logs(
    project_slug: str = "-home-alex-coding-OpenAeroStruct",
) -> list[Path]:
    """Enumerate ~/.claude/projects/<slug>/*.jsonl, sorted by mtime desc."""
```

### Design notes
- Parse line-by-line with `json.loads()` — avoid loading full multi-MB files.
- Skip `progress` type lines entirely.
- Store `thinking` blocks paired with their parent message uuid for later join.
- `prefix` is a module-level constant overridable via `OAS_MCP_PREFIX` env var.

---

## T3-3 — Session Joiner

**File**: `oas_mcp/provenance/session_joiner.py`

### JoinedNode dataclass

```python
@dataclass
class JoinedNode:
    # Original fields from db.get_session_graph() node dict
    id: str
    type: str                    # 'tool_call' | 'decision'
    seq: int
    tool_name: str | None
    started_at: str
    # Tier 3 additions
    thinking_before: str | None  # assistant thinking text immediately before tool_use
    human_prompt: str | None     # most recent human text block before this tool call
    conversation_uuid: str | None  # JSONL message uuid
    join_quality: str            # 'exact' | 'timestamp' | 'sequence' | 'none'
```

### Join strategy (multi-tier)

```
Tier A — exact call_id match (aspirational)
    If tool_result content contains _provenance.call_id == SQLite call_id → quality='exact'

Tier B — tool_name + timestamp window (primary)
    For each SQLite tool_call row:
      candidate = JSONL tool_use events where:
        extract_oas_tool_name(event.tool_name) == row.tool_name
        AND abs(parse(event.timestamp) - parse(row.started_at)) <= window_s (default 5s)
      Pick closest timestamp match.  quality='timestamp'

Tier C — sequence order fallback
    If N SQLite tool_calls with the same tool_name exist in a session
    AND N JSONL tool_use events exist for the same tool_name:
      zip by sequence index.  quality='sequence'

Tier D — no match
    quality='none'; node retained with Tier 3 fields as None.
```

### Functions

```python
def join_session(
    graph: dict,                  # output of db.get_session_graph()
    events: list[ConversationEvent],
    window_s: float = 5.0,
) -> tuple[dict, dict]:
    """
    Returns (enriched_graph, join_stats).
    enriched_graph has same shape as input graph but nodes are JoinedNode dicts.
    join_stats: {'exact': int, 'timestamp': int, 'sequence': int, 'none': int}
    """

def _find_thinking_before(
    events: list[ConversationEvent],
    tool_call_id: str,
) -> str | None:
    """Return thinking block content from same assistant message as tool_use."""

def _find_human_prompt_before(
    events: list[ConversationEvent],
    tool_event: ConversationEvent,
) -> str | None:
    """Walk parentUuid chain backwards to find most recent human text block."""
```

### New edge types added to enriched graph

| Label | Source → Target | Meaning |
|-------|----------------|---------|
| `wasInformedBy` | `tool_call` → `tool_call` | Already exists in Tier 1/2 as `sequence`/`decides` |
| `wasAttributedTo` | `tool_call` → `ai-agent` | Links tool call to the AI that issued it |
| `prompted` | `human-prompt` → `tool_call` | Human intent caused this tool call |

### CLI extension (`cli.py`)

```
python -m oas_mcp.provenance.cli join <session_id> \
    --log <path/to/session.jsonl> \
    --db ./oas_data/provenance.db \
    [-o joined_graph.json] \
    [--window 5.0]
```

---

## T3-4 — TypeDB Schema

**File**: `oas_mcp/provenance/typedb/oas_prov_agent.tql`

### Schema (TypeDB 2.x syntax — user-provided verbatim)

```typeql
define

# ── Core PROV classes ──────────────────────────────────────────
entity prov-entity owns entity-id @key, owns label, owns created-at;
entity prov-activity owns activity-id @key, owns label,
    owns started-at, owns ended-at, owns status;
entity prov-agent owns agent-id @key, owns label;

# ── PROV-AGENT extensions ──────────────────────────────────────

# Workflow structure
entity campaign sub prov-activity,
    owns description, owns goal;

entity workflow sub prov-activity,
    owns workflow-type;                  # 'aerostructural' | 'optimization' | 'polar'

entity task sub prov-activity,
    owns tool-name, owns duration-s,
    owns inputs-json, owns outputs-json,
    owns error-message;                  # Tier 1: every @capture_tool call

entity agent-tool sub task;             # MCP tool call driven by AI decision

# AI layer
entity ai-agent sub prov-agent,
    owns model-name, owns model-provider;

entity ai-model-invocation sub prov-activity,
    owns prompt-text, owns response-text,
    owns confidence;                     # Tier 2 + Tier 3

entity prompt sub prov-entity,
    owns prompt-text, owns decision-type;

entity response-data sub prov-entity,
    owns response-text, owns selected-action;

# Domain data (OAS-specific)
entity domain-data sub prov-entity,
    owns data-type,                      # 'wing_geometry' | 'aero_result' | 'struct_result' | 'opt_result'
    owns data-json;

entity wing-geometry sub domain-data,
    owns span, owns chord, owns sweep, owns mesh-nx, owns mesh-ny;

entity aero-result sub domain-data,
    owns cl, owns cd, owns cm, owns aoa;

entity opt-result sub domain-data,
    owns objective-value, owns n-iterations, owns converged,
    owns active-dvs-json, owns constraint-violations-json;

# ── Relations (W3C PROV + extensions) ─────────────────────────
relation used relates activity, relates entity;
relation was-generated-by relates entity, relates activity;
relation was-attributed-to relates entity, relates agent;
relation was-associated-with relates activity, relates agent;
relation was-informed-by relates informed, relates informant
    sub prov-activity, sub prov-activity;
relation had-member relates collection, relates member
    sub campaign, sub prov-activity;

# OAS-specific
relation was-derived-from relates derived, relates source
    sub domain-data, sub domain-data;  # e.g. opt-result derived from wing-geometry

# ── Attributes ────────────────────────────────────────────────
attribute entity-id    value string;
attribute activity-id  value string;
attribute agent-id     value string;
attribute label        value string;
attribute created-at   value datetime;
attribute started-at   value datetime;
attribute ended-at     value datetime;
attribute status       value string;    # 'ok' | 'error'
attribute description  value string;
attribute goal         value string;
attribute workflow-type value string;
attribute tool-name    value string;
attribute duration-s   value double;
attribute inputs-json  value string;
attribute outputs-json value string;
attribute error-message value string;
attribute model-name   value string;
attribute model-provider value string;
attribute prompt-text  value string;
attribute response-text value string;
attribute confidence   value string;
attribute decision-type value string;
attribute selected-action value string;
attribute data-type    value string;
attribute data-json    value string;
attribute span         value double;
attribute chord        value double;
attribute sweep        value double;
attribute mesh-nx      value long;
attribute mesh-ny      value long;
attribute cl           value double;
attribute cd           value double;
attribute cm           value double;
attribute aoa          value double;
attribute objective-value value double;
attribute n-iterations  value long;
attribute converged    value boolean;
attribute active-dvs-json value string;
attribute constraint-violations-json value string;
```

### Migration notes for TypeDB 3.x

- `@key` → `@key` attribute annotation syntax changed; verify with 3.x docs.
- `relation X sub Y` role inheritance syntax may differ; check role-playing
  entity declarations.
- `value string` → `value string` unchanged; `value datetime` may need ISO format.
- Test schema loading with `typedb server --version` to confirm driver compat.

### SQLite → TypeDB mapping

| SQLite table / column | TypeDB entity / attribute |
|-----------------------|--------------------------|
| `sessions.session_id` | `campaign.activity-id` |
| `sessions.notes` | `campaign.description` |
| `sessions.started_at` | `campaign.started-at` |
| `tool_calls.call_id` | `agent-tool.activity-id` |
| `tool_calls.tool_name` | `agent-tool.tool-name` |
| `tool_calls.inputs_json` | `agent-tool.inputs-json` |
| `tool_calls.outputs_json` | `agent-tool.outputs-json` |
| `tool_calls.status` | `agent-tool.status` |
| `tool_calls.started_at` | `agent-tool.started-at` |
| `tool_calls.duration_s` | `agent-tool.duration-s` |
| `decisions.decision_id` | `ai-model-invocation.activity-id` |
| `decisions.reasoning` | `ai-model-invocation.prompt-text` |
| `decisions.selected_action` | `ai-model-invocation.response-text` |
| `decisions.confidence` | `ai-model-invocation.confidence` |
| JSONL `thinking` block | `ai-model-invocation.prompt-text` (Tier 3) |
| JSONL tool output JSON | `domain-data` subtypes extracted by `data-type` |

---

## T3-5 — SQLite → TypeDB Exporter

**Files**:
- `oas_mcp/provenance/typedb/export.py`
- `oas_mcp/provenance/typedb/client.py`

### client.py

```python
def get_typedb_client(address: str = "localhost:1729") -> TypeDB:
    """Return a TypeDB 2.x driver client; raise ImportError if not installed."""

def ensure_database(client, db_name: str = "oas_provenance") -> None:
    """Create database if it does not exist."""

def load_schema(client, db_name: str, tql_path: Path) -> None:
    """Load oas_prov_agent.tql schema (idempotent via define)."""
```

### export.py

```python
def export_session_to_typedb(
    session_id: str,
    db_path: Path,
    typedb_address: str = "localhost:1729",
    typedb_db: str = "oas_provenance",
    joined_graph: dict | None = None,   # pass if T3-3 join already done
) -> dict:
    """
    Export one session to TypeDB.  Idempotent — uses session_id as key.
    Returns {'inserted': int, 'updated': int, 'skipped': int}.
    """

def _extract_domain_data(outputs_json: str, tool_name: str) -> dict | None:
    """
    Parse tool outputs and return typed domain-data dict:
      create_surface       → wing-geometry
      run_aero_analysis    → aero-result
      run_aerostruct_analysis → aero-result + struct_result
      run_optimization     → opt-result
    Returns None for tools with no structured domain output.
    """
```

### Dependency

```toml
# pyproject.toml optional extra
[project.optional-dependencies]
typedb = ["typedb-driver==2.28.*"]
```

### Prerequisite check

```bash
# Verify TypeDB server is available
docker run --rm typedb/typedb:2.25.0 typedb server --version
```

### CLI extension (`cli.py`)

```
python -m oas_mcp.provenance.cli export-typedb <session_id> \
    --db ./oas_data/provenance.db \
    [--typedb-address localhost:1729] \
    [--typedb-db oas_provenance] \
    [--log <path/to/session.jsonl>]
```

---

## T3-6 — Dashboard

**Directory**: `oas_mcp/provenance/dashboard/`

### Technology stack

- **Framework**: Next.js 14 (App Router)
- **TypeDB client**: `typedb-driver` npm package
- **Graph rendering**: Cytoscape.js (reuse patterns from `viewer/index.html`)
- **Styling**: Tailwind CSS

### Pages

| Route | Description |
|-------|-------------|
| `/` | Session Explorer — table of all campaigns with metadata |
| `/session/[id]` | Session Detail — Cytoscape DAG with Tier 1/2/3 nodes |
| `/patterns` | Workflow Patterns — FSM aggregates across sessions |
| `/optimization/[id]` | Optimization History — objective / DV evolution charts |

### Docker Compose

```yaml
# oas_mcp/provenance/dashboard/docker-compose.yml
services:
  typedb:
    image: typedb/typedb:2.25.0
    ports: ["1729:1729"]
    volumes: ["typedb-data:/opt/typedb-all/server/data"]

  dashboard:
    build: .
    ports: ["3000:3000"]
    environment:
      TYPEDB_ADDRESS: typedb:1729
      TYPEDB_DB: oas_provenance
    depends_on: [typedb]

volumes:
  typedb-data:
```

### Tier 1/2 compatibility

The existing HTML viewer (`viewer/index.html`) and `cli.py serve` command remain
fully functional for sessions that have not been exported to TypeDB.

---

## T3-7 — CLAUDE.md Update

Add the following section to `CLAUDE.md` under the existing provenance section:

```markdown
## Tier 3: Full Provenance Export

Tier 3 joins Claude Code conversation logs with SQLite Tier 1/2 records and
exports the enriched graph to TypeDB.

### When to use

Run T3 after completing a multi-step OAS workflow to capture the full
Prompt → Reasoning → ToolCall → Result chain for audit / reproducibility.

### Quick start

\`\`\`bash
# 1. Parse + join a Claude Code session log with SQLite
python -m oas_mcp.provenance.cli join <session_id> \
    --log ~/.claude/projects/-home-alex-coding-OpenAeroStruct/<uuid>.jsonl

# 2. Export to TypeDB (requires running TypeDB server)
python -m oas_mcp.provenance.cli export-typedb <session_id>

# 3. Open dashboard
open http://localhost:3000
\`\`\`

### Docker dependency

TypeDB server must be running before export:
\`\`\`bash
cd oas_mcp/provenance/dashboard
docker compose up -d typedb
\`\`\`

### Viewer fallback

For sessions without TypeDB export, use the Tier 1/2 HTML viewer:
\`\`\`bash
python -m oas_mcp.provenance.cli serve --port 7654
# open http://localhost:7654/viewer
\`\`\`
```

---

## Implementation Order

### Phase 1 — No external dependencies

```
T3-2  conversation_parser.py
      └── tests: synthetic JSONL fixtures, test extract/filter/discover functions
T3-3  session_joiner.py
      └── tests: mock graph + events, verify join quality tiers, edge generation
CLI   cli.py: add `join` subcommand
```

### Phase 2 — Requires TypeDB server

```
T3-4  typedb/oas_prov_agent.tql  (schema file, no runtime deps)
T3-5  typedb/client.py + export.py
      └── tests: mock typedb client, @pytest.mark.typedb for integration
CLI   cli.py: add `export-typedb` subcommand
Docker: verify docker run --rm typedb/typedb:2.25.0 typedb server --version
```

### Phase 3 — Requires Node.js

```
T3-6  dashboard/ scaffolding (npx create-next-app)
      └── pages: SessionExplorer, SessionDetail, Patterns, OptHistory
      └── docker-compose.yml
```

### Phase 4 — Documentation

```
T3-7  CLAUDE.md update
      TODO_tier3.md: mark completed steps as ✅
```

---

## Testing Strategy

### Unit tests (no external deps)

Follow patterns in `oas_mcp/tests/test_provenance.py`:
- `tmp_path` isolation for SQLite files
- `isolate_provenance` conftest fixture for DB path isolation

```python
# Synthetic JSONL fixture
SAMPLE_JSONL = [
    {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": "s1",
     "timestamp": "2026-03-10T10:00:00.000Z",
     "message": {"role": "user", "content": [{"type": "text", "text": "Analyze wing"}]}},
    {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": "s1",
     "timestamp": "2026-03-10T10:00:01.000Z",
     "message": {"role": "assistant", "content": [
         {"type": "thinking", "thinking": "I should call run_aero_analysis"},
         {"type": "tool_use", "id": "toolu_01", "name": "mcp__claude_ai_OpenAeroStruct__run_aero_analysis",
          "input": {"surface_name": "wing"}}
     ]}},
    ...
]
```

### Integration tests

```python
@pytest.mark.skipif(
    not Path("~/.claude/projects/-home-alex-coding-OpenAeroStruct/").expanduser().exists(),
    reason="No real Claude Code logs available"
)
def test_join_real_session(tmp_path): ...

@pytest.mark.typedb
def test_export_to_typedb(tmp_path): ...
```

Mark TypeDB tests with `@pytest.mark.typedb` and skip by default:
```ini
# pytest.ini / pyproject.toml
markers = typedb: requires running TypeDB server
addopts = -m "not typedb"
```

---

## Open Questions

1. **`_provenance` propagation**: Can `@capture_tool` inject `call_id` into
   MCP tool metadata (not the result body) so it appears in JSONL `tool_result`?
   If yes, Tier A exact matching becomes trivial.

2. **Thinking block availability**: Extended thinking is redacted in some Claude
   API tiers (signature field present but `thinking` is empty string). The parser
   should handle this gracefully.

3. **TypeDB 3.x migration**: The schema uses TypeDB 2.x syntax. TypeDB 3.0 has
   breaking changes. Confirm target version before Phase 2 work begins.

4. **Multi-session join**: A single OAS provenance session may span multiple
   Claude Code JSONL files (user re-opens the project). `auto_discover_logs()`
   should support multi-file join with timestamp-based ordering.
