"""Unit tests for context-aware validation and requirements checking."""

from __future__ import annotations

import pytest
from oas_mcp.core.validation import (
    ValidationFinding,
    findings_to_dict,
    validate_aero,
    validate_aerostruct,
    validate_drag_polar,
    validate_optimization,
    validate_stability,
)
from oas_mcp.core.requirements import check_requirements


# ---------------------------------------------------------------------------
# ValidationFinding
# ---------------------------------------------------------------------------


class TestValidationFinding:
    def test_to_dict_has_all_keys(self):
        f = ValidationFinding(
            check_id="physics.cd_positive",
            category="physics",
            severity="error",
            confidence="high",
            passed=False,
            message="CD < 0",
            remediation="fix it",
        )
        d = f.to_dict()
        assert d["check_id"] == "physics.cd_positive"
        assert d["severity"] == "error"
        assert d["passed"] is False
        assert d["remediation"] == "fix it"

    def test_findings_to_dict_all_passed(self):
        findings = [
            ValidationFinding("a.b", "physics", "error", "high", True, "OK"),
            ValidationFinding("a.c", "physics", "warning", "medium", True, "OK"),
        ]
        d = findings_to_dict(findings)
        assert d["passed"] is True
        assert d["error_count"] == 0
        assert d["warning_count"] == 0
        assert d["findings"] == []  # no failed findings

    def test_findings_to_dict_with_failures(self):
        findings = [
            ValidationFinding("a", "physics", "error", "high", False, "bad"),
            ValidationFinding("b", "physics", "warning", "medium", False, "warn"),
            ValidationFinding("c", "physics", "info", "low", False, "info"),
            ValidationFinding("d", "physics", "info", "low", True, "ok"),
        ]
        d = findings_to_dict(findings)
        assert d["passed"] is False
        assert d["error_count"] == 1
        assert d["warning_count"] == 1
        assert d["info_count"] == 1
        assert len(d["findings"]) == 3   # only failed findings
        assert len(d["all_findings"]) == 4


# ---------------------------------------------------------------------------
# validate_aero
# ---------------------------------------------------------------------------


class TestValidateAero:
    def _good_results(self):
        return {"CL": 0.5, "CD": 0.035, "CM": -0.1, "L_over_D": 14.3, "surfaces": {}}

    def test_good_results_pass(self):
        findings = validate_aero(self._good_results(), context={"alpha": 5.0})
        agg = findings_to_dict(findings)
        assert agg["passed"] is True

    def test_negative_cd_fails(self):
        results = self._good_results()
        results["CD"] = -0.01
        findings = validate_aero(results)
        errors = [f for f in findings if not f.passed and f.severity == "error"]
        assert any(f.check_id == "physics.cd_positive" for f in errors)

    def test_cd_over_1_fails(self):
        results = self._good_results()
        results["CD"] = 1.5
        findings = validate_aero(results)
        errors = [f for f in findings if not f.passed]
        assert any(f.check_id == "physics.cd_not_too_large" for f in errors)

    def test_negative_cl_at_positive_alpha_warns(self):
        results = self._good_results()
        results["CL"] = -0.8  # unusual at positive alpha
        findings = validate_aero(results, context={"alpha": 5.0})
        warnings = [f for f in findings if not f.passed and f.severity in ("warning", "error")]
        assert any("cl_reasonable" in f.check_id for f in warnings)

    def test_negative_cl_at_negative_alpha_ok(self):
        results = self._good_results()
        results["CL"] = -0.4
        results["L_over_D"] = -11.0
        findings = validate_aero(results, context={"alpha": -5.0})
        # No error for CL check at negative alpha
        errors = [f for f in findings if not f.passed and f.severity == "error"]
        # CD is still positive, so no errors expected
        assert len(errors) == 0


# ---------------------------------------------------------------------------
# validate_aerostruct
# ---------------------------------------------------------------------------


class TestValidateAerostruct:
    def _good_results(self):
        return {
            "CL": 0.5, "CD": 0.035, "CM": -0.1, "L_over_D": 14.3,
            "fuelburn": 90000.0, "structural_mass": 900.0,
            "L_equals_W": 0.0,
            "surfaces": {
                "wing": {"CL": 0.5, "CD": 0.035, "failure": -0.3, "max_vonmises_Pa": 100e6}
            },
        }

    def test_good_results_pass(self):
        findings = validate_aerostruct(self._good_results(), context={"alpha": 5.0, "W0": 120000})
        agg = findings_to_dict(findings)
        assert agg["passed"] is True

    def test_structural_failure_detected(self):
        results = self._good_results()
        results["surfaces"]["wing"]["failure"] = 1.5  # > 1.0 = failed
        findings = validate_aerostruct(results, context={"alpha": 5.0, "W0": 120000})
        errors = [f for f in findings if not f.passed and f.severity == "error"]
        assert any("structural_failure" in f.check_id for f in errors)

    def test_large_lew_residual_warns(self):
        results = self._good_results()
        results["L_equals_W"] = 50000.0  # large residual relative to W0
        findings = validate_aerostruct(results, context={"alpha": 5.0, "W0": 120000})
        failed = [f for f in findings if not f.passed]
        assert any("lew_residual" in f.check_id for f in failed)

    def test_negative_fuelburn_errors(self):
        results = self._good_results()
        results["fuelburn"] = -100.0
        findings = validate_aerostruct(results, context={"alpha": 5.0, "W0": 120000})
        errors = [f for f in findings if not f.passed and f.severity == "error"]
        assert any("fuelburn" in f.check_id for f in errors)

    def test_failure_below_1_passes(self):
        results = self._good_results()
        results["surfaces"]["wing"]["failure"] = 0.9  # approaching limit but not failed
        findings = validate_aerostruct(results, context={"alpha": 5.0, "W0": 120000})
        failure_checks = [f for f in findings if "structural_failure" in f.check_id]
        assert all(f.passed for f in failure_checks)


