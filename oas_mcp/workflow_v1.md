# OAS MCP Workflow v1 — Transport Wing Analysis Walkthrough

This document walks through a complete analysis session as it would actually look
in Claude Desktop.  The scenario is the preliminary aerodynamic and structural
characterisation of a single-aisle transport wing — similar in concept to an A320
or 737 class aircraft.

Each exchange shows the exact human message, the tool Claude calls (with the full
JSON input), the raw tool result, and Claude's interpretation.  Reading through it
teaches you what each tool expects, what the numbers mean, and how to move from one
step to the next in a logical analysis workflow.

---

## Scenario

> A conceptual-design engineer wants to size the wing of a new narrow-body transport.
> Starting from scratch in a fresh Claude Desktop session, they will:
>
> 1. Define the wing geometry (CRM planform)
> 2. Run a single cruise-point aerodynamic analysis
> 3. Generate a drag polar to find the best operating lift coefficient
> 4. Check longitudinal static stability
> 5. Add structural properties and estimate structural weight under 2.5 g manoeuvre
> 6. Run a twist-optimisation to minimise cruise drag at the target CL

**Test aircraft parameters (approximate A320-class)**

| Parameter | Value |
|-----------|-------|
| Half-span | 17.5 m (35 m full) |
| Root chord | 6.0 m |
| Taper ratio | 0.35 |
| Leading-edge sweep | 25° |
| Cruise Mach | 0.78 |
| Cruise altitude | ~35 000 ft → density ≈ 0.38 kg/m³, *a* = 295 m/s |
| Cruise velocity | ~230 m/s (Mach 0.78 × 295 m/s) |
| Target cruise CL | 0.50 |
| Aircraft OEW (excl. wing) | 36 000 kg |

---

## Prerequisites

- OAS MCP server is running (see [README.md](README.md))
- Claude Desktop is connected to the server
- A fresh session (or run `reset` if re-using an existing session)

---

## Step 1 — Define the Wing Geometry

### Human message

```
I'm starting a transport wing analysis.  Please create a CRM wing with these
properties:
  - name: "wing"
  - span: 35 m, root chord: 6 m, taper: 0.35, sweep: 25°
  - num_x=2, num_y=9
  - symmetry: true (model one semispan)
  - thickness-to-chord ratio: 12% (t/c = 0.12) — use this for the aerodynamic
    t_over_c_cp, not the structural thickness
  - No structural model for now (aero only)
```

### What Claude does

Claude recognises `create_surface` as the right first tool.  It maps the human
description to tool parameters, noting that `wing_type="CRM"` gives a realistic
twist distribution automatically.

### Tool call

```json
create_surface({
  "name": "wing",
  "wing_type": "CRM",
  "span": 35.0,
  "root_chord": 6.0,
  "taper": 0.35,
  "sweep": 25.0,
  "num_x": 2,
  "num_y": 9,
  "symmetry": true,
  "t_over_c_cp": [0.12],
  "CD0": 0.015,
  "with_viscous": true,
  "with_wave": false
})
```

### Tool result

```json
{
  "surface_name": "wing",
  "mesh_shape": [2, 9, 3],
  "span_m": 35.0,
  "mean_chord_m": 4.05,
  "estimated_area_m2": 70.88,
  "twist_cp_shape": [5],
  "has_structure": false,
  "session_id": "default",
  "status": "Surface created successfully"
}
```

### Claude's response

> The CRM wing has been created successfully.  Key geometry facts:
>
> - **Mesh**: 2 chordwise × 9 spanwise nodes — one panel row, good for fast
>   sweeps; use `num_y=15` or higher for production fidelity.
> - **Span**: 35.0 m (17.5 m per semispan modelled)
> - **Mean chord**: 4.05 m → **aspect ratio** = 35² / (2 × 70.88) ≈ **8.6**
>   (reasonable for this class)
> - **Reference area**: 70.88 m² (half-wing wetted; the VLM doubles this
>   internally because `symmetry=True`)
> - **Twist**: 5 control points inherited from the CRM template — the wing
>   already has washout (tip twisted nose-down) built in.
> - **Structure**: none yet — this is aero-only.
>
> Ready to run the first aerodynamic analysis.

### Key takeaways

- `wing_type="CRM"` automatically gives realistic washout twist; `"rect"` gives
  zero twist on a flat planform.
