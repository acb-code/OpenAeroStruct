"""Input validation for OAS MCP tools."""

from __future__ import annotations


def validate_positive(value, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")


def validate_mesh_params(num_x: int, num_y: int) -> None:
    if num_x < 2:
        raise ValueError(f"num_x must be >= 2, got {num_x}")
    if num_y < 3:
        raise ValueError(f"num_y must be >= 3, got {num_y}")
    if num_y % 2 == 0:
        raise ValueError(
            f"num_y must be odd (symmetry requires odd panel count), got {num_y}. "
            f"Try {num_y + 1} or {num_y - 1}."
        )


def validate_wing_type(wing_type: str) -> None:
    valid = {"rect", "CRM"}
    if wing_type not in valid:
        raise ValueError(f"wing_type must be one of {valid}, got {wing_type!r}")


def validate_fem_model_type(fem_model_type: str) -> None:
    valid = {"tube", "wingbox", "none", None}
    if fem_model_type not in valid:
        raise ValueError(f"fem_model_type must be one of {valid}, got {fem_model_type!r}")


def validate_flight_conditions(velocity, alpha, Mach_number, reynolds_number, density) -> None:
    validate_positive(velocity, "velocity")
    validate_positive(Mach_number, "Mach_number")
    validate_positive(reynolds_number, "reynolds_number")
    validate_positive(density, "density")
    if not (-90 <= alpha <= 90):
        raise ValueError(f"alpha must be between -90 and 90 deg, got {alpha}")


def validate_surface_names_exist(names: list[str], session) -> None:
    missing = [n for n in names if n not in session.surfaces]
    if missing:
        available = list(session.surfaces.keys())
        raise ValueError(
            f"Surface(s) not found: {missing}. "
            f"Available surfaces: {available}. "
            f"Use create_surface first."
        )


def validate_struct_props_present(surface: dict) -> None:
    """Ensure structural properties are present for aerostruct analysis."""
    required = ["E", "G", "yield", "mrho", "fem_model_type"]
    missing = [k for k in required if k not in surface]
    if missing:
        raise ValueError(
            f"Surface {surface.get('name', '?')!r} is missing structural properties: {missing}. "
            f"Set fem_model_type and structural parameters in create_surface."
        )
    if surface.get("fem_model_type") in (None, "none"):
        raise ValueError(
            f"Surface {surface.get('name', '?')!r} has fem_model_type=None. "
            f"Set fem_model_type='tube' or 'wingbox' for aerostruct analysis."
        )
