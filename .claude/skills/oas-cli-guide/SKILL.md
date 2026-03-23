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

`--pretty`, `--workspace`, and `--save-to` are parser-level flags. They must
appear **before** the subcommand name, not after it:

```bash
# Correct:
oas-cli --pretty run-aero-analysis --surfaces '["wing"]' --alpha 5

# WRONG — argparse will reject this:
oas-cli run-aero-analysis --pretty --surfaces '["wing"]' --alpha 5
```

### Global flags reference

| Flag | Effect |
|------|--------|
| `--pretty` | Indent JSON output for readability |
| `--workspace NAME` | Namespace for one-shot state file (default: "default") |
| `--save-to FILE` | Write JSON response to FILE instead of stdout |

**Important**: `--save-to` writes the full JSON response to a file. This is
different from the `visualize` tool's `--output` parameter, which controls the
visualization rendering mode (`inline`/`file`/`url`). Don't confuse the two.

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

### run_id chaining in interactive mode

The CLI tracks the `run_id` from the most recent successful analysis. You can
use `"latest"` or `"last"` as the run_id value in any tool that accepts one
(e.g. `visualize`, `get_run`, `get_detailed_results`), and it will be resolved
automatically to the last run_id seen in this session.

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
              velocity=248.136, Mach_number=0.84, density=0.38,
              reynolds_number=1e6)
print(result["results"]["CL"], result["results"]["L_over_D"])

# Step 3: visualize using "latest" — no need to track run_id manually
viz = call("visualize", run_id="latest", plot_type="lift_distribution",
           output="file")
print(viz[0]["file_path"])   # visualize returns a list, not a dict

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
{"tool":"run_aero_analysis","args":{"surfaces":["wing"],"alpha":5,"velocity":50,"Mach_number":0.3,"density":1.225,"reynolds_number":1e6}}\n
{"tool":"visualize","args":{"run_id":"latest","plot_type":"lift_distribution","output":"file"}}\n' \
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
        --velocity 248.136 --Mach-number 0.84 --density 0.38 \
        --reynolds-number 1e6

# Step 3: visualize using "latest" — resolves to the most recent run
oas-cli visualize --run-id latest --plot-type lift_distribution --output file

# Step 4: optimization
oas-cli run-optimization \
  --surfaces '["wing"]' \
  --analysis-type aero \
  --design-variables '[{"name":"twist","lower":-10,"upper":10,"n_cp":3},{"name":"alpha","lower":-5,"upper":15}]' \
  --constraints '[{"name":"CL","equals":0.5}]' \
  --objective CD

# Use a named workspace to isolate state from other workflows
oas-cli --workspace myproject create-surface --name wing --num-y 7
oas-cli --workspace myproject run-aero-analysis --surfaces '["wing"]' --alpha 5

# Clear state when done
oas-cli reset --workspace myproject   # or: oas-cli reset
```

### Extracting run_id in bash

When chaining one-shot commands that need a specific run_id (instead of
"latest"), extract it from the JSON response:

```bash
# Extract run_id with python
RUN_ID=$(oas-cli run-aero-analysis --surfaces '["wing"]' --alpha 5 \
         --velocity 248 --Mach-number 0.84 --density 0.38 --reynolds-number 1e6 \
  | python -c "import sys,json; print(json.load(sys.stdin)['result']['run_id'])")

# Use the extracted run_id
oas-cli visualize --run-id "$RUN_ID" --plot-type lift_distribution --output file

# Or with jq if installed
oas-cli run-aero-analysis --surfaces '["wing"]' --alpha 5 --pretty \
  | jq -r '.result.run_id'
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

### run_id interpolation in scripts

Scripts support variable references so you can chain create→analyze→visualize
in a single self-contained file:

- `"$prev.run_id"` → run_id from the most recent successful step
- `"$2.run_id"` → run_id from step 2 (1-indexed)

### Workflow file format

```json
[
  {"tool": "create_surface", "args": {"name": "wing", "wing_type": "CRM",
                                       "num_y": 7, "symmetry": true,
                                       "with_viscous": true}},
  {"tool": "run_aero_analysis", "args": {"surfaces": ["wing"], "alpha": 5.0,
                                          "Mach_number": 0.84, "velocity": 248.136,
                                          "density": 0.38, "reynolds_number": 1e6}},
  {"tool": "visualize", "args": {"run_id": "$prev.run_id",
                                  "plot_type": "lift_distribution",
                                  "output": "file"}}
]
```

```bash
oas-cli run-script workflow.json
oas-cli --pretty --save-to results.json run-script workflow.json
```

Each step's result is printed as a JSON line as it completes. With `--save-to`,
all results are collected and written to a single file at the end.

---

## Convenience commands

These shorthand commands simplify common workflows:

### list-runs — browse recent analysis runs