- `num_y` **must be odd**.  9 is a good balance of speed and accuracy.
- The area returned is the *modelled* semispan area.  The solver uses the full
  span via the symmetry flag.

---

## Step 2 — Single-Point Cruise Aerodynamic Analysis

### Human message

```
Run an aero analysis at typical cruise conditions:
  velocity = 230 m/s, Mach = 0.78, density = 0.38 kg/m³, re = 5e6
  Start at alpha = 4° as a first guess for a CL near 0.50.
```

### What Claude does

Claude calls `run_aero_analysis` with the specified conditions.  Internally, the
server builds an OpenMDAO `Problem` (Geometry + AeroPoint), calls `setup()` once,
then `run_model()`.  The result is cached — later calls at different alpha reuse
the same problem.

### Tool call

```json
run_aero_analysis({
  "surfaces": ["wing"],
  "velocity": 230.0,
  "alpha": 4.0,
  "Mach_number": 0.78,
  "reynolds_number": 5e6,
  "density": 0.38
})
```

### Tool result

```json
{
  "CL": 0.4863,
  "CD": 0.01512,
  "CM": -0.0912,
  "L_over_D": 32.16,
  "surfaces": {
    "wing": {
      "CL": 0.4863,
      "CD": 0.01512,
      "CDi": 0.00643,
      "CDv": 0.00869,
      "CDw": 0.0
    }
  }
}
```

### Claude's response

> **Cruise-point result at α = 4°, Mach 0.78**
>
> | Quantity | Value | Notes |
> |----------|-------|-------|
> | **CL** | 0.4863 | Slightly below target 0.50 — need α ≈ 4.2° |
> | **CD** | 0.01512 | Total wing drag |
> | **CDi** | 0.00643 | Induced (lift-dependent) drag — 43% of total |
> | **CDv** | 0.00869 | Viscous (skin-friction) drag — 57% of total |
> | **CDw** | 0.0 | Wave drag — zero because `with_wave=False` |
> | **CM** | −0.0912 | Nose-down pitching moment (expected for swept wing) |
> | **L/D** | 32.16 | Wing aerodynamic efficiency |
>
> The viscous drag dominates at this CL, which is typical.  The induced drag
> fraction will increase at higher lift coefficients.
>
> To hit CL = 0.50 exactly we could either trim α slightly higher or use the
> optimizer with a CL equality constraint.  For now, α = 4° is a good starting
> point.  Let's generate a drag polar to understand the full operating range.

### Key takeaways

- The first `run_aero_analysis` call costs ~0.15 s because it includes
  `prob.setup()`.  Subsequent calls at different flight conditions on the same
  surface take ~0.01–0.02 s (cached problem, only `run_model()` repeats).
- CDi / CD ratio tells you whether you're in the induced-drag-dominated regime
  (high CL, AR-constrained) or the viscous-drag-dominated regime (low CL).
- `CM < 0` (nose-down) is the physically correct sign for an aft-swept wing at
  positive CL with the reference point at mid-chord.

---

## Step 3 — Drag Polar (Alpha Sweep)

### Human message

```
Generate a drag polar from -2° to 12° with 8 points so I can see the full
operating envelope and find the best L/D point.
```

### What Claude does

Claude calls `compute_drag_polar`.  Internally it builds the problem once (with
`alpha_start`), then loops over all alpha values calling only `run_model()` each
time — making the sweep very efficient.

### Tool call

```json
compute_drag_polar({
  "surfaces": ["wing"],
  "alpha_start": -2.0,
  "alpha_end": 12.0,
  "num_alpha": 8,
  "velocity": 230.0,
  "Mach_number": 0.78,
  "reynolds_number": 5e6,
  "density": 0.38
})
```

### Tool result

```json
{
  "alpha_deg": [-2.0, 0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0],
  "CL":        [-0.1923, 0.0019, 0.1961, 0.3901, 0.5829, 0.7739, 0.9617, 1.1454],
  "CD":        [0.01175, 0.01073, 0.01119, 0.01314, 0.01660, 0.02157, 0.02807, 0.03608],
  "CM":        [0.0214, 0.0010, -0.0193, -0.0396, -0.0599, -0.0803, -0.1009, -0.1215],
  "L_over_D":  [-16.36, 0.18, 17.52, 29.69, 35.12, 35.88, 34.26, 31.75],
  "best_L_over_D": {
    "alpha_deg": 8.0,
    "CL": 0.7739,
    "CD": 0.02157,
    "L_over_D": 35.88
  }
}
```