# ---------------------------------------------------------------------------
# validate_drag_polar
# ---------------------------------------------------------------------------


class TestValidateDragPolar:
    def _good_polar(self):
        return {
            "alpha_deg": [-5.0, 0.0, 5.0, 10.0],
            "CL": [-0.2, 0.0, 0.4, 0.8],
            "CD": [0.03, 0.025, 0.035, 0.06],
            "L_over_D": [None, 0.0, 11.4, 13.3],
            "best_L_over_D": {"alpha_deg": 10.0, "CL": 0.8, "CD": 0.06, "L_over_D": 13.3},
        }

    def test_good_polar_passes(self):
        findings = validate_drag_polar(self._good_polar())
        assert all(f.passed for f in findings if f.check_id == "physics.cd_positive_polar")

    def test_negative_cd_in_polar_fails(self):
        polar = self._good_polar()
        polar["CD"][0] = -0.01
        findings = validate_drag_polar(polar)
        failed = [f for f in findings if not f.passed]
        assert any("cd_positive_polar" in f.check_id for f in failed)

    def test_non_monotone_cl_warns(self):
        polar = self._good_polar()
        polar["CL"] = [0.0, 0.5, 0.3, 0.8]  # dips at index 2
        findings = validate_drag_polar(polar)
        failed = [f for f in findings if not f.passed]
        assert any("cl_monotonic" in f.check_id for f in failed)


# ---------------------------------------------------------------------------
# validate_stability
# ---------------------------------------------------------------------------


class TestValidateStability:
    def test_positive_cl_alpha_is_info(self):
        findings = validate_stability({"CL_alpha": 0.11, "static_margin": 0.1})
        cl_alpha_checks = [f for f in findings if "cl_alpha" in f.check_id]
        assert all(f.severity == "info" for f in cl_alpha_checks)

    def test_static_margin_always_passes(self):
        for sm in [-0.1, 0.0, 0.1, 0.5]:
            findings = validate_stability({"CL_alpha": 0.1, "static_margin": sm})
            sm_checks = [f for f in findings if "static_margin" in f.check_id]
            assert all(f.passed for f in sm_checks)


# ---------------------------------------------------------------------------
# validate_optimization
# ---------------------------------------------------------------------------


class TestValidateOptimization:
    def test_converged_optimization_passes(self):
        results = {
            "success": True,
            "final_results": {"CL": 0.5, "CD": 0.03},
            "optimized_design_variables": {},
        }
        findings = validate_optimization(results)
        conv_checks = [f for f in findings if "optimizer_converged" in f.check_id]
        assert all(f.passed for f in conv_checks)

    def test_failed_optimization_errors(self):
        results = {
            "success": False,
            "final_results": {"CL": 0.2, "CD": 0.05},
        }
        findings = validate_optimization(results)
        failed = [f for f in findings if not f.passed and f.severity == "error"]
        assert any("optimizer_converged" in f.check_id for f in failed)


# ---------------------------------------------------------------------------
# check_requirements
# ---------------------------------------------------------------------------


class TestCheckRequirements:
    def _results(self):
        return {
            "CL": 0.5,
            "CD": 0.035,
            "L_over_D": 14.3,
            "surfaces": {"wing": {"failure": -0.3, "CL": 0.5}},
        }

    def test_satisfied_requirement_passes(self):
        reqs = [{"path": "CL", "operator": ">=", "value": 0.4, "label": "min_CL"}]
        report = check_requirements(reqs, self._results())
        assert report["passed"] is True
        assert report["results"][0]["passed"] is True

    def test_violated_requirement_fails(self):
        reqs = [{"path": "CL", "operator": ">=", "value": 0.6, "label": "high_CL"}]
        report = check_requirements(reqs, self._results())
        assert report["passed"] is False
        assert report["results"][0]["passed"] is False
        assert report["results"][0]["actual"] == pytest.approx(0.5)

    def test_nested_path_resolves(self):
        reqs = [{"path": "surfaces.wing.failure", "operator": "<", "value": 0.0}]
        report = check_requirements(reqs, self._results())
        assert report["passed"] is True

    def test_missing_path_fails(self):
        reqs = [{"path": "surfaces.tail.failure", "operator": "<", "value": 0.0}]
        report = check_requirements(reqs, self._results())
        assert report["passed"] is False
        assert "not found" in report["results"][0].get("error", "")

    def test_unknown_operator_fails(self):
        reqs = [{"path": "CL", "operator": "~=", "value": 0.5}]
        report = check_requirements(reqs, self._results())
        assert report["passed"] is False
        assert "Unknown operator" in report["results"][0].get("error", "")

    def test_multiple_requirements_all_pass(self):
        reqs = [
            {"path": "CL", "operator": ">=", "value": 0.4},
            {"path": "CD", "operator": "<", "value": 0.1},
            {"path": "L_over_D", "operator": ">", "value": 10.0},
        ]
        report = check_requirements(reqs, self._results())
        assert report["passed"] is True
        assert report["passed_count"] == 3

    def test_partial_pass_partial_fail(self):
        reqs = [
            {"path": "CL", "operator": ">=", "value": 0.4},  # passes
            {"path": "CL", "operator": ">=", "value": 0.8},  # fails
        ]
        report = check_requirements(reqs, self._results())
        assert report["passed"] is False
        assert report["passed_count"] == 1
        assert report["total"] == 2

    def test_empty_requirements_passes(self):
        report = check_requirements([], self._results())
        assert report["passed"] is True
        assert report["total"] == 0
