# OpenAeroStruct — Developer Guide for Claude Code

## Provenance & Decision Logging

The OAS MCP server includes a built-in provenance system that records every tool
call and supports explicit reasoning capture via three dedicated tools.

### Quick start

```
# 1. Start a named session at the beginning of every workflow
start_session(notes="CRM wing optimization study")

# 2. Log decisions before major steps (optional but recommended)
log_decision(
    decision_type="dv_selection",
    reasoning="Twist and alpha provide the best L/D improvement for aero-only problems",
    selected_action="twist_cp, alpha",
    prior_call_id="<_provenance.call_id from previous tool result>",
    confidence="high"
)

# 3. Export the provenance graph at the end
export_session_graph(
    session_id="<session_id from start_session>",
    output_path="study_provenance.json"
)
```

### Decision types

| `decision_type`           | When to use                                     |
|---------------------------|-------------------------------------------------|
| `mesh_resolution`         | Choosing num_x / num_y                          |
| `dv_selection`            | Choosing design variables and their bounds      |
| `constraint_choice`       | Choosing optimization constraints               |
| `result_interpretation`   | Explaining what a result means and next steps   |
| `convergence_assessment`  | Assessing whether an optimizer converged        |

### Viewing the graph

The viewer server starts automatically on port 7654:
  http://127.0.0.1:7654/viewer?session_id=<session_id>

Or open `oas_mcp/provenance/viewer/index.html` in a browser and drop the
exported JSON file onto the page.

### Where data lives

- SQLite DB: `~/.oas_provenance/sessions.db` (override via `OAS_PROV_DB`)
- Each tool call's `_provenance.call_id` can be passed to `log_decision` to
  create a causal link between analysis results and decisions.

### Rules for Claude Code sessions

- Always call `start_session` at the start of a multi-step OAS workflow.
- Use `log_decision` before choosing design variables or interpreting surprising results.
- Pass `prior_call_id` whenever the decision is directly informed by a specific tool result.
- Call `export_session_graph` at the end and mention the file path in the summary.

### Visualization in CLI environments

MCP `ImageContent` renders as `[image]` in CLI — not useful. Use output modes instead:

- `configure_session(visualization_output="file")` — saves PNGs to disk, no `[image]` noise
- `configure_session(visualization_output="url")` — returns clickable dashboard/plot URLs
- Per-call override: `visualize(run_id, plot_type, output="file")`
- Dashboard: `http://localhost:7654/dashboard?run_id=<id>` shows results + plots in browser
