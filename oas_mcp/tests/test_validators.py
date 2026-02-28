"""Unit tests for input validators — no OAS computation needed."""

import pytest
from oas_mcp.core.validators import (
    validate_fem_model_type,
    validate_flight_conditions,
    validate_mesh_params,
    validate_positive,
    validate_struct_props_present,
    validate_wing_type,
)


class TestValidatePositive:
    def test_positive_passes(self):
        validate_positive(1.0, "x")  # no error

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="must be positive"):
            validate_positive(0.0, "x")

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="must be positive"):
            validate_positive(-5.0, "velocity")


class TestValidateMeshParams:
    def test_valid(self):
        validate_mesh_params(2, 5)

    def test_num_x_too_small(self):
        with pytest.raises(ValueError, match="num_x"):
            validate_mesh_params(1, 5)

    def test_num_y_even_raises(self):
        with pytest.raises(ValueError, match="odd"):
            validate_mesh_params(2, 6)

    def test_num_y_even_suggests_alternatives(self):
        with pytest.raises(ValueError, match="7.*5|5.*7"):
            validate_mesh_params(2, 6)

    def test_num_y_too_small(self):
        with pytest.raises(ValueError, match="num_y"):
            validate_mesh_params(2, 1)


class TestValidateWingType:
    def test_rect_ok(self):
        validate_wing_type("rect")

    def test_crm_ok(self):
        validate_wing_type("CRM")

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="wing_type"):
            validate_wing_type("naca0012")


class TestValidateFemModelType:
    def test_tube_ok(self):
        validate_fem_model_type("tube")

    def test_wingbox_ok(self):
        validate_fem_model_type("wingbox")

    def test_none_ok(self):
        validate_fem_model_type(None)

    def test_string_none_ok(self):
        validate_fem_model_type("none")

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="fem_model_type"):
            validate_fem_model_type("solid")


class TestValidateFlightConditions:
    def test_valid(self):
        validate_flight_conditions(250.0, 5.0, 0.84, 1e6, 0.38)

    def test_negative_velocity(self):
        with pytest.raises(ValueError, match="velocity"):
            validate_flight_conditions(-1.0, 5.0, 0.84, 1e6, 0.38)

    def test_zero_mach(self):
        with pytest.raises(ValueError, match="Mach"):
            validate_flight_conditions(250.0, 5.0, 0.0, 1e6, 0.38)

    def test_alpha_out_of_range(self):
        with pytest.raises(ValueError, match="alpha"):
            validate_flight_conditions(250.0, 95.0, 0.84, 1e6, 0.38)


class TestValidateStructProps:
    def test_tube_surface_passes(self):
        surface = {
            "name": "wing",
            "fem_model_type": "tube",
            "E": 70e9, "G": 30e9, "yield": 500e6, "mrho": 3000.0,
        }
        validate_struct_props_present(surface)  # no error

    def test_missing_key_raises(self):
        surface = {"name": "wing", "fem_model_type": "tube", "E": 70e9}
        with pytest.raises(ValueError, match="missing structural"):
            validate_struct_props_present(surface)

    def test_fem_none_raises(self):
        surface = {
            "name": "wing", "fem_model_type": None,
            "E": 70e9, "G": 30e9, "yield": 500e6, "mrho": 3000.0,
        }
        with pytest.raises(ValueError, match="fem_model_type"):
            validate_struct_props_present(surface)
