"""Internal contracts shared across components (RFC-001 §1.2, §6, §7.2).

These are deliberately framework-free. Adapters, the tool broker and the runtime depend on
these definitions, never on each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any, Protocol, runtime_checkable


class Effect(StrEnum):
    READ = "read"
    IDEMPOTENT_WRITE = "idempotent_write"
    SIDE_EFFECT = "side_effect"
    DESTRUCTIVE = "destructive"

    @property
    def mutates(self) -> bool:
        return self is not Effect.READ


class ToolInvocationStatus(StrEnum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    DENIED = "DENIED"


class RecoveryAction(StrEnum):
    SUBMIT = "submit"
    RECONCILE = "reconcile"
    RESUBMIT_SAME_KEY = "resubmit_same_key"
    ESCALATE = "escalate"
    RETURN_RECORDED = "return_recorded"


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    version: str
    effect: Effect
    input_schema: dict[str, Any]
    description: str = ""
    idempotency_supported: bool = False
    reconciliation_supported: bool = False
    approval_required: bool = False
    timeout_seconds: int = 30
    credentials_ref: str | None = None

    def __post_init__(self) -> None:
        if self.credentials_ref is not None and not self.credentials_ref.startswith("secret://"):
            raise ValueError("credentials must be a secret:// reference, never a raw secret")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.effect is Effect.DESTRUCTIVE and not self.approval_required:
            # Platform default: destructive tools always require approval unless a policy
            # explicitly waives it at the project level (SECURITY.md §5).
            object.__setattr__(self, "approval_required", True)


def recovery_action(tool: ToolDefinition, status: ToolInvocationStatus) -> RecoveryAction:
    """What the broker may do with an invocation found in ``status`` after a crash.

    Implements the table in EXECUTION_MODEL.md §6. The rule that matters: an operation that
    may have left MAIT is never blindly repeated.
    """
    if status in (
        ToolInvocationStatus.CONFIRMED,
        ToolInvocationStatus.REJECTED,
        ToolInvocationStatus.DENIED,
    ):
        return RecoveryAction.RETURN_RECORDED
    if status is ToolInvocationStatus.PENDING:
        return RecoveryAction.SUBMIT
    # SUBMITTED or UNKNOWN: the request may have reached the provider.
    if tool.reconciliation_supported:
        return RecoveryAction.RECONCILE
    if tool.effect in (Effect.READ, Effect.IDEMPOTENT_WRITE) or tool.idempotency_supported:
        return RecoveryAction.RESUBMIT_SAME_KEY
    return RecoveryAction.ESCALATE


class IntegrationLevel(IntEnum):
    OBSERVED = 0
    CONTROLLED = 1
    CHECKPOINTED = 2
    NATIVE = 3


@dataclass(frozen=True)
class AdapterCapabilities:
    streaming: bool = False
    checkpointing: bool = False
    resume: bool = False
    tool_interception: bool = False
    cancellation: bool = False
    deterministic_replay: bool = False

    def integration_level(self) -> IntegrationLevel:
        """The highest level the declared capabilities actually justify.

        The runtime uses this, never a level the adapter merely claims.
        """
        if (
            self.deterministic_replay
            and self.checkpointing
            and self.resume
            and self.tool_interception
        ):
            return IntegrationLevel.NATIVE
        if self.checkpointing and self.resume and self.tool_interception:
            return IntegrationLevel.CHECKPOINTED
        if self.tool_interception:
            return IntegrationLevel.CONTROLLED
        return IntegrationLevel.OBSERVED


@dataclass(frozen=True)
class Guarantees:
    policy_enforced_on_tools: bool
    resume_after_crash: bool
    replay_without_side_effects: bool


GUARANTEES: dict[IntegrationLevel, Guarantees] = {
    IntegrationLevel.OBSERVED: Guarantees(False, False, False),
    IntegrationLevel.CONTROLLED: Guarantees(True, False, False),
    IntegrationLevel.CHECKPOINTED: Guarantees(True, True, False),
    IntegrationLevel.NATIVE: Guarantees(True, True, True),
}


@dataclass(frozen=True)
class Checkpoint:
    execution_id: str
    sequence: int
    format: str
    state_ref: str
    state_digest: str


@dataclass
class ExecutionContextInfo:
    """Identity facet of the Agent Execution Envelope passed to adapters."""

    organization_id: str
    project_id: str
    agent_id: str
    agent_version_id: str
    execution_id: str
    attempt: int
    lease_generation: int
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AgentAdapter(Protocol):
    """Contract between the runtime and a framework integration (RFC-001 §7.2).

    Adapters implement what their framework supports and declare it in ``capabilities()``.
    Unsupported methods raise ``NotImplementedError``. The runtime never calls a method the
    capability manifest does not declare.
    """

    def capabilities(self) -> AdapterCapabilities: ...

    async def invoke(self, input: Any, context: ExecutionContextInfo) -> Any: ...

    async def get_checkpoint(self, execution_id: str) -> Checkpoint | None: ...

    async def restore(self, checkpoint: Checkpoint) -> None: ...

    async def cancel(self, execution_id: str) -> None: ...
