# OpenAeroStruct MCP Server

An [MCP](https://modelcontextprotocol.io) server that wraps [OpenAeroStruct](https://mdolab-openaerostruct.readthedocs-hosted.com) so AI agents can perform aerodynamic and aerostructural wing analysis through simple tool calls — no OpenMDAO boilerplate required.

## Contents

- [Overview](#overview)
- [Installation](#installation)
- [Running the server](#running-the-server)
  - [stdio (Claude Desktop)](#stdio-transport-default--for-claude-desktop-and-most-mcp-clients)
  - [HTTP transport](#http-transport)
  - [Docker](#docker)
- [Running the tests](#running-the-tests)
- [Artifact storage](#artifact-storage)
- [How agents learn to use the server](#how-agents-learn-to-use-the-server)
- [Architecture](#architecture)
- [Tools reference](#tools-reference)
- [Example walkthrough](#example-walkthrough)
- [Tips](#tips)

---

## Overview

Setting up an OpenAeroStruct analysis normally requires 50–100 lines of OpenMDAO boilerplate: `IndepVarComp`, `Geometry` groups, `AeroPoint` or `AerostructPoint`, and a dozen `connect()` calls. This server hides all of that behind tool calls.

**Analysis tools:**

| Tool | Purpose |
|------|---------|
| `create_surface` | Define a lifting surface geometry |
| `run_aero_analysis` | Single-point VLM analysis (CL, CD, CM) |
| `run_aerostruct_analysis` | Coupled aero + structural analysis (fuel burn, failure) |
| `compute_drag_polar` | Sweep α and return CL-CD-CM arrays |
| `compute_stability_derivatives` | CL_α, CM_α, static margin |
| `run_optimization` | Optimise twist / thickness / α |
| `reset` | Clear surfaces and cached problems |

**Artifact management tools** (every analysis auto-saves; use these to retrieve past results):

| Tool | Purpose |
|------|---------|
| `list_artifacts` | Browse saved runs with optional filters |
| `get_artifact` | Retrieve full metadata + results by `run_id` |
| `get_artifact_summary` | Metadata only — no results payload |
| `delete_artifact` | Remove a saved artifact permanently |

**Key properties:**

- **Persistent artifacts** — every analysis run is saved to disk and returns a `run_id`. Results survive server restarts and can be retrieved at any time.
- **Stateful sessions** — surfaces are stored by name; call `create_surface` once, then run analyses repeatedly without redefining geometry.
- **Problem caching** — `om.Problem.setup()` is expensive. The server runs it once per unique geometry and reuses the cached problem for parameter sweeps, making repeated calls with different α/Mach values fast.
- **Async** — all OpenMDAO computation runs in `asyncio.to_thread()` so the event loop stays responsive.
- **Multiple transports** — stdio (default, for Claude Desktop) or streamable HTTP (for remote/cloud deployment).

---

## Installation

### Prerequisites

- Python ≥ 3.11
- A C/Fortran toolchain for numpy/scipy (`gcc`, `gfortran`, `libopenblas-dev` on Linux; Xcode CLI tools on macOS)

### Step 1 — Clone and enter the repository

```bash
git clone https://github.com/mdolab/OpenAeroStruct.git
cd OpenAeroStruct
```

### Step 2 — Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows PowerShell
```

Or with `uv`:

```bash
uv venv
source .venv/bin/activate
```

### Step 3 — Install the package

**For Claude Desktop / stdio use (minimum):**

```bash
pip install -e ".[mcp]"
# or: uv pip install -e ".[mcp]"
```

**For HTTP transport + Keycloak auth:**

```bash
pip install -e ".[http]"
# or: uv pip install -e ".[http]"
```

The `[http]` extra adds `uvicorn`, `PyJWT[crypto]`, and `httpx` on top of `mcp[cli]`.

**Everything (including test and docs dependencies):**

```bash
pip install -e ".[all]"
```

### Step 4 — Verify the installation

```bash
oas-mcp --help
```

```
usage: oas-mcp [-h] [--transport {stdio,http}] [--host HOST] [--port PORT]
```

---

## Running the server

### stdio transport (default — for Claude Desktop and most MCP clients)

```bash
oas-mcp
# or equivalently:
python -m oas_mcp.server
```

The server speaks the MCP stdio protocol on stdin/stdout. Claude Desktop and most MCP clients use this mode.

#### Add to Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the equivalent on your platform:

```json
{
  "mcpServers": {
    "openaerostruct": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "oas_mcp.server"]
    }
  }
}
```

Replace `/path/to/.venv` with the absolute path to the virtual environment where you installed the package. On Linux with the repository's `.venv`:

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

Restart Claude Desktop after saving the config file. The server should appear in the MCP panel.

#### Smoke-test the server interactively

```bash
mcp dev oas_mcp/server.py
```

This opens the MCP Inspector in your browser. You can call tools directly from the UI to verify everything is working before connecting to Claude Desktop.

---

### HTTP transport

HTTP transport exposes the server as a streamable HTTP endpoint — useful for remote deployment, Docker, or multi-user environments.

#### Step 1 — Install HTTP dependencies

```bash
pip install -e ".[http]"
```

#### Step 2 — Start the server

```bash
oas-mcp --transport http --host 127.0.0.1 --port 8000
```

Or via environment variables (useful in Docker / CI):

```bash
OAS_TRANSPORT=http OAS_PORT=8000 oas-mcp
```

The server will print:
```
INFO:     Started server process [...]
INFO:     Uvicorn running on http://127.0.0.1:8000
```

#### Step 3 — Connect an MCP client

Point your MCP client at `http://127.0.0.1:8000/mcp`.

#### Step 4 — (Optional) Enable Keycloak authentication

Copy `env.example` to `.env` and fill in your Keycloak details:

```bash
cp env.example .env
```

```dotenv
OAS_TRANSPORT=http
OAS_DATA_DIR=/data/artifacts
KEYCLOAK_ISSUER_URL=https://your-keycloak.railway.app/realms/oas
KEYCLOAK_CLIENT_ID=oas-mcp
KEYCLOAK_CLIENT_SECRET=<your-secret>
RESOURCE_SERVER_URL=http://localhost:8000
```

Then start the server — it will automatically pick up the env vars and wire in the Keycloak token verifier:

```bash
source .env && oas-mcp --transport http
```

When `KEYCLOAK_ISSUER_URL` is set the server validates RS256 JWTs on every request. Requests without a valid Bearer token receive `401 Unauthorized`.

---

### Docker

Docker is the easiest way to deploy the HTTP server with persistent artifact storage.

#### Step 1 — Build the image

```bash
docker build -t oas-mcp .
```

#### Step 2 — Start the server

```bash
docker compose up
```

This starts the `oas-mcp` service on port 8000 with a named volume (`oas-data`) mounted at `/data`. Artifacts are stored under `/data/artifacts` inside the container and persist across restarts.

#### Step 3 — Verify the server is running

```bash
curl http://localhost:8000/mcp
```

You should receive an MCP protocol response.

#### Step 4 — Connect from Claude Desktop (HTTP mode)

Add the server to `claude_desktop_config.json` using its HTTP URL:

```json
{
  "mcpServers": {
    "openaerostruct": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

#### Customise the Docker deployment

Override environment variables in `docker-compose.yml` or with `-e` flags:

```bash
docker run -p 8000:8000 -v oas-data:/data \
  -e OAS_TRANSPORT=http \
  -e OAS_DATA_DIR=/data/artifacts \
  -e KEYCLOAK_ISSUER_URL=https://your-keycloak.railway.app/realms/oas \
  oas-mcp
```

---

## Running the tests

Tests use pytest with `asyncio_mode = "auto"` (configured in `pyproject.toml`).

### Step 1 — Install test dependencies

```bash
pip install pytest pytest-asyncio
# or (if you used the [all] extra, these are already installed)
```

### Step 2 — Run all tests

```bash
pytest
# ~105 tests in ~5 s
```

### Fast feedback — unit tests only (no OAS computation)

```bash
pytest -m "not slow"
# 68 tests in ~0.2 s
```

This covers:
- `test_artifacts.py` — 20 tests for `ArtifactStore` (no OAS required)
- `test_validators.py` — input validation
- `test_session.py` — session caching
- `test_mesh.py` — mesh building and transforms

### Integration tests only

```bash
pytest -m slow
# 37 tests in ~4 s (runs real OAS computations)
```

### Run a single test file

```bash
pytest oas_mcp/tests/test_artifacts.py -v
pytest oas_mcp/tests/test_tools.py -v
```

---

## Artifact storage

Every analysis tool automatically saves its results to disk and returns a `run_id` in its response. You can retrieve, list, or delete artifacts at any time — results persist across server restarts.

### Storage layout

```
$OAS_DATA_DIR/                        # default: ./oas_data/artifacts/
  {session_id}/
    index.json                        # fast index: [{run_id, analysis_type, timestamp, …}]
    20260301T143022_a7f3.json         # full artifact: {metadata: {…}, results: {…}}
    20260301T143055_b2c1.json
```

Set `OAS_DATA_DIR` to control the storage root (default: `./oas_data/artifacts/`; in Docker: `/data/artifacts`).

### Run ID format

Run IDs are formatted as `YYYYMMDDTHHMMSS_{4hex}` — human-readable, chronologically sortable, and collision-resistant (e.g. `20260301T143022_a7f3`).

### Step-by-step: save and retrieve results

**Step 1 — Run an analysis (artifact is saved automatically):**

```python
result = await run_aero_analysis(surfaces=["wing"], alpha=5.0)
# result now contains a "run_id" key
run_id = result["run_id"]   # e.g. "20260301T143022_a7f3"
```

**Step 2 — List saved artifacts:**

```python
# All artifacts across all sessions
list_artifacts()

# Filter by session
list_artifacts(session_id="default")

# Filter by analysis type
list_artifacts(session_id="default", analysis_type="aero")
```

Returns `{count, artifacts: [{run_id, session_id, analysis_type, timestamp, surfaces, tool_name}]}`.

**Step 3 — Retrieve a past result:**

```python
# Full artifact (metadata + results)
get_artifact(run_id="20260301T143022_a7f3")

# Metadata only (lightweight — no results payload)
get_artifact_summary(run_id="20260301T143022_a7f3")
```

**Step 4 — Access via resource URI:**

Any artifact can also be read directly as a resource:

```
oas://artifacts/20260301T143022_a7f3
```

**Step 5 — Delete an artifact:**

```python
delete_artifact(run_id="20260301T143022_a7f3")
```

### Self-healing index

If `index.json` is missing or corrupt (e.g. after a crash or manual file deletion), the index is automatically rebuilt by scanning all artifact files in the session directory. No data is lost.

---

## How agents learn to use the server

MCP provides three built-in mechanisms for conveying knowledge to an LLM or agent. The server uses all three layers:

### Layer 1 — Server instructions (automatic, always-on)

The `instructions` field on the `FastMCP` instance is sent to the LLM when the server connects. It covers:

- The mandatory workflow order (`create_surface` → analysis → optimisation)
- Hard constraints that cause errors if violated (odd `num_y`, structural properties required for aerostruct)
- Sensible default parameter values for cruise flight conditions
- Artifact storage: how to retrieve past results using `run_id`
- A note about performance (problem caching)

This is the first thing an agent sees — it sets the framing without requiring any user action.

### Layer 2 — Prompts (invokable workflows)

MCP prompts are named templates that encode "how to accomplish goal X" rather than "what does tool Y do". They accept arguments and return a user message that seeds the conversation with a fully specified plan. The server exposes three:

| Prompt | Arguments | What it produces |
|--------|-----------|-----------------|
| `analyze_wing` | `wing_type`, `target_CL`, `Mach`, `span` | Step-by-step plan: create → single-point → drag polar → stability check |
| `aerostructural_design` | `W0_kg`, `load_factor`, `material` | Plan including material properties, structural interpretation guide, fallback to optimisation if structure fails |
| `optimize_wing` | `objective`, `target_CL`, `analysis_type` | Plan with correctly formatted DV/constraint dicts for the chosen objective and analysis type |

In **Claude Desktop**, prompts appear in the `+` attachment menu and can be invoked by the user as conversation starters. In **agentic pipelines**, the orchestrator calls `prompts/get` to retrieve the message and prepends it to the agent's context.

### Layer 3 — Resources (on-demand reference material)

Resources are URI-addressable documents the LLM can read when it needs detail.

| URI | Content |
|-----|---------|
| `oas://reference` | Parameter tables for all tools, valid value lists, return-value schemas, artifact storage guide, common errors and fixes |
| `oas://workflows` | Five complete step-by-step workflows (aero analysis, aerostructural sizing, aero optimisation, aerostructural optimisation, multi-surface wing+tail) |
| `oas://artifacts/{run_id}` | Full JSON artifact for any saved run |

---

## Architecture

```
oas_mcp/
├── server.py          # FastMCP entry point — all @mcp.tool() registrations
├── core/
│   ├── artifacts.py   # ArtifactStore — filesystem-backed, thread-safe result persistence
│   ├── auth.py        # KeycloakTokenVerifier — RS256 JWT validation for HTTP transport
│   ├── defaults.py    # Default flight conditions and surface properties
│   ├── validators.py  # Input validation with descriptive error messages
│   ├── mesh.py        # generate_mesh() wrapper + sweep/dihedral/taper transforms
│   ├── connections.py # connect_aero_surface() / connect_aerostruct_surface()
│   ├── builders.py    # build_aero_problem() / build_aerostruct_problem() / build_optimization_problem()
│   ├── results.py     # extract_aero_results() / extract_aerostruct_results()
│   └── session.py     # Session / SessionManager — surface store + problem cache
└── tests/
    ├── conftest.py          # Fixtures: isolate_artifacts (autouse), clean_session (autouse), aero_wing, struct_wing
    ├── test_artifacts.py    # Unit tests for ArtifactStore (20 tests, no OAS required)
    ├── test_validators.py   # Unit tests for validators
    ├── test_session.py      # Unit tests for session caching
    ├── test_mesh.py         # Unit tests for mesh building and transforms
    └── test_tools.py        # Integration tests for all tools
```

### How a tool call flows

```
MCP client
  └─ tool call (e.g. run_aero_analysis)
       └─ server.py: validates inputs, checks session
            └─ asyncio.to_thread(_run)          ← leaves event loop free
                 └─ session.get_cached_problem()
                      ├─ HIT  → set new flight conditions, run_model()
                      └─ MISS → builders.build_aero_problem()
                                   ├─ mesh.py: Geometry group + connections
                                   ├─ builders.py: AeroPoint + IndepVarComp
                                   ├─ prob.setup()   ← expensive, done once
                                   └─ session.store_problem()
                 └─ results.extract_aero_results() → plain dict
            └─ _artifacts.save(results)          ← thread-safe file I/O
            └─ return {**results, "run_id": ...}
```

### Session caching

Each `Session` object holds:

- **`surfaces`** — a `dict[name → surface dict]` populated by `create_surface`.
- **`_cache`** — a `dict[key → _CachedProblem]` where the key encodes the set of surface names and analysis type (`"aero:wing"`, `"aerostruct:wing"`).

Cache invalidation is fingerprint-based. When `create_surface` is called for a surface that already exists, the stored surface dict is replaced and any cached problem referencing that surface is evicted. The fingerprint is a SHA-256 hash of the surface dict contents (numpy arrays are serialised to lists).

### OpenMDAO model structure

**Aero-only** (`build_aero_problem`):

```
prob.model
├── prob_vars (IndepVarComp)  v, alpha, Mach_number, re, rho, cg
├── {name} (Geometry)         for each surface
└── aero (AeroPoint)
      ├── {name}              def_mesh
      ├── aero_states         vortex lattice solve
      └── {name}_perf         CL, CD, CDi, CDv, CDw
```

**Aerostructural** (`build_aerostruct_problem`):

```
prob.model
├── prob_vars (IndepVarComp)  + CT, R, W0, speed_of_sound, load_factor, empty_cg
├── {name} (AerostructGeometry)  mesh, nodes, stiffness, structural_mass
└── AS_point_0 (AerostructPoint)
      ├── coupled             aero-structural iteration
      ├── {name}_perf         CL, CD, failure, vonmises
      └── total_perf          fuelburn, L_equals_W, CG
```

---

## Tools reference

### `create_surface`

Defines a lifting surface and stores it in the session. Must be called before any analysis tool.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | str | `"wing"` | Unique surface identifier |
| `wing_type` | str | `"rect"` | `"rect"` for rectangular or `"CRM"` for Common Research Model |
| `span` | float | `10.0` | Full wingspan in metres |
| `root_chord` | float | `1.0` | Root chord in metres |
| `taper` | float | `1.0` | Taper ratio (tip/root chord) |
| `sweep` | float | `0.0` | Leading-edge sweep in degrees |
| `dihedral` | float | `0.0` | Dihedral angle in degrees |
| `num_x` | int | `2` | Chordwise mesh nodes (≥ 2) |
| `num_y` | int | `7` | Spanwise mesh nodes (must be **odd**, ≥ 3) |
| `symmetry` | bool | `True` | Model half-span (recommended) |
| `twist_cp` | float[] | `None` | Twist control-point values in degrees |
| `t_over_c_cp` | float[] | `[0.15]` | Thickness-to-chord ratio control points |
| `CL0` | float | `0.0` | Profile CL at α=0 |
| `CD0` | float | `0.015` | Zero-lift profile drag |
| `with_viscous` | bool | `True` | Include viscous drag |
| `with_wave` | bool | `False` | Include wave drag |
| `fem_model_type` | str | `None` | `"tube"`, `"wingbox"`, or `None` for aero-only |
| `thickness_cp` | float[] | `None` | Tube wall thickness control points in metres |
| `E` | float | `70e9` | Young's modulus in Pa |
| `G` | float | `30e9` | Shear modulus in Pa |
| `yield_stress` | float | `500e6` | Yield stress in Pa |
| `safety_factor` | float | `2.5` | Safety factor on yield stress |
| `mrho` | float | `3000.0` | Material density in kg/m³ |
| `offset` | float[3] | `None` | [x, y, z] origin offset in metres |

**Returns:** `{surface_name, mesh_shape, span_m, mean_chord_m, estimated_area_m2, twist_cp_shape, has_structure, status}`

> **Note on `num_y`:** OpenAeroStruct requires an odd number of spanwise nodes. If you pass an even value the tool raises a `ValueError` and suggests the nearest valid options.

---

### `run_aero_analysis`

Single-point vortex-lattice (VLM) aerodynamic analysis.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `surfaces` | str[] | — | Surface names to include |
| `velocity` | float | `248.136` | Free-stream velocity in m/s |
| `alpha` | float | `5.0` | Angle of attack in degrees |
| `Mach_number` | float | `0.84` | Mach number |
| `reynolds_number` | float | `1e6` | Reynolds number per metre |
| `density` | float | `0.38` | Air density in kg/m³ |
| `cg` | float[3] | `[0,0,0]` | Centre of gravity in metres |
| `session_id` | str | `"default"` | Session identifier |

**Returns:** `{CL, CD, CM, L_over_D, surfaces: {name: {CL, CD, CDi, CDv, CDw}}, run_id}`

The `run_id` can be passed to `get_artifact` at any future time to retrieve this result.

---

### `run_aerostruct_analysis`

Coupled VLM + beam FEM analysis. Surfaces must have `fem_model_type` set.

Additional parameters beyond `run_aero_analysis`:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `W0` | float | `120000` | Empty weight (excl. wing structure) in kg |
| `CT` | float | auto | Specific fuel consumption in 1/s |
| `R` | float | `11.165e6` | Mission range in metres |
| `speed_of_sound` | float | `295.4` | Speed of sound in m/s |
| `load_factor` | float | `1.0` | Load factor |
| `empty_cg` | float[3] | `[0,0,0]` | Empty CG location in metres |

**Returns:** aero results + `{fuelburn, structural_mass, L_equals_W, surfaces: {name: {..., failure, max_vonmises_Pa, structural_mass_kg}}, run_id}`

The `failure` metric uses a KS-aggregated stress constraint (negative = no failure, positive = failed).

---

### `compute_drag_polar`

Sweeps angle of attack and returns CL, CD, CM arrays.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `surfaces` | str[] | — | Surface names |
| `alpha_start` | float | `-5.0` | Start angle in degrees |
| `alpha_end` | float | `15.0` | End angle in degrees |
| `num_alpha` | int | `21` | Number of alpha points |
| + flight conditions | | | Same as `run_aero_analysis` (no `alpha`) |

**Returns:** `{alpha_deg[], CL[], CD[], CM[], L_over_D[], best_L_over_D: {alpha_deg, CL, CD, L_over_D}, run_id}`

---

### `compute_stability_derivatives`

Computes CL_α, CM_α, and static margin using finite differencing between two `AeroPoint` instances (Δα = 1×10⁻⁴ deg).

**Returns:** `{CL, CD, CM, CL_alpha [1/deg], CM_alpha [1/deg], static_margin, stability, run_id}`

`static_margin = −CM_α / CL_α`. Positive means statically stable.

---

### `run_optimization`

Minimises an objective subject to constraints using SciPy's SLSQP.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `surfaces` | str[] | — | Surface names |
| `analysis_type` | str | `"aero"` | `"aero"` or `"aerostruct"` |
| `objective` | str | `"CD"` | See table below |
| `design_variables` | dict[] | `[{name:alpha}]` | See table below |
| `constraints` | dict[] | `[{name:CL, equals:0.5}]` | See table below |
| `tolerance` | float | `1e-6` | Convergence tolerance |
| `max_iterations` | int | `200` | Maximum iterations |

**Objective names:**

| Analysis type | Options |
|---------------|---------|
| `aero` | `CD`, `CL` |
| `aerostruct` | `fuelburn`, `structural_mass`, `CD` |

**Design variable names:** `twist`, `thickness`, `chord`, `sweep`, `taper`, `alpha`, `spar_thickness`, `skin_thickness`

Each DV dict: `{"name": "twist", "lower": -10.0, "upper": 15.0}` (scaler optional)

**Constraint names:**

| Analysis type | Options |
|---------------|---------|
| `aero` | `CL`, `CD`, `CM` |
| `aerostruct` | all aero + `failure`, `thickness_intersects`, `L_equals_W` |

Each constraint dict: `{"name": "CL", "equals": 0.5}` or `{"name": "failure", "upper": 0.0}`

**Returns:** `{success, optimized_design_variables, final_results, run_id}`

---

### `reset`

Clears surfaces and cached problems.

```
reset()                       # clears all sessions
reset(session_id="default")   # clears one session
```

---

### `list_artifacts`

Browse saved analysis runs.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `session_id` | str | `None` | Filter by session, or `None` for all sessions |
| `analysis_type` | str | `None` | Filter by type: `"aero"`, `"aerostruct"`, `"drag_polar"`, `"stability"`, `"optimization"` |

**Returns:** `{count, artifacts: [{run_id, session_id, analysis_type, timestamp, surfaces, tool_name}]}`

---

### `get_artifact`

Retrieve a saved artifact by `run_id`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_id` | str | — | Run ID returned by an analysis tool |
| `session_id` | str | `None` | Optional hint — speeds up lookup when provided |

**Returns:** `{metadata: {run_id, session_id, analysis_type, timestamp, surfaces, tool_name, parameters}, results: {…}}`

Raises `ValueError` if the run ID is not found.

---

### `get_artifact_summary`

Retrieve artifact metadata only — no results payload. Much smaller response than `get_artifact`.

Same parameters as `get_artifact`.

**Returns:** `{run_id, session_id, analysis_type, timestamp, surfaces, tool_name, parameters}`

---

### `delete_artifact`

Permanently remove a saved artifact from disk and its index entry.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_id` | str | — | Run ID to delete |
| `session_id` | str | `None` | Optional hint |

**Returns:** `{status: "deleted", run_id}`

Raises `ValueError` if the run ID is not found.

---

## Example walkthrough

This example defines a CRM wing, runs a cruise-point analysis, sweeps the drag polar, checks stability, runs an aerostructural analysis, optimises twist for minimum drag, and then retrieves the saved results.

### Step 1 — Define the wing

```python
create_surface(
    name           = "wing",
    wing_type      = "CRM",       # Common Research Model planform
    num_x          = 2,           # chordwise panels
    num_y          = 7,           # spanwise panels (must be odd)
    symmetry       = True,
    with_viscous   = True,
    CD0            = 0.015,       # fuselage + nacelle parasite drag
    fem_model_type = "tube",      # enables structural analysis
    E              = 70e9,        # Al 7075
    G              = 30e9,
    yield_stress   = 500e6,
    safety_factor  = 2.5,
    mrho           = 3000.0,
)
```

### Step 2 — Single-point aero analysis

```python
result = run_aero_analysis(
    surfaces        = ["wing"],
    velocity        = 248.136,   # m/s (~Mach 0.84 at cruise altitude)
    alpha           = 5.0,       # deg
    Mach_number     = 0.84,
    reynolds_number = 1e6,
    density         = 0.38,      # kg/m³ (≈ 11 000 m altitude)
)
run_id_aero = result["run_id"]   # e.g. "20260301T143022_a7f3"
```

```json
{
  "CL": 0.5459, "CD": 0.0367, "CM": -0.6844, "L_over_D": 14.88,
  "surfaces": {"wing": {"CL": 0.5459, "CDi": 0.0229, "CDv": 0.0123}},
  "run_id": "20260301T143022_a7f3"
}
```

### Step 3 — Drag polar

```python
compute_drag_polar(
    surfaces    = ["wing"],
    alpha_start = 0.0,
    alpha_end   = 12.0,
    num_alpha   = 7,
    Mach_number = 0.84,
    density     = 0.38,
)
```

```
alpha   CL      CD      L/D
  0.0   0.1623  0.0279   5.82
  2.0   0.3161  0.0304  10.40
  4.0   0.4695  0.0342  13.71
  6.0   0.6220  0.0395  15.76
  8.0   0.7736  0.0460  16.82
 10.0   0.9239  0.0538  17.16   ← best L/D
 12.0   1.0727  0.0629  17.05
```

### Step 4 — Stability derivatives

```python
compute_stability_derivatives(
    surfaces    = ["wing"],
    alpha       = 5.0,
    Mach_number = 0.84,
    density     = 0.38,
    cg          = [5.0, 0.0, 0.0],   # CG at 5 m from leading edge
)
```

```json
{
  "CL_alpha": 0.0862, "CM_alpha": -0.0214,
  "static_margin": 0.248,
  "stability": "statically stable (positive static margin)"
}
```

### Step 5 — Coupled aerostructural analysis

```python
run_aerostruct_analysis(
    surfaces       = ["wing"],
    velocity       = 248.136,
    alpha          = 5.0,
    Mach_number    = 0.84,
    density        = 0.38,
    W0             = 120000,    # kg (aircraft empty weight excl. wing)
    R              = 11.165e6,  # m (range)
    speed_of_sound = 295.4,
    load_factor    = 1.0,
)
```

```json
{
  "CL": 0.5281, "CD": 0.0364, "L_over_D": 14.50,
  "fuelburn": 165564, "structural_mass": 124259,
  "L_equals_W": 0.496,
  "surfaces": {"wing": {"failure": -0.7676, "structural_mass_kg": 124259}},
  "run_id": "20260301T143055_b2c1"
}
```

`failure < 0` means no structural failure at this load condition.

### Step 6 — Aerodynamic optimisation

Minimise CD subject to CL = 0.5 by varying twist and angle of attack:

```python
run_optimization(
    surfaces         = ["wing"],
    analysis_type    = "aero",
    objective        = "CD",
    design_variables = [
        {"name": "twist", "lower": -10.0, "upper": 15.0},
        {"name": "alpha", "lower":  -5.0, "upper": 15.0},
    ],
    constraints      = [{"name": "CL", "equals": 0.5}],
    Mach_number      = 0.84,
    density          = 0.38,
)
```

```json
{
  "success": true,
  "optimized_design_variables": {
    "alpha": [4.537],
    "twist": [-3.821, -1.786, 0.495, 6.517]
  },
  "final_results": {"CL": 0.5000, "CD": 0.0351, "L_over_D": 14.25},
  "run_id": "20260301T143120_c4d9"
}
```

### Step 7 — Retrieve a past result

```python
# Look up the aero analysis saved in step 2
get_artifact(run_id="20260301T143022_a7f3")

# Or browse all saved runs for this session
list_artifacts(session_id="default")

# Or check just the metadata (no results payload)
get_artifact_summary(run_id="20260301T143022_a7f3")
```

---

## Tips

**Parameter sweeps are fast.** After the first `run_aero_analysis` call, the OpenMDAO problem is cached. Subsequent calls with different `alpha` or `Mach_number` values skip `setup()` entirely — useful when an agent is iterating to find an operating point.

**Multi-surface configurations.** Pass multiple surface names to analysis tools to model wing + tail configurations. Each surface must be created separately with `create_surface`.

**Sessions.** All tools accept an optional `session_id` parameter (default `"default"`). Use different session IDs to maintain multiple independent configurations in the same server process. Artifacts are stored per session and can be filtered by `session_id` when listed.

**Structural sizing.** The default `thickness_cp` is `0.1 * root_chord` at every control point. For realistic structural analysis, provide explicit values or let the solver size the structure with a `failure` constraint via `run_optimization`.

**Artifact `session_id` hint.** If you know which session produced a `run_id`, passing `session_id` to `get_artifact`, `get_artifact_summary`, and `delete_artifact` makes the lookup O(1) instead of scanning all session directories.