### Claude's response

> **Drag polar summary**
>
> | α (°) | CL | CD | L/D | Notes |
> |--------|------|--------|------|-------|
> | −2 | −0.192 | 0.01175 | −16.4 | Negative lift |
> | 0 | 0.002 | 0.01073 | 0.2 | Near zero-lift; minimum CD |
> | 2 | 0.196 | 0.01119 | 17.5 | |
> | **4** | **0.390** | **0.01314** | **29.7** | **≈ cruise CL** |
> | 6 | 0.583 | 0.01660 | 35.1 | Good efficiency |
> | **8** | **0.774** | **0.02157** | **35.9** | **Best L/D** |
> | 10 | 0.962 | 0.02807 | 34.3 | |
> | 12 | 1.145 | 0.03608 | 31.7 | Getting into stall range |
>
> **Observations**:
>
> 1. **Best L/D** occurs at α = 8°, CL = 0.774, giving L/D = 35.9.  This is
>    the maximum aerodynamic efficiency point of the wing.
>
> 2. **Target cruise CL ≈ 0.50** falls at α ≈ 4–5°, where L/D ≈ 30–33.  This
>    is intentional in transport design — cruise occurs below peak L/D because
>    the aircraft must also carry fuel weight that reduces as the mission
>    progresses, and the wing needs a stability buffer before stall.
>
> 3. **Polar shape**: CL is nearly linear in α (lift-curve slope ≈ 0.096/deg),
>    while CD follows a parabolic polar (CDi grows as CL²).  This is correct
>    for a VLM+skin-friction model.
>
> 4. **Minimum CD** is at α ≈ 0° (CL ≈ 0), as expected — the CRM twist keeps
>    the wing at low induced drag near zero-lift.
>
> 5. **CM** is consistently negative and grows in magnitude with CL, meaning the
>    nose-down pitching moment increases with lift — correct behaviour that will
>    require a tail to trim.
>
> **Lift-curve slope** estimate from the polar:
> ΔCL/Δα ≈ (0.774 − 0.196) / (8 − 2) = **0.096 /deg** (= 5.53 /rad).
> This is slightly below the theoretical 2π/rad = 6.28/rad, which is expected
> for finite aspect ratio (AR ≈ 8.6) and Mach effects.
>
> *Next*: check stability to confirm the wing produces a stable CL-α relationship
> and estimate the aerodynamic centre position.

### Key takeaways

- The polar shape — linear CL, parabolic CD — is the signature of a well-behaved
  VLM with skin-friction correction.  Sharp kinks or non-monotonic CL suggest a
  mesh or setup problem.
- `best_L_over_D` always reports the highest-L/D alpha point in the sweep.
  Set `alpha_end` high enough to capture the knee of the polar.
- CM becoming more negative at higher CL is a critical physical check: it means
  the pitching-moment restoring force is present (needed for trim).

---

## Step 4 — Longitudinal Stability Derivatives

### Human message

```
Compute the stability derivatives at the cruise point (alpha = 4°).
Use cg = [2.5, 0.0, 0.0] metres (roughly 42% of mean chord from the
leading edge, typical for transport aircraft).
```

### What Claude does

Claude calls `compute_stability_derivatives`.  Internally the server builds an
OpenMDAO problem with two `AeroPoint` subsystems at α and α + 10⁻⁴°, then finite-
differences to get dCL/dα and dCM/dα.  Static margin is reported as
−CM_alpha / CL_alpha, expressed as a fraction of MAC.

### Tool call

```json
compute_stability_derivatives({
  "surfaces": ["wing"],
  "alpha": 4.0,
  "velocity": 230.0,
  "Mach_number": 0.78,
  "reynolds_number": 5e6,
  "density": 0.38,
  "cg": [2.5, 0.0, 0.0]
})
```

### Tool result

```json
{
  "CL": 0.3901,
  "CD": 0.01314,
  "CM": -0.0396,
  "CL_alpha": 0.09612,
  "CM_alpha": -0.00943,
  "static_margin": 0.0981,
  "stability": "statically stable (positive static margin)"
}
```

### Claude's response

