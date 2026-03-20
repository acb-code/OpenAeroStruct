# OAS MCP Server — Observability & Trust Guide

This guide explains how to use the server's observability features to monitor, validate, and debug analyses — both interactively and in automated agentic pipelines.

## Contents

- [Response envelope](#response-envelope)
- [Validation](#validation)
- [Custom requirements](#custom-requirements)
- [Run manifest: `get_run`](#run-manifest-get_run)
- [Cache pinning](#cache-pinning)
- [Detailed results](#detailed-results)
- [Server logs: `get_last_logs`](#server-logs-get_last_logs)
- [Telemetry environment variables](#telemetry-environment-variables)
- [Session configuration: `configure_session`](#session-configuration-configure_session)

---

## Response envelope

Every analysis tool (`run_aero_analysis`, `run_aerostruct_analysis`, `compute_drag_polar`, `compute_stability_derivatives`, `run_optimization`) returns a **versioned response envelope** rather than a bare results dict. This gives agents a stable contract to program against — new fields can be added without breaking existing code.

### Envelope schema

```json
{
  "schema_version": "1.0",
  "tool_name": "run_aero_analysis",
  "run_id": "20260302T143022_a7f3",
  "timestamp": "2026-03-02T14:30:22+00:00",
  "inputs_hash": "sha256-a3f7c1...",
  "results": {
    "CL": 0.5459,
    "CD": 0.0367,
    "CM": -0.6844,
    "L_over_D": 14.88,
    "surfaces": { "wing": { "CL": 0.5459, "CDi": 0.0229 } }
  },
  "validation": {
    "passed": true,
    "error_count": 0,
    "warning_count": 0,
    "info_count": 2,
    "findings": []
  },
  "telemetry": {
    "elapsed_s": 0.127,
    "oas.cache.hit": true,
    "oas.surface.count": 1,
    "oas.mesh.nx": 2,
    "oas.mesh.ny": 7
  }
}
```

| Field | Description |
|-------|-------------|
| `schema_version` | `"1.0"` — increment signals a breaking change |
| `tool_name` | Which tool produced this response |
| `run_id` | Opaque handle for all follow-up calls (`get_run`, `pin_run`, `visualize`, etc.) |
| `timestamp` | ISO-8601 UTC time of the call |
| `inputs_hash` | SHA-256 of the serialised inputs — use to detect identical re-runs |
| `results` | Tool-specific payload; contents documented in [tools reference](README.md#tools-reference) |
| `validation` | Physics and numerics checks — always check `passed` before trusting results |
| `telemetry` | Timing and cache info |

> **Extracting results in code:** `envelope["results"]["CL"]` — the full results dict is at `results`, not at the top level.

### Error envelope

When a tool fails with a known error the same outer structure is returned with `results: null` and an `error` block:

```json
{
  "schema_version": "1.0",
  "tool_name": "run_aero_analysis",
  "run_id": null,
  "results": null,
  "error": {
    "code": "USER_INPUT_ERROR",
    "message": "num_y must be odd, got 8. Try 7 or 9.",
    "details": {}
  }
}
```

| Error code | Cause | Action |
|------------|-------|--------|
| `USER_INPUT_ERROR` | Bad parameter value (odd num_y, unknown surface name, etc.) | Fix the input and retry |
| `SOLVER_CONVERGENCE_ERROR` | OpenMDAO solver did not converge | Try a different alpha or flight condition |
| `CACHE_EVICTED_ERROR` | The cached problem was evicted between pin and use | Call the analysis tool again to rebuild the cache |
| `INTERNAL_ERROR` | Unexpected server-side exception | Check `get_last_logs(run_id)` for the traceback |

---

## Validation

Every analysis response includes a `validation` block containing automatically-run physics and numerics checks. These catch common mistakes before results are used downstream.

### Validation block structure

```json
{
  "passed": false,
  "error_count": 1,
  "warning_count": 0,
  "info_count": 2,
  "findings": [
    {
      "check_id": "physics.cd_positive",
      "category": "physics",
      "severity": "error",
      "confidence": "high",
      "passed": false,
      "message": "CD = -0.002 is negative — drag must be positive.",
      "remediation": "Check for mesh issues or unrealistically low CD0."
    }
  ],
  "all_findings": [ ... ]
}
```

`findings` contains only the **failed** checks. `all_findings` contains every check including passed ones.

### Check severity levels

| Severity | Meaning |
|----------|---------|
| `error` | Almost certainly wrong — don't use these results without investigation |
| `warning` | Likely a problem — review before trusting |
| `info` | Informational note — context-dependent, not necessarily an issue |

### Physics checks by analysis type

#### Aerodynamic analysis (`validate_aero`)

| Check ID | What it tests | Severity |
|----------|---------------|----------|
| `physics.cd_positive` | CD > 0 | error |
| `physics.cd_not_too_large` | CD ≤ 1.0 | error |
| `physics.cl_reasonable` | CL sign is consistent with alpha direction | warning |
| `numerics.ld_reasonable` | L/D magnitude is physically plausible | info |

#### Aerostructural analysis (`validate_aerostruct`)

All aerodynamic checks plus:

| Check ID | What it tests | Severity |
|----------|---------------|----------|
| `structural.structural_failure` | `failure > 1.0` means the structure has exceeded yield | error |
| `structural.fuelburn_positive` | Fuel burn > 0 | error |
| `structural.lew_residual` | `abs(L_equals_W) / W0 < 0.01` — lift ≈ weight trim | warning |

> **Note on `failure`:** The failure metric is a KS-aggregated utilisation ratio. Values **above 1.0** mean structural failure. Values close to 1.0 (e.g. 0.9) mean the structure is near its limit but not failed. Negative values are healthy with margin to spare.

#### Drag polar (`validate_drag_polar`)

| Check ID | What it tests | Severity |
|----------|---------------|----------|
| `physics.cd_positive_polar` | All CD values > 0 | error |
| `physics.cl_monotonic` | CL increases monotonically with alpha | warning |

#### Stability (`validate_stability`)

| Check ID | What it tests | Severity |
|----------|---------------|----------|
| `stability.cl_alpha` | CL_alpha sign (context-dependent) | info |
| `stability.static_margin` | Static margin value recorded | info |

#### Optimization (`validate_optimization`)

| Check ID | What it tests | Severity |
|----------|---------------|----------|
| `optimization.optimizer_converged` | `success == True` | error |

---

## Custom requirements

Requirements let you define project-specific pass/fail criteria that are automatically checked against every analysis result. Failed requirements appear as `error` severity findings in the validation block.

### Setting requirements

```python
set_requirements(
    requirements=[
        {"path": "CL",                        "operator": ">=", "value": 0.4,  "label": "min_CL"},
        {"path": "L_over_D",                  "operator": ">",  "value": 10.0, "label": "min_LD"},
        {"path": "surfaces.wing.failure",     "operator": "<",  "value": 1.0,  "label": "no_failure"},
    ],
    session_id="default",
)
```

Or as part of `configure_session`:

```python
configure_session(
    session_id="default",
    requirements=[
        {"path": "CL", "operator": ">=", "value": 0.4, "label": "min_CL"},
    ],
)
```

### Dot-path notation

Paths use dots to navigate nested result dicts:

| Path | Accesses |
|------|----------|
| `CL` | `results["CL"]` |
| `surfaces.wing.failure` | `results["surfaces"]["wing"]["failure"]` |
| `surfaces.wing.CL` | `results["surfaces"]["wing"]["CL"]` |
| `fuelburn` | `results["fuelburn"]` (aerostruct only) |

### Supported operators

`==`, `!=`, `<`, `<=`, `>`, `>=`

### Requirements check output

When requirements are set, the `validation` block includes a `requirements` sub-block:

```json
{
  "passed": false,
  "requirements": {
    "passed": false,
    "total": 3,
    "passed_count": 2,
    "results": [
      {"label": "min_CL",     "path": "CL",    "operator": ">=", "target": 0.4, "actual": 0.35, "passed": false},
      {"label": "min_LD",     "path": "L_over_D", "operator": ">", "target": 10.0, "actual": 12.3, "passed": true},
      {"label": "no_failure", "path": "surfaces.wing.failure", "operator": "<", "target": 1.0, "actual": -0.3, "passed": true}
    ]
  }
}
```

---

## Run manifest: `get_run`

`get_run(run_id)` is the primary "what do I know about this run?" endpoint. It aggregates everything the server knows about a completed run into a single call.

### Example

```python
manifest = get_run(run_id="20260302T143022_a7f3")
```

```json
{
  "run_id": "20260302T143022_a7f3",
  "tool_name": "run_aero_analysis",
  "analysis_type": "aero",
  "timestamp": "2026-03-02T14:30:22+00:00",
  "surfaces": ["wing"],
  "inputs": {
    "alpha": 5.0,
    "Mach_number": 0.84,
    "velocity": 248.136
  },
  "outputs_summary": {
    "CL": 0.5459,
    "CD": 0.0367,
    "L_over_D": 14.88
  },
  "cache_state": {
    "cached": true,
    "pinned": false,
    "surfaces": ["wing"],
    "analysis_type": "aero"
  },
  "detail_levels_available": {
    "summary": true,
    "standard": true
  },
  "available_plots": ["lift_distribution", "planform"]
}
```

Use `available_plots` to know which `visualize()` calls will succeed before calling them. Use `detail_levels_available.standard` to check whether `get_detailed_results(run_id, "standard")` will return sectional data.

---

## Cache pinning

The server caches OpenMDAO `Problem` objects in memory so that parameter sweeps (varying alpha, Mach, etc.) don't rebuild the model each time. By default the cache holds a fixed number of problems and evicts old ones when new surfaces are defined.

In multi-step agent workflows — where an agent does analysis → thinks → retrieves details — the cache may be evicted between steps. **Pinning** prevents this.

### When to pin

Pin a run when:
- You plan to call `get_detailed_results` or `visualize` significantly after the analysis
- You are running a long optimization loop and want the baseline available throughout
- You are comparing two configurations side-by-side and need both cached simultaneously

### Workflow

```python
# Step 1 — run analysis
envelope = run_aero_analysis(surfaces=["wing"], alpha=5.0)
run_id = envelope["run_id"]

# Step 2 — pin the result so the cache won't be evicted
pin_run(run_id=run_id, surfaces=["wing"], analysis_type="aero")

# Step 3 — do other work: run more analyses, think, check results …
run_aero_analysis(surfaces=["wing"], alpha=7.0)  # this would normally evict the first

# Step 4 — retrieve details — cache is guaranteed still present
details = get_detailed_results(run_id=run_id, detail_level="standard")

# Step 5 — release the pin when done
unpin_run(run_id=run_id)
```

> **Note:** Even without pinning, `get_detailed_results` at the `"standard"` level works because sectional data and mesh snapshots are persisted to disk at run time. Pinning is only needed when you need the live `om.Problem` in memory for operations that require it.

---

## Detailed results

`get_detailed_results(run_id, detail_level)` returns richer data than the top-level scalars in the analysis response.

### Detail levels

| Level | Contents | Requires cache? |
|-------|----------|-----------------|
| `"summary"` | Top-level scalars (CL, CD, fuelburn, etc.) and `surfaces` dict | No — from artifact |
| `"standard"` | Spanwise sectional Cl, von Mises stress per node, mesh coordinates | No — persisted at run time |

### Example: standard detail

```python
details = get_detailed_results(run_id="20260302T143022_a7f3", detail_level="standard")
```

```json
{
  "run_id": "20260302T143022_a7f3",
  "detail_level": "standard",
  "sectional_data": {
    "wing": {
      "y_span_norm": [0.0, 0.166, 0.333, 0.5, 0.666, 0.833, 1.0],
      "Cl": [0.612, 0.598, 0.571, 0.531, 0.471, 0.382, 0.241],
      "vonmises_MPa": [95.3, 88.1, 79.4, 68.2, 54.1, 36.8, 12.3],
      "failure_index": [-0.41, -0.44, -0.48, -0.53, -0.61, -0.73, -0.90]
    }
  },
  "mesh_snapshot": {
    "wing": {
      "leading_edge": [[0.0, 0.0, 0.0], [0.0, 0.833, 0.0], ...],
      "trailing_edge": [[1.0, 0.0, 0.0], [1.0, 0.833, 0.0], ...],
      "nx": 2,
      "ny": 7
    }
  }
}
```

- **`Cl`** — spanwise local lift coefficient distribution (not the integrated CL)
- **`vonmises_MPa`** — von Mises stress at each spanwise node in MPa (aerostruct only)
- **`failure_index`** — KS-aggregated failure index per node (aerostruct only); negative = safe, > 1.0 = failed
- **`y_span_norm`** — normalised spanwise position [0 = root, 1 = tip]

---

## Server logs: `get_last_logs`

Agents cannot access server stderr, so `get_last_logs(run_id)` exposes structured server-side log records captured during a run through MCP.

### Example

```python
logs = get_last_logs(run_id="20260302T143022_a7f3")
```

```json
{
  "run_id": "20260302T143022_a7f3",
  "log_count": 4,
  "logs": [
    {"time": "2026-03-02T14:30:22", "level": "INFO",    "message": "START run_aero_analysis run_id=20260302T143022_a7f3 session=default", "logger": "oas_mcp"},
    {"time": "2026-03-02T14:30:22", "level": "INFO",    "message": "Cache HIT: reusing problem for surfaces=['wing'] type=aero", "logger": "oas_mcp"},
    {"time": "2026-03-02T14:30:22", "level": "INFO",    "message": "END   run_aero_analysis run_id=20260302T143022_a7f3 elapsed=0.127s", "logger": "oas_mcp"},
    {"time": "2026-03-02T14:30:22", "level": "INFO",    "message": "Validation passed: 0 errors, 0 warnings", "logger": "oas_mcp"}
  ]
}
```

Use this when:
- A validation finding says something unexpected
- A `SOLVER_CONVERGENCE_ERROR` occurs and you want to see the residual history
- An `INTERNAL_ERROR` occurs — the traceback will be in the logs

Logs are retained for the last 100 runs (configurable via `OAS_LOG_MAX_RUNS`). Logs for runs beyond this limit are silently dropped.

---

## Telemetry environment variables

| Variable | Values | Default | Effect |
|----------|--------|---------|--------|
| `OAS_TELEMETRY_MODE` | `off`, `logging`, `otel` | `logging` | `off` = no logging; `logging` = structured stdout; `otel` = OpenTelemetry spans |
| `OAS_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` | Minimum log level emitted |
| `OAS_LOG_MAX_RUNS` | integer | `100` | How many run log buffers to keep in memory |

### OpenTelemetry integration

Setting `OAS_TELEMETRY_MODE=otel` enables OpenTelemetry spans. Install the optional dependency first:

```bash
pip install opentelemetry-api opentelemetry-sdk
```

Each span includes semantic attributes:

| Attribute | Description |
|-----------|-------------|
| `mcp.tool.name` | Tool name (e.g. `run_aero_analysis`) |
| `oas.run_id` | Run ID |
| `oas.session_id` | Session ID |
| `oas.surface.count` | Number of surfaces in the analysis |
| `oas.mesh.nx` | Chordwise mesh nodes |
| `oas.mesh.ny` | Spanwise mesh nodes |
| `oas.cache.hit` | Whether the OpenMDAO problem was retrieved from cache |
| `oas.solver.converged` | Whether the solver converged (when captured) |

Configure an exporter to send spans to Jaeger, Zipkin, or an OTLP endpoint using standard OpenTelemetry SDK configuration.

---

## Session configuration: `configure_session`

`configure_session` sets per-session defaults that apply to every subsequent call in the session. Settings persist until `reset()` is called or the server restarts.

```python
configure_session(
    session_id="default",
    default_detail_level="standard",           # get_detailed_results will default to "standard"
    validation_severity_threshold="warning",   # only show errors and warnings in validation
    auto_visualize=["lift_distribution"],       # auto-generate these plots after each analysis
    telemetry_mode="logging",                   # override OAS_TELEMETRY_MODE for this session
    visualization_output="file",               # "inline" | "file" | "url" — controls visualize() return
    requirements=[                             # checked against every result in this session
        {"path": "CL",           "operator": ">=", "value": 0.4,  "label": "min_CL"},
        {"path": "L_over_D",     "operator": ">",  "value": 10.0, "label": "min_LD"},
    ],
)
```

### Configuration parameters

| Parameter | Values | Default | Description |
|-----------|--------|---------|-------------|
| `default_detail_level` | `"summary"`, `"standard"` | `"summary"` | Default for `get_detailed_results` calls that omit `detail_level` |
| `validation_severity_threshold` | `"error"`, `"warning"`, `"info"` | `"info"` | Suppress findings below this severity from the validation block |
| `auto_visualize` | list of plot type strings | `[]` | Plot types to auto-generate and return in `auto_plots` after each analysis |
| `telemetry_mode` | `"off"`, `"logging"`, `"otel"` | from env | Per-session override for telemetry mode |
| `visualization_output` | `"inline"`, `"file"`, `"url"` | `"inline"` | Default output mode for `visualize()` — see [visualization.md](visualization.md#output-modes) |
| `requirements` | list of requirement dicts | `[]` | See [Custom requirements](#custom-requirements) |

### Auto-visualization

When `auto_visualize` is set, each analysis response includes an `auto_plots` key containing the generated plots inline:

```python
configure_session(session_id="default", auto_visualize=["lift_distribution"])
envelope = run_aero_analysis(surfaces=["wing"], alpha=5.0)

# envelope["auto_plots"] is now present:
# {
#   "lift_distribution": { "plot_type": "lift_distribution", "image_base64": "...", ... }
# }
```

See [visualization.md](visualization.md) for details on plot types and the full image response format.

---

## Full example: multi-step agentic workflow

```python
# 1. Configure session with requirements and auto-plots
configure_session(
    session_id="design_study",
    validation_severity_threshold="warning",
    requirements=[
        {"path": "CL",               "operator": ">=", "value": 0.45, "label": "min_CL"},
        {"path": "surfaces.wing.failure", "operator": "<", "value": 1.0, "label": "no_failure"},
    ],
)

# 2. Define surface
create_surface(
    name="wing", wing_type="CRM", num_x=2, num_y=7, symmetry=True,
    fem_model_type="tube", E=70e9, G=30e9, yield_stress=500e6,
    mrho=3000.0, session_id="design_study",
)

# 3. Run analysis — response includes validation + requirements check
envelope = run_aerostruct_analysis(
    surfaces=["wing"], alpha=5.0, W0=120000, session_id="design_study",
)

run_id = envelope["run_id"]
validation = envelope["validation"]

# 4. Check requirements
if not validation["passed"]:
    for finding in validation["findings"]:
        print(f"[{finding['severity']}] {finding['check_id']}: {finding['message']}")
        print(f"  Remediation: {finding['remediation']}")

# 5. Pin the run for multi-step follow-up
pin_run(run_id=run_id, surfaces=["wing"], analysis_type="aerostruct", session_id="design_study")

# 6. Get the run manifest
manifest = get_run(run_id=run_id)
print(f"Available plots: {manifest['available_plots']}")

# 7. Get sectional detail
details = get_detailed_results(run_id=run_id, detail_level="standard")
cl_dist = details["sectional_data"]["wing"]["Cl"]

# 8. Visualize
plot = visualize(run_id=run_id, plot_type="stress_distribution", case_name="CRM cruise")
# plot["image_base64"] — embed in a report or display in the client

# 9. Release pin
unpin_run(run_id=run_id, session_id="design_study")
```
