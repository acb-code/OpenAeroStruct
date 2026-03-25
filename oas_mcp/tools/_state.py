"""Module-level singletons shared across all tool modules.

Kept in its own module to avoid circular imports between server.py and
the tool submodules.
"""

from ..core.artifacts import ArtifactStore
from ..core.session import SessionManager

sessions = SessionManager()
artifacts = ArtifactStore()
