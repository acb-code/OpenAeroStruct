---
name: oas-aero-analysis
description: >
  Run OpenAeroStruct aerodynamic and aerostructural analyses.
  Use when user asks to analyze a wing, run a drag polar,
  aerodynamically optimize a surface, or do any OAS/VLM/MDO work.
---

# OpenAeroStruct Analysis Workflow

## Prerequisites
- OAS MCP server must be connected (check with /mcp)
- Start a provenance session before any analysis

## Standard Workflow

### 1. Session Setup
- Call `start_session` with a descriptive session name
- Call `set_requirements` if the user has constraints
  (e.g., CL > 0.5, stress < yield)

### 2. Surface Definition
- Call `create_surface` with the user's geometry
- For TTBW-style studies, define wing + strut as separate surfaces
- Always confirm DV names match exactly what OAS expects
  (known bug: silent mismatches cause optimization failures)
- REQUIRED: Call `log_decision(decision_type="mesh_resolution",
  reasoning="<why this wing_type, num_x, num_y>",
  selected_action="<chosen values>")`

### 3. Analysis
- For single-point: `run_aero_analysis` or `run_aerostruct_analysis`
- For sweep: `compute_drag_polar` with appropriate alpha range
- REQUIRED: After each analysis, call
  `log_decision(decision_type="result_interpretation",
  reasoning="<summarise key metrics and what they mean>",
  selected_action="<next step based on results>",
  prior_call_id=<_provenance.call_id from the analysis result>)`

### 4. Visualization
- Use `visualize` with appropriate plot_type after each run
- Common: lift_distribution, drag_polar, planform, stress_distribution

### 5. Optimization (if requested)
- REQUIRED: Before calling `run_optimization`, log two decisions:
  - `log_decision(decision_type="dv_selection",
    reasoning="<why these DVs and bounds>",
    selected_action="<DV list>")`
  - `log_decision(decision_type="constraint_choice",
    reasoning="<why these constraints and targets>",
    selected_action="<constraint list>")`
- Call `run_optimization` with design variables and constraints
- Pin the run with `pin_run` if you need to preserve it
  for multi-step work
- REQUIRED: After optimization completes, call
  `log_decision(decision_type="convergence_assessment",
  reasoning="<did it converge, iterations, objective change>",
  selected_action="<accept result / re-run with changes>",
  prior_call_id=<_provenance.call_id from the optimization result>)`
- Visualize with opt_history and opt_comparison after completion

### 6. Session Export
- Call `export_session_graph` to save the provenance DAG

## Known Issues
- load_factor caching: if you change load_factor between runs
  in the same session, the cached problem may not update.
  Use `reset` if values seem stale.
- DV name mismatches: always verify design variable names
  against OAS documentation. Silent failures are possible.
