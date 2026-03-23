# Optimize Wing

> This workflow is also available as the MCP prompt `optimize_wing`.

Run a wing design optimization using the OpenAeroStruct MCP server.

## Steps

0. **Start provenance session** — call `start_session(notes="optimize_wing workflow")` and save the returned `session_id`.

1. **Check/create surface** — if no surface exists in the session, call `create_surface`
   with appropriate parameters. For aero optimization, `fem_model_type=None` is fine.
   For aerostruct optimization, use `fem_model_type="tube"` with material properties.

2. **Baseline analysis** — run `run_aero_analysis` (or `run_aerostruct_analysis`) to establish
   a baseline. Note baseline CL, CD, L/D from `summary.narrative`.
   Save the baseline `run_id` for comparison.

3. **Record DV selection rationale** — before running optimization, call
   `log_decision(decision_type="dv_selection", reasoning="<why these DVs and bounds>", selected_action="<DV list>", prior_call_id=<baseline call_id>, confidence="medium")`.

   **Run optimization** — call `run_optimization` with:
   - `objective`: "CD" for aero, "fuelburn" for aerostruct
   - `design_variables`: include `twist` (lower=-10, upper=15) and `alpha` (lower=-5, upper=10)
     For aerostruct: also add `thickness` (lower=0.003, upper=0.25)
   - `constraints`: `CL=target_CL` for aero; `L_equals_W=0, failure<=0` for aerostruct

4. **Visualize convergence** — call `visualize(run_id, "opt_history")` to see objective convergence.
   Also call `visualize(run_id, "opt_dv_evolution")` if design variables changed significantly.
   Call `log_decision(decision_type="convergence_assessment", reasoning="<convergence quality assessment>", selected_action="<accept or re-run>", prior_call_id=<opt call_id>)`.

5. **Compare baseline vs optimized** — use `summary.derived_metrics` delta to see improvements.
   Call `visualize(run_id, "opt_comparison")` for a side-by-side DV comparison.

6. **Report results**:
   - Convergence: `success`, number of iterations
   - Objective improvement: `summary.derived_metrics.objective_improvement_pct`
   - Optimized DV values: `results.optimized_design_variables`
   - Final performance: CL, CD, L/D from `results.final_results`
   - Constraint satisfaction: check CL residual, failure margin
   - Any validation warnings

7. **Export provenance** — call `export_session_graph(session_id=<session_id>)` to capture the
   full decision audit trail including DV selection rationale and convergence assessment.

## SLSQP scaling — critical for convergence

OAS uses SciPy's SLSQP optimizer, which is gradient-based and **very sensitive
to problem scaling**. Without proper scaling, SLSQP may report `success: false`
or converge to a suboptimal/infeasible point.

### Rule of thumb

The scaled objective and scaled DV values should all be O(1) — roughly in
the range 0.1–100. SLSQP computes finite-difference gradients, and if the
objective is 100,000 while a DV is 0.01, the gradient is poorly conditioned.

### Computing the right scaler

The `objective_scaler` should be approximately `1 / initial_objective_value`.
You can get the initial value from the baseline analysis in Step 2:

- **Baseline CD ≈ 0.03** → `objective_scaler ≈ 1/0.03 ≈ 30` (use `1e2` or `30`)
- **Baseline fuelburn ≈ 100,000 kg** → `objective_scaler ≈ 1/1e5 = 1e-5`
- **Baseline structural_mass ≈ 25,000 kg** → `objective_scaler ≈ 1/2.5e4 ≈ 4e-5`

For DV scalers, apply the same logic to the DV magnitude:

- **thickness** (values ~0.01–0.3 m) → `scaler: 100` (i.e. `1e2`)
- **spar_thickness, skin_thickness** (values ~0.001–0.05 m) → `scaler: 1000`
- **twist, alpha, sweep** (values ~1–15 deg) → no scaler needed (already O(1))
- **chord** (values ~1–10 m) → no scaler needed

### Quick recipe

After running the baseline analysis, compute scalers from the result:

```
objective_scaler = 1.0 / baseline_{objective}
```

For example, if the baseline `run_aero_analysis` returns `CD = 0.035`:
```json
{"name": "twist", "lower": -10, "upper": 15},
{"name": "alpha", "lower": -5, "upper": 10}
```
with `objective_scaler = 30` (≈ 1/0.035).

For aerostruct with baseline fuelburn = 95,000:
```json
{"name": "twist", "lower": -10, "upper": 15},
{"name": "thickness", "lower": 0.01, "upper": 0.5, "scaler": 100},
{"name": "alpha", "lower": -10, "upper": 10}
```
with `objective_scaler = 1e-5`, `tolerance = 1e-9`.

### Reference scalers from OAS examples

| Example | Objective | `objective_scaler` | DV scalers |
|---------|-----------|-------------------|------------|
| `run_CRM.py` | fuelburn | `1e-5` | thickness: `1e2` |
| `run_scaneagle.py` | fuelburn | `0.1` | thickness: `1e3` |

### When to suspect a scaling problem

- Optimizer reports `success: false` with few iterations
- Failure constraint violated at termination despite feasible initial point
- Design variables pinned at bounds without physical justification
- The validation block will flag these cases with specific remediation hints

## Decision guide
- **Minimize drag (aero-only)**: `objective="CD"`, DVs=[twist, alpha], constraints=[CL=target]
  Scaling: `objective_scaler ≈ 1/baseline_CD` (typically 30–100)
- **Minimize fuel burn (aerostruct)**: `objective="fuelburn"`, DVs=[twist, thickness, alpha],
  constraints=[L_equals_W=0, failure<=0, thickness_intersects<=0]
  Scaling: `objective_scaler=1e-5`, thickness `scaler=100`, `tolerance=1e-9`
- **Minimize structural mass**: `objective="structural_mass"`, same DVs/constraints as fuelburn
  Scaling: `objective_scaler ≈ 1/baseline_mass` (typically `1e-4` to `1e-5`)

## Example invocations
- "Optimize twist and alpha for minimum drag at CL=0.5"
- "Find the minimum-weight wing structure that doesn't fail at 2.5g"
- "Minimize fuel burn for a CRM wing with W0=120000 kg"
