"""Typed error taxonomy for the OAS MCP Server.

Agents can inspect `error_code` to take corrective action:
  - UserInputError      → fix parameter values, then retry
  - SolverConvergenceError → relax mesh, adjust initial conditions, then retry
  - CacheEvictedError   → call create_surface again, then rerun
  - InternalError       → report bug; do not retry automatically
"""

from __future__ import annotations


class OASMCPError(Exception):
    """Base class for all typed OAS MCP Server errors."""

    error_code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "code": self.error_code,
            "message": str(self),
            "details": self.details,
        }


class UserInputError(OASMCPError):
    """Invalid user input — bad parameter values, missing surfaces, etc.

    Agents should inspect `details` for which field is wrong and the
    allowed values before retrying.
    """

    error_code = "USER_INPUT_ERROR"


class SolverConvergenceError(OASMCPError):
    """OpenMDAO solver failed to converge.

    Agents should consider: coarser mesh, stricter initial alpha range,
    or lower Mach number before retrying.  `details` may include
    `iterations`, `final_residual`, and `solver_type`.
    """

    error_code = "SOLVER_CONVERGENCE_ERROR"


class CacheEvictedError(OASMCPError):
    """The cached OpenMDAO problem was evicted from session memory.

    The artifact (run_id) is still on disk and can be retrieved via
    get_artifact().  To run further analyses, call create_surface() again
    then rerun the analysis.
    """

    error_code = "CACHE_EVICTED_ERROR"


class InternalError(OASMCPError):
    """Unexpected internal server error — likely a bug.

    Agents should NOT retry automatically.  Surface the full `message`
    and `details` to the user for reporting.
    """

    error_code = "INTERNAL_ERROR"
