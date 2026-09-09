"""Swarmboard: a local, event-driven multi-agent discussion board."""

from .database import SessionLocal, init_db
from .repository import Repository

__all__ = ["Repository", "SessionLocal", "init_db"]
__version__ = "0.1.0"
