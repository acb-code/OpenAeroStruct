---
name: oas-cli-guide
description: >
  How to run OpenAeroStruct (OAS) analyses from within Claude Code using the
  oas-cli command-line tool — without needing MCP. Use this skill whenever the
  user asks you to run an OAS analysis, compute a drag polar, run a wing
  optimization, or do anything with OpenAeroStruct from a terminal or script.
  Covers all three CLI modes: interactive (JSON-lines subprocess), one-shot
  subcommands, and batch script execution. Always consult this skill before
  reaching for Bash commands that involve oas-cli.
---

# OAS CLI Guide for Coding Agents

`oas-cli` is the command-line interface to the OAS MCP server. It gives you
full access to all 23 OAS tools (create_surface, run_aero_analysis, etc.)
without needing an MCP connection. Use it whenever you're working in Claude
Code or any shell-based environment.

## Prerequisites — installing the entry point

`oas-cli` is a console_scripts entry point registered in `pyproject.toml`. It
is **only** available after `pip install` or `uv pip install` of the package:

```bash
# If oas-cli is not found, install the package first:
uv pip install -e ".[mcp]"   # or: pip install -e ".[mcp]"

# Verify:
oas-cli list-tools
```

If you get `command not found`, the virtualenv is not activated or the package
was not installed. You can also invoke via `python -m oas_mcp.cli <args>`.

## Global flags come BEFORE the subcommand

`--pretty`, `--workspace`, and `--output` are parser-level flags. They must
appear **before** the subcommand name, not after it:

```bash
# Correct:
oas-cli --pretty run-aero-analysis --surfaces '["wing"]' --alpha 5

# WRONG — argparse will reject this:
oas-cli run-aero-analysis --pretty --surfaces '["wing"]' --alpha 5
```

## Flag names preserve Python parameter case

Only underscores become hyphens; capitalisation is kept verbatim from the
function signature. So `Mach_number` → `--Mach-number`, `CD0` → `--CD0`,
`CL0` → `--CL0`. When in doubt, run `oas-cli <subcommand> --help`.

## Choosing a mode

| Situation | Best mode |
|-----------|-----------|
| Multiple related analyses in one session | **Interactive** — in-memory state, fastest |
| Quick one-off check from the terminal | **One-shot** — one subcommand per tool call |
| Reproducible workflow to hand off / re-run | **Script** — JSON file, single process |

---

## Mode 1 — Interactive (JSON-lines subprocess)

Spawn a single `oas-cli interactive` process, write JSON commands to its
stdin, and read JSON responses from its stdout. All state (surfaces, cached
OpenMDAO problems) lives in memory for the lifetime of the process — no
rebuilding between calls.

### Protocol

Every request: one JSON object per line on stdin.
```json
{"tool": "<tool_name>", "args": {<keyword args>}}
```

Every response: one JSON object per line on stdout.
```json
{"ok": true, "result": { ... }}
{"ok": false, "error": {"code": "USER_INPUT_ERROR", "message": "..."}}
```

### Example — full aero workflow

```python
import subprocess, json

proc = subprocess.Popen(
    ["oas-cli", "interactive"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    text=True,
    bufsize=1,          # line-buffered
)

def call(tool, **args):
    proc.stdin.write(json.dumps({"tool": tool, "args": args}) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    resp = json.loads(line)
    if not resp["ok"]:
        raise RuntimeError(resp["error"]["message"])
    return resp["result"]

# Step 1: define the wing
call("create_surface", name="wing", wing_type="CRM", num_y=7, symmetry=True,
     with_viscous=True, CD0=0.015)

# Step 2: single-point aero analysis
result = call("run_aero_analysis", surfaces=["wing"], alpha=5.0,
              velocity=248.136, Mach_number=0.84, density=0.38)
print(result["results"]["CL"], result["results"]["L_over_D"])

# Step 3: drag polar sweep
polar = call("compute_drag_polar", surfaces=["wing"],
             alpha_start=-5.0, alpha_end=15.0, num_alpha=21)
print(polar["results"]["best_L_over_D"])

proc.stdin.close()
proc.wait()
```