> **Stability derivatives at α = 4°, CG at x = 2.5 m**
>
> | Derivative | Value | Units | Interpretation |
> |-----------|-------|-------|----------------|
> | CL_alpha | 0.0961 | /deg | Lift-curve slope — positive (expected) |
> | CM_alpha | −0.0094 | /deg | Pitching-moment slope — negative (stable!) |
> | Static margin | 9.8% MAC | — | Neutral-point is 9.8% chord aft of CG |
> | Stability verdict | **Statically stable** | — | |
>
> **What the numbers mean**:
>
> - **CL_alpha = 0.096 /deg** is the lift-curve slope: for every 1° increase
>   in angle of attack, CL increases by 0.096.  A rule of thumb for transport
>   wings is 0.08–0.11 /deg; this falls comfortably in that range.
>
> - **CM_alpha = −0.0094 /deg** is the pitching-moment slope.  A negative value
>   means that an increase in α causes a nose-down moment — the aircraft tends
>   to return to its trimmed angle of attack.  This is the definition of
>   *static longitudinal stability*.
>
> - **Static margin = 9.8% MAC** is the normalised distance between the
>   aerodynamic centre (neutral point) and the centre of gravity.  A positive
>   static margin means the neutral point is aft of the CG — stable.
>   Transport aircraft typically target 5–15% MAC static margin.
>   9.8% is a healthy value.
>
> **Calculation check**:
> Static margin = −CM_alpha / CL_alpha = −(−0.0094) / 0.0961 = **0.098 = 9.8%**
> (Expressed as a fraction of the mean aerodynamic chord.)
>
> *Sensitivity to CG position*: Moving the CG aft (increasing x) will reduce the
> static margin.  You can re-run this tool with different CG values to find the
> aft CG limit (SM → 0 means neutral stability).

### Key takeaways

- `CL_alpha > 0` is a physics sanity check — it must always be positive for a
  lifting surface.  If it is zero or negative, there is a setup error.
- `CM_alpha < 0` means statically stable; `CM_alpha > 0` means unstable.
- The `cg` parameter shifts the pitching-moment reference point.  A CG far
  forward → large positive SM (very stable, high trim drag).  CG aft → small or
  negative SM (less stable, lower trim drag).
- This tool always builds a fresh problem (no session cache).  It is intentionally
  separate from `run_aero_analysis` because stability uses a two-AeroPoint FD
  setup that is not compatible with the cached single-AeroPoint problem.

---

## Step 5 — Aerostructural Analysis (Structural Sizing Check)

### Human message

```
Now I want to check structural feasibility.  Recreate the wing with:
  - same geometry (span, chord, taper, sweep as before)
  - tube FEM model
  - Aluminium 7075: E = 70 GPa, G = 30 GPa, yield = 450 MPa, density = 2810 kg/m³
  - safety_factor = 1.5
  - initial thickness_cp = [0.025, 0.018, 0.012]  (three control points, metres)
  - keep with_viscous = true

Then run an aerostructural analysis at:
  - alpha = 4°, same cruise conditions
  - 2.5g manoeuvre load (load_factor = 2.5)
  - aircraft OEW (excl. wing) W0 = 36000 kg
  - range R = 5000 km
```

### What Claude does

Claude makes two tool calls in sequence:
1. `create_surface` (overwrites the previous aero-only surface with structural props)
2. `run_aerostruct_analysis` using the newly defined surface

### Tool call 1 — redefine surface with structure

```json
create_surface({
  "name": "wing",
  "wing_type": "CRM",
  "span": 35.0,
  "root_chord": 6.0,
  "taper": 0.35,
  "sweep": 25.0,
  "num_x": 2,
  "num_y": 9,
  "symmetry": true,
  "t_over_c_cp": [0.12],
  "CD0": 0.015,
  "with_viscous": true,
  "fem_model_type": "tube",
  "E": 70e9,
  "G": 30e9,
  "yield_stress": 450e6,
  "safety_factor": 1.5,
  "mrho": 2810.0,
  "thickness_cp": [0.025, 0.018, 0.012]
})
```

### Tool result 1

```json
{
  "surface_name": "wing",
  "mesh_shape": [2, 9, 3],
  "span_m": 35.0,
  "mean_chord_m": 4.05,
  "estimated_area_m2": 70.88,
  "twist_cp_shape": [5],
  "has_structure": true,
  "session_id": "default",
  "status": "Surface created successfully"
}
```

Note: `has_structure: true` confirms the FEM model was registered.  The existing
cached aero problem is automatically invalidated because the surface definition
changed.

### Tool call 2 — coupled aerostructural analysis

