"""
Tests for composite material support in OAS MCP tools.

Uses IM7/8552 carbon fiber properties from the OAS examples.
"""

import pytest
import pytest_asyncio
from oas_mcp.server import create_surface, reset, run_aerostruct_analysis

pytestmark = pytest.mark.slow

# IM7/8552 carbon fiber composite properties
COMPOSITE_PROPS = dict(
    use_composite=True,
    ply_angles=[0, 45, -45, 90],
    ply_fractions=[0.4441, 0.222, 0.222, 0.1119],
    E1=117.7e9,
    E2=10.1e9,
    nu12=0.30,
    G12=5.2e9,
    sigma_t1=1648.0e6,
    sigma_c1=1034.0e6,
    sigma_t2=64.0e6,
    sigma_c2=228.0e6,
    sigma_12max=118.0e6,
)

SMALL_COMPOSITE_WINGBOX = dict(
    name="wing",
    wing_type="rect",
    span=10.0,
    root_chord=1.0,
    num_x=2,
    num_y=5,
    symmetry=True,
    with_viscous=True,
    fem_model_type="wingbox",
    safety_factor=1.5,
    mrho=1580.0,
    **COMPOSITE_PROPS,
)


def _r(envelope: dict) -> dict:
    """Extract results from versioned response envelope."""
    assert "schema_version" in envelope, f"Not an envelope: {list(envelope)}"
    return envelope["results"]


# ---------------------------------------------------------------------------
# create_surface — composite validation
# ---------------------------------------------------------------------------


class TestCreateComposite:
    async def test_create_composite_surface(self):
        result = await create_surface(**SMALL_COMPOSITE_WINGBOX)
        assert result["surface_name"] == "wing"
        assert result["use_composite"] is True
        assert result["has_structure"] is True
        assert "effective_E" in result
        assert "effective_G" in result
        # Effective E should differ from aluminum default (70 GPa)
        assert result["effective_E"] != 70.0e9
        assert result["effective_E"] > 0
        assert result["effective_G"] > 0

    async def test_composite_stiffness_stored(self):
        """Verify that compute_composite_stiffness overwrites E/G in surface dict."""
        await create_surface(**SMALL_COMPOSITE_WINGBOX)
        from oas_mcp.server import _sessions

        surf = _sessions.get("default").surfaces["wing"]
        assert surf["useComposite"] is True
        # E and G should be effective laminate values, not aluminum defaults
        assert surf["E"] != 70.0e9
        assert surf["G"] != 30.0e9

    async def test_composite_requires_wingbox(self):
        with pytest.raises(ValueError, match="wingbox"):
            await create_surface(
                name="wing",
                wing_type="rect",
                num_x=2,
                num_y=5,
                fem_model_type="tube",
                **COMPOSITE_PROPS,
            )

    async def test_composite_missing_params(self):
        props = {**COMPOSITE_PROPS}
        del props["E1"]
        with pytest.raises(ValueError, match="E1"):
            await create_surface(
                name="wing",
                wing_type="rect",
                num_x=2,
                num_y=5,
                fem_model_type="wingbox",
                **props,
            )

    async def test_composite_ply_fraction_sum(self):
        props = {**COMPOSITE_PROPS}
        props["ply_fractions"] = [0.1, 0.1, 0.1, 0.1]  # sum = 0.4
        with pytest.raises(ValueError, match="sum to 1.0"):
            await create_surface(
                name="wing",
                wing_type="rect",
                num_x=2,
                num_y=5,
                fem_model_type="wingbox",
                **props,
            )

    async def test_composite_ply_length_mismatch(self):
        props = {**COMPOSITE_PROPS}
        props["ply_fractions"] = [0.5, 0.5]  # 2 fractions, 4 angles
        with pytest.raises(ValueError, match="same length"):
            await create_surface(
                name="wing",
                wing_type="rect",
                num_x=2,
                num_y=5,
                fem_model_type="wingbox",
                **props,
            )

    async def test_isotropic_unchanged(self):
        """Isotropic wingbox still works identically."""
        result = await create_surface(
            name="wing",
            wing_type="rect",
            span=10.0,
            root_chord=1.0,
            num_x=2,
            num_y=5,
            symmetry=True,
            fem_model_type="wingbox",
            E=73.1e9,
            G=27.5e9,
            yield_stress=420.0e6,
            safety_factor=1.5,
            mrho=2.78e3,
        )
        assert result["use_composite"] is False
        assert result["has_structure"] is True
        assert "effective_E" not in result


# ---------------------------------------------------------------------------
# run_aerostruct_analysis — composite integration
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def composite_wing():
    """Create a small composite wingbox surface."""
    await create_surface(**SMALL_COMPOSITE_WINGBOX)
    return "wing"


class TestAerostructComposite:
    async def test_aerostruct_composite(self, composite_wing):
        envelope = await run_aerostruct_analysis(
            surfaces=["wing"],
            velocity=248.0,
            alpha=5.0,
            Mach_number=0.84,
            reynolds_number=1e6,
            density=0.38,
        )
        results = _r(envelope)

        assert "CL" in results
        assert "CD" in results
        assert results["CL"] > 0

        # Per-surface checks
        wing = results["surfaces"]["wing"]
        assert wing["material_model"] == "composite"
        assert "failure" in wing
        assert "max_tsaiwu_sr" in wing
        assert "max_vonmises_Pa" not in wing
        assert wing["max_tsaiwu_sr"] > 0

    async def test_composite_standard_detail(self, composite_wing):
        envelope = await run_aerostruct_analysis(
            surfaces=["wing"],
            velocity=248.0,
            alpha=5.0,
            Mach_number=0.84,
            reynolds_number=1e6,
            density=0.38,
        )
        run_id = envelope["run_id"]

        # Standard detail is stored in the artifact, not in the envelope results
        from oas_mcp.server import _artifacts

        artifact = _artifacts.get(run_id)
        standard = artifact["results"].get("standard_detail", {})
        sectional = standard.get("sectional_data", {}).get("wing", {})
        assert sectional.get("material_model") == "composite"
        assert "tsaiwu_sr_max" in sectional
        assert "vonmises_MPa" not in sectional
        assert "failure_index" in sectional
        assert len(sectional["tsaiwu_sr_max"]) > 0


# ---------------------------------------------------------------------------
# Stress plot — composite
# ---------------------------------------------------------------------------


class TestCompositeStressPlot:
    async def test_stress_plot_composite(self, composite_wing):
        envelope = await run_aerostruct_analysis(
            surfaces=["wing"],
            velocity=248.0,
            alpha=5.0,
            Mach_number=0.84,
            reynolds_number=1e6,
            density=0.38,
        )
        results = _r(envelope)
        run_id = envelope["run_id"]

        from oas_mcp.core.plotting import plot_stress_distribution

        plot_result = plot_stress_distribution(run_id, results)
        assert plot_result.metadata["plot_type"] == "stress_distribution"
