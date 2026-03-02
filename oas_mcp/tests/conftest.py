"""
Shared fixtures for OAS MCP tests.

The global session manager lives in oas_mcp.server, so we reset it between
tests via the reset() tool to keep tests isolated.

The global artifact store is redirected to a per-test tmp_path so that
tests never write to the real oas_data/ directory.
"""

import pytest
import pytest_asyncio
from oas_mcp.server import _artifacts, create_surface, reset

# Tiny mesh used across integration tests — fast but produces real results.
SMALL_RECT = dict(
    name="wing",
    wing_type="rect",
    span=10.0,
    root_chord=1.0,
    num_x=2,
    num_y=5,   # smallest valid odd value
    symmetry=True,
    with_viscous=True,
)

SMALL_RECT_STRUCT = dict(
    **SMALL_RECT,
    fem_model_type="tube",
    E=70.0e9,
    G=30.0e9,
    yield_stress=500.0e6,
    safety_factor=2.5,
    mrho=3.0e3,
    thickness_cp=[0.05, 0.1, 0.05],
)

SMALL_RECT_WINGBOX = dict(
    **SMALL_RECT,
    fem_model_type="wingbox",
    E=73.1e9,
    G=27.5e9,
    yield_stress=420.0e6,
    safety_factor=1.5,
    mrho=2.78e3,
)


@pytest.fixture(autouse=True)
def isolate_artifacts(tmp_path):
    """Redirect artifact storage to a per-test temp directory."""
    original = _artifacts._data_dir
    _artifacts._data_dir = tmp_path / "artifacts"
    yield
    _artifacts._data_dir = original


@pytest_asyncio.fixture(autouse=True)
async def clean_session():
    """Reset the global session before every test."""
    await reset()
    yield
    await reset()


@pytest_asyncio.fixture
async def aero_wing():
    """Create a small aero-only wing and return its name."""
    await create_surface(**SMALL_RECT)
    return "wing"


@pytest_asyncio.fixture
async def struct_wing():
    """Create a small wing with structural properties."""
    await create_surface(**SMALL_RECT_STRUCT)
    return "wing"


@pytest_asyncio.fixture
async def wingbox_wing():
    """Create a small wing with wingbox structural properties."""
    await create_surface(**SMALL_RECT_WINGBOX)
    return "wing"
