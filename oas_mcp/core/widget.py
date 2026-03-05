"""MCP Apps widget support for OAS MCP server.

Provides:
  - ``DASHBOARD_HTML``: embedded HTML string for the interactive dashboard
    resource (MIME type ``text/html;profile=mcp-app``).
  - ``extract_plot_data()``: converts OAS result dicts to a JSON-serialisable
    structure that the widget's Plotly renderer consumes.

Interactive plot types (rendered by Plotly.js in the widget):
  lift_distribution, drag_polar, stress_distribution, planform

PNG-fallback plot types (displayed as <img> in the widget):
  convergence, opt_history, opt_dv_evolution, opt_comparison
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Interactive (Plotly-capable) plot types
# ---------------------------------------------------------------------------

_INTERACTIVE_TYPES = frozenset({
    "lift_distribution",
    "drag_polar",
    "stress_distribution",
    "planform",
})

_PNG_FALLBACK_TYPES = frozenset({
    "convergence",
    "opt_history",
    "opt_dv_evolution",
    "opt_comparison",
})


# ---------------------------------------------------------------------------
# Data extraction helpers
# ---------------------------------------------------------------------------


def _extract_lift_distribution(results: dict) -> dict:
    """Extract spanwise Cl data for a Plotly bar/line chart."""
    sectional = results.get("sectional_data", {})
    Cl = sectional.get("Cl")
    y = sectional.get("y_span_norm")

    # Nested by surface name
    if (Cl is None or y is None) and sectional:
        for surf_data in sectional.values():
            if isinstance(surf_data, dict):
                Cl = surf_data.get("Cl")
                y = surf_data.get("y_span_norm")
                if Cl and y:
                    break

    if Cl and y:
        if len(Cl) == len(y) - 1:
            y_plot = [(y[i] + y[i + 1]) / 2.0 for i in range(len(Cl))]
        else:
            y_plot = list(y)
        return {
            "type": "lift_distribution",
            "interactive": True,
            "traces": [
                {
                    "kind": "bar",
                    "x": list(y_plot),
                    "y": list(Cl),
                    "name": "Sectional Cl",
                    "marker_color": "steelblue",
                }
            ],
            "xaxis": {"title": "Normalised spanwise station η = 2y/b  [—]  (0=root, 1=tip)"},
            "yaxis": {"title": "Sectional lift coefficient  Cl  [—]"},
            "title": "Lift Distribution",
        }

    # Fallback: per-surface CL bars
    surfaces = results.get("surfaces", {})
    names = list(surfaces.keys())
    cls = [surfaces[n].get("CL", 0.0) for n in names]
    return {
        "type": "lift_distribution",
        "interactive": True,
        "traces": [
            {
                "kind": "bar",
                "x": names,
                "y": cls,
                "name": "CL per surface",
                "marker_color": "steelblue",
            }
        ],
        "xaxis": {"title": "Surface"},
        "yaxis": {"title": "CL  [—]"},
        "title": "Lift Distribution (per-surface)",
    }


def _extract_drag_polar(results: dict) -> dict:
    """Extract CL/CD/alpha data for a Plotly scatter chart."""
    alphas = results.get("alpha_deg", [])
    CLs = results.get("CL", [])
    CDs = results.get("CD", [])
    LoDs = results.get("L_over_D", [])
    best = results.get("best_L_over_D", {})

    traces = []
    traces.append({
        "kind": "scatter",
        "x": list(CDs),
        "y": list(CLs),
        "name": "Drag Polar (CL vs CD)",
        "mode": "lines+markers",
        "yaxis": "y1",
    })
    if best and best.get("CL") is not None and best.get("CD") is not None:
        traces.append({
            "kind": "scatter",
            "x": [best["CD"]],
            "y": [best["CL"]],
            "name": f"Best L/D = {best.get('L_over_D', 0):.2f}",
            "mode": "markers",
            "marker": {"size": 12, "symbol": "star", "color": "red"},
            "yaxis": "y1",
        })

    valid_ld = [(a, ld) for a, ld in zip(alphas, LoDs) if ld is not None]
    if valid_ld:
        a_vals, ld_vals = zip(*valid_ld)
        traces.append({
            "kind": "scatter",
            "x": list(a_vals),
            "y": list(ld_vals),
            "name": "L/D vs α",
            "mode": "lines+markers",
            "yaxis": "y2",
        })

    return {
        "type": "drag_polar",
        "interactive": True,
        "traces": traces,
        "xaxis": {"title": "CD  [—]  /  α [deg]"},
        "yaxis": {"title": "CL  [—]"},
        "yaxis2": {"title": "L/D  [—]", "overlaying": "y", "side": "right"},
        "title": "Drag Polar",
    }


def _extract_stress_distribution(results: dict) -> dict:
    """Extract spanwise stress/failure index for Plotly."""
    traces = []

    def _elem_y(y_nodes: list, n_elem: int) -> list | None:
        if len(y_nodes) == n_elem:
            return y_nodes
        if len(y_nodes) == n_elem + 1:
            return [(y_nodes[i] + y_nodes[i + 1]) / 2.0 for i in range(n_elem)]
        return None

    for surf_name, surf_res in results.get("surfaces", {}).items():
        sectional = surf_res.get("sectional_data", {})
        y_nodes = sectional.get("y_span_norm")
        vm = sectional.get("vonmises_MPa")
        fi = sectional.get("failure_index")

        if y_nodes and vm:
            y_vm = _elem_y(y_nodes, len(vm))
            if y_vm is not None:
                traces.append({
                    "kind": "bar",
                    "x": list(y_vm),
                    "y": list(vm),
                    "name": f"{surf_name} von Mises [MPa]",
                    "yaxis": "y1",
                })

        if y_nodes and fi:
            y_fi = _elem_y(y_nodes, len(fi))
            if y_fi is not None:
                traces.append({
                    "kind": "scatter",
                    "x": list(y_fi),
                    "y": list(fi),
                    "name": f"{surf_name} failure index",
                    "mode": "lines+markers",
                    "yaxis": "y2",
                })

    # Failure threshold reference line
    traces.append({
        "kind": "hline",
        "y": 1.0,
        "name": "Failure threshold",
        "yaxis": "y2",
        "line": {"color": "red", "dash": "dash"},
    })

    return {
        "type": "stress_distribution",
        "interactive": True,
        "traces": traces,
        "xaxis": {"title": "Normalised spanwise station η  [—]  (0=root, 1=tip)"},
        "yaxis": {"title": "von Mises stress  [MPa]"},
        "yaxis2": {"title": "Failure index  [—]", "overlaying": "y", "side": "right"},
        "title": "Stress Distribution",
    }


def _extract_planform(mesh_data: dict) -> dict:
    """Extract LE/TE coordinates for a Plotly planform view."""
    import numpy as np

    mesh_list = mesh_data.get("mesh")
    if mesh_list is None:
        return {
            "type": "planform",
            "interactive": True,
            "traces": [],
            "xaxis": {"title": "Spanwise y  [m]"},
            "yaxis": {"title": "Chordwise x  [m]"},
            "title": "Wing Planform (no mesh data)",
        }

    mesh = np.array(mesh_list)
    le = mesh[0, :, :]
    te = mesh[-1, :, :]

    traces = [
        {
            "kind": "scatter",
            "x": le[:, 1].tolist(),
            "y": le[:, 0].tolist(),
            "name": "LE (undeformed)",
            "mode": "lines",
            "line": {"color": "blue", "width": 2},
        },
        {
            "kind": "scatter",
            "x": te[:, 1].tolist(),
            "y": te[:, 0].tolist(),
            "name": "TE (undeformed)",
            "mode": "lines",
            "line": {"color": "blue", "width": 1, "dash": "dash"},
        },
        # Root chord
        {
            "kind": "scatter",
            "x": [le[0, 1], te[0, 1]],
            "y": [le[0, 0], te[0, 0]],
            "name": "Root",
            "mode": "lines",
            "line": {"color": "blue", "width": 1},
            "showlegend": False,
        },
        # Tip chord
        {
            "kind": "scatter",
            "x": [le[-1, 1], te[-1, 1]],
            "y": [le[-1, 0], te[-1, 0]],
            "name": "Tip",
            "mode": "lines",
            "line": {"color": "blue", "width": 1},
            "showlegend": False,
        },
    ]

    def_mesh_list = mesh_data.get("def_mesh")
    if def_mesh_list is not None:
        def_mesh = np.array(def_mesh_list)
        def_le = def_mesh[0, :, :]
        def_te = def_mesh[-1, :, :]
        traces.append({
            "kind": "scatter",
            "x": def_le[:, 1].tolist(),
            "y": def_le[:, 0].tolist(),
            "name": "LE (deformed)",
            "mode": "lines",
            "line": {"color": "red", "width": 2},
        })
        traces.append({
            "kind": "scatter",
            "x": def_te[:, 1].tolist(),
            "y": def_te[:, 0].tolist(),
            "name": "TE (deformed)",
            "mode": "lines",
            "line": {"color": "red", "width": 1, "dash": "dash"},
        })

    return {
        "type": "planform",
        "interactive": True,
        "traces": traces,
        "xaxis": {"title": "Spanwise y  [m]"},
        "yaxis": {"title": "Chordwise x  [m]"},
        "title": "Wing Planform",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_plot_data(
    plot_type: str,
    results: dict,
    conv_data: dict | None = None,
    mesh_data: dict | None = None,
    opt_history: dict | None = None,
) -> dict[str, Any]:
    """Extract JSON-serialisable plot data for the widget's Plotly renderer.

    For interactive plot types, returns a dict with ``traces``, axis config,
    and ``interactive: True``.

    For PNG-fallback types, returns ``{"interactive": False}`` — the widget
    will display the base64 PNG image from the tool response instead.

    Parameters
    ----------
    plot_type:
        One of the supported OAS plot types.
    results:
        Aerodynamic/structural results dict (from artifact).
    conv_data:
        Convergence data dict (for convergence plot — unused here, PNG fallback).
    mesh_data:
        Mesh snapshot dict (for planform plot).
    opt_history:
        Optimization history dict (for opt_* plots — PNG fallback).
    """
    if plot_type in _PNG_FALLBACK_TYPES:
        return {"type": plot_type, "interactive": False}

    if plot_type == "lift_distribution":
        return _extract_lift_distribution(results)
    if plot_type == "drag_polar":
        return _extract_drag_polar(results)
    if plot_type == "stress_distribution":
        return _extract_stress_distribution(results)
    if plot_type == "planform":
        return _extract_planform(mesh_data or {})

    return {"type": plot_type, "interactive": False}


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenAeroStruct Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" crossorigin="anonymous"></script>
<style>
  :root {
    --bg: #ffffff;
    --fg: #1a1a1a;
    --card: #f5f5f5;
    --border: #d0d0d0;
    --accent: #0066cc;
    --muted: #666;
    --danger: #c0392b;
    --btn: #e8e8e8;
    --btn-hover: #d8d8d8;
    --plot-bg: #f9f9f9;
  }
  [data-theme="dark"] {
    --bg: #1a1a2e;
    --fg: #e0e0e0;
    --card: #16213e;
    --border: #374151;
    --accent: #4da6ff;
    --muted: #9ca3af;
    --danger: #e74c3c;
    --btn: #2d3748;
    --btn-hover: #3d4a5c;
    --plot-bg: #1e2a3a;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 13px;
    background: var(--bg);
    color: var(--fg);
  }
  /* ---- header ---- */
  #header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    background: var(--bg);
    z-index: 10;
  }
  #header h1 { font-size: 13px; font-weight: 600; }
  #run-label {
    font-size: 11px;
    color: var(--muted);
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  /* ---- chart ---- */
  #status {
    padding: 40px 20px;
    text-align: center;
    color: var(--muted);
    font-size: 14px;
    line-height: 1.6;
  }
  #chart { width: 100%; height: 360px; }
  /* ---- metrics ---- */
  #metrics {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 6px 10px;
    border-top: 1px solid var(--border);
  }
  .metric {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 3px 8px;
    font-size: 11px;
  }
  .metric-label { color: var(--muted); font-size: 9px; text-transform: uppercase; letter-spacing: 0.05em; }
  .metric-value { font-weight: 600; }
  /* ---- browser panel ---- */
  #browser {
    border-top: 2px solid var(--border);
  }
  #browser-header {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 10px;
    background: var(--card);
    border-bottom: 1px solid var(--border);
  }
  #browser-header span { font-weight: 600; font-size: 12px; flex: 1; }
  #run-list { max-height: 220px; overflow-y: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 11px; }
  th {
    background: var(--card);
    padding: 4px 8px;
    text-align: left;
    font-weight: 600;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
  }
  td { padding: 4px 8px; border-bottom: 1px solid var(--border); }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: var(--card); }
  .badge {
    display: inline-block;
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 10px;
    font-weight: 600;
    background: var(--accent);
    color: #fff;
    white-space: nowrap;
  }
  .badge.aero { background: #2980b9; }
  .badge.aerostruct { background: #8e44ad; }
  .badge.drag_polar { background: #27ae60; }
  .badge.stability { background: #e67e22; }
  .badge.optimization { background: #c0392b; }
  /* ---- buttons / selects ---- */
  button {
    padding: 3px 8px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--btn);
    color: var(--fg);
    cursor: pointer;
    font-size: 11px;
    white-space: nowrap;
  }
  button:hover { background: var(--btn-hover); }
  button:disabled { opacity: 0.5; cursor: default; }
  select {
    padding: 2px 4px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--bg);
    color: var(--fg);
    font-size: 11px;
  }
  img.fallback-png { max-width: 100%; display: block; margin: 0 auto; }
  .err { color: var(--danger); }
</style>
</head>
<body>

<div id="header">
  <h1>OpenAeroStruct</h1>
  <span id="run-label">waiting for tool call…</span>
  <button id="browse-btn">Browse Runs ▾</button>
</div>

<div id="status">Run <strong>visualize</strong> in the chat to see an interactive chart,<br>or use <strong>Browse Runs</strong> to explore saved analyses.</div>
<div id="chart" style="display:none"></div>
<div id="metrics" style="display:none"></div>

<div id="browser" style="display:none">
  <div id="browser-header">
    <span>Saved Runs</span>
    <select id="pt-select">
      <option value="lift_distribution">Lift Distribution</option>
      <option value="drag_polar">Drag Polar</option>
      <option value="stress_distribution">Stress Distribution</option>
      <option value="planform">Planform</option>
      <option value="convergence">Convergence</option>
      <option value="opt_history">Opt History</option>
      <option value="opt_dv_evolution">DV Evolution</option>
      <option value="opt_comparison">Opt Comparison</option>
    </select>
    <button id="refresh-btn">↻ Refresh</button>
    <button id="close-btn">✕</button>
  </div>
  <div id="run-list"></div>
</div>

<!-- IMPORTANT: app-with-deps.js is an ES module — must use type="module" import.
     A plain <script src> tag will cause a SyntaxError on the export{} at EOF
     and MCPApps will never be defined as a global. -->
<script type="module">
import { App, applyDocumentTheme, applyHostStyleVariables }
  from 'https://unpkg.com/@modelcontextprotocol/ext-apps@1.1.2/dist/src/app-with-deps.js';

// ---------------------------------------------------------------------------
// App init — set ALL handlers before connect()
// ---------------------------------------------------------------------------
let app;
try {
  app = new App({ name: 'OAS Dashboard', version: '1.0.0' });

  app.onhostcontextchanged = (ctx) => {
    // Apply host-provided theme and CSS variables
    if (ctx.theme?.colorScheme) applyDocumentTheme(ctx.theme.colorScheme);
    if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
  };

  app.ontoolresult = (result) => {
    // result.content is the standard MCP content array
    renderFromContent(Array.isArray(result.content) ? result.content : []);
  };

  await app.connect();

  // Apply initial host context (theme already set via onhostcontextchanged during connect)
  const ctx = app.getHostContext();
  if (ctx?.theme?.colorScheme) applyDocumentTheme(ctx.theme.colorScheme);
  if (ctx?.styles?.variables) applyHostStyleVariables(ctx.styles.variables);

} catch (e) {
  document.getElementById('status').innerHTML =
    'Widget initialisation error:<br><code>' + esc(e.message || String(e)) + '</code>';
}

// ---------------------------------------------------------------------------
// Wire up buttons (can't use onclick= in module context)
// ---------------------------------------------------------------------------
document.getElementById('browse-btn').addEventListener('click', toggleBrowser);
document.getElementById('refresh-btn').addEventListener('click', loadRunList);
document.getElementById('close-btn').addEventListener('click', toggleBrowser);

// ---------------------------------------------------------------------------
// Parse & render
// ---------------------------------------------------------------------------
function parseContent(content) {
  let meta = null;
  let pngDataUrl = null;
  for (const item of content) {
    if (item.type === 'text' && !meta) {
      try { meta = JSON.parse(item.text); } catch (_) {}
    }
    if (item.type === 'image' && item.data && !pngDataUrl) {
      pngDataUrl = 'data:' + (item.mimeType || 'image/png') + ';base64,' + item.data;
    }
  }
  return { meta, pngDataUrl };
}

function renderFromContent(content) {
  const { meta, pngDataUrl } = parseContent(content);
  if (!meta || !meta.plot_type) return;   // not a visualize result — ignore

  const plotData = meta.plot_data || null;
  const runId    = meta.run_id    || '';
  const plotType = meta.plot_type || '';

  document.getElementById('run-label').textContent =
    (runId ? runId.slice(0, 18) + ' · ' : '') + plotType;
  document.getElementById('status').style.display = 'none';
  document.getElementById('chart').style.display = 'block';

  if (plotData?.interactive) {
    renderPlotly(plotData, plotType === 'drag_polar' || plotType === 'stress_distribution');
  } else if (pngDataUrl) {
    renderPNG(pngDataUrl);
  } else {
    document.getElementById('status').textContent = 'No renderable content in tool result.';
    document.getElementById('status').style.display = '';
    document.getElementById('chart').style.display = 'none';
  }

  renderMetrics(meta, plotData);
}

// ---------------------------------------------------------------------------
// Plotly
// ---------------------------------------------------------------------------
function plotlyColors() {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  return {
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: dark ? '#1e2a3a' : '#f9f9f9',
    font: { color: dark ? '#e0e0e0' : '#1a1a1a', size: 11 },
  };
}

function buildTraces(specs) {
  return specs.filter(s => s.kind !== 'hline').map(s => {
    const t = {
      x: s.x, y: s.y, name: s.name,
      yaxis: s.yaxis || 'y',
      showlegend: s.showlegend !== false,
    };
    if (s.kind === 'bar') {
      t.type = 'bar';
      if (s.marker_color) t.marker = { color: s.marker_color };
    } else {
      t.type = 'scatter';
      t.mode = s.mode || 'lines+markers';
      if (s.line)   t.line   = s.line;
      if (s.marker) t.marker = s.marker;
    }
    return t;
  });
}

function buildShapes(specs) {
  return specs.filter(s => s.kind === 'hline').map(s => ({
    type: 'line', xref: 'paper', x0: 0, x1: 1,
    yref: s.yaxis || 'y', y0: s.y, y1: s.y,
    line: s.line || { color: 'red', dash: 'dash' },
  }));
}

function renderPlotly(plotData, dualAxis) {
  const el = document.getElementById('chart');
  el.style.height = '360px';
  const layout = {
    title:  { text: plotData.title || '', font: { size: 13 } },
    xaxis:  plotData.xaxis || {},
    yaxis:  plotData.yaxis || {},
    shapes: buildShapes(plotData.traces || []),
    margin: { t: 46, b: 56, l: 56, r: dualAxis ? 60 : 16 },
    autosize: true,
    ...plotlyColors(),
  };
  if (dualAxis && plotData.yaxis2) {
    layout.yaxis2 = { ...plotData.yaxis2, overlaying: 'y', side: 'right' };
  }
  Plotly.newPlot(el, buildTraces(plotData.traces || []), layout,
    { responsive: true, displayModeBar: true, displaylogo: false });
}

function renderPNG(dataUrl) {
  const el = document.getElementById('chart');
  el.style.height = '';
  el.innerHTML = '';
  const img = document.createElement('img');
  img.src = dataUrl;
  img.className = 'fallback-png';
  img.alt = 'Plot image';
  el.appendChild(img);
}

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------
function renderMetrics(meta, plotData) {
  const container = document.getElementById('metrics');
  container.innerHTML = '';
  const items = [];

  if (plotData?.type === 'lift_distribution') {
    const ys = plotData.traces?.[0]?.y ?? [];
    if (ys.length) {
      items.push({ label: 'Peak Cl', value: Math.max(...ys).toFixed(4) });
      items.push({ label: 'Root Cl', value: ys[0].toFixed(4) });
    }
  }
  if (plotData?.type === 'drag_polar') {
    const t0 = plotData.traces?.[0];
    if (t0?.x?.length) {
      items.push({ label: 'Max CL', value: Math.max(...t0.y).toFixed(4) });
      items.push({ label: 'Min CD', value: Math.min(...t0.x).toFixed(5) });
    }
  }

  container.style.display = items.length ? 'flex' : 'none';
  for (const { label, value } of items) {
    const d = document.createElement('div');
    d.className = 'metric';
    d.innerHTML =
      '<div class="metric-label">' + label + '</div>' +
      '<div class="metric-value">' + value + '</div>';
    container.appendChild(d);
  }
}

// ---------------------------------------------------------------------------
// Run browser
// ---------------------------------------------------------------------------
let browserOpen = false;

function toggleBrowser() {
  browserOpen = !browserOpen;
  document.getElementById('browser').style.display = browserOpen ? 'block' : 'none';
  document.getElementById('browse-btn').textContent = browserOpen ? 'Browse Runs ▴' : 'Browse Runs ▾';
  if (browserOpen && !document.getElementById('run-list').firstChild) {
    loadRunList();   // auto-load on first open
  }
}

async function loadRunList() {
  const listEl = document.getElementById('run-list');
  const btn    = document.getElementById('refresh-btn');
  listEl.innerHTML = '<em style="padding:8px;display:block;color:var(--muted)">Loading…</em>';
  btn.disabled = true;
  try {
    if (!app) throw new Error('App not initialised');
    const result = await app.callServerTool({ name: 'list_artifacts', arguments: {} });
    const content = Array.isArray(result.content) ? result.content : [];
    let data = null;
    for (const item of content) {
      if (item.type === 'text') { try { data = JSON.parse(item.text); } catch (_) {} if (data) break; }
    }
    if (!data || !Array.isArray(data.artifacts)) {
      listEl.innerHTML = '<em class="err" style="padding:8px;display:block">Could not parse artifact list.</em>';
      return;
    }
    renderRunList(data.artifacts);
  } catch (e) {
    listEl.innerHTML =
      '<em class="err" style="padding:8px;display:block">Error: ' + esc(e.message || String(e)) + '</em>';
  } finally {
    btn.disabled = false;
  }
}

function renderRunList(artifacts) {
  const listEl = document.getElementById('run-list');
  if (!artifacts.length) {
    listEl.innerHTML = '<em style="padding:8px;display:block;color:var(--muted)">No saved runs found.</em>';
    return;
  }
  const sorted = [...artifacts].sort((a, b) => b.run_id.localeCompare(a.run_id));

  const table = document.createElement('table');
  table.innerHTML = '<thead><tr><th>Run</th><th>Type</th><th>Surfaces</th><th>Date</th><th></th></tr></thead>';
  const tbody = document.createElement('tbody');

  for (const run of sorted) {
    const tr = document.createElement('tr');
    const ts = run.timestamp
      ? new Date(run.timestamp).toLocaleString(undefined, { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' })
      : '';
    const surfStr  = Array.isArray(run.surfaces) ? run.surfaces.join(', ') : '';
    const label    = run.name || (run.run_id ? run.run_id.slice(0, 15) + '…' : '?');
    const typeKey  = (run.analysis_type || '').replace(/[^a-z_]/g, '');

    tr.innerHTML =
      '<td title="' + esc(run.run_id || '') + '">' + esc(label) + '</td>' +
      '<td><span class="badge ' + typeKey + '">' + esc(run.analysis_type || '?') + '</span></td>' +
      '<td>' + esc(surfStr) + '</td>' +
      '<td>' + esc(ts) + '</td>' +
      '<td></td>';

    const viewBtn = document.createElement('button');
    viewBtn.textContent = 'View';
    viewBtn.addEventListener('click', () => visualizeRun(run.run_id, viewBtn));
    tr.lastElementChild.appendChild(viewBtn);
    tbody.appendChild(tr);
  }

  table.appendChild(tbody);
  listEl.innerHTML = '';
  listEl.appendChild(table);
}

async function visualizeRun(runId, btn) {
  const plotType = document.getElementById('pt-select').value;
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const result = await app.callServerTool({
      name: 'visualize',
      arguments: { run_id: runId, plot_type: plotType },
    });
    renderFromContent(Array.isArray(result.content) ? result.content : []);
  } catch (e) {
    document.getElementById('status').textContent = 'Error: ' + esc(e.message || String(e));
    document.getElementById('status').style.display = '';
    document.getElementById('chart').style.display = 'none';
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

function esc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
</script>
</body>
</html>
"""
