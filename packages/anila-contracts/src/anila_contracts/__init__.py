"""ANILA v0 minimal cross-service contracts.

The top-level API is intentionally limited to the three Gate 1 F5
contracts.  Implementation enums and schema-version constants remain in
their defining modules without becoming additional public contracts.
"""

from .classification import Classification
from .errors import AgentError
from .events import StepEvent

__version__ = "0.1.0"

__all__ = [
    "AgentError",
    "Classification",
    "StepEvent",
]