### Tips for interactive mode

- The process caches the OpenMDAO problem after the first analysis. Subsequent
  calls with the same surface names are much faster (~0.01 s vs ~0.1 s).
- Calling `create_surface` again with the same name invalidates the cache.
- Call `{"tool": "reset", "args": {}}` to clear all session state.
- If you don't need Python, pipe newline-separated JSON directly:

```bash
printf '{"tool":"create_surface","args":{"name":"wing","num_y":7}}\n
{"tool":"run_aero_analysis","args":{"surfaces":["wing"],"alpha":5}}\n' \
  | oas-cli interactive
```

---

## Mode 2 — One-shot subcommands

Each invocation is a standalone process. Surface definitions are persisted to
`~/.oas_mcp/state/<workspace>.json` so multi-step workflows work across calls.

### Naming convention

Tool names use underscores; subcommands use hyphens:
`run_aero_analysis` → `oas-cli run-aero-analysis`

### Parameter types

| Python type | CLI form |
|-------------|----------|
| `str`, `int`, `float` | `--name value` |
| `bool` | `--flag` / `--no-flag` |
| `list`, `dict`, complex | `--param '[1,2,3]'` (JSON string) |

### Example — multi-step one-shot workflow

```bash
# Step 1: create surface (args saved to ~/.oas_mcp/state/default.json)
oas-cli create-surface --name wing --wing-type CRM --num-y 7 \
        --symmetry --with-viscous --CD0 0.015

# Step 2: run analysis (surface loaded from state file automatically)
oas-cli run-aero-analysis --surfaces '["wing"]' --alpha 5.0 \
        --velocity 248.136 --Mach-number 0.84 --density 0.38

# Step 3: optimization
oas-cli run-optimization \
  --surfaces '["wing"]' \
  --analysis-type aero \
  --design-variables '[{"name":"twist","lower":-10,"upper":10,"n_cp":3},{"name":"alpha","lower":-5,"upper":15}]' \
  --constraints '[{"name":"CL","equals":0.5}]' \
  --objective CD

# Use a named workspace to isolate state from other workflows
oas-cli create-surface --name wing --num-y 7 --workspace myproject
oas-cli run-aero-analysis --surfaces '["wing"]' --alpha 5 --workspace myproject

# Clear state when done
oas-cli reset --workspace myproject   # or: oas-cli reset
```

### Useful flags

| Flag | Effect |
|------|--------|
| `--pretty` | Indent JSON output for readability |
| `--workspace NAME` | Namespace for state file (default: "default") |
| `--output FILE` | Write response to file instead of stdout |

### Parsing output in bash

```bash
# Extract a field with python (available everywhere)
CL=$(oas-cli run-aero-analysis --surfaces '["wing"]' --alpha 5 \
     | python -c "import sys,json; d=json.load(sys.stdin); print(d['result']['results']['CL'])")

# Or with jq if installed
oas-cli run-aero-analysis --surfaces '["wing"]' --alpha 5 --pretty \
  | jq '.result.results.CL'
```

### Important: one-shot mode rebuilds the OpenMDAO problem each invocation

The surface dict is persisted; the compiled OpenMDAO problem is not. Each call
to an analysis tool costs ~0.1 s for problem setup. This is usually negligible
compared to analysis time but matters for tight loops — use interactive mode
for sweeps.

---

## Mode 3 — Script / batch

Write a JSON array of tool calls, execute in one process. State is shared
across all steps in memory.

### Workflow file format

```json
[
  {"tool": "create_surface", "args": {"name": "wing", "wing_type": "CRM",
                                       "num_y": 7, "symmetry": true,
                                       "with_viscous": true}},
  {"tool": "run_aero_analysis", "args": {"surfaces": ["wing"], "alpha": 5.0,
                                          "Mach_number": 0.84}},
  {"tool": "compute_drag_polar", "args": {"surfaces": ["wing"],
                                           "alpha_start": -5, "alpha_end": 15,
                                           "num_alpha": 21}}
]
```

```bash
oas-cli run-script workflow.json
oas-cli run-script workflow.json --pretty --output results.json
```

