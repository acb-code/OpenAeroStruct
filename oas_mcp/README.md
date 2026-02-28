# OpenAeroStruct MCP Server

An [MCP](https://modelcontextprotocol.io) server that wraps [OpenAeroStruct](https://mdolab-openaerostruct.readthedocs-hosted.com) so AI agents can perform aerodynamic and aerostructural wing analysis through simple tool calls — no OpenMDAO boilerplate required.

## Contents

- [Overview](#overview)
- [Installation](#installation)
- [Running the server](#running-the-server)
- [Running the tests](#running-the-tests)
- [Architecture](#architecture)
- [Tools reference](#tools-reference)
- [Example: wing analysis and drag polar](#example-wing-analysis-and-drag-polar)

---

## Overview

Setting up an OpenAeroStruct analysis normally requires 50–100 lines of OpenMDAO boilerplate: `IndepVarComp`, `Geometry` groups, `AeroPoint` or `AerostructPoint`, and a dozen `connect()` calls. This server hides all of that behind seven tool calls.

**What you can do:**

| Goal | Tool |
|------|------|
| Define a wing geometry | `create_surface` |
| Single-point VLM analysis (CL, CD, CM) | `run_aero_analysis` |
| Coupled aero + structural analysis | `run_aerostruct_analysis` |
| Generate a CL-CD-CM drag polar | `compute_drag_polar` |
| Compute CL_α, CM_α, static margin | `compute_stability_derivatives` |
| Optimise twist/thickness/alpha | `run_optimization` |
| Clear state between experiments | `reset` |

**Key properties:**

- **Stateful sessions** — surfaces are stored by name; call `create_surface` once, then run analyses repeatedly.
- **Problem caching** — `om.Problem.setup()` is expensive. The server runs it once per unique geometry and reuses the cached problem for parameter sweeps, making repeated `run_aero_analysis` calls with different alpha/Mach values fast.
- **Async** — all OpenMDAO computation runs in `asyncio.to_thread()` so the event loop stays responsive.

---

## Installation

The server lives inside the `oas_mcp/` package in the OpenAeroStruct repository. It requires the `mcp` package in addition to normal OpenAeroStruct dependencies.

```bash
# From the repository root:
uv pip install -e ".[mcp]"
```

Or with pip:

```bash
pip install -e ".[mcp]"
```

This installs OpenAeroStruct in editable mode together with `mcp[cli]`.

---

## Running the server

### stdio transport (default — for Claude Desktop and most MCP clients)

```bash
python -m oas_mcp.server
```

Or, if installed via the package entry point:

```bash
oas-mcp
```

### Add to Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the equivalent on your platform:

```json
{
  "mcpServers": {
    "openaerostruct": {
      "command": "/path/to/venv/bin/python",
      "args": ["-m", "oas_mcp.server"]
    }
  }
}
```

Replace `/path/to/venv` with the path to the virtual environment where OpenAeroStruct is installed. On Linux with the repository's `.venv`:

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

---

## Running the tests

Tests use pytest with `asyncio_mode = "auto"` (configured in `pyproject.toml`).

### Install test dependencies

```bash
uv pip install pytest pytest-asyncio
# or: pip install pytest pytest-asyncio
```

### Run everything

```bash
pytest
# 85 tests in ~4 s
```

### Fast feedback — unit tests only (no OAS computation)

```bash
pytest -m "not slow"
# 48 tests in ~0.1 s
```

### Integration tests only

```bash
pytest -m slow
# 37 tests in ~4 s
```

The `slow` marker is applied to all tests in `test_tools.py`. Unit tests in `test_validators.py`, `test_session.py`, and `test_mesh.py` have no marker and run instantly.

---

## Architecture

```
oas_mcp/
├── server.py          # FastMCP entry point — all @mcp.tool() registrations
├── core/
│   ├── defaults.py    # Default flight conditions and surface properties
│   ├── validators.py  # Input validation with descriptive error messages
│   ├── mesh.py        # generate_mesh() wrapper + sweep/dihedral/taper transforms
│   ├── connections.py # connect_aero_surface() / connect_aerostruct_surface()
│   ├── builders.py    # build_aero_problem() / build_aerostruct_problem() / build_optimization_problem()
│   ├── results.py     # extract_aero_results() / extract_aerostruct_results()
│   └── session.py     # Session / SessionManager — surface store + problem cache
└── tests/
    ├── conftest.py          # Fixtures: clean_session (autouse), aero_wing, struct_wing
    ├── test_validators.py   # Unit tests for validators
    ├── test_session.py      # Unit tests for session caching
    ├── test_mesh.py         # Unit tests for mesh building and transforms
    └── test_tools.py        # Integration tests for all 7 tools
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

**Returns:** `{CL, CD, CM, L_over_D, surfaces: {name: {CL, CD, CDi, CDv, CDw}}}`

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

**Returns:** aero results + `{fuelburn, structural_mass, L_equals_W, surfaces: {name: {..., failure, max_vonmises_Pa, structural_mass_kg}}}`

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

**Returns:** `{alpha_deg[], CL[], CD[], CM[], L_over_D[], best_L_over_D: {alpha_deg, CL, CD, L_over_D}}`

---

### `compute_stability_derivatives`

Computes CL_α, CM_α, and static margin using finite differencing between two `AeroPoint` instances (Δα = 1×10⁻⁴ deg).

**Returns:** `{CL, CD, CM, CL_alpha [1/deg], CM_alpha [1/deg], static_margin, stability}`

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

**Returns:** `{success, optimized_design_variables, final_results}`

---

### `reset`

Clears surfaces and cached problems.

```
reset()                       # clears all sessions
reset(session_id="default")   # clears one session
```

---

## Example: wing analysis and drag polar

This example defines a CRM wing, runs a cruise-point analysis, sweeps the drag polar, and then optimises twist for minimum drag at CL = 0.5.

### 1. Define the wing

```python
create_surface(
    name        = "wing",
    wing_type   = "CRM",       # Common Research Model planform
    num_x       = 2,           # chordwise panels
    num_y       = 7,           # spanwise panels (must be odd)
    symmetry    = True,
    with_viscous = True,
    CD0         = 0.015,       # fuselage + nacelle parasite drag
    # Aluminium 7075 tube spar
    fem_model_type = "tube",
    E              = 70e9,
    G              = 30e9,
    yield_stress   = 500e6,
    safety_factor  = 2.5,
    mrho           = 3000.0,
)
```

### 2. Single-point aero analysis

```python
run_aero_analysis(
    surfaces      = ["wing"],
    velocity      = 248.136,   # m/s  (~Mach 0.84 at cruise altitude)
    alpha         = 5.0,       # deg
    Mach_number   = 0.84,
    reynolds_number = 1e6,
    density       = 0.38,      # kg/m³  (≈ 11,000 m altitude)
)
```

```
{
  "CL": 0.5459,
  "CD": 0.0367,
  "CM": -0.6844,
  "L_over_D": 14.88,
  "surfaces": {
    "wing": {"CL": 0.5459, "CD": 0.0367, "CDi": 0.0229, "CDv": 0.0123}
  }
}
```

### 3. Drag polar

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

best_L_over_D: {"alpha_deg": 10.0, "CL": 0.9239, "CD": 0.0538, "L_over_D": 17.16}
```

### 4. Coupled aerostructural analysis

```python
run_aerostruct_analysis(
    surfaces      = ["wing"],
    velocity      = 248.136,
    alpha         = 5.0,
    Mach_number   = 0.84,
    density       = 0.38,
    W0            = 120000,    # kg  (aircraft empty weight excl. wing)
    R             = 11.165e6,  # m   (range)
    speed_of_sound = 295.4,
    load_factor   = 1.0,
)
```

```
{
  "CL": 0.5281,
  "CD": 0.0364,
  "L_over_D": 14.50,
  "fuelburn": 165564,           # kg
  "structural_mass": 124259,    # kg
  "L_equals_W": 0.496,          # residual: 0 means L = W exactly
  "surfaces": {
    "wing": {
      "failure": -0.7676,       # negative → no structural failure
      "structural_mass_kg": 124259
    }
  }
}
```

### 5. Aerodynamic optimisation

Minimise CD subject to CL = 0.5 by varying wing twist and angle of attack:

```python
run_optimization(
    surfaces          = ["wing"],
    analysis_type     = "aero",
    objective         = "CD",
    design_variables  = [
        {"name": "twist", "lower": -10.0, "upper": 15.0},
        {"name": "alpha", "lower":  -5.0, "upper": 15.0},
    ],
    constraints       = [{"name": "CL", "equals": 0.5}],
    Mach_number       = 0.84,
    density           = 0.38,
)
```

```
{
  "success": true,
  "optimized_design_variables": {
    "alpha":  [4.537],
    "twist":  [-3.821, -1.786, 0.495, 6.517]
  },
  "final_results": {
    "CL": 0.5000,
    "CD": 0.0351,           # reduced from 0.0367 at baseline
    "L_over_D": 14.25
  }
}
```

### 6. Stability derivatives

```python
compute_stability_derivatives(
    surfaces  = ["wing"],
    alpha     = 5.0,
    Mach_number = 0.84,
    density   = 0.38,
    cg        = [5.0, 0.0, 0.0],   # CG at 5 m from leading edge
)
```

```
{
  "CL_alpha": 0.0862,         # 1/deg  (lift-curve slope)
  "CM_alpha": -0.0214,        # 1/deg  (negative → stabilising)
  "static_margin": 0.248,     # positive → statically stable
  "stability": "statically stable (positive static margin)"
}
```

---

## Tips

**Parameter sweeps are fast.** After the first `run_aero_analysis` call, the OpenMDAO problem is cached. Subsequent calls with different `alpha` or `Mach_number` values skip `setup()` entirely — useful when an agent is iterating to find an operating point.

**Multi-surface configurations.** Pass multiple surface names to analysis tools to model wing + tail configurations. Each surface must be created separately with `create_surface`.

**Sessions.** All tools accept an optional `session_id` parameter (default `"default"`). Use different session IDs to maintain multiple independent configurations in the same server process.

**Structural sizing.** The default `thickness_cp` is `0.1 * root_chord` at every control point. For realistic structural analysis, provide explicit values or follow the optimisation example and let the solver size the structure with a `failure` constraint.
