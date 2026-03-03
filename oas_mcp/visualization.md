# OAS MCP Server — Visualization Guide

The server generates publication-quality 900×540 px plots from any completed run and returns them as base64-encoded PNGs. Plots are generated from data persisted at run time — most plots do not require the live OpenMDAO problem to still be in memory.

## Contents

- [Quick start](#quick-start)
- [Available plot types](#available-plot-types)
- [Step-by-step workflow](#step-by-step-workflow)
- [Auto-visualization](#auto-visualization)
- [Progressive zoom workflow](#progressive-zoom-workflow)
- [Response format](#response-format)
- [Client-side image caching](#client-side-image-caching)
- [Checking available plots before calling](#checking-available-plots-before-calling)

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
| `aero` (single-point) | `lift_distribution`, `planform` |
| `aerostruct` (single-point) | `lift_distribution`, `stress_distribution`, `planform` |
| `drag_polar` | `drag_polar`, `planform` |
| `stability` | `planform` |
| Any with convergence data | `convergence` |