Each step's result is printed as a JSON line as it completes. With `--output`,
all results are collected and written to a single file at the end.

---

## Complete workflow examples

### Aero-only optimization (minimize CD at fixed CL)

```json
[
  {"tool": "create_surface", "args": {
    "name": "wing", "wing_type": "CRM", "num_y": 7,
    "symmetry": true, "with_viscous": true, "CD0": 0.015
  }},
  {"tool": "run_optimization", "args": {
    "surfaces": ["wing"],
    "analysis_type": "aero",
    "objective": "CD",
    "design_variables": [
      {"name": "twist", "lower": -10, "upper": 10, "n_cp": 3},
      {"name": "alpha", "lower": -5,  "upper": 15}
    ],
    "constraints": [{"name": "CL", "equals": 0.5}],
    "Mach_number": 0.84, "density": 0.38, "velocity": 248.136
  }}
]
```

### Aerostructural analysis (tube spar)

```json
[
  {"tool": "create_surface", "args": {
    "name": "wing", "wing_type": "CRM", "num_y": 7, "symmetry": true,
    "fem_model_type": "tube",
    "thickness_cp": [0.05, 0.08, 0.05],
    "E": 70e9, "G": 30e9, "yield_stress": 500e6, "mrho": 3000.0
  }},
  {"tool": "run_aerostruct_analysis", "args": {
    "surfaces": ["wing"],
    "velocity": 248.136, "Mach_number": 0.84,
    "density": 0.38, "alpha": 5.0,
    "W0": 120000, "R": 11.165e6, "speed_of_sound": 295.4,
    "load_factor": 1.0
  }}
]
```

### Retrieving and visualizing a past run

```bash
# One-shot: get artifact metadata
oas-cli get-run --run-id 20240315T143022_a1b2

# Visualize lift distribution (returns base64 PNG in JSON)
oas-cli visualize --run-id 20240315T143022_a1b2 --plot-type lift_distribution \
        --output lift_plot.json
```

---

## Structural tools — required surface parameters

If you need `run_aerostruct_analysis` or an aerostruct optimization, the
surface **must** include:

- `fem_model_type`: `"tube"` or `"wingbox"`
- `E`, `G`, `yield_stress`, `mrho` (material properties)
- For tube: `thickness_cp` (list of control-point values in metres)
- For wingbox: `spar_thickness_cp` and `skin_thickness_cp`

Omitting these will produce a `USER_INPUT_ERROR`.

---

## Error handling

All responses follow the same envelope:

```json
{"ok": true,  "result": { ... }}
{"ok": false, "error": {"code": "USER_INPUT_ERROR", "message": "..."}}
```

Error codes and what to do:

| Code | Cause | Fix |
|------|-------|-----|
| `USER_INPUT_ERROR` | Bad param values, missing surface, bad JSON | Check parameter values and surface existence |
| `SOLVER_CONVERGENCE_ERROR` | OpenMDAO solver failed | Coarser mesh (`num_y=5`), lower Mach, adjust alpha |
| `CACHE_EVICTED_ERROR` | Cached problem was cleared | Call `create_surface` again, then rerun |
| `INTERNAL_ERROR` | Bug in OAS/MCP code | Surface to the user; do not auto-retry |

In interactive mode (Python), check `resp["ok"]` before using `resp["result"]`.
In one-shot mode, a non-zero exit code signals failure — the JSON error is on
stdout.

---

## Quick reference — key parameter defaults

```
velocity=248.136 m/s   # cruise
Mach_number=0.84
density=0.38 kg/m³
reynolds_number=1e6
alpha=5.0 degrees

num_x=2, num_y=7       # fast mesh (num_y must be ODD)
wing_type="CRM"        # realistic transport wing
symmetry=True          # model half-span
```

## Available tools

```
oas-cli list-tools
```

Key tools: `create_surface`, `run_aero_analysis`, `run_aerostruct_analysis`,
`compute_drag_polar`, `compute_stability_derivatives`, `run_optimization`,
`visualize`, `get_run`, `list_artifacts`, `reset`, `start_session`,
`log_decision`, `export_session_graph`