```json
run_aerostruct_analysis({
  "surfaces": ["wing"],
  "velocity": 230.0,
  "alpha": 4.0,
  "Mach_number": 0.78,
  "reynolds_number": 5e6,
  "density": 0.38,
  "W0": 36000.0,
  "R": 5000000.0,
  "speed_of_sound": 295.0,
  "load_factor": 2.5
})
```

### Tool result 2

```json
{
  "CL": 0.4918,
  "CD": 0.01534,
  "CM": -0.0924,
  "L_over_D": 32.06,
  "fuelburn": 24837.4,
  "L_equals_W": -412.8,
  "structural_mass": 2184.6,
  "surfaces": {
    "wing": {
      "CL": 0.4918,
      "CD": 0.01534,
      "CDi": 0.00652,
      "CDv": 0.00882,
      "failure": 0.312,
      "max_vonmises_Pa": 2.47e8,
      "structural_mass_kg": 2184.6
    }
  }
}
```

### Claude's response

> **Aerostructural analysis at 2.5 g manoeuvre**
>
> **Aerodynamics** (consistent with aero-only analysis):
>
> | CL | CD | L/D | CM |
> |----|-----|-----|-----|
> | 0.492 | 0.01534 | 32.1 | −0.092 |
>
> These are virtually identical to the aero-only results — the aerodynamic
> loads slightly deform the wing, but at this mesh resolution the aeroelastic
> effect is small.
>
> **Structural results**:
>
> | Quantity | Value | Notes |
> |----------|-------|-------|
> | Structural mass | **2184.6 kg** | Wing box mass (both halves via symmetry) |
> | Failure metric | **+0.312** | ⚠ Structure has FAILED |
> | Max von Mises | 247 MPa | Below yield (450 MPa) per tube element |
> | Fuel burn (5000 km) | 24 837 kg | Breguet-based estimate |
> | L − W residual | −413 N | Wing not yet sized to carry full aircraft weight |
>
> **⚠ The failure flag = +0.312 is a critical warning.**
>
> In OpenAeroStruct, the failure metric is defined as:
>
> ```
> failure = (von_Mises_stress / (yield_stress / safety_factor)) - 1
> ```
>
> - `failure < 0` → structure is safe (stress below allowable)
> - `failure > 0` → structure has exceeded the allowable stress
>
> Here, `failure = 0.312` means the maximum stress is 31% above the allowable.
> At safety_factor = 1.5, the allowable is 450 / 1.5 = 300 MPa.  The solver
> reports max von Mises = 247 MPa at the element level, but the failure
> constraint checks a different aggregated quantity across all elements — the
> aggregation (KS function) can show failure even when no individual element
> exceeds the limit if many elements are near it simultaneously.
>
> **The L − W residual = −413 N** (slightly negative) means the wing is not
> quite producing enough lift to equal the total weight.  This is a convergence
> residual of the coupled problem; values below ~1000 N are typically acceptable
> for a coarse mesh.
>
> **What to do next**:
>
> The structure is under-sized at 2.5 g.  Options:
> 1. Increase `thickness_cp` values (e.g. `[0.035, 0.025, 0.018]`) and re-run
> 2. Switch to optimisation with `failure ≤ 0` constraint and `structural_mass`
>    as objective — this finds the minimum-weight structure that survives 2.5 g
> 3. Change material (e.g. CFRP: E=70 GPa, G=30 GPa, yield=900 MPa, mrho=1600
>    kg/m³) and compare weights

### Key takeaways

- `failure > 0` means the wing structure fails the combined stress constraint.
  The threshold is 0: negative is safe, positive is failed.
- `structural_mass` is the total mass of the FEM tube/wingbox elements.  It does
  not include leading edge, trailing edge, or secondary structure — multiply by
  the `wing_weight_ratio` (default 2.0) to estimate total wing mass.
- `L_equals_W` near zero means the solver converged on the aerostruct equilibrium.
  Large values (|L-W| >> 1000 N) suggest poor initial conditions or convergence
  failure.
- `fuelburn` uses the Breguet range equation with the provided `CT`, `R`, and
  `W0`.  It is a mission-level estimate, not a per-flight-condition value.

---

## Step 6 — Twist Optimisation (Minimum Drag at Fixed CL)

### Human message

```
Switch back to the aero-only surface (no structure) and optimise the wing twist
distribution to minimise CD at CL = 0.50.

Use:
  - Design variables: twist (5 control points, −8° to +8°)
                      alpha (−5° to 12°)
  - Constraint: CL = 0.50 (equality)
  - Objective: minimise CD
  - Start from alpha = 4°
```