```bash
oas-cli list-runs                      # show last 10 runs
oas-cli list-runs --limit 5            # show last 5 runs
oas-cli list-runs --analysis-type aero # filter by type
```

### show — quick summary of a run

```bash
oas-cli show                  # show latest run (default)
oas-cli show latest           # same thing
oas-cli show 20240315T143022_a1b2c3   # specific run
```

### plot — save a plot PNG to disk

```bash
oas-cli plot latest lift_distribution              # saves to auto-named file
oas-cli plot latest drag_polar -o polar.png        # custom output path
oas-cli plot 20240315T143022_a1b2c3 stress_distribution
```

---

## Visualization output modes

The `visualize` tool supports three output modes via its `--output` parameter:

| Mode | Behaviour | Best for |
|------|-----------|----------|
| `inline` | Returns `[metadata, ImageContent]` — base64 PNG in JSON | claude.ai |
| `file` | Saves PNG to disk, returns `[metadata]` with `file_path` | CLI / scripts |
| `url` | Returns `[metadata]` with `dashboard_url` and `plot_url` | Remote / VPS |

**Important**: `visualize` returns a **list**, not a dict. The first element is
always a metadata dict. The second element (if present) is the image content.
```python
result = call("visualize", run_id="latest", plot_type="lift_distribution", output="file")
# result is a list: [{"plot_type": "...", "file_path": "/path/to/plot.png", ...}]
file_path = result[0]["file_path"]
```

Set a session default to avoid passing `--output` every time:
```bash
oas-cli configure-session --visualization-output file
```

### Viewer dashboard

The MCP server starts an HTTP viewer on port 7654 (when running via stdio
transport). Access it at:
- Dashboard: `http://localhost:7654/dashboard?run_id=<id>`
- Provenance: `http://localhost:7654/viewer?session_id=<id>`

The `visualize(..., output="url")` mode returns clickable links to these.

---

## Provenance — recording decisions and tracing workflows

The CLI has built-in provenance recording: every tool call is automatically
logged to a SQLite database (`~/.oas_provenance/sessions.db`). Three additional
tools let you group calls into named sessions, record reasoning, and export
the full DAG.

### When to use provenance

**Always** call `start_session` at the beginning of a multi-step workflow in
interactive or script mode. Use `log_decision` before major choices (mesh
resolution, design variables, interpreting surprising results). Call
`export_session_graph` at the end to save the audit trail.

### The three provenance tools

| Tool | Purpose |
|------|---------|
| `start_session` | Begin a named session — groups all subsequent calls |
| `log_decision` | Record why a choice was made (DV selection, mesh, etc.) |
| `export_session_graph` | Export the session DAG as JSON |

### Decision types

Use these standard `decision_type` values with `log_decision`:

| `decision_type` | When to use |
|-----------------|-------------|
| `mesh_resolution` | Choosing `num_x` / `num_y` |
| `dv_selection` | Choosing design variables and their bounds |
| `constraint_choice` | Choosing optimization constraints |
| `result_interpretation` | Explaining what a result means and next steps |
| `convergence_assessment` | Assessing whether an optimizer converged |

### Chaining prior_call_id

Every successful tool call returns a `_provenance` field in its result dict:
```json
{"ok": true, "result": {"CL": 0.5, ..., "_provenance": {"call_id": "uuid-...", "session_id": "sess-..."}}}
```

Pass this `call_id` as `prior_call_id` in `log_decision` to create a causal
link between the analysis result and your decision. This makes the provenance
graph show *which result informed which decision*.

### Interactive mode example (Python)

```python
# Start session
sess = call("start_session", notes="CRM drag study")

# Create surface
call("create_surface", name="wing", wing_type="CRM", num_y=7,
     symmetry=True, with_viscous=True, CD0=0.015)

# Log mesh decision
call("log_decision",
     decision_type="mesh_resolution",
     reasoning="num_y=7 for fast iteration; will refine later",
     selected_action="num_y=7")

# Run analysis
result = call("run_aero_analysis", surfaces=["wing"], alpha=5.0,
              velocity=248.136, Mach_number=0.84, density=0.38,
              reynolds_number=1e6)

# Log interpretation, linking to the analysis call_id
call("log_decision",
     decision_type="result_interpretation",
     reasoning=f"CL={result['results']['CL']:.3f}, L/D={result['results']['L_over_D']:.1f} — reasonable",
     selected_action="proceed to optimization",
     prior_call_id=result["_provenance"]["call_id"])

# Export the graph
graph = call("export_session_graph", output_path="study_provenance.json")
```

### Script mode example with provenance

