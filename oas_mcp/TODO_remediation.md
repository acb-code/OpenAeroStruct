# Remediation System — Design Notes

## Problem
Validation findings currently return human-readable strings. Agents must parse
natural language to act on them. As diagnostics grow across tools, ad-hoc
remediation logic in each validator will become hard to maintain.

## Constraint
Keep the MCP server lean. Remediation logic must not bloat individual tool
implementations or couple validators to each other.

## Proposal: separate remediation layer

```
validation.py  (existing)     remediation.py  (new, single file)
  validate_*() -> findings       suggest(findings, run_context) -> actions
```

- Validators stay pure: they produce findings with check_id, severity, message.
  No remediation_action logic inside validators.
- A single `remediation.py` module maps check_ids to structured actions.
  One function, one lookup table, easy to audit.
- Actions are a simple schema:

  ```json
  {"type": "retry_with_params", "params": {"objective_scaler": 4.5e-6}}
  {"type": "widen_bounds", "dv": "alpha", "suggested_upper": 15.0}
  {"type": "refine_mesh", "suggested_num_y": 13}
  ```

- The envelope gets an optional `"remediation_actions"` key (list), populated
  by calling `suggest()` in `_finalize_analysis`. Tools don't touch it.

## Cross-run lookup (future)

- Query artifact store for successful runs with matching (surface, objective,
  analysis_type). Diff key params (scalers, bounds, mesh) against current run.
- Lives in `remediation.py` as `suggest_from_history(findings, artifact_store)`.
- Only called when `success=False` to avoid unnecessary I/O.
- Risk: artifact store coupling. Mitigate by making it opt-in via
  `configure_session(cross_run_hints=True)`.

## Open questions
- Should actions be advisory-only, or should a skill auto-apply them?
- How to version the action schema as new types are added?
- Should cross-run lookup filter by user/project or search globally?
