"""
Visualization parity tests — verify MCP plot data matches OAS reference.

Compares the data extracted by extract_standard_detail() against the
reference computation from openaerostruct/utils/plot_wing.py.
"""
from __future__ import annotations

import numpy as np
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.slow]


def _r(envelope: dict) -> dict:
    return envelope["results"]


# ---------------------------------------------------------------------------
# Helpers: reference extraction (mirrors plot_wing.py logic)
# ---------------------------------------------------------------------------


def _ref_lift_loading(prob, surface_name, point_name, is_aerostruct=False):
    """Compute lift loading the way plot_wing.py does.

    Returns (lift_loading, span_stations, lift_elliptical).
    """
    prefix = f"{point_name}.coupled." if is_aerostruct else f"{point_name}."
    sf_path = f"{prefix}aero_states.{surface_name}_sec_forces"
    w_path = f"{prefix}{surface_name}.widths"

    sec_forces = prob.get_val(sf_path)  # shape (nx-1, ny-1, 3)
    widths = prob.get_val(w_path)       # shape (ny-1,)
    alpha_deg = float(np.asarray(prob.get_val("alpha")).ravel()[0])
    alpha = alpha_deg * np.pi / 180.0
    rho = float(np.asarray(prob.get_val("rho")).ravel()[0])
    v = float(np.asarray(prob.get_val("v")).ravel()[0])

    cosa = np.cos(alpha)
    sina = np.sin(alpha)
    forces = np.sum(sec_forces, axis=0)  # sum chordwise: (ny-1, 3)
    lift = (-forces[:, 0] * sina + forces[:, 2] * cosa) / widths / 0.5 / rho / v**2

    # Span stations for elliptical overlay (from plot_wing.py lines 349-354)
    mesh_path = f"{surface_name}.mesh" if not is_aerostruct else f"{surface_name}.mesh"
    mesh = prob.get_val(mesh_path)  # shape (nx, ny, 3)
    m_vals = mesh.copy()
    span = m_vals[0, :, 1] / (m_vals[0, -1, 1] - m_vals[0, 0, 1])
    span = span - (span[0] + 0.5)
    span_diff = ((m_vals[0, :-1, 1] + m_vals[0, 1:, 1]) / 2 - m_vals[0, 0, 1]) * 2 / (
        m_vals[0, -1, 1] - m_vals[0, 0, 1]
    ) - 1

    lift_area = np.sum(lift * (span[1:] - span[:-1]))
    lift_ell = 4 * lift_area / np.pi * np.sqrt(np.clip(1 - (2 * span) ** 2, 0, None))

    return lift, span_diff, lift_ell, span


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def aero_crm_results():
    """Run a CRM aero analysis and return (envelope, prob, surface_dicts)."""
    from oas_mcp.server import create_surface, reset, _sessions

    await reset()
    await create_surface(
        name="wing", wing_type="CRM",
        num_x=2, num_y=7, num_twist_cp=5, symmetry=True,
        CD0=0.015, with_viscous=True, with_wave=False,
    )
    session = _sessions.get("default")
    surfaces = ["wing"]
    surface_dicts = session.get_surfaces(surfaces)

    from oas_mcp.core.builders import build_aero_problem
    prob = build_aero_problem(
        surface_dicts,
        velocity=248.136, alpha=5.0, Mach_number=0.84,
        reynolds_number=1e6, density=0.38,
    )
    prob.run_model()

    return prob, surface_dicts


@pytest_asyncio.fixture
async def aerostruct_crm_results():
    """Run a CRM aerostruct analysis and return (prob, surface_dicts)."""
    from oas_mcp.server import create_surface, reset, _sessions

    await reset()
    await create_surface(
        name="wing", wing_type="CRM",
        num_x=2, num_y=7, num_twist_cp=5, symmetry=True,
        CD0=0.015, with_viscous=True, with_wave=False,
        fem_model_type="tube",
        thickness_cp=[0.3, 0.2, 0.1],
        E=70e9, G=30e9, yield_stress=500e6, safety_factor=2.5, mrho=3e3,
        wing_weight_ratio=2.0, struct_weight_relief=False,
        distributed_fuel_weight=False,
    )
    session = _sessions.get("default")
    surfaces = ["wing"]
    surface_dicts = session.get_surfaces(surfaces)

    from oas_mcp.core.builders import build_aerostruct_problem
    prob = build_aerostruct_problem(
        surface_dicts,
        velocity=248.136, alpha=5.0, Mach_number=0.84,
        reynolds_number=1e6, density=0.38,
        W0=120000.0, R=11.165e6, speed_of_sound=295.4,
    )
    prob.run_model()

    return prob, surface_dicts


