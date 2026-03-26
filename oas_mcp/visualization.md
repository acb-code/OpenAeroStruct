# OAS MCP Server — Visualization Guide

The server generates publication-quality 900×540 px plots from any completed run and returns them as base64-encoded PNGs. Plots are generated from data persisted at run time — most plots do not require the live OpenMDAO problem to still be in memory.

## Contents

- [Quick start](#quick-start)
- [Output modes](#output-modes)
- [Available plot types](#available-plot-types)
- [Step-by-step workflow](#step-by-step-workflow)
- [Auto-visualization](#auto-visualization)
- [Progressive zoom workflow](#progressive-zoom-workflow)
- [Response format](#response-format)
- [Client-side image caching](#client-side-image-caching)
- [Checking available plots before calling](#checking-available-plots-before-calling)
- [Dashboard](#dashboard)

---

## Quick start

```python
# 1. Run any analysis
envelope = run_aero_analysis(surfaces=["wing"], alpha=5.0)
run_id = envelope["run_id"]

# 2. Call visualize with any supported plot type
plot = visualize(run_id=run_id, plot_type="lift_distribution")

# 3. Use the base64 image
import base64
png_bytes = base64.b64decode(plot["image_base64"])
with open("lift_distribution.png", "wb") as f:
    f.write(png_bytes)
```

---

## Output modes

`visualize()` supports three output modes, controlled per-call or per-session. This is particularly useful in CLI environments (Claude Code, Codex) where MCP `ImageContent` renders as unhelpful `[image]` text.

### Per-session (set once, applies to all calls)

```python
configure_session(visualization_output="file")   # or "url" or "inline"
```

### Per-call (overrides session default)

```python
visualize(run_id=run_id, plot_type="lift_distribution", output="url")
```

### Mode reference

| Mode | `visualize()` returns | Best for |
|------|----------------------|----------|
| `"inline"` (default) | `[metadata, ImageContent]` | claude.ai — image renders natively |
| `"file"` | `[metadata]` with `file_path` | Claude Code (local) — PNG saved to disk, no `[image]` noise |
| `"url"` | `[metadata]` with `dashboard_url` + `plot_url` | Claude Code (VPS) — clickable links open in browser |

### File mode

PNGs are saved to `{OAS_DATA_DIR}/{user}/{project}/{session_id}/plots/{run_id}_{plot_type}.png`. The `file_path` key in the metadata points to the absolute path.

```python
configure_session(visualization_output="file")
result = visualize(run_id=run_id, plot_type="lift_distribution")
# result[0]["file_path"] → "/path/to/oas_data/.../plots/run123_lift_distribution.png"
```

### URL mode

Returns `dashboard_url` (a context-rich HTML page) and `plot_url` (direct PNG) using the server's public URL.

- **Local (stdio transport):** URLs point to `http://localhost:{OAS_PROV_PORT}` (default 7654)
- **VPS (HTTP transport):** URLs use `RESOURCE_SERVER_URL` (e.g. `https://mcp.lakesideai.dev`)

```python
configure_session(visualization_output="url")
result = visualize(run_id=run_id, plot_type="lift_distribution")
# result[0]["dashboard_url"] → "https://mcp.lakesideai.dev/dashboard?run_id=..."
# result[0]["plot_url"]      → "https://mcp.lakesideai.dev/plot?run_id=...&plot_type=lift_distribution"
```

---

## Available plot types

### `lift_distribution`

**What it shows:** Spanwise local lift coefficient (Cl) distribution as a bar chart with normalised spanwise position [0 = root, 1 = tip] on the x-axis.

**Requires:** Any `run_aero_analysis` or `run_aerostruct_analysis` run.

**Interpretation:**
- For a well-designed wing, Cl should decrease smoothly from root to tip.
- A peak near the root with a sharp drop can indicate the structure is over-loaded at the root.
- An elliptical distribution minimises induced drag.

```python
plot = visualize(run_id=run_id, plot_type="lift_distribution", case_name="CRM cruise α=5°")
```

---

### `drag_polar`

**What it shows:** Two-panel plot — CL vs CD (left) and L/D vs α (right) — for a full drag polar sweep.

**Requires:** A `compute_drag_polar` run. Fails (raises `ValueError`) for single-point runs.

**Interpretation:**
- The leftmost point of the CL-CD parabola is the zero-lift drag point.
- The tangent from the origin to the CL-CD curve identifies the best L/D operating point.
- L/D vs α peaks at the optimal cruise angle of attack.

```python
# Run a drag polar first
dp_envelope = compute_drag_polar(
    surfaces=["wing"], alpha_start=-5.0, alpha_end=15.0, num_alpha=11
)
plot = visualize(run_id=dp_envelope["run_id"], plot_type="drag_polar")
```

---

### `stress_distribution`

**What it shows:** Two-panel plot — von Mises stress (MPa) on the left and KS failure index on the right — both against normalised spanwise position.

**Requires:** A `run_aerostruct_analysis` run with sectional data captured.

**Interpretation:**
- Von Mises stress should peak near the wing root where bending moment is highest.
- The failure index threshold is 1.0 — values above this mean structural failure. A horizontal dashed line is drawn at 1.0 for reference.
- Negative failure index values indicate healthy structure with margin to spare.
- Values near 1.0 (e.g. 0.9) mean the structure is near its limit.

```python
struct_envelope = run_aerostruct_analysis(surfaces=["wing"], alpha=5.0, W0=120000)
plot = visualize(run_id=struct_envelope["run_id"], plot_type="stress_distribution")
```

---

### `convergence`

**What it shows:** Solver residual history — residual magnitude vs iteration number on a log scale.

**Requires:** Convergence data to have been captured for the run. Not all runs capture this (depends on solver configuration and `OAS_TELEMETRY_MODE`).

**Interpretation:**
- A well-converged solve shows residuals dropping by at least 6–8 orders of magnitude.
- A flat or slowly decreasing residual curve indicates the solver is struggling — try a different starting point or tighter tolerances.
- For optimization runs, the outer optimizer loop iterations are plotted alongside the inner linear solver.

```python
plot = visualize(run_id=run_id, plot_type="convergence")
```

---

### `planform`

**What it shows:** Top-down view of the wing planform with leading edge (LE) and trailing edge (TE) drawn, panel grid shown, and optional deflection overlay for aerostruct runs.

**Requires:** Mesh snapshot data persisted at run time (present for all runs).

**Interpretation:**
- Panel layout shows how chordwise (num_x) and spanwise (num_y) panels are distributed.
- For symmetric surfaces (symmetry=True), only the half-span is shown — the full span is mirrored about the y=0 axis.
- For aerostruct runs, the deformed mesh is overlaid in a contrasting colour if deflection data is available.

```python
plot = visualize(run_id=run_id, plot_type="planform", case_name="CRM wing — 7×2 mesh")
```

---

### `deflection_profile`

**What it shows:** Spanwise vertical deflection (z-displacement) as a line plot from root to tip.

**Requires:** A `run_aerostruct_analysis` run with structural deflection captured.

**Interpretation:**
- Deflection increases from root (zero) to tip (maximum).
- Large tip deflection relative to semi-span may indicate insufficient stiffness.
- Negative deflection at the root is physically unexpected and may indicate a modelling issue.

```python
plot = visualize(run_id=run_id, plot_type="deflection_profile")
```

---

### `weight_breakdown`

**What it shows:** Horizontal bar chart of per-surface structural mass (kg).

**Requires:** A `run_aerostruct_analysis` run with structural mass data.

**Interpretation:**
- Compares structural mass contributions across surfaces (e.g. wing vs tail).
- Bar annotations show exact mass values.

```python
plot = visualize(run_id=run_id, plot_type="weight_breakdown")
```

---

### `failure_heatmap`

**What it shows:** Wing planform coloured by failure index — green for safe regions, red for structurally failed regions (failure > 1.0).

**Requires:** A `run_aerostruct_analysis` run with failure_index and mesh data.

**Interpretation:**
- Green panels (failure < 1.0) are within structural limits.
- Red panels (failure >= 1.0) indicate structural failure at that spanwise station.
- The colorbar shows the full failure index range; a threshold line at 1.0 separates safe/failed.

```python
plot = visualize(run_id=run_id, plot_type="failure_heatmap")
```

---

### `twist_chord_overlay`

**What it shows:** Dual y-axis plot of twist (degrees, blue) and chord (metres, red) vs spanwise station.

**Requires:** Any run with mesh data (aero or aerostruct).

**Interpretation:**
- Twist distribution shows geometric washout/washin along the span.
- Chord distribution shows taper ratio and planform shape.
- Useful for verifying that geometry parameters were applied correctly.

```python
plot = visualize(run_id=run_id, plot_type="twist_chord_overlay")
```

---

### `mesh_3d`

**What it shows:** 3D isometric wireframe of the wing mesh with optional structural FEM elements and deflection overlay.

**Requires:** Any run with mesh data. For full visualization:
- **Wireframe only**: Any `run_aero_analysis` or `run_aerostruct_analysis` run.
- **Structural tubes/wingbox**: Requires `create_surface` with `fem_model_type="tube"` (or `"wingbox"`) plus material properties (`E`, `G`, `yield_stress`, `mrho`), then `run_aerostruct_analysis`. The coloured elements along the spar show thickness variation (viridis colormap).
- **Deflection overlay**: Requires `run_aerostruct_analysis` (not aero-only). Shows deformed mesh in black and undeformed in light gray, with 2x exaggeration.

**Interpretation:**
- Black wireframe shows the panel grid in 3D.
- Coloured cylinders (tube) or panels (wingbox) show structural elements along the spar, coloured by thickness — yellow = thickest, green = thinnest.
- For aerostruct: deformed mesh in black, undeformed in light gray.
- Deflection is exaggerated (default 2x) for visibility.

**Example — full structural 3D plot:**
```python
# 1. Create surface with structural properties
create_surface(
    name="wing", wing_type="CRM", num_x=3, num_y=7, symmetry=True,
    fem_model_type="tube",
    thickness_cp=[0.04, 0.06, 0.08, 0.06, 0.04],
    E=70e9, G=30e9, yield_stress=500e6, mrho=3000.0,
)

# 2. Run aerostruct analysis (NOT aero-only — needed for deflection + structure)
envelope = run_aerostruct_analysis(
    surfaces=["wing"], alpha=5.0,
    velocity=248.136, Mach_number=0.84, density=0.38, reynolds_number=1e6,
    W0=120000, speed_of_sound=295.4, load_factor=1.0,
)

# 3. Visualize — will show wireframe + tube structure + deflection overlay
plot = visualize(run_id=envelope["run_id"], plot_type="mesh_3d")
```

---

### `multipoint_comparison`

**What it shows:** 2x2 subplot grid comparing cruise vs maneuver flight points — CL/CD bars, failure index, deflection, and a summary table.

**Requires:** A multipoint `run_optimization` result with at least 2 flight points.

**Interpretation:**
- Maneuver point typically shows higher CL, higher failure, and larger deflection than cruise.
- The summary table provides a quick numeric comparison across points.

```python
plot = visualize(run_id=run_id, plot_type="multipoint_comparison")
```

---

## Step-by-step workflow

### Single-point aerodynamic analysis

```python
# 1. Create surface
create_surface(
    name="wing", wing_type="CRM",
    num_x=2, num_y=7, symmetry=True,
    with_viscous=True, CD0=0.015,
)

# 2. Run analysis
envelope = run_aero_analysis(surfaces=["wing"], alpha=5.0, Mach_number=0.84)
run_id = envelope["run_id"]

# 3. Check what plots are available
manifest = get_run(run_id=run_id)
print(manifest["available_plots"])   # ['lift_distribution', 'planform']

# 4. Generate plots
lift_plot = visualize(run_id=run_id, plot_type="lift_distribution", case_name="CRM α=5°")
planform_plot = visualize(run_id=run_id, plot_type="planform")
```

### Drag polar sweep

```python
# 1. Create surface (as above)
# 2. Run drag polar
dp_envelope = compute_drag_polar(
    surfaces=["wing"], alpha_start=-5.0, alpha_end=15.0, num_alpha=11,
    Mach_number=0.84, density=0.38,
)
dp_run_id = dp_envelope["run_id"]

# 3. Visualize polar
polar_plot = visualize(run_id=dp_run_id, plot_type="drag_polar", case_name="CRM cruise sweep")

# 4. Best L/D from results
best = dp_envelope["results"]["best_L_over_D"]
print(f"Best L/D: {best['L_over_D']:.1f} at α={best['alpha_deg']:.1f}°")
```

### Aerostructural analysis with stress plot

```python
# 1. Create surface with structural model
create_surface(
    name="wing", wing_type="CRM",
    num_x=2, num_y=7, symmetry=True,
    fem_model_type="tube",
    E=70e9, G=30e9, yield_stress=500e6, safety_factor=2.5, mrho=3000.0,
)

# 2. Run aerostructural analysis
struct_envelope = run_aerostruct_analysis(
    surfaces=["wing"],
    alpha=5.0, Mach_number=0.84, density=0.38,
    W0=120000, R=11.165e6,
)
struct_run_id = struct_envelope["run_id"]

# 3. Check structural health from validation
validation = struct_envelope["validation"]
if validation["passed"]:
    print("Structure OK")
else:
    for f in validation["findings"]:
        print(f"[{f['severity']}] {f['message']}")

# 4. Stress distribution plot
stress_plot = visualize(run_id=struct_run_id, plot_type="stress_distribution")

# 5. Spanwise lift distribution
lift_plot = visualize(run_id=struct_run_id, plot_type="lift_distribution")
```

---

## Auto-visualization

Use `configure_session` to automatically generate plots after every analysis without calling `visualize` separately:

```python
configure_session(
    session_id="default",
    auto_visualize=["lift_distribution", "drag_polar"],
)

# Now every analysis response includes auto_plots:
envelope = run_aero_analysis(surfaces=["wing"], alpha=5.0)

# envelope["auto_plots"] is present when auto_visualize is configured:
# {
#   "lift_distribution": { "plot_type": "lift_distribution", "image_base64": "...", ... }
# }
```

**Notes:**
- Only applicable plot types are generated — a `drag_polar` auto-plot will only appear for `compute_drag_polar` runs.
- Generating plots adds a small amount of time to each response. Keep `auto_visualize` to 1–2 types for performance-sensitive sweeps.
- Disable by passing `auto_visualize=[]` to `configure_session`.

---

## Progressive zoom workflow

The server supports a "zoom" pattern where agents first get scalar summaries, then retrieve progressively more detail as needed:

```
Level 0 — Analysis response     CL=0.546, CD=0.037, L/D=14.9, validation=passed
     ↓
Level 1 — get_run(run_id)       available_plots=['lift_distribution','planform']
     ↓
Level 2 — get_detailed_results  sectional Cl[7], vonmises[7], mesh coordinates
     ↓
Level 3 — visualize             900×540 px PNG, image_hash for caching
```

This flow minimises data transfer — most tool calls return only scalar summaries. Detailed data is fetched only when the agent (or engineer) decides it's needed.

```python
# Level 0 — scalar summary
envelope = run_aerostruct_analysis(surfaces=["wing"], alpha=5.0, W0=120000)
run_id = envelope["run_id"]
print(f"CL={envelope['results']['CL']:.3f}, failure={envelope['results']['surfaces']['wing']['failure']:.3f}")

# Level 1 — check what's available
manifest = get_run(run_id)
if "stress_distribution" in manifest["available_plots"]:

    # Level 2 — get numerical sectional data
    details = get_detailed_results(run_id, detail_level="standard")
    max_stress = max(details["sectional_data"]["wing"].get("vonmises_MPa", [0]))
    print(f"Peak von Mises stress: {max_stress:.1f} MPa")

    # Level 3 — generate plot only if stress is near limit
    if max_stress > 400:
        stress_plot = visualize(run_id, plot_type="stress_distribution", case_name="Near limit!")
```

---

## Response format

`visualize()` returns:

```json
{
  "plot_type": "lift_distribution",
  "run_id": "20260302T143022_a7f3",
  "format": "png",
  "width_px": 900,
  "height_px": 540,
  "image_hash": "a3f7c1b2",
  "image_base64": "iVBORw0KGgoAAAANSUhEUgAAA..."
}
```

| Field | Description |
|-------|-------------|
| `plot_type` | Which plot type was generated |
| `run_id` | Run ID this plot was generated from |
| `format` | Always `"png"` |
| `width_px`, `height_px` | Always 900×540 (6 in × 3.6 in at 150 DPI) |
| `image_hash` | SHA-256 first 8 hex chars of the PNG bytes — use for client-side deduplication |
| `image_base64` | Standard base64-encoded PNG |

### Decoding and displaying

**Python:**

```python
import base64
png_bytes = base64.b64decode(plot["image_base64"])

# Save to file
with open("plot.png", "wb") as f:
    f.write(png_bytes)

# Display in Jupyter
from IPython.display import Image, display
display(Image(png_bytes))
```

**HTML:**

```html
<img src="data:image/png;base64,{{ plot.image_base64 }}" width="900" height="540" />
```

**JavaScript:**

```javascript
const img = document.createElement('img');
img.src = `data:image/png;base64,${plot.image_base64}`;
img.width = 900;
img.height = 540;
document.body.appendChild(img);
```

---

## Client-side image caching

`image_hash` contains the first 8 hex characters of `sha256(png_bytes)`. Use this to avoid re-rendering plots that haven't changed:

```python
cache = {}  # { (run_id, plot_type): image_hash }

def get_or_refresh_plot(run_id, plot_type):
    key = (run_id, plot_type)
    plot = visualize(run_id=run_id, plot_type=plot_type)
    if cache.get(key) == plot["image_hash"]:
        return None  # same as before, no re-render needed
    cache[key] = plot["image_hash"]
    return plot  # new image
```

For a given `run_id`, plots are deterministic — calling `visualize` twice with the same parameters always returns the same image and therefore the same hash.

---

## Checking available plots before calling

Call `get_run(run_id)` first to see which plots are available. Calling `visualize` with an unsupported plot type for a given run returns a `ValueError`.

```python
manifest = get_run(run_id)
available = manifest["available_plots"]
# e.g. ['lift_distribution', 'planform', 'stress_distribution']

for plot_type in available:
    plot = visualize(run_id=run_id, plot_type=plot_type)
    # save or display …
```

### Which runs support which plots

| Analysis type | Supported plot types |
|---------------|---------------------|
| `aero` (single-point) | `lift_distribution`, `planform`, `mesh_3d`, `twist_chord_overlay` |
| `aerostruct` (single-point) | `lift_distribution`, `stress_distribution`, `planform`, `mesh_3d`, `failure_heatmap`, `deflection_profile`, `twist_chord_overlay`, `weight_breakdown` |
| `drag_polar` | `drag_polar`, `planform` |
| `stability` | `planform` |
| `optimization` | `opt_history`, `opt_dv_evolution`, `opt_comparison`, `planform`, `mesh_3d`, `twist_chord_overlay` |
| Any with convergence data | `convergence` |
| Multipoint optimization | `multipoint_comparison` (in addition to optimization plots) |

---

## Dashboard

The `/dashboard?run_id=<id>` HTTP endpoint provides a context-rich HTML page for any saved run. It includes:

- **Header** — analysis type, surface names, run_name label, run_id, timestamp
- **Flight conditions** — velocity, Mach, density, Re, alpha
- **Key results** — CL, CD, L/D, weight, failure index
- **Validation status** — pass/fail badge with findings details
- **Plots panel** — all applicable plot types rendered as PNG images
- **Provenance link** — link to `/viewer?session_id=X` if session is active

### Access

| Transport | URL | Auth |
|-----------|-----|------|
| stdio (local) | `http://localhost:7654/dashboard?run_id=X` | None |
| HTTP (VPS) | `https://<host>/dashboard?run_id=X` | Basic Auth (`OAS_VIEWER_USER`/`OAS_VIEWER_PASSWORD`) |

The dashboard URL is included in `visualize()` metadata when `output="url"` is used.
