"""
Session management for OAS MCP server.

A Session stores surface definitions and optionally caches a built/set-up
om.Problem so that parameter sweeps (varying alpha, Mach, etc.) can reuse
the expensive setup() call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import openmdao.api as om


def _surface_fingerprint(surface: dict) -> str:
    """
    Produce a stable string fingerprint of a surface dict for cache invalidation.
    Numpy arrays are converted to lists before hashing.
    """

    def _convert(obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, dict):
            return {k: _convert(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_convert(v) for v in obj]
        return obj

    serialisable = _convert(surface)
    raw = json.dumps(serialisable, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class _CachedProblem:
    prob: om.Problem
    analysis_type: str  # "aero" or "aerostruct"
    surface_fingerprints: dict[str, str]  # name → fingerprint at build time


@dataclass
class Session:
    """Stores surfaces and cached OpenMDAO problems for a session."""

    # name → surface dict (includes mesh as numpy array)
    surfaces: dict[str, dict] = field(default_factory=dict)

    # Cached problems: key = frozenset of surface names + analysis_type
    _cache: dict[str, _CachedProblem] = field(default_factory=dict, repr=False)

    def add_surface(self, surface: dict) -> None:
        name = surface["name"]
        self.surfaces[name] = surface
        # Invalidate any cached problems that include this surface
        stale = [k for k, v in self._cache.items() if name in v.surface_fingerprints]
        for k in stale:
            del self._cache[k]

    def get_surfaces(self, names: list[str]) -> list[dict]:
        return [self.surfaces[n] for n in names]

    def _cache_key(self, names: list[str], analysis_type: str) -> str:
        return analysis_type + ":" + ",".join(sorted(names))

    def get_cached_problem(
        self, names: list[str], analysis_type: str
    ) -> om.Problem | None:
        key = self._cache_key(names, analysis_type)
        cached = self._cache.get(key)
        if cached is None:
            return None
        # Validate fingerprints
        for name in names:
            current = _surface_fingerprint(self.surfaces[name])
            if cached.surface_fingerprints.get(name) != current:
                del self._cache[key]
                return None
        return cached.prob

    def store_problem(
        self, names: list[str], analysis_type: str, prob: om.Problem
    ) -> None:
        key = self._cache_key(names, analysis_type)
        fingerprints = {n: _surface_fingerprint(self.surfaces[n]) for n in names}
        self._cache[key] = _CachedProblem(
            prob=prob,
            analysis_type=analysis_type,
            surface_fingerprints=fingerprints,
        )

    def clear(self) -> None:
        self.surfaces.clear()
        self._cache.clear()


class SessionManager:
    """Global registry of named sessions. The default session is always available."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {"default": Session()}

    def get(self, session_id: str = "default") -> Session:
        if session_id not in self._sessions:
            self._sessions[session_id] = Session()
        return self._sessions[session_id]

    def reset(self) -> None:
        """Clear all sessions and cached problems."""
        self._sessions = {"default": Session()}
