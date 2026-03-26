---
name: oas-trade-study
description: >
  Run a parametric trade study across OAS configurations.
  Use when user wants to sweep parameters or compare designs.
context: fork
---

# OAS Trade Study

Run the requested parametric sweep:

1. Start a session named after the study via `start_session`
2. For each configuration point:
   - Create the surface with varied parameters
   - Run analysis
   - REQUIRED: Call `log_decision(decision_type="result_interpretation",
     reasoning="<what this data point shows — key metrics and trends>",
     selected_action="<next parameter point or adjustment>",
     prior_call_id=<_provenance.call_id from the analysis result>)`
3. Compile a summary table of all configurations
4. Identify the Pareto-optimal designs
   - REQUIRED: Call `log_decision(decision_type="result_interpretation",
     reasoning="<Pareto analysis: which designs dominate and why>",
     selected_action="<recommended design(s) and rationale>")`
5. Call `export_session_graph` to save the provenance DAG
6. Return findings to the main conversation
