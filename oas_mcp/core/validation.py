"""Context-aware physics and numerics validation for OAS MCP analysis results.

Every check produces a ``ValidationFinding`` with:
  - check_id:    unique dot-path identifier
  - category:    "physics" | "numerics" | "constraints" | "stability"
  - severity:    "error" | "warning" | "info"
  - confidence:  "high" | "medium" | "low"
  - passed:      bool
  - message:     human-readable description
  - remediation: hint for fixing the issue

Severity guidelines
-------------------
  "error"   → almost certainly wrong; agent should not trust results
  "warning"  → likely a problem; agent should investigate
  "info"     → heuristic / sign-convention note; no action required

Confidence guidelines
---------------------
  "high"   → physics dictates this is always true (CD > 0)
  "medium" → true for typical cruise configurations
  "low"    → heuristic; depends on context

Usage
-----
    from oas_mcp.core.validation import validate_aero, validate_aerostruct

    findings = validate_aero(results, context={"alpha": 5.0})
    validation_block = findings_to_dict(findings)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ValidationFinding:
    check_id: str
    category: str  # physics | numerics | constraints | stability
    severity: str  # error | warning | info
    confidence: str  # high | medium | low
    passed: bool
    message: str
    remediation: str = ""

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "category": self.category,
            "severity": self.severity,
            "confidence": self.confidence,
            "passed": self.passed,
            "message": self.message,
            "remediation": self.remediation,
        }


def findings_to_dict(findings: list[ValidationFinding]) -> dict:
    """Aggregate findings into a block suitable for the response envelope."""
    errors = [f for f in findings if not f.passed and f.severity == "error"]
    warnings = [f for f in findings if not f.passed and f.severity == "warning"]
    infos = [f for f in findings if not f.passed and f.severity == "info"]
    return {
        "passed": len(errors) == 0,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "info_count": len(infos),
        "findings": [f.to_dict() for f in findings if not f.passed],
        "all_findings": [f.to_dict() for f in findings],
    }


# ---------------------------------------------------------------------------
# Shared checks
# ---------------------------------------------------------------------------


def _check_cd_positive(CD: float) -> ValidationFinding:
    passed = CD > 0
    return ValidationFinding(
        check_id="physics.cd_positive",
        category="physics",
        severity="error",
        confidence="high",
        passed=passed,
        message=f"CD = {CD:.6f} (must be > 0)" if not passed else f"CD = {CD:.6f} > 0 ✓",
        remediation="Negative CD violates physics. Check mesh quality and that viscous/wave drag is correctly configured.",
    )


def _check_cl_reasonable(CL: float, alpha: float | None) -> ValidationFinding:
    """CL should be reasonable — context-aware for alpha sweeps with negative alpha."""
    # For negative alpha, negative CL is expected
    if alpha is not None and alpha < -5.0:
        # Allow negative CL but check it's not absurdly large
        passed = abs(CL) < 5.0
        message = (
            f"CL = {CL:.4f} at alpha = {alpha:.1f}° (negative CL expected for negative alpha)"
            if passed
            else f"|CL| = {abs(CL):.4f} seems unreasonably large at alpha = {alpha:.1f}°"
        )
        remediation = "Very large |CL| at negative alpha may indicate mesh or solver issue."
    else:
        # Positive alpha: CL should generally be positive and < ~3
        passed = -0.5 <= CL <= 3.0
        message = (
            f"CL = {CL:.4f} is in expected range [-0.5, 3.0]"
            if passed
            else f"CL = {CL:.4f} is outside expected range [-0.5, 3.0]"
        )
        remediation = (
            "CL > 3 may indicate stall or mesh issues. CL < -0.5 at positive alpha is unusual. "
            "Check twist, angle of attack, and mesh quality."
        )
    return ValidationFinding(
        check_id="physics.cl_reasonable",
        category="physics",
        severity="warning",
        confidence="medium",
        passed=passed,
        message=message,
        remediation=remediation if not passed else "",
    )


def _check_ld_reasonable(CL: float, CD: float, alpha: float | None) -> ValidationFinding:
    """L/D should be reasonable — positive at moderate positive alpha."""
    if CD <= 0:
        return ValidationFinding(
            check_id="physics.ld_reasonable",
            category="physics",
            severity="info",
            confidence="low",
            passed=True,
            message="L/D check skipped: CD ≤ 0 (see cd_positive check)",
        )
    LD = CL / CD
    # Context-aware: skip check for obviously negative-alpha cases
    if alpha is not None and alpha < 0.0:
        return ValidationFinding(
            check_id="physics.ld_reasonable",
            category="physics",
            severity="info",
            confidence="low",
            passed=True,
            message=f"L/D = {LD:.2f} (skipping positive-L/D check for negative alpha = {alpha:.1f}°)",
        )
    passed = LD > 0
    return ValidationFinding(
        check_id="physics.ld_reasonable",
        category="physics",
        severity="warning",
        confidence="medium",
        passed=passed,
        message=f"L/D = {LD:.2f}" + (" > 0 ✓" if passed else " ≤ 0 — unexpected at positive alpha"),
        remediation="Negative L/D at positive alpha suggests CL < 0. Check wing orientation and twist.",
    )


def _check_cd_not_too_large(CD: float) -> ValidationFinding:
    """CD > 1.0 is physically implausible for a lifting wing."""
    passed = CD < 1.0
    return ValidationFinding(
        check_id="physics.cd_not_too_large",
        category="physics",
        severity="error",
        confidence="high",
        passed=passed,
        message=f"CD = {CD:.4f} < 1.0 ✓" if passed else f"CD = {CD:.4f} ≥ 1.0 — physically implausible",
        remediation="CD ≥ 1 is physically impossible for a subsonic lifting wing. Check mesh, Mach, and drag model settings.",
    )


# ---------------------------------------------------------------------------
# Aerodynamic-only validation
# ---------------------------------------------------------------------------


def validate_aero(results: dict, context: dict | None = None) -> list[ValidationFinding]:
    """Run all aerodynamic validation checks.

    Parameters
    ----------
    results:
        Output of ``extract_aero_results``.
    context:
        Dict with optional keys: ``alpha`` (float), ``alpha_start`` (float).
    """
    ctx = context or {}
    alpha = ctx.get("alpha")

    CL = results.get("CL", 0.0)
    CD = results.get("CD", 0.0)

    findings: list[ValidationFinding] = [
        _check_cd_positive(CD),
        _check_cd_not_too_large(CD),
        _check_cl_reasonable(CL, alpha),
        _check_ld_reasonable(CL, CD, alpha),
    ]
    return findings


# ---------------------------------------------------------------------------
# Drag polar validation
# ---------------------------------------------------------------------------


def validate_drag_polar(results: dict, context: dict | None = None) -> list[ValidationFinding]:
    """Validate a drag polar (sweep over alpha).

    Checks:
    - CD always > 0
    - CL monotonically increases with alpha (physics.cl_monotonic)
    - L/D polar has a clear maximum (physics.ld_has_max)
    """
    ctx = context or {}
    CLs = results.get("CL", [])
    CDs = results.get("CD", [])
    alphas = results.get("alpha_deg", [])

    findings: list[ValidationFinding] = []

    # CD > 0 everywhere
    neg_cd = [i for i, cd in enumerate(CDs) if cd <= 0]
    findings.append(ValidationFinding(
        check_id="physics.cd_positive_polar",
        category="physics",
        severity="error",
        confidence="high",
        passed=len(neg_cd) == 0,
        message=(
            "All CD values > 0 ✓"
            if len(neg_cd) == 0
            else f"CD ≤ 0 at {len(neg_cd)} alpha point(s): indices {neg_cd}"
        ),
        remediation="CD ≤ 0 at any alpha is physically impossible. Check drag model and mesh.",
    ))

    # CL monotonically increasing with alpha
    if len(CLs) >= 2:
        non_monotone = [
            i for i in range(1, len(CLs))
            if CLs[i] < CLs[i - 1] - 1e-4
        ]
        findings.append(ValidationFinding(
            check_id="physics.cl_monotonic",
            category="physics",
            severity="warning",
            confidence="medium",
            passed=len(non_monotone) == 0,
            message=(
                "CL increases monotonically with alpha ✓"
                if len(non_monotone) == 0
                else f"CL is non-monotone at {len(non_monotone)} alpha transition(s)"
            ),
            remediation=(
                "Non-monotone CL vs alpha may indicate flow separation (not modeled by VLM) "
                "or numerical issues. Consider a narrower alpha range."
            ),
        ))

    # Best L/D exists
    best = results.get("best_L_over_D", {})
    best_ld = best.get("L_over_D") if best else None
    findings.append(ValidationFinding(
        check_id="physics.ld_has_max",
        category="physics",
        severity="warning",
        confidence="medium",
        passed=best_ld is not None and best_ld > 0,
        message=(
            f"Best L/D = {best_ld:.2f} at alpha = {best.get('alpha_deg', '?')}° ✓"
            if best_ld and best_ld > 0
            else "No positive L/D found in polar"
        ),
        remediation="No positive L/D suggests all computed CL values are negative. Check alpha range.",
    ))

    return findings


# ---------------------------------------------------------------------------
# Aerostructural validation
# ---------------------------------------------------------------------------


def validate_aerostruct(results: dict, context: dict | None = None) -> list[ValidationFinding]:
    """Run all aerostructural validation checks.

    Includes all aero checks plus structural ones.
    """
    ctx = context or {}
    alpha = ctx.get("alpha")

    CL = results.get("CL", 0.0)
    CD = results.get("CD", 0.0)

    findings: list[ValidationFinding] = [
        _check_cd_positive(CD),
        _check_cd_not_too_large(CD),
        _check_cl_reasonable(CL, alpha),
        _check_ld_reasonable(CL, CD, alpha),
    ]

    # L=W residual (normalized)
    lew = results.get("L_equals_W")
    if lew is not None:
        # L_equals_W is L - W; normalize by W0 if available
        W0 = ctx.get("W0", 1.0)  # fallback to avoid div-by-zero
        normalized = abs(lew) / max(abs(W0), 1.0)
        passed = normalized < 0.01  # 1% tolerance
        findings.append(ValidationFinding(
            check_id="numerics.lew_residual",
            category="numerics",
            severity="warning",
            confidence="high",
            passed=passed,
            message=(
                f"|L - W| / W₀ = {normalized:.4f} < 0.01 ✓"
                if passed
                else f"|L - W| / W₀ = {normalized:.4f} ≥ 0.01 — lift-weight balance not satisfied"
            ),
            remediation=(
                "Large L=W residual means the solver did not converge the coupled aero-structural "
                "trim. Consider tighter solver tolerances or adjusting W0."
            ),
        ))

    # Structural mass positive
    struct_mass = results.get("structural_mass")
    if struct_mass is not None:
        passed = struct_mass > 0
        findings.append(ValidationFinding(
            check_id="physics.structural_mass_positive",
            category="physics",
            severity="error",
            confidence="high",
            passed=passed,
            message=(
                f"Structural mass = {struct_mass:.1f} kg > 0 ✓"
                if passed
                else f"Structural mass = {struct_mass:.1f} kg ≤ 0 — physically impossible"
            ),
            remediation="Non-positive structural mass indicates a problem with material properties or FEM setup.",
        ))

    # Per-surface structural failure checks
    for surf_name, surf_res in results.get("surfaces", {}).items():
        failure = surf_res.get("failure")
        if failure is not None:
            # failure > 1.0 means the structure has failed
            # (failure is a utilization ratio; 1.0 = at yield)
            struct_failed = failure > 1.0
            findings.append(ValidationFinding(
                check_id=f"constraints.structural_failure.{surf_name}",
                category="constraints",
                severity="error" if struct_failed else "info",
                confidence="high",
                passed=not struct_failed,
                message=(
                    f"Surface '{surf_name}': failure index = {failure:.4f} ≤ 1.0 ✓ (structure intact)"
                    if not struct_failed
                    else f"Surface '{surf_name}': failure index = {failure:.4f} > 1.0 — STRUCTURAL FAILURE"
                ),
                remediation=(
                    "failure > 1 means von Mises stress exceeds yield/safety_factor. "
                    "Increase thickness, reduce load factor, or choose a stronger material."
                    if struct_failed
                    else ""
                ),
            ))

    # Fuel burn sanity (if present)
    fuelburn = results.get("fuelburn")
    if fuelburn is not None:
        passed = fuelburn > 0
        findings.append(ValidationFinding(
            check_id="physics.fuelburn_positive",
            category="physics",
            severity="error",
            confidence="high",
            passed=passed,
            message=(
                f"Fuel burn = {fuelburn:.1f} kg > 0 ✓"
                if passed
                else f"Fuel burn = {fuelburn:.1f} kg ≤ 0 — physically impossible"
            ),
            remediation="Non-positive fuel burn indicates a problem with mission parameters (CT, R, W0).",
        ))

    return findings


# ---------------------------------------------------------------------------
# Stability validation
# ---------------------------------------------------------------------------


def validate_stability(results: dict, context: dict | None = None) -> list[ValidationFinding]:
    """Run stability-specific validation checks."""
    findings: list[ValidationFinding] = []

    # CL_alpha should be positive (typically 2π/rad ≈ 0.11/deg for thin wings)
    cl_alpha = results.get("CL_alpha")
    if cl_alpha is not None:
        # Note: sign convention can vary; this is an "info" only check
        findings.append(ValidationFinding(
            check_id="stability.cl_alpha_sign",
            category="stability",
            severity="info",
            confidence="low",
            passed=cl_alpha > 0,
            message=f"CL_alpha = {cl_alpha:.4f} /deg" + (" (positive ✓)" if cl_alpha > 0 else " (negative — check sign convention)"),
            remediation="Negative CL_alpha is unusual but may occur depending on sign conventions.",
        ))

    # Static margin: positive = stable
    sm = results.get("static_margin")
    if sm is not None:
        findings.append(ValidationFinding(
            check_id="stability.static_margin",
            category="stability",
            severity="info",
            confidence="high",
            passed=True,  # Not a pass/fail; informational
            message=(
                f"Static margin = {sm:.4f} (statically stable)"
                if sm > 0
                else f"Static margin = {sm:.4f} (statically UNSTABLE)"
            ),
        ))

    return findings


# ---------------------------------------------------------------------------
# Optimization validation
# ---------------------------------------------------------------------------


def validate_optimization(results: dict, context: dict | None = None) -> list[ValidationFinding]:
    """Validate optimization results."""
    findings: list[ValidationFinding] = []

    success = results.get("success", False)
    findings.append(ValidationFinding(
        check_id="numerics.optimizer_converged",
        category="numerics",
        severity="error" if not success else "info",
        confidence="high",
        passed=success,
        message="Optimizer converged successfully ✓" if success else "Optimizer did NOT converge",
        remediation=(
            "Optimizer failed to converge. Try: looser tolerance, smaller design variable bounds, "
            "better initial conditions, or fewer design variables."
            if not success
            else ""
        ),
    ))

    # Run aero/aerostruct checks on final results
    final = results.get("final_results", {})
    if final:
        # Minimal checks on final design
        CD = final.get("CD", 0.0)
        findings.append(_check_cd_positive(CD))
        findings.append(_check_cd_not_too_large(CD))

    return findings