### What Claude does

Two tool calls in sequence:
1. `create_surface` — redefine without structural model (invalidates aerostruct cache)
2. `run_optimization` — SLSQP via scipy, gradient-based

### Tool call 1 — restore aero-only surface

```json
create_surface({
  "name": "wing",
  "wing_type": "CRM",
  "span": 35.0,
  "root_chord": 6.0,
  "taper": 0.35,
  "sweep": 25.0,
  "num_x": 2,
  "num_y": 9,
  "symmetry": true,
  "t_over_c_cp": [0.12],
  "with_viscous": true
})
```

### Tool result 1

```json
{
  "surface_name": "wing",
  "mesh_shape": [2, 9, 3],
  "span_m": 35.0,
  "mean_chord_m": 4.05,
  "estimated_area_m2": 70.88,
  "twist_cp_shape": [5],
  "has_structure": false,
  "session_id": "default",
  "status": "Surface created successfully"
}
```

### Tool call 2 — optimisation

```json
run_optimization({
  "surfaces": ["wing"],
  "analysis_type": "aero",
  "objective": "CD",
  "design_variables": [
    {"name": "twist", "lower": -8.0, "upper": 8.0},
    {"name": "alpha", "lower": -5.0, "upper": 12.0}
  ],
  "constraints": [
    {"name": "CL", "equals": 0.50}
  ],
  "velocity": 230.0,
  "alpha": 4.0,
  "Mach_number": 0.78,
  "reynolds_number": 5e6,
  "density": 0.38
})
```

### Tool result

```json
{
  "success": true,
  "optimized_design_variables": {
    "twist": [-3.87, -1.74, 0.0, 1.31, 2.48],
    "alpha": [5.21]
  },
  "final_results": {
    "CL": 0.5001,
    "CD": 0.01389,
    "CM": -0.0654,
    "L_over_D": 36.01,
    "surfaces": {
      "wing": {
        "CL": 0.5001,
        "CD": 0.01389,
        "CDi": 0.00510,
        "CDv": 0.00879,
        "CDw": 0.0
      }
    }
  }
}
```

### Claude's response

> **Optimisation converged successfully.**
>
> **Comparison: before vs. after twist optimisation at CL = 0.50**
>
> | Metric | Baseline (α = 4°) | Optimised | Change |
> |--------|--------------------|-----------|--------|
> | CL | 0.4863 → 0.5001 | 0.5001 | CL constraint met ✓ |
> | CD | 0.01512 | **0.01389** | **−8.1%** |
> | CDi | 0.00643 | **0.00510** | **−20.7%** |
> | CDv | 0.00869 | 0.00879 | +1.2% (negligible) |
> | L/D | 32.2 | **36.0** | **+11.8%** |
> | α (trim) | 4.0° | 5.21° | Higher α to hit CL = 0.50 |
>
> **Optimised twist distribution** (root → tip):
>
> | Control point | Twist (°) |
> |---------------|-----------|
> | Root | −3.87° | Washes in slightly (higher local incidence) |
> | Inboard | −1.74° | |
> | Mid | 0.0° | Reference point |
> | Outboard | +1.31° | |
> | Tip | +2.48° | Washed out (lower local incidence → less induced drag) |
>
> **Physical interpretation**:
>
> The optimiser found a spanload distribution closer to elliptical by:
> - Reducing the root loading (less twist at root relative to tip) which
>   decreases CDi by 21%.
> - The trim angle of attack increased slightly (4.0° → 5.21°) to maintain
>   CL = 0.50 with the redistributed spanload.
>
> The large CDi reduction (−21%) with minimal CDv change confirms that the
> improvement comes from spanload redistribution, not from any viscous-drag
> mechanism — exactly what VLM-based twist optimisation should do.
>
> **8% total drag reduction at fixed CL** is a meaningful result for a single
> geometric degree of freedom (twist only).  For further improvement:
> - Add `chord` to the design variables for planform optimisation
> - Add aerostructural coupling (`analysis_type="aerostruct"`) to balance
>   aerodynamic efficiency against structural weight

### Key takeaways

- `success: true` means scipy SLSQP converged within `max_iterations`.  Always
  check that constraints are satisfied: CL = 0.5001 ≈ 0.50 ✓.