# ---------------------------------------------------------------------------
# Phase 1: Lift distribution parity
# ---------------------------------------------------------------------------


class TestLiftDistributionParity:
    """Verify MCP lift data matches plot_wing.py reference."""

    @pytest.mark.asyncio
    async def test_mcp_extracts_lift_loading(self, aero_crm_results):
        """After fix: extract_standard_detail should include lift_loading."""
        from oas_mcp.core.results import extract_standard_detail

        prob, surface_dicts = aero_crm_results
        standard = extract_standard_detail(prob, surface_dicts, "aero", "aero")
        sect = standard["sectional_data"]["wing"]

        assert "lift_loading" in sect, (
            "extract_standard_detail should extract lift_loading (c*Cl)"
        )
        assert "lift_elliptical" in sect, (
            "extract_standard_detail should compute elliptical overlay"
        )

    @pytest.mark.asyncio
    async def test_lift_loading_matches_reference(self, aero_crm_results):
        """MCP lift_loading must match plot_wing.py computation."""
        from oas_mcp.core.results import extract_standard_detail

        prob, surface_dicts = aero_crm_results
        standard = extract_standard_detail(prob, surface_dicts, "aero", "aero")
        sect = standard["sectional_data"]["wing"]

        # Reference computation (OAS tip→root order)
        ref_lift, ref_span_diff, ref_ell, ref_span = _ref_lift_loading(
            prob, "wing", "aero", is_aerostruct=False
        )

        # MCP stores root→tip; reference is tip→root. Compare sorted values.
        mcp_lift = np.array(sect["lift_loading"])
        np.testing.assert_allclose(
            mcp_lift, ref_lift[::-1], rtol=1e-10,
            err_msg="lift_loading must match plot_wing.py reference (root→tip)"
        )

    @pytest.mark.asyncio
    async def test_elliptical_overlay_half_span(self, aero_crm_results):
        """MCP elliptical overlay should peak at root and go to zero at tip."""
        from oas_mcp.core.results import extract_standard_detail

        prob, surface_dicts = aero_crm_results
        standard = extract_standard_detail(prob, surface_dicts, "aero", "aero")
        sect = standard["sectional_data"]["wing"]
        y = sect["y_span_norm"]
        ell = np.array(sect["lift_elliptical"])

        # η=0 (root) should have the maximum value
        assert ell[0] == max(ell), (
            f"Elliptical should peak at root (η=0): ell[0]={ell[0]}, max={max(ell)}"
        )
        # η=1 (tip) should be zero
        assert ell[-1] == pytest.approx(0.0, abs=1e-10), (
            f"Elliptical should be zero at tip (η=1): ell[-1]={ell[-1]}"
        )
        # Should be monotonically decreasing from root to tip
        for i in range(len(ell) - 1):
            assert ell[i] >= ell[i + 1], (
                f"Elliptical should decrease root→tip: ell[{i}]={ell[i]} < ell[{i+1}]={ell[i+1]}"
            )

    @pytest.mark.asyncio
    async def test_y_span_norm_ascending(self, aero_crm_results):
        """y_span_norm should be in ascending order (root=0 to tip=1)."""
        from oas_mcp.core.results import extract_standard_detail

        prob, surface_dicts = aero_crm_results
        standard = extract_standard_detail(prob, surface_dicts, "aero", "aero")
        sect = standard["sectional_data"]["wing"]
        y = sect["y_span_norm"]

        assert y == sorted(y), (
            f"y_span_norm should be ascending (root→tip): got {y}"
        )


# ---------------------------------------------------------------------------
# Phase 2: Stress distribution parity (aerostruct)
# ---------------------------------------------------------------------------