```json
[
  {"tool": "start_session", "args": {"notes": "CRM aero optimization"}},
  {"tool": "create_surface", "args": {
    "name": "wing", "wing_type": "CRM", "num_y": 7,
    "symmetry": true, "with_viscous": true, "CD0": 0.015
  }},
  {"tool": "log_decision", "args": {
    "decision_type": "dv_selection",
    "reasoning": "Twist and alpha give best L/D improvement for aero-only",
    "selected_action": "twist (3 cp, -10..10), alpha (-5..15)",
    "confidence": "high"
  }},
  {"tool": "run_optimization", "args": {
    "surfaces": ["wing"], "analysis_type": "aero", "objective": "CD",
    "design_variables": [
      {"name": "twist", "lower": -10, "upper": 10, "n_cp": 3},
      {"name": "alpha", "lower": -5, "upper": 15}
    ],
    "constraints": [{"name": "CL", "equals": 0.5}],
    "Mach_number": 0.84, "density": 0.38, "velocity": 248.136,
    "reynolds_number": 1e6
  }},
  {"tool": "visualize", "args": {
    "run_id": "$prev.run_id", "plot_type": "opt_history", "output": "file"
  }},
  {"tool": "export_session_graph", "args": {"output_path": "provenance.json"}}
]
```

Note: in script mode you cannot pass `prior_call_id` referencing a previous
step's `_provenance.call_id` because there's no interpolation for nested
fields. The automatic call recording still captures the full sequence; explicit
`prior_call_id` links are only possible in interactive mode (Python) where you
can extract the value from the response dict.

### One-shot mode limitation

Each one-shot invocation is a separate process, so `start_session` in one call
does not carry over to the next. All calls are still recorded in the provenance
DB under session `"default"`, but they won't be grouped into a named session.
**Use interactive or script mode for provenance-tracked workflows.**

### Viewing the provenance graph

- **Browser**: Open `http://localhost:7654/viewer?session_id=<id>` (viewer
  server starts automatically with the MCP server)
- **Offline**: Open `oas_mcp/provenance/viewer/index.html` and drop the
  exported JSON file onto the page

---

## Complete workflow examples

### Aero-only optimization (minimize CD at fixed CL)

```json
[
  {"tool": "start_session", "args": {"notes": "Aero optimization — min CD at CL=0.5"}},
  {"tool": "create_surface", "args": {
    "name": "wing", "wing_type": "CRM", "num_y": 7,
    "symmetry": true, "with_viscous": true, "CD0": 0.015
  }},
  {"tool": "log_decision", "args": {
    "decision_type": "dv_selection",
    "reasoning": "Twist + alpha for aero-only min-drag at fixed lift",
    "selected_action": "twist (3 cp), alpha",
    "confidence": "high"
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
    "Mach_number": 0.84, "density": 0.38, "velocity": 248.136,
    "reynolds_number": 1e6
  }},
  {"tool": "visualize", "args": {
    "run_id": "$prev.run_id",
    "plot_type": "opt_history",
    "output": "file"
  }},
  {"tool": "export_session_graph", "args": {"output_path": "aero_opt_provenance.json"}}
]
```

### Aerostructural analysis (tube spar)

```json
[
  {"tool": "start_session", "args": {"notes": "CRM aerostruct baseline"}},
  {"tool": "create_surface", "args": {
    "name": "wing", "wing_type": "CRM", "num_y": 7, "symmetry": true,
    "fem_model_type": "tube",
    "thickness_cp": [0.05, 0.08, 0.05],
    "E": 70e9, "G": 30e9, "yield_stress": 500e6, "mrho": 3000.0
  }},
  {"tool": "run_aerostruct_analysis", "args": {
    "surfaces": ["wing"],
    "velocity": 248.136, "Mach_number": 0.84,
    "density": 0.38, "alpha": 5.0, "reynolds_number": 1e6,
    "W0": 120000, "R": 11.165e6, "speed_of_sound": 295.4,
    "load_factor": 1.0
  }},
  {"tool": "visualize", "args": {
    "run_id": "$prev.run_id",
    "plot_type": "stress_distribution",
    "output": "file"
  }},
  {"tool": "export_session_graph", "args": {"output_path": "aerostruct_provenance.json"}}
]
```

### Incompressible analysis (Mach = 0)

OAS supports `Mach_number=0` for incompressible flow:
```bash
oas-cli create-surface --name wing --wing-type rect --num-y 7 --symmetry
oas-cli run-aero-analysis --surfaces '["wing"]' --alpha 5.0 \
        --velocity 50 --Mach-number 0 --density 1.225 --reynolds-number 1e6
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

**Important**: Most tools return `result` as a **dict**, but `visualize`
returns `result` as a **list** (`[metadata_dict]` or `[metadata_dict,
image_dict]`). Always check the type before accessing fields.

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
density=0.38 kg/m^3
reynolds_number=1e6    # REQUIRED — server rejects if omitted
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

Convenience commands: `list-runs`, `show`, `plot`
