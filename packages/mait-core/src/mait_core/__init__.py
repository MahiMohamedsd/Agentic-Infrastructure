"""mait-core: canonical contracts shared by every MAIT component.

Contains no I/O. Everything here is pure and property-tested so the rest of the system can
rely on it as the definition of MAIT's execution semantics.
"""

from mait_core.contracts import (
    AdapterCapabilities,
    AgentAdapter,
    Effect,
    IntegrationLevel,
    ToolDefinition,
    ToolInvocationStatus,
)
from mait_core.events import Event
from mait_core.lifecycle import ExecutionStatus, Writer
from mait_core.projection import ExecutionState, apply, fold

__all__ = [
    "AdapterCapabilities",
    "AgentAdapter",
    "Effect",
    "Event",
    "ExecutionState",
    "ExecutionStatus",
    "IntegrationLevel",
    "ToolDefinition",
    "ToolInvocationStatus",
    "Writer",
    "apply",
    "fold",
]

__version__ = "0.0.1"
