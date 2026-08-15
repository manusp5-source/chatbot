"""Tools del agente. Cada uno se autoregistra al importarse."""
from app.agents.tools import (  # noqa: F401
    calendar,
    contact_lookup,
    contact_upsert,
    human_handoff,
    kb_search,
)
from app.agents.tools.registry import ALL_TOOLS

__all__ = ["ALL_TOOLS"]
