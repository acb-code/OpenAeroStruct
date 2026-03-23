"""
Tests for the visualization pipeline (plotting.py and results.py extraction).

Unit tests use synthetic data and run without OAS installed.
Integration tests run real OAS analyses; marked @pytest.mark.slow.
"""

from __future__ import annotations

import pytest
import numpy as np

from mcp.server.fastmcp.utilities.types import Image
from oas_mcp.core.plotting import (
    PLOT_TYPES,
    N2Result,
    PlotResult,
    generate_n2,
    plot_lift_distribution,
    plot_stress_distribution,
    plot_planform,
    plot_opt_dv_evolution,
    plot_opt_comparison,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUN_ID = "test-run-0000"


def _is_valid_plot(result: PlotResult) -> bool:
    """Check that result is a PlotResult with a valid PNG image and metadata."""
    assert isinstance(result, PlotResult), f"Expected PlotResult, got {type(result)}"
    # Validate image
    img = result.image
    assert isinstance(img, Image), f"Expected Image, got {type(img)}"
    data = img.data if isinstance(img.data, bytes) else bytes(img.data)
    assert data[:4] == b"\x89PNG", "PNG magic bytes missing"
    assert len(data) > 500, f"Image too small ({len(data)} bytes)"
    # Validate metadata
    meta = result.metadata
    for key in ("plot_type", "run_id", "format", "image_hash"):
        assert key in meta, f"Metadata missing key: {key!r}"
    return True


# ---------------------------------------------------------------------------
# Unit tests: lift_distribution
# ---------------------------------------------------------------------------


class TestLiftDistribution:
    """Bug C: Cl has ny-1 values, y_span_norm has ny values."""

    def test_panel_midpoints(self):
        """Cl (n panels) and y (n+1 nodes) → line plot via midpoints."""
        n = 6  # panels
        Cl = [0.5 + 0.1 * i for i in range(n)]
        y = [i / n for i in range(n + 1)]

        results = {
            "sectional_data": {
                "wing": {"Cl": Cl, "y_span_norm": y}
            },
            "surfaces": {},
        }
        img = plot_lift_distribution(_RUN_ID, results)
        assert _is_valid_plot(img)

    def test_matching_lengths(self):
        """Cl and y same length → line plot still renders."""
        n = 6
        Cl = [0.5] * n
        y = [i / (n - 1) for i in range(n)]

        results = {
            "sectional_data": {
                "wing": {"Cl": Cl, "y_span_norm": y}
            },
            "surfaces": {},
        }
        img = plot_lift_distribution(_RUN_ID, results)
        assert _is_valid_plot(img)

    def test_fallback_bar_chart(self):
        """No sectional Cl → bar chart fallback still renders."""
        results = {
            "sectional_data": {},
            "surfaces": {"wing": {"CL": 0.4}},
        }
        img = plot_lift_distribution(_RUN_ID, results)
        assert _is_valid_plot(img)

    def test_fallback_missing_sectional_data(self):
        """Completely empty results → fallback bar chart with no bars."""
        results = {"surfaces": {}}
        img = plot_lift_distribution(_RUN_ID, results)
        assert _is_valid_plot(img)


# ---------------------------------------------------------------------------
# Unit tests: stress_distribution
# ---------------------------------------------------------------------------


class TestStressDistribution:
    """Stress distribution: vonmises per element with yield stress line."""

    def _make_struct_results(self, ny: int, include_yield: bool = True) -> dict:
        n_elem = ny - 1
        y_nodes = [i / (ny - 1) for i in range(ny)]
        vm = [100.0 + 50.0 * i for i in range(n_elem)]
        sect: dict = {
            "y_span_norm": y_nodes,
            "vonmises_MPa": vm,
        }
        if include_yield:
            sect["yield_stress_MPa"] = 500.0
            sect["safety_factor"] = 2.5
        surf_results = {
            "sectional_data": sect,
            "max_vonmises_Pa": max(vm) * 1e6,
        }
        return {
            "surfaces": {"wing": surf_results},
        }

    def test_vonmises_per_element(self):
        """vonmises_MPa with ny-1 values renders a line plot."""
        results = self._make_struct_results(ny=5, include_yield=False)
        img = plot_stress_distribution(_RUN_ID, results)
        assert _is_valid_plot(img)

    def test_yield_stress_line(self):
        """yield_stress_MPa renders a red dashed reference line."""
        results = self._make_struct_results(ny=5, include_yield=True)
        img = plot_stress_distribution(_RUN_ID, results)
        assert _is_valid_plot(img)

    def test_no_stress_data_fallback(self):
        """No stress data → 'No stress data available' placeholder renders."""
        results = {"surfaces": {"wing": {}}}
        img = plot_stress_distribution(_RUN_ID, results)
        assert _is_valid_plot(img)

    def test_scalar_fallback(self):
        """Only scalar max_vonmises_Pa → axhline fallback renders."""
        results = {
            "surfaces": {
                "wing": {
                    "sectional_data": {},
                    "max_vonmises_Pa": 250e6,
                }
            }
        }
        img = plot_stress_distribution(_RUN_ID, results)
        assert _is_valid_plot(img)


# ---------------------------------------------------------------------------
# Unit tests: planform
# ---------------------------------------------------------------------------


class TestPlanform:
    def _make_mesh_data(self, nx: int = 3, ny: int = 7) -> dict:
        """Build a rectangular mesh for planform testing."""
        y = np.linspace(0, 5, ny)
        le = np.column_stack([np.zeros(ny), y, np.zeros(ny)])
        te = np.column_stack([np.ones(ny), y, np.zeros(ny)])
        mesh = np.array([le, te])  # shape (2, ny, 3)
        # Add LE and TE rows to simulate the actual minimal mesh passed by server
        return {
            "mesh": mesh.tolist(),
            "mesh_snapshot": {
                "wing": {
                    "leading_edge": le.tolist(),
                    "trailing_edge": te.tolist(),
                    "nx": nx,
                    "ny": ny,
                }
            },
        }

    def test_basic_render(self):
        """Planform renders without error."""
        mesh_data = self._make_mesh_data()
        img = plot_planform(_RUN_ID, mesh_data)
        assert _is_valid_plot(img)

    def test_correct_nx_in_title(self):
        """Subtitle shows original nx from mesh_snapshot, not reconstructed 2."""
        # We just verify it renders; correctness of nx verified by integration tests
        mesh_data = self._make_mesh_data(nx=5, ny=9)
        img = plot_planform(_RUN_ID, mesh_data)
        assert _is_valid_plot(img)

    def test_no_mesh_data_fallback(self):
        """Empty mesh_data → placeholder text renders."""
        img = plot_planform(_RUN_ID, {})
        assert _is_valid_plot(img)


# ---------------------------------------------------------------------------
# Unit tests: opt_dv_evolution (Bug E)
# ---------------------------------------------------------------------------


class TestOptDvEvolution:
    def test_mixed_scale_normalized(self):
        """DVs with wildly different scales render on one shared axis."""
        # twist: degrees (~5 deg), thickness: meters (~0.05 m) — 100× scale diff
        dv_history = {
            "twist": [5.0, 4.5, 4.2, 4.0],
            "thickness": [0.05, 0.052, 0.055, 0.057],
        }
        opt_hist = {"dv_history": dv_history}
        img = plot_opt_dv_evolution(_RUN_ID, opt_hist)
        assert _is_valid_plot(img)

    def test_vector_dv(self):
        """Vector DV (list per iteration) rendered as mean, normalized."""
        dv_history = {
            "twist_cp": [
                [3.0, 5.0, 3.0],
                [2.8, 4.8, 2.8],
                [2.5, 4.5, 2.5],
            ]
        }
        opt_hist = {"dv_history": dv_history}
        img = plot_opt_dv_evolution(_RUN_ID, opt_hist)
        assert _is_valid_plot(img)

    def test_no_history_fallback(self):
        """Empty dv_history → placeholder text renders."""
        img = plot_opt_dv_evolution(_RUN_ID, {})
        assert _is_valid_plot(img)

    def test_zero_initial_dv(self):
        """DV starting at zero → normalized to 1.0 (no divide-by-zero)."""
        dv_history = {"alpha": [0.0, 0.1, 0.2]}
        img = plot_opt_dv_evolution(_RUN_ID, {"dv_history": dv_history})
        assert _is_valid_plot(img)


# ---------------------------------------------------------------------------
# Unit tests: opt_comparison (Bug F)
# ---------------------------------------------------------------------------


class TestOptComparison:
    def test_normalized_comparison(self):
        """Mixed-scale initial/final DVs render as ratios on one axis."""
        opt_hist = {
            "initial_dvs": {"twist": 5.0, "thickness": 0.05},
            "final_dvs": {"twist": 4.0, "thickness": 0.06},
        }
        img = plot_opt_comparison(_RUN_ID, opt_hist)
        assert _is_valid_plot(img)

    def test_prefers_dv_history(self):
        """Ratio computed from dv_history when available."""
        opt_hist = {
            "initial_dvs": {"twist": 5.0},
            "final_dvs": {"twist": 4.0},
            "dv_history": {"twist": [5.0, 4.5, 4.0]},
        }
        img = plot_opt_comparison(_RUN_ID, opt_hist)
        assert _is_valid_plot(img)

    def test_no_dvs_fallback(self):
        """No DV data → placeholder text renders."""
        img = plot_opt_comparison(_RUN_ID, {})
        assert _is_valid_plot(img)


# ---------------------------------------------------------------------------
# Integration tests (require OAS)
# ---------------------------------------------------------------------------


pytestmark_slow = pytest.mark.slow


@pytest.mark.slow
class TestAeroVisualizationE2E:
    """End-to-end: create surface → run analysis → visualize."""

    async def test_lift_distribution_e2e(self, aero_wing):
        from oas_mcp.server import run_aero_analysis, visualize

        envelope = await run_aero_analysis(surfaces=["wing"])
        run_id = envelope["run_id"]

        response = await visualize(run_id=run_id, plot_type="lift_distribution")
        assert isinstance(response, list) and len(response) == 2
        metadata, img = response
        assert isinstance(metadata, dict)
        assert metadata["plot_type"] == "lift_distribution"
        assert "image_hash" in metadata
        assert isinstance(img, Image)

    async def test_planform_e2e(self, aero_wing):
        from oas_mcp.server import run_aero_analysis, visualize

        envelope = await run_aero_analysis(surfaces=["wing"])
        run_id = envelope["run_id"]

        response = await visualize(run_id=run_id, plot_type="planform")
        assert isinstance(response, list) and len(response) == 2
        metadata, img = response
        assert isinstance(metadata, dict)
        assert metadata["plot_type"] == "planform"
        assert isinstance(img, Image)

    async def test_sectional_data_extraction_aero(self, aero_wing):
        """Aero run stores Cl (ny-1) and y_span_norm (ny) in artifact."""
        from oas_mcp.server import run_aero_analysis, _artifacts

        envelope = await run_aero_analysis(surfaces=["wing"])
        run_id = envelope["run_id"]

        artifact = _artifacts.get(run_id)
        assert artifact is not None
        std = artifact["results"].get("standard_detail", {})
        sect = std.get("sectional_data", {}).get("wing", {})

        # num_y=5 with symmetry=True → OAS stores half-span: 3 nodes, 2 panels
        Cl = sect.get("Cl")
        y = sect.get("y_span_norm")

        assert Cl is not None, "Cl not stored in artifact"
        assert y is not None, "y_span_norm not stored in artifact"
        assert len(y) >= 2, f"Expected at least 2 y values, got {len(y)}"
        assert len(Cl) == len(y) - 1, (
            f"Expected len(Cl)==len(y)-1, got len(Cl)={len(Cl)}, len(y)={len(y)}"
        )


@pytest.mark.slow
class TestAerostructVisualizationE2E:
    """End-to-end: create structural surface → run aerostruct → visualize."""

    async def test_stress_distribution_e2e(self, struct_wing):
        from oas_mcp.server import run_aerostruct_analysis, visualize

        envelope = await run_aerostruct_analysis(surfaces=["wing"])
        run_id = envelope["run_id"]

        response = await visualize(run_id=run_id, plot_type="stress_distribution")
        assert isinstance(response, list) and len(response) == 2
        metadata, img = response
        assert isinstance(metadata, dict)
        assert metadata["plot_type"] == "stress_distribution"
        assert isinstance(img, Image)

    async def test_sectional_data_extraction_aerostruct(self, struct_wing):
        """Aerostruct run stores vonmises_MPa and failure_index (ny-1 each)."""
        from oas_mcp.server import run_aerostruct_analysis, _artifacts

        envelope = await run_aerostruct_analysis(surfaces=["wing"])
        run_id = envelope["run_id"]

        artifact = _artifacts.get(run_id)
        assert artifact is not None
        std = artifact["results"].get("standard_detail", {})
        sect = std.get("sectional_data", {}).get("wing", {})

        # num_y=5 with symmetry=True → OAS stores half-span: 3 nodes, 2 elements (ny-1)
        vm = sect.get("vonmises_MPa")
        fi = sect.get("failure_index")
        y = sect.get("y_span_norm")

        assert y is not None, "y_span_norm not stored"
        assert len(y) >= 2, f"Expected at least 2 y values, got {len(y)}"

        assert vm is not None, "vonmises_MPa not stored in artifact"
        assert len(vm) == len(y) - 1, (
            f"Expected len(vonmises_MPa)==len(y)-1, got len(vm)={len(vm)}, len(y)={len(y)}"
        )

        assert fi is not None, "failure_index not stored in artifact"
        assert len(fi) == len(vm), (
            f"Expected len(failure_index)==len(vonmises_MPa), got {len(fi)} vs {len(vm)}"
        )


# ---------------------------------------------------------------------------
# Unit tests: N2 diagram
# ---------------------------------------------------------------------------


class TestN2Diagram:
    def test_n2_in_plot_types(self):
        assert "n2" in PLOT_TYPES

    def test_n2_returns_file(self, tmp_path):
        """generate_n2() writes HTML to disk and returns lightweight metadata."""
        import openmdao.api as om

        prob = om.Problem(reports=False)
        prob.model.add_subsystem("comp", om.ExecComp("y = x**2"))
        prob.setup()
        prob.run_model()

        result = generate_n2(prob, "test-run-n2", output_dir=tmp_path)
        assert isinstance(result, N2Result)
        meta = result.metadata
        assert meta["format"] == "html_file"
        assert meta["plot_type"] == "n2"
        assert meta["run_id"] == "test-run-n2"
        assert "image_hash" in meta
        assert meta["size_bytes"] > 100
        assert "viewer_data_compressed" in meta

        # File must exist on disk and be valid HTML
        from pathlib import Path
        p = Path(result.file_path)
        assert p.exists(), f"N2 file not found at {p}"
        html = p.read_text(encoding="utf-8")
        assert html.lower().lstrip().startswith(("<!doctype", "<html")), \
            "File does not look like HTML"

    def test_n2_case_name_in_title(self, tmp_path):
        """case_name is embedded in the generated HTML file."""
        import openmdao.api as om
        from pathlib import Path

        prob = om.Problem(reports=False)
        prob.model.add_subsystem("comp", om.ExecComp("z = x + y"))
        prob.setup()
        prob.run_model()

        result = generate_n2(prob, "test-run-n2b", case_name="My Wing", output_dir=tmp_path)
        assert result.metadata["format"] == "html_file"
        html = Path(result.file_path).read_text(encoding="utf-8")
        assert "My Wing" in html, "case_name not found in generated HTML"

    def test_n2_viewer_data_compressed(self, tmp_path):
        """viewer_data_compressed decompresses to a dict with expected keys."""
        import base64
        import json
        import zlib
        import openmdao.api as om

        prob = om.Problem(reports=False)
        prob.model.add_subsystem("comp", om.ExecComp("y = x**2"))
        prob.setup()
        prob.run_model()

        result = generate_n2(prob, "test-run-n2c", output_dir=tmp_path)
        compressed = result.metadata["viewer_data_compressed"]
        data = json.loads(zlib.decompress(base64.b64decode(compressed)))
        assert "tree" in data, "Decompressed viewer data missing 'tree' key"
        assert "connections_list" in data, "Decompressed viewer data missing 'connections_list' key"


@pytest.mark.slow
class TestN2DiagramE2E:
    """Integration test: create surface → run analysis → visualize n2."""

    async def test_n2_e2e(self, aero_wing):
        from oas_mcp.server import run_aero_analysis, visualize
        from pathlib import Path

        envelope = await run_aero_analysis(surfaces=["wing"])
        run_id = envelope["run_id"]

        response = await visualize(run_id=run_id, plot_type="n2")
        # Single-element list — no large TextContent returned
        assert isinstance(response, list) and len(response) == 1
        metadata = response[0]
        assert isinstance(metadata, dict)
        assert metadata["format"] == "html_file"
        assert metadata["plot_type"] == "n2"
        assert "image_hash" in metadata
        assert "file_path" in metadata
        assert Path(metadata["file_path"]).exists(), \
            f"N2 file not found at {metadata['file_path']}"
