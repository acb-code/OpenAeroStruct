# Optimize Wing

> This workflow is also available as the MCP prompt `optimize_wing`.

Run a wing design optimization using the OpenAeroStruct MCP server.

## Steps

1. **Check/create surface** — if no surface exists in the session, call `create_surface`
   with appropriate parameters. For aero optimization, `fem_model_type=None` is fine.
   For aerostruct optimization, use `fem_model_type="tube"` with material properties.

2. **Baseline analysis** — run `run_aero_analysis` (or `run_aerostruct_analysis`) to establish
   a baseline. Note baseline CL, CD, L/D from `summary.narrative`.
   Save the baseline `run_id` for comparison.

3. **Run optimization** — call `run_optimization` with:
   - `objective`: "CD" for aero, "fuelburn" for aerostruct
   - `design_variables`: include `twist` (lower=-10, upper=15) and `alpha` (lower=-5, upper=10)
     For aerostruct: also add `thickness` (lower=0.003, upper=0.25)
   - `constraints`: `CL=target_CL` for aero; `L_equals_W=0, failure<=0` for aerostruct

4. **Visualize convergence** — call `visualize(run_id, "opt_history")` to see objective convergence.
   Also call `visualize(run_id, "opt_dv_evolution")` if design variables changed significantly.

5. **Compare baseline vs optimized** — use `summary.derived_metrics` delta to see improvements.
   Call `visualize(run_id, "opt_comparison")` for a side-by-side DV comparison.

6. **Report results**:
   - Convergence: `success`, number of iterations
   - Objective improvement: `summary.derived_metrics.objective_improvement_pct`
   - Optimized DV values: `results.optimized_design_variables`
   - Final performance: CL, CD, L/D from `results.final_results`
   - Constraint satisfaction: check CL residual, failure margin
   - Any validation warnings

## Decision guide
- **Minimize drag (aero-only)**: `objective="CD"`, DVs=[twist, alpha], constraints=[CL=target]
- **Minimize fuel burn (aerostruct)**: `objective="fuelburn"`, DVs=[twist, thickness, alpha],
  constraints=[L_equals_W=0, failure<=0, thickness_intersects<=0]
- **Minimize structural mass**: `objective="structural_mass"`, same DVs/constraints as fuelburn

## Example invocations
- "Optimize twist and alpha for minimum drag at CL=0.5"
- "Find the minimum-weight wing structure that doesn't fail at 2.5g"
- "Minimize fuel burn for a CRM wing with W0=120000 kg"
