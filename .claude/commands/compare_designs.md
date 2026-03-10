# Compare Designs

> This workflow is also available as the MCP prompt `compare_designs`.

Compare two OAS analysis runs side by side using run_ids.

## Steps

0. **Start provenance session** — call `start_session(notes="compare_designs workflow")` and save the returned `session_id`.

1. **Identify the two runs** — accept either:
   - Two explicit run_ids from the user
   - "last two runs" → call `list_artifacts` and use the two most recent run_ids
   - "before and after" → use the run_id from before optimization and after

2. **Retrieve both artifacts** — call `get_artifact(run_id_1)` and `get_artifact(run_id_2)` in parallel.
   Extract `metadata.analysis_type`, `results`, and `metadata.parameters` from each.

3. **Build comparison table** — create a markdown table with these metrics (where applicable):

   | Metric | Run 1 | Run 2 | Change | Change % |
   |--------|-------|-------|--------|----------|
   | CL     | ...   | ...   | ...    | ...      |
   | CD     | ...   | ...   | ...    | ...      |
   | L/D    | ...   | ...   | ...    | ...      |
   | CM     | ...   | ...   | ...    | ...      |
   | fuelburn (kg) | ... | ... | ... | ...   |
   | structural_mass (kg) | ... | ... | ... | ... |
   | failure | ...  | ...   | ...    | ...      |

   Highlight rows with >5% change in bold or with a ★ marker.

4. **Compare design variables** — if both runs have `results.optimized_design_variables`
   (optimization runs) or different input parameters, note what changed.

5. **Spanwise distribution qualitative comparison** — if both have standard_detail
   (call `get_detailed_results(run_id, "standard")` for each), describe:
   - Whether the lift distribution became more/less elliptical
   - Whether stress distribution changed significantly

6. **Summarize** — 3-5 sentences: what changed, by how much, and what it means
   for the design. Reference the analysis_type context (aero vs aerostruct, cruise vs polar).
   Call `log_decision(decision_type="result_interpretation", reasoning="<comparison interpretation>", selected_action="<design recommendation>")`.

7. **Export provenance** — call `export_session_graph(session_id=<session_id>)` to capture the
   comparison reasoning as an audit trail.

## Output format
- Markdown table for quantitative metrics
- Bullet list for qualitative observations
- Final 3-5 sentence summary with design recommendation

## Example invocations
- "Compare these two run_ids: 20240115T123456_ab12 vs 20240115T124530_cd34"
- "What changed between the last two runs?"
- "Compare the baseline and optimized designs"