class TestStressDistributionParity:
    """Verify MCP stress data matches OAS reference."""

    @pytest.mark.asyncio
    async def test_vonmises_matches_reference(self, aerostruct_crm_results):
        """MCP vonmises_MPa must match max(vonmises, axis=1) from OAS."""
        from oas_mcp.core.results import extract_standard_detail

        prob, surface_dicts = aerostruct_crm_results
        standard = extract_standard_detail(prob, surface_dicts, "aerostruct", "AS_point_0")
        sect = standard["sectional_data"]["wing"]

        # Reference: direct extraction from OAS
        vm_raw = prob.get_val("AS_point_0.wing_perf.vonmises")
        ref_vm = np.max(vm_raw, axis=1) / 1e6  # Pa → MPa

        mcp_vm = np.array(sect["vonmises_MPa"])

        # Both should have the same values (order may differ after sort fix)
        np.testing.assert_allclose(
            np.sort(mcp_vm), np.sort(ref_vm), rtol=1e-10,
            err_msg="vonmises_MPa values must match OAS reference"
        )

    @pytest.mark.asyncio
    async def test_vonmises_ordered_root_to_tip(self, aerostruct_crm_results):
        """vonmises_MPa should be ordered root (high stress) to tip (low stress)."""
        from oas_mcp.core.results import extract_standard_detail

        prob, surface_dicts = aerostruct_crm_results
        standard = extract_standard_detail(prob, surface_dicts, "aerostruct", "AS_point_0")
        sect = standard["sectional_data"]["wing"]
        y = sect["y_span_norm"]

        assert y == sorted(y), (
            f"y_span_norm should be ascending (root→tip): got {y}"
        )


# ---------------------------------------------------------------------------
# Phase 3: Mesh snapshot parity
# ---------------------------------------------------------------------------


class TestMeshSnapshotParity:
    """Verify mesh snapshot stores full mesh for 3D wireframe."""

    @pytest.mark.asyncio
    async def test_full_mesh_stored(self, aero_crm_results):
        """mesh_snapshot should contain the full mesh array, not just LE/TE."""
        from oas_mcp.core.results import extract_standard_detail

        prob, surface_dicts = aero_crm_results
        standard = extract_standard_detail(prob, surface_dicts, "aero", "aero")
        snap = standard["mesh_snapshot"]["wing"]

        assert "mesh" in snap, "mesh_snapshot should contain full 'mesh' array"
        mesh = np.array(snap["mesh"])
        assert mesh.ndim == 3, f"mesh should be 3D [nx, ny, 3], got shape {mesh.shape}"
        assert mesh.shape[0] == snap["nx"]
        assert mesh.shape[1] == snap["ny"]
        assert mesh.shape[2] == 3

    @pytest.mark.asyncio
    async def test_def_mesh_stored_for_aerostruct(self, aerostruct_crm_results):
        """mesh_snapshot should include def_mesh for aerostruct analyses."""
        from oas_mcp.core.results import extract_standard_detail

        prob, surface_dicts = aerostruct_crm_results
        standard = extract_standard_detail(prob, surface_dicts, "aerostruct", "AS_point_0")
        snap = standard["mesh_snapshot"]["wing"]

        assert "def_mesh" in snap, "aerostruct mesh_snapshot should include 'def_mesh'"
        def_mesh = np.array(snap["def_mesh"])
        assert def_mesh.shape == tuple([snap["nx"], snap["ny"], 3])

    @pytest.mark.asyncio
    async def test_aerostruct_lift_loading_matches_reference(self, aerostruct_crm_results):
        """lift_loading in aerostruct mode must match plot_wing.py computation."""
        from oas_mcp.core.results import extract_standard_detail

        prob, surface_dicts = aerostruct_crm_results
        standard = extract_standard_detail(prob, surface_dicts, "aerostruct", "AS_point_0")
        sect = standard["sectional_data"]["wing"]

        assert "lift_loading" in sect
        ref_lift, _, _, _ = _ref_lift_loading(
            prob, "wing", "AS_point_0", is_aerostruct=True
        )

        # MCP stores root→tip; reference is tip→root.
        mcp_lift = np.array(sect["lift_loading"])
        np.testing.assert_allclose(
            mcp_lift, ref_lift[::-1], rtol=1e-10,
            err_msg="aerostruct lift_loading must match plot_wing.py reference (root→tip)"
        )
