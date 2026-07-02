"""Per-agent functions (extensible).

Developers design functions for each agent in the CSP console; the ANILA
chat UI surfaces them for the active agent. A function is a ``kind`` plus a
``config`` JSON blob, so new function kinds can be added without a schema
change — only a frontend renderer (and optional executor).

Air-gapped / audited deployment: function configs are plain declarative
data (text templates, flags), never executable code.

Kinds (initial registry — keep in sync with the ANILA UI renderer map):
- ``preset_prompt``  config = {"text": str, "autosend": bool}
    A ready-made prompt the user picks; fills the composer (or sends it
    immediately when ``autosend``).
- ``prompt_action``  config = {"template": str}
    A post-response action button next to the rating controls; ``{content}``
    in the template is replaced with the message text and sent as a turn.
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


class AgentFunction(Base):
    __tablename__ = "agent_functions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(
        Integer,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Discriminator — selects the frontend renderer / behaviour.
    kind = Column(String(40), nullable=False, default="preset_prompt")
    # Short label shown in the picker / on the button.
    label = Column(String(120), nullable=False)
    # Kind-specific declarative config (text templates, flags). No code.
    config = Column(JSONValue, nullable=False, default=dict)
    sort_order = Column(Integer, nullable=False, default=0)
    created_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# Back-compat alias: earlier code imported AgentPrompt. Keep the name
# pointing at the generalised model so existing imports don't break.
AgentPrompt = AgentFunction
