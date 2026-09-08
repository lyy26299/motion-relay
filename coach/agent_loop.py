"""Bounded, evidence-carrying coach decision loop.

The loop deliberately knows nothing about a particular LLM or transport.  An
application binds a user-scoped allowlist of tools, a decision maker and an
actor.  The loop owns the budgets and the turn identity, so model output can
never select a user scope or execute an unregistered capability.

The supported flow is::

    immutable observation -> decide -> optional retrieve -> decide -> act
                          -> immutable post-action observation

Sync callables are run in worker threads; async callables are awaited directly.
Cancelling a turn drops late results from either form.  A synchronous callable
cannot be force-stopped once its thread has started, so tools and actors must
still avoid side effects, or check ``context.is_current()`` immediately before
performing one.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from coach.working_memory import WorkingMemory, WorkingMemoryView

CoachAction = Literal["answer", "cue", "propose_plan", "ask_clarification", "abstain"]
ToolStatus = Literal["ok", "empty", "error", "timeout"]
LoopStatus = Literal[
    "completed",
    "abstained",
    "cancelled",
    "stale",
    "timeout",
    "invalid_decision",
    "budget_exhausted",
    "error",
]
PlaybackStatus = Literal[
    "accepted", "generated", "queued", "completed", "interrupted", "rejected", "unknown"
]

_ALLOWED_ACTIONS = frozenset({"answer", "cue", "propose_plan", "ask_clarification", "abstain"})
_FORBIDDEN_SCOPE_ARGUMENTS = frozenset(
    {
        "user_id",
        "scope_user_id",
        "session_epoch",
        "memory_epoch",
        "scope_epoch",
        "scope_id",
        "tenant_id",
        "account_id",
    }
)


def _nonempty(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _copy_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return copy.deepcopy(dict(value or {}))


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A source reference a decision may cite.

    ``available_until_mono`` is only for process-local, short-lived evidence
    such as a pose window.  Durable ledger and knowledge references leave it
    unset.  ``user_id`` is optional for public reviewed knowledge, but when it
    is present the loop enforces the bound user scope.
    """

    source_type: str
    source_id: str
    revision: int | str | None = 1
    user_id: str | None = None
    session_id: str | None = None
    available_until_mono: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_type", _nonempty(self.source_type, "source_type"))
        object.__setattr__(self, "source_id", _nonempty(self.source_id, "source_id"))
        if isinstance(self.revision, bool) or not isinstance(self.revision, (int, str, type(None))):
            raise TypeError("revision must be an integer or string")
        if isinstance(self.revision, int) and self.revision < 0:
            raise ValueError("revision must not be negative")
        if isinstance(self.revision, str) and not self.revision.strip():
            raise ValueError("revision must not be empty")
        if self.available_until_mono is not None and not math.isfinite(self.available_until_mono):
            raise ValueError("available_until_mono must be finite")

    @property
    def evidence_id(self) -> str:
        """Stable identifier used by ``ClaimRef`` within one decision."""

        revision = "unversioned" if self.revision is None else self.revision
        return f"{self.source_type}:{self.source_id}@{revision}"

    @classmethod
    def from_value(cls, value: EvidenceRef | Mapping[str, Any] | str) -> EvidenceRef:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(source_type="opaque", source_id=value)
        if not isinstance(value, Mapping):
            raise TypeError("evidence reference must be a mapping, string or EvidenceRef")
        source_id = value.get("source_id", value.get("evidence_id"))
        if source_id is None:
            raise ValueError("evidence reference requires source_id")
        return cls(
            source_type=str(value.get("source_type", "opaque")),
            source_id=str(source_id),
            revision=value.get("revision", 1),
            user_id=(str(value["user_id"]) if value.get("user_id") is not None else None),
            session_id=(str(value["session_id"]) if value.get("session_id") is not None else None),
            available_until_mono=(
                float(value["available_until_mono"])
                if value.get("available_until_mono") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ClaimRef:
    claim_key: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_key", _nonempty(self.claim_key, "claim_key"))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        if not self.evidence_ids or any(
            not isinstance(item, str) or not item.strip() for item in self.evidence_ids
        ):
            raise ValueError("claim references require non-empty evidence IDs")


@dataclass(frozen=True, slots=True)
class ProposedPlan:
    exercise: str
    target_reps: int
    reason: str
    expected_plan_version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "exercise", _nonempty(self.exercise, "exercise"))
        object.__setattr__(self, "reason", _nonempty(self.reason, "reason"))
        if (
            isinstance(self.target_reps, bool)
            or not isinstance(self.target_reps, int)
            or self.target_reps <= 0
        ):
            raise ValueError("target_reps must be positive")
        if (
            isinstance(self.expected_plan_version, bool)
            or not isinstance(self.expected_plan_version, int)
            or self.expected_plan_version < 0
        ):
            raise ValueError("expected_plan_version must not be negative")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model-proposed call; the loop assigns the authoritative call ID."""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonempty(self.name, "tool name"))
        object.__setattr__(self, "arguments", _copy_mapping(self.arguments))


@dataclass(frozen=True, slots=True)
class ToolRequest:
    call_id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolPayload:
    """Transport-neutral successful or failed tool response payload."""

    items: tuple[Mapping[str, Any], ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    truncated: bool = False
    error_code: str | None = None
    error_message: str | None = None
    scope_epoch: int | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    status: ToolStatus
    items: tuple[Mapping[str, Any], ...]
    evidence_refs: tuple[EvidenceRef, ...]
    truncated: bool
    elapsed_ms: float
    error_code: str | None = None
    error_message: str | None = None
    scope_epoch: int | None = None


@dataclass(frozen=True, slots=True)
class AgentTrigger:
    kind: str
    text: str = ""
    occurred_at_mono: float | None = None
    ttl_s: float | None = None
    event_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _nonempty(self.kind, "trigger kind"))
        object.__setattr__(self, "text", self.text.strip())
        object.__setattr__(self, "metadata", _copy_mapping(self.metadata))
        if self.occurred_at_mono is not None and not math.isfinite(self.occurred_at_mono):
            raise ValueError("occurred_at_mono must be finite")
        if self.ttl_s is not None and (not math.isfinite(self.ttl_s) or self.ttl_s <= 0):
            raise ValueError("ttl_s must be positive and finite")


@dataclass(frozen=True, slots=True)
class LoopBasis:
    user_id: str
    session_id: str
    session_epoch: int
    memory_epoch: int | None
    turn_id: str
    generation: int
    basis_state_version: int
    started_at_mono: float
    deadline_mono: float
    trigger: AgentTrigger


@dataclass(frozen=True, slots=True)
class LoopObservation:
    basis: LoopBasis
    memory: WorkingMemoryView
    evidence_refs: tuple[EvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class DecisionContext:
    observation: LoopObservation
    round_index: int
    available_tools: tuple[str, ...]
    tool_results: tuple[ToolResult, ...]
    remaining_s: float


@dataclass(frozen=True, slots=True)
class DecisionDraft:
    """Untrusted decider output, validated before retrieval or action.

    A draft is either a retrieval step (one or more ``tool_calls`` and no
    action) or a final decision (an action and no tool calls).
    """

    action: CoachAction | str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    claim_refs: tuple[ClaimRef, ...] = ()
    proposed_plan: ProposedPlan | None = None
    utterance_intent: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "claim_refs", tuple(self.claim_refs))


@dataclass(frozen=True, slots=True)
class CoachDecision:
    decision_id: str
    user_id: str
    session_id: str
    session_epoch: int
    memory_epoch: int | None
    turn_id: str
    generation: int
    basis_state_version: int
    deadline_mono: float
    action: CoachAction
    evidence_refs: tuple[EvidenceRef, ...]
    claim_refs: tuple[ClaimRef, ...]
    proposed_plan: ProposedPlan | None
    utterance_intent: str


@dataclass(frozen=True, slots=True)
class ActionContext:
    basis: LoopBasis
    is_current: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class ToolContext:
    basis: LoopBasis
    is_current: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    status: PlaybackStatus
    response_id: str | None = None
    feedback_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {
            "accepted",
            "generated",
            "queued",
            "completed",
            "interrupted",
            "rejected",
            "unknown",
        }:
            raise ValueError("unsupported action receipt status")
        object.__setattr__(self, "details", _copy_mapping(self.details))


@dataclass(frozen=True, slots=True)
class PostActionObservation:
    observed_at_mono: float
    memory: WorkingMemoryView
    receipt: ActionReceipt | None


@dataclass(frozen=True, slots=True)
class LoopResult:
    status: LoopStatus
    basis: LoopBasis
    observation: LoopObservation
    tool_results: tuple[ToolResult, ...]
    decision: CoachDecision | None
    receipt: ActionReceipt | None
    post_observation: PostActionObservation
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    max_decision_rounds: int = 2
    max_tools_per_round: int = 2
    max_total_tools: int = 4
    default_deadline_s: float = 4.0
    max_deadline_s: float = 10.0
    tool_timeout_s: float = 0.3
    max_tool_items: int = 20
    max_evidence_refs: int = 50
    max_tool_result_bytes: int = 65_536
    max_tool_argument_bytes: int = 8_192
    max_utterance_chars: int = 800

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_decision_rounds,
            self.max_tools_per_round,
            self.max_total_tools,
            self.max_tool_items,
            self.max_evidence_refs,
            self.max_tool_result_bytes,
            self.max_tool_argument_bytes,
            self.max_utterance_chars,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in integer_limits
        ):
            raise ValueError("agent loop limits must be positive integers")
        deadlines = (self.default_deadline_s, self.max_deadline_s, self.tool_timeout_s)
        if any(not math.isfinite(value) or value <= 0 for value in deadlines):
            raise ValueError("agent loop time limits must be positive and finite")
        if self.default_deadline_s > self.max_deadline_s:
            raise ValueError("default_deadline_s must not exceed max_deadline_s")


type ToolReturn = ToolPayload | Mapping[str, Any]
type DecisionReturn = DecisionDraft | Awaitable[DecisionDraft]
type ActorReturn = ActionReceipt | Mapping[str, Any] | Awaitable[ActionReceipt | Mapping[str, Any]]
type GuardReturn = bool | str | None | Awaitable[bool | str | None]


class DecisionMaker(Protocol):
    def __call__(self, context: DecisionContext) -> DecisionReturn: ...


class CoachTool(Protocol):
    def __call__(
        self, request: ToolRequest, context: ToolContext
    ) -> ToolReturn | Awaitable[ToolReturn]: ...


class DecisionActor(Protocol):
    def __call__(self, decision: CoachDecision, context: ActionContext) -> ActorReturn: ...


class RelevanceGuard(Protocol):
    def __call__(
        self, basis: LoopBasis, decision: CoachDecision, latest: WorkingMemoryView
    ) -> GuardReturn: ...


@dataclass(slots=True)
class _TurnControl:
    task_id: str
    generation: int
    deadline_mono: float
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    abort_status: LoopStatus = "cancelled"
    abort_reason: str = "cancelled"

    def abort(self, status: LoopStatus, reason: str) -> None:
        if not self.cancelled.is_set():
            self.abort_status = status
            self.abort_reason = reason
            self.cancelled.set()


class _TurnAborted(Exception):
    def __init__(self, status: LoopStatus, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class _InvalidDecision(ValueError):
    pass


def _is_async_callable(callback: Callable[..., Any]) -> bool:
    return inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        callback.__call__ if callable(callback) else None
    )


async def _invoke(callback: Callable[..., Any], *args: Any) -> Any:
    if _is_async_callable(callback):
        return await callback(*args)
    result = await asyncio.to_thread(callback, *args)
    if inspect.isawaitable(result):
        return await result
    return result


class AgentLoop:
    """One-active-turn, user-scoped bounded agent loop."""

    def __init__(
        self,
        *,
        user_id: str,
        memory: WorkingMemory,
        session_epoch: int | Callable[[], int],
        memory_epoch: int | Callable[[], int] | None = None,
        tools: Mapping[str, CoachTool],
        decider: DecisionMaker,
        actor: DecisionActor,
        config: AgentLoopConfig | None = None,
        relevance_guard: RelevanceGuard | None = None,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.user_id = _nonempty(user_id, "user_id")
        self.memory = memory
        self.session_id = _nonempty(memory.session_id, "session_id")
        self._session_epoch = session_epoch
        self._memory_epoch = memory_epoch
        self.tools = dict(tools)
        if any(
            not isinstance(name, str) or not name.strip() or not callable(tool)
            for name, tool in self.tools.items()
        ):
            raise ValueError("tools must have non-empty names and callable implementations")
        if not callable(decider) or not callable(actor):
            raise TypeError("decider and actor must be callable")
        self.decider = decider
        self.actor = actor
        self.config = config or AgentLoopConfig()
        self.relevance_guard = relevance_guard
        self.clock = clock
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._active_lock = asyncio.Lock()
        self._active: _TurnControl | None = None

    @property
    def active_turn_id(self) -> str | None:
        control = self._active
        return control.task_id if control else None

    async def cancel_active(self, reason: str = "user_interrupted") -> bool:
        """Invalidate the current generation and wake pending async waits."""

        reason = _nonempty(reason, "cancel reason")
        async with self._active_lock:
            control = self._active
            if control is None:
                return False
            control.abort("cancelled", reason)
            if self.memory.task_is_current(control.task_id, control.generation):
                self.memory.cancel_task()
            return True

    async def run(self, trigger: AgentTrigger, *, timeout_s: float | None = None) -> LoopResult:
        """Run one bounded turn and return an auditable result.

        Starting a new turn supersedes an older turn on the same ``AgentLoop``.
        A trigger TTL and the configured deadline are both hard upper bounds.
        """

        if not isinstance(trigger, AgentTrigger):
            raise TypeError("trigger must be an AgentTrigger")
        started = self.clock()
        timeout = self.config.default_deadline_s if timeout_s is None else float(timeout_s)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_s must be positive and finite")
        timeout = min(timeout, self.config.max_deadline_s)
        deadline = started + timeout
        if trigger.ttl_s is not None:
            occurred_at = (
                trigger.occurred_at_mono if trigger.occurred_at_mono is not None else started
            )
            deadline = min(deadline, occurred_at + trigger.ttl_s)

        turn_id = f"turn-{self.id_factory()}"
        task_id = turn_id
        control = _TurnControl(task_id=task_id, generation=0, deadline_mono=deadline)

        if deadline <= started:
            initial_view = replace(self._memory_snapshot(), active_task=None)
            epoch = self._current_epoch()
            basis = LoopBasis(
                user_id=self.user_id,
                session_id=self.session_id,
                session_epoch=epoch,
                memory_epoch=self._current_memory_epoch(),
                turn_id=turn_id,
                generation=0,
                basis_state_version=initial_view.state_version,
                started_at_mono=started,
                deadline_mono=deadline,
                trigger=trigger,
            )
            observation = LoopObservation(
                basis=basis,
                memory=initial_view,
                evidence_refs=self._observation_evidence(initial_view),
            )
            return self._result(
                "timeout",
                basis,
                observation,
                [],
                None,
                None,
                "trigger_expired",
            )

        async with self._active_lock:
            if self._active is not None:
                self._active.abort("stale", "superseded_by_new_turn")
            initial_view = replace(self._memory_snapshot(), active_task=None)
            epoch = self._current_epoch()
            memory_epoch = self._current_memory_epoch()
            generation = self.memory.begin_task(
                task_id,
                kind=trigger.kind,
                deadline=deadline,
                turn_id=turn_id,
                session_epoch=epoch,
            )
            control.generation = generation
            self._active = control

        basis = LoopBasis(
            user_id=self.user_id,
            session_id=self.session_id,
            session_epoch=epoch,
            memory_epoch=memory_epoch,
            turn_id=turn_id,
            generation=generation,
            basis_state_version=initial_view.state_version,
            started_at_mono=started,
            deadline_mono=deadline,
            trigger=trigger,
        )
        observation = LoopObservation(
            basis=basis,
            memory=initial_view,
            evidence_refs=self._observation_evidence(initial_view),
        )
        tool_results: list[ToolResult] = []
        decision: CoachDecision | None = None
        receipt: ActionReceipt | None = None

        try:
            self._checkpoint(control, basis)
            tools_used = 0
            for round_index in range(1, self.config.max_decision_rounds + 1):
                context = DecisionContext(
                    observation=observation,
                    round_index=round_index,
                    available_tools=tuple(sorted(self.tools)),
                    tool_results=tuple(tool_results),
                    remaining_s=max(0.0, deadline - self.clock()),
                )
                draft = await self._guarded_call(
                    self.decider, (context,), control=control, basis=basis
                )
                self._checkpoint(control, basis)
                if not isinstance(draft, DecisionDraft):
                    raise _InvalidDecision("decider must return DecisionDraft")

                if draft.tool_calls:
                    self._validate_retrieval_draft(draft, tools_used)
                    if round_index == self.config.max_decision_rounds:
                        return self._result(
                            "budget_exhausted",
                            basis,
                            observation,
                            tool_results,
                            decision,
                            receipt,
                            "no_decision_round_remains_after_retrieval",
                        )
                    requests = tuple(
                        ToolRequest(
                            call_id=f"{turn_id}-r{round_index}-t{index}",
                            name=call.name,
                            arguments=_copy_mapping(call.arguments),
                        )
                        for index, call in enumerate(draft.tool_calls, start=1)
                    )
                    results = await self._execute_tool_round(requests, control=control, basis=basis)
                    self._checkpoint(control, basis)
                    tool_results.extend(results)
                    tools_used += len(results)
                    continue

                decision = self._build_decision(draft, basis, observation, tool_results)
                latest = self._memory_snapshot()
                await self._check_relevance(decision, latest, control, basis)
                self._checkpoint(control, basis)
                self._validate_decision_evidence(decision)
                self._checkpoint(control, basis)

                if decision.action == "abstain":
                    post = self._post_observation(None, basis=basis)
                    return LoopResult(
                        status="abstained",
                        basis=basis,
                        observation=observation,
                        tool_results=tuple(tool_results),
                        decision=decision,
                        receipt=None,
                        post_observation=post,
                    )

                action_context = self._action_context(control, basis)
                raw_receipt = await self._guarded_call(
                    self.actor,
                    (decision, action_context),
                    control=control,
                    basis=basis,
                )
                self._checkpoint(control, basis)
                receipt = self._normalize_receipt(raw_receipt)
                post = self._post_observation(receipt, basis=basis)
                return LoopResult(
                    status="completed",
                    basis=basis,
                    observation=observation,
                    tool_results=tuple(tool_results),
                    decision=decision,
                    receipt=receipt,
                    post_observation=post,
                )

            return self._result(
                "budget_exhausted",
                basis,
                observation,
                tool_results,
                decision,
                receipt,
                "decision_round_budget_exhausted",
            )
        except _InvalidDecision as exc:
            return self._result(
                "invalid_decision",
                basis,
                observation,
                tool_results,
                decision,
                receipt,
                str(exc),
            )
        except _TurnAborted as exc:
            return self._result(
                exc.status,
                basis,
                observation,
                tool_results,
                decision,
                receipt,
                exc.reason,
            )
        except asyncio.CancelledError:
            control.abort("cancelled", "caller_cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - plugin boundary becomes a typed result
            return self._result(
                "error",
                basis,
                observation,
                tool_results,
                decision,
                receipt,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            async with self._active_lock:
                if self._active is control:
                    self._active = None
                    if self.memory.task_is_current(control.task_id, control.generation):
                        self.memory.cancel_task()

    def _current_epoch(self) -> int:
        value = self._session_epoch() if callable(self._session_epoch) else self._session_epoch
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("session_epoch must be a non-negative integer")
        return value

    def _current_memory_epoch(self) -> int | None:
        if self._memory_epoch is None:
            return None
        value = self._memory_epoch() if callable(self._memory_epoch) else self._memory_epoch
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("memory_epoch must be a non-negative integer")
        return value

    def _memory_snapshot(self) -> WorkingMemoryView:
        snapshot = copy.deepcopy(self.memory.view())
        if snapshot.session_id != self.session_id:
            raise ValueError("working memory session changed")
        return snapshot

    def _observation_evidence(self, view: WorkingMemoryView) -> tuple[EvidenceRef, ...]:
        refs: list[EvidenceRef] = [
            EvidenceRef(
                source_type="working_memory",
                source_id=f"{view.session_id}:{view.state_version}",
                revision=view.state_version,
                user_id=self.user_id,
                session_id=view.session_id,
            )
        ]
        if view.current_motion is not None:
            refs.append(
                EvidenceRef(
                    source_type="motion_snapshot",
                    source_id=f"{view.session_id}:{view.current_motion.frame_id}",
                    revision=view.state_version,
                    user_id=self.user_id,
                    session_id=view.session_id,
                )
            )
        refs.extend(
            EvidenceRef(
                source_type="event",
                source_id=event.event_id,
                user_id=self.user_id,
                session_id=view.session_id,
            )
            for event in view.events
        )
        refs.extend(
            EvidenceRef(
                source_type="rep",
                source_id=rep.rep_id,
                user_id=self.user_id,
                session_id=view.session_id,
            )
            for rep in view.reps
        )
        if view.poses:
            first, last = view.poses[0], view.poses[-1]
            refs.append(
                EvidenceRef(
                    source_type="pose_window",
                    source_id=f"{view.session_id}:{first.frame_id}-{last.frame_id}",
                    revision=view.state_version,
                    user_id=self.user_id,
                    session_id=view.session_id,
                    available_until_mono=last.observed_at + self.memory.pose_window_s,
                )
            )
        return tuple(refs[: self.config.max_evidence_refs])

    def _checkpoint(self, control: _TurnControl, basis: LoopBasis) -> None:
        if control.cancelled.is_set():
            raise _TurnAborted(control.abort_status, control.abort_reason)
        if self.clock() >= control.deadline_mono:
            raise _TurnAborted("timeout", "turn_deadline_exceeded")
        if self._current_epoch() != basis.session_epoch:
            raise _TurnAborted("stale", "session_epoch_changed")
        if self._current_memory_epoch() != basis.memory_epoch:
            raise _TurnAborted("stale", "memory_epoch_changed")
        if not self.memory.task_is_current(control.task_id, control.generation):
            raise _TurnAborted("stale", "turn_generation_changed")

    def _action_context(self, control: _TurnControl, basis: LoopBasis) -> ActionContext:
        def is_current() -> bool:
            try:
                self._checkpoint(control, basis)
            except _TurnAborted:
                return False
            return True

        return ActionContext(basis=basis, is_current=is_current)

    def _tool_context(self, control: _TurnControl, basis: LoopBasis) -> ToolContext:
        action_context = self._action_context(control, basis)
        return ToolContext(basis=action_context.basis, is_current=action_context.is_current)

    async def _guarded_call(
        self,
        callback: Callable[..., Any],
        args: tuple[Any, ...],
        *,
        control: _TurnControl,
        basis: LoopBasis,
        timeout_s: float | None = None,
    ) -> Any:
        self._checkpoint(control, basis)
        operation = asyncio.create_task(_invoke(callback, *args))
        cancellation = asyncio.create_task(control.cancelled.wait())
        remaining = control.deadline_mono - self.clock()
        if timeout_s is not None:
            remaining = min(remaining, timeout_s)
        if remaining <= 0:
            operation.cancel()
            cancellation.cancel()
            raise _TurnAborted("timeout", "turn_deadline_exceeded")
        try:
            done, _ = await asyncio.wait(
                {operation, cancellation}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation in done:
                operation.cancel()
                with suppress(BaseException):
                    await operation
                raise _TurnAborted(control.abort_status, control.abort_reason)
            if operation in done:
                cancellation.cancel()
                with suppress(asyncio.CancelledError):
                    await cancellation
                result = await operation
                self._checkpoint(control, basis)
                return result
            operation.cancel()
            with suppress(BaseException):
                await operation
            if self.clock() >= control.deadline_mono:
                raise _TurnAborted("timeout", "turn_deadline_exceeded")
            raise TimeoutError("operation_timeout")
        except asyncio.CancelledError:
            operation.cancel()
            cancellation.cancel()
            with suppress(BaseException):
                await operation
            raise
        finally:
            cancellation.cancel()

    def _validate_retrieval_draft(self, draft: DecisionDraft, tools_used: int) -> None:
        if draft.action is not None:
            raise _InvalidDecision("a retrieval draft cannot also contain an action")
        if draft.evidence_refs or draft.claim_refs or draft.proposed_plan is not None:
            raise _InvalidDecision("a retrieval draft cannot contain final decision fields")
        if draft.utterance_intent.strip():
            raise _InvalidDecision("a retrieval draft cannot contain an utterance")
        calls = draft.tool_calls
        if len(calls) > self.config.max_tools_per_round:
            raise _InvalidDecision("tool calls exceed the per-round budget")
        if tools_used + len(calls) > self.config.max_total_tools:
            raise _InvalidDecision("tool calls exceed the total turn budget")
        for call in calls:
            if not isinstance(call, ToolCall):
                raise _InvalidDecision("tool_calls must contain ToolCall values")
            if call.name not in self.tools:
                raise _InvalidDecision(f"tool is not allowlisted: {call.name}")
            forbidden = self._find_forbidden_argument(call.arguments)
            if forbidden is not None:
                raise _InvalidDecision(f"tool argument cannot select scope: {forbidden}")
            try:
                encoded = json.dumps(call.arguments, allow_nan=False, ensure_ascii=False).encode()
            except (TypeError, ValueError) as exc:
                raise _InvalidDecision(f"tool arguments are not valid JSON: {exc}") from exc
            if len(encoded) > self.config.max_tool_argument_bytes:
                raise _InvalidDecision("tool arguments exceed the byte budget")

    def _find_forbidden_argument(self, value: Any) -> str | None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key)
                if key_text in _FORBIDDEN_SCOPE_ARGUMENTS:
                    return key_text
                nested = self._find_forbidden_argument(child)
                if nested is not None:
                    return nested
        elif isinstance(value, (list, tuple)):
            for child in value:
                nested = self._find_forbidden_argument(child)
                if nested is not None:
                    return nested
        return None

    async def _execute_tool(
        self, request: ToolRequest, *, control: _TurnControl, basis: LoopBasis
    ) -> ToolResult:
        started = self.clock()
        context = self._tool_context(control, basis)
        try:
            raw = await self._guarded_call(
                self.tools[request.name],
                (request, context),
                control=control,
                basis=basis,
                timeout_s=self.config.tool_timeout_s,
            )
            payload = self._normalize_tool_payload(raw)
            status: ToolStatus
            if payload.error_code or payload.error_message:
                status = "error"
            elif payload.items:
                status = "ok"
            else:
                status = "empty"
            return ToolResult(
                call_id=request.call_id,
                name=request.name,
                status=status,
                items=payload.items,
                evidence_refs=payload.evidence_refs,
                truncated=payload.truncated,
                elapsed_ms=max(0.0, (self.clock() - started) * 1000.0),
                error_code=payload.error_code,
                error_message=payload.error_message,
                scope_epoch=payload.scope_epoch,
            )
        except TimeoutError:
            return ToolResult(
                call_id=request.call_id,
                name=request.name,
                status="timeout",
                items=(),
                evidence_refs=(),
                truncated=False,
                elapsed_ms=max(0.0, (self.clock() - started) * 1000.0),
                error_code="tool_timeout",
                error_message="tool exceeded its per-call deadline",
                scope_epoch=None,
            )
        except _TurnAborted as exc:
            control.abort(exc.status, exc.reason)
            raise
        except Exception as exc:  # noqa: BLE001 - tool failures are data for the next round
            return ToolResult(
                call_id=request.call_id,
                name=request.name,
                status="error",
                items=(),
                evidence_refs=(),
                truncated=False,
                elapsed_ms=max(0.0, (self.clock() - started) * 1000.0),
                error_code="tool_error",
                error_message=f"{type(exc).__name__}: {exc}",
                scope_epoch=None,
            )

    async def _execute_tool_round(
        self,
        requests: tuple[ToolRequest, ...],
        *,
        control: _TurnControl,
        basis: LoopBasis,
    ) -> tuple[ToolResult, ...]:
        tasks = tuple(
            asyncio.create_task(self._execute_tool(request, control=control, basis=basis))
            for request in requests
        )
        try:
            return tuple(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def _normalize_tool_payload(self, raw: Any) -> ToolPayload:
        if isinstance(raw, ToolPayload):
            items = list(raw.items)
            refs = list(raw.evidence_refs)
            truncated = raw.truncated
            error_code = raw.error_code
            error_message = raw.error_message
            scope_epoch = raw.scope_epoch
        elif isinstance(raw, Mapping):
            raw_items = raw.get("items", ())
            if raw_items is None:
                raw_items = ()
            if not isinstance(raw_items, (list, tuple)):
                raise TypeError("tool result items must be a list or tuple")
            items = list(raw_items)
            raw_refs = raw.get("evidence_refs", ()) or ()
            if not isinstance(raw_refs, (list, tuple)):
                raise TypeError("tool result evidence_refs must be a list or tuple")
            refs = [EvidenceRef.from_value(item) for item in raw_refs]
            truncated = bool(raw.get("truncated", False))
            error = raw.get("error")
            error_code = raw.get("error_code")
            error_message = raw.get("error_message")
            scope_epoch = raw.get("scope_epoch")
            if isinstance(error, Mapping):
                error_code = error_code or error.get("code")
                error_message = error_message or error.get("message")
            elif error:
                error_message = error_message or str(error)
        else:
            raise TypeError("tool must return ToolPayload or a result envelope mapping")

        if scope_epoch is not None and (
            isinstance(scope_epoch, bool) or not isinstance(scope_epoch, int) or scope_epoch < 0
        ):
            raise TypeError("tool result scope_epoch must be a non-negative integer")
        expected_scope_epoch = self._current_memory_epoch()
        if expected_scope_epoch is not None and scope_epoch is None:
            raise _TurnAborted("stale", "tool_scope_epoch_missing")
        if (
            expected_scope_epoch is not None
            and scope_epoch is not None
            and scope_epoch != expected_scope_epoch
        ):
            raise _TurnAborted("stale", "tool_scope_epoch_mismatch")

        if any(not isinstance(item, Mapping) for item in items):
            raise ValueError("tool result items must be mappings")
        if len(items) > self.config.max_tool_items:
            items = items[: self.config.max_tool_items]
            truncated = True
        if len(refs) > self.config.max_evidence_refs:
            refs = refs[: self.config.max_evidence_refs]
            truncated = True

        copied_items = tuple(_copy_mapping(item) for item in items)
        normalized_refs = tuple(EvidenceRef.from_value(item) for item in refs)
        if any(ref.user_id is not None and ref.user_id != self.user_id for ref in normalized_refs):
            raise ValueError("tool returned evidence from another user scope")
        if any(
            ref.session_id is not None and ref.session_id != self.session_id
            for ref in normalized_refs
        ):
            raise ValueError("tool returned evidence from another session")
        if error_code or error_message:
            # An error is not an empty result and cannot introduce facts.
            copied_items = ()
            normalized_refs = ()
        encoded = json.dumps(
            {
                "items": copied_items,
                "evidence_refs": [ref.evidence_id for ref in normalized_refs],
                "error_code": error_code,
                "error_message": error_message,
                "scope_epoch": scope_epoch,
            },
            allow_nan=False,
            ensure_ascii=False,
        ).encode()
        if len(encoded) > self.config.max_tool_result_bytes:
            raise ValueError("tool result exceeds the byte budget")
        return ToolPayload(
            items=copied_items,
            evidence_refs=normalized_refs,
            truncated=truncated,
            error_code=str(error_code) if error_code else None,
            error_message=str(error_message) if error_message else None,
            scope_epoch=scope_epoch,
        )

    def _build_decision(
        self,
        draft: DecisionDraft,
        basis: LoopBasis,
        observation: LoopObservation,
        tool_results: list[ToolResult],
    ) -> CoachDecision:
        if draft.action not in _ALLOWED_ACTIONS:
            raise _InvalidDecision(f"unsupported action: {draft.action}")
        if draft.tool_calls:
            raise _InvalidDecision("a final decision cannot contain tool calls")
        utterance = draft.utterance_intent.strip()
        if len(utterance) > self.config.max_utterance_chars:
            raise _InvalidDecision("utterance exceeds the character budget")
        if draft.action != "abstain" and not utterance:
            raise _InvalidDecision("non-abstain decisions require an utterance intent")
        if draft.action == "propose_plan" and draft.proposed_plan is None:
            raise _InvalidDecision("propose_plan requires a proposal")
        if draft.action != "propose_plan" and draft.proposed_plan is not None:
            raise _InvalidDecision("only propose_plan may contain a proposal")
        if len(draft.evidence_refs) > self.config.max_evidence_refs:
            raise _InvalidDecision("evidence references exceed the turn budget")
        if len(draft.claim_refs) > self.config.max_evidence_refs:
            raise _InvalidDecision("claim references exceed the turn budget")

        available = {
            ref.evidence_id: ref
            for ref in (
                *observation.evidence_refs,
                *(
                    ref
                    for result in tool_results
                    if result.status in {"ok", "empty"}
                    for ref in result.evidence_refs
                ),
            )
        }
        selected: list[EvidenceRef] = []
        seen: set[str] = set()
        for raw_ref in draft.evidence_refs:
            try:
                ref = EvidenceRef.from_value(raw_ref)
            except (TypeError, ValueError) as exc:
                raise _InvalidDecision(str(exc)) from exc
            evidence_id = ref.evidence_id
            admitted = available.get(evidence_id)
            if admitted is None:
                raise _InvalidDecision(f"decision cites unavailable evidence: {evidence_id}")
            if admitted.user_id is not None and admitted.user_id != self.user_id:
                raise _InvalidDecision("decision cites evidence from another user scope")
            if admitted.session_id is not None and admitted.session_id != self.session_id:
                raise _InvalidDecision("decision cites evidence from another session")
            if (
                admitted.available_until_mono is not None
                and self.clock() >= admitted.available_until_mono
            ):
                raise _InvalidDecision(f"decision cites expired evidence: {evidence_id}")
            if evidence_id not in seen:
                selected.append(admitted)
                seen.add(evidence_id)

        claim_keys: set[str] = set()
        for claim in draft.claim_refs:
            if not isinstance(claim, ClaimRef):
                raise _InvalidDecision("claim_refs must contain ClaimRef values")
            if claim.claim_key in claim_keys:
                raise _InvalidDecision(f"duplicate claim key: {claim.claim_key}")
            claim_keys.add(claim.claim_key)
            for evidence_id in claim.evidence_ids:
                if evidence_id not in seen:
                    raise _InvalidDecision(
                        f"claim references evidence not selected by decision: {evidence_id}"
                    )

        return CoachDecision(
            decision_id=f"decision-{self.id_factory()}",
            user_id=self.user_id,
            session_id=self.session_id,
            session_epoch=basis.session_epoch,
            memory_epoch=basis.memory_epoch,
            turn_id=basis.turn_id,
            generation=basis.generation,
            basis_state_version=basis.basis_state_version,
            deadline_mono=basis.deadline_mono,
            action=draft.action,
            evidence_refs=tuple(selected),
            claim_refs=draft.claim_refs,
            proposed_plan=draft.proposed_plan,
            utterance_intent=utterance,
        )

    def _validate_decision_evidence(self, decision: CoachDecision) -> None:
        """Recheck short-lived evidence after the relevance callback returns."""

        now = self.clock()
        for ref in decision.evidence_refs:
            if ref.user_id is not None and ref.user_id != self.user_id:
                raise _TurnAborted("stale", "decision_evidence_scope_changed")
            if ref.session_id is not None and ref.session_id != self.session_id:
                raise _TurnAborted("stale", "decision_evidence_session_changed")
            if ref.available_until_mono is not None and now >= ref.available_until_mono:
                raise _TurnAborted("stale", "decision_evidence_expired")

    async def _check_relevance(
        self,
        decision: CoachDecision,
        latest: WorkingMemoryView,
        control: _TurnControl,
        basis: LoopBasis,
    ) -> None:
        if self.relevance_guard is None:
            return
        result = await self._guarded_call(
            self.relevance_guard,
            (basis, decision, latest),
            control=control,
            basis=basis,
        )
        if result is False:
            raise _TurnAborted("stale", "decision_no_longer_relevant")
        if isinstance(result, str) and result.strip():
            raise _TurnAborted("stale", result.strip())
        if result not in (None, True):
            raise _InvalidDecision("relevance guard must return bool, reason string or None")

    def _normalize_receipt(self, raw: Any) -> ActionReceipt:
        if isinstance(raw, ActionReceipt):
            return raw
        if not isinstance(raw, Mapping):
            raise TypeError("actor must return ActionReceipt or a mapping")
        return ActionReceipt(
            status=str(raw.get("status", "unknown")),
            response_id=(str(raw["response_id"]) if raw.get("response_id") else None),
            feedback_id=(str(raw["feedback_id"]) if raw.get("feedback_id") else None),
            details=raw.get("details", {}),
        )

    def _post_observation(
        self, receipt: ActionReceipt | None, *, basis: LoopBasis | None = None
    ) -> PostActionObservation:
        memory = self._memory_snapshot()
        if basis is not None and not self.memory.task_is_current(basis.turn_id, basis.generation):
            # A superseded result must not reveal the successor turn's task or
            # metadata to the caller handling the stale result.
            memory = replace(memory, active_task=None)
        return PostActionObservation(observed_at_mono=self.clock(), memory=memory, receipt=receipt)

    def _result(
        self,
        status: LoopStatus,
        basis: LoopBasis,
        observation: LoopObservation,
        tool_results: list[ToolResult],
        decision: CoachDecision | None,
        receipt: ActionReceipt | None,
        reason: str,
    ) -> LoopResult:
        return LoopResult(
            status=status,
            basis=basis,
            observation=observation,
            tool_results=tuple(tool_results),
            decision=decision,
            receipt=receipt,
            post_observation=self._post_observation(receipt, basis=basis),
            reason=reason,
        )