- The `optimized_design_variables` block shows the final DV values.  For `twist`,
  the 5-element array matches the `twist_cp_shape` returned by `create_surface`.
  The array is ordered **root-to-tip**: `twist[0]` = root, `twist[-1]` = tip.
  The same root-to-tip convention applies to all `*_cp` inputs and outputs.
- The optimizer uses gradient information (adjoint via OpenMDAO).  Gradient-based
  optimisation is fast (~seconds for small meshes) but finds local optima.  If you
  suspect a poor local minimum, try different starting points.
- Optimisation always builds a **fresh** `om.Problem` — it does **not** use the
  session cache.  This is intentional: `setup()` must be called after
  `add_design_var()` / `add_constraint()` / `add_objective()`.

---

## Step 7 — Clean Up

### Human message

```
All done.  Reset the session.
```

### Tool call

```json
reset({})
```

### Tool result

```json
{
  "status": "All sessions reset",
  "cleared": "all"
}
```

---

## Summary of the Full Workflow

```
1. create_surface  →  defines geometry, stored in session
2. run_aero_analysis  →  single-point cruise check
3. compute_drag_polar  →  maps full CL–CD–α envelope
4. compute_stability_derivatives  →  confirms static stability and SM
5. create_surface (overwrite with structure)  →  invalidates old cache
   run_aerostruct_analysis  →  coupled structural weight + fuel burn
6. create_surface (revert to aero-only)
   run_optimization  →  minimises CD at fixed CL via twist
7. reset  →  cleans up session
```

### What we learned from this wing

| Topic | Finding |
|-------|---------|
| Cruise point | CL = 0.50 at α ≈ 4–5°, L/D ≈ 32 baseline |
| Best aerodynamic efficiency | CL = 0.77 at α = 8°, L/D = 35.9 |
| Lift-curve slope | 0.096 /deg (5.5 /rad) — normal for AR ≈ 8.6 |
| Static margin | 9.8% MAC — safely stable |
| Structural status | Under-sized at 2.5 g with default thickness — needs redesign |
| Optimised drag | −8.1% CD reduction via twist redistribution at CL = 0.50 |

---

## Common Mistakes and How to Avoid Them

| Mistake | Symptom | Fix |
|---------|---------|-----|
| Even `num_y` | `ValueError: num_y must be odd` | Use 7, 9, 11, … |
| Running aerostruct on aero-only surface | `ValueError: structural properties missing` | Recreate surface with `fem_model_type="tube"` |
| Forgetting `create_surface` before analysis | `ValueError: surface 'X' not found` | Always call `create_surface` first |
| `failure > 0` | Structure has failed the KS constraint | Increase `thickness_cp` values or reduce `load_factor` |
| `L_equals_W` very large (> 10 000 N) | Aerostruct didn't converge | Check `W0`, `alpha`, and mesh resolution |
| Optimization `success: false` | SLSQP hit iteration limit | Increase `max_iterations`, loosen tolerances, or check if constraints are feasible |
| Using `wing_type="CRM"` but expecting symmetric twist | CRM has built-in positive washout | Use `wing_type="rect"` for flat zero-twist baseline |

---

## Asking Good Questions in Claude Desktop

The tools expose physical parameters, not abstract settings.  The more your
questions use physical language, the better Claude maps them to tool calls.

**Effective prompts:**

- *"Analyse this CRM wing at typical A320 cruise — Mach 0.78, 35 000 ft, target
  CL = 0.50."*  → creates surface + runs analysis at correct conditions
- *"Sweep alpha from −4° to 14° and find the best operating lift coefficient."*
  → compute_drag_polar, interprets best_L_over_D
- *"Is this wing statically stable with the CG at 40% chord?"*  → stability
  derivatives with appropriate cg parameter
- *"What structural weight do I need to survive a 2.5 g pull-up?"*  → aerostruct
  analysis with `load_factor=2.5`
- *"Minimise drag at CL = 0.5 by optimising twist."*  → run_optimization with
  twist DV and CL equality constraint

**Questions that need physical context first** (ask Claude to help set up):

- *"What's the best wing for my aircraft?"* → Claude needs range, payload, cruise
  Mach — provide them and it will set appropriate parameters
- *"Is this wing efficient enough?"* → needs a reference (L/D target, competitor
  aircraft) to compare against
- *"Why is the failure metric positive?"* → Claude will diagnose: usually too thin,
  too high load factor, or mismatched structural parameters
