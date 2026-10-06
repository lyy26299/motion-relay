# ADR 0007: Monotonic, scope-bound feedback receipts

- Date: 2026-09-24
- Status: Implemented in the research branch; PR remains Draft.
- Baseline: V2.1 `fb4c3b1b104accc144fdb547b878cfa800682fe4`.

## Context

Independent response observers finish in different orders. A late queued update
can overwrite interrupted, even though each SQLite UPDATE is thread-safe.
Cancelling an asyncio waiter does not roll back an already running worker.
User-only mutation also lacks a session/epoch fence at the final commit boundary.

## Decision

Keep MemoryStore as transaction owner. Introduce a pure finite status join and
an immutable FeedbackRecorder bound to user/session/memory_epoch by the runtime.
Use one outer BEGIN IMMEDIATE transaction for current-scope check, receipt read,
response-identity check, merge and write. Preserve existing playback evidence;
never infer played_at from a provider generation event. Add fixed-reason counters
at the Bridge without making the pose path wait for SQLite.

Interruption is delivery invalidation, not a generation or speaker observation.
Contradictory known outcomes produce delivery_conflict. Unknown legacy states are
preserved. A conflicting nonempty response ID is rejected, not rebound; request
causality remains unverified.

## Alternatives

An asyncio.Lock alone cannot fence other connections or detached worker commits.
A durable sequenced observer log/outbox could add replay and delivery guarantees,
but requires separate schema, sequence-source, retention, retry and shutdown
contracts. It is not implemented by pretending callback arrival order is causal.

## Consequences

Read/merge/write is atomic for this capability, with extra transaction and scope
checks. The compatibility store API can still bypass it and is trusted-only.
Bounded callback/IO capacity can reject observations; no lossless delivery claim.
Timeout is an unconfirmed commit outcome, not rollback. User deletion invalidates
old recorders; same IDs cannot grant them new authority. No DB migration or new
runtime dependency, and no hard realtime, physical playback or cloud cost claim.

## Migration and validation

The default Bridge uses the recorder; custom sinks must do the same. Handle the
new delivery_conflict summary as uncertainty, not success. Keep old records.
Tests cover every finite-state join triple, arrival permutations, independent
SQLite connections, rollback, scope replacement, delayed workers and cancellation.
The benchmark compares invariant preservation and cost; it does not claim speedup.
See [the technical note](../FEEDBACK_RECEIPTS_V2_2.md) and
[the test report](../TEST_REPORT.md) for measured results and release gates.

## Primary references

- [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
- [SQLite isolation](https://www.sqlite.org/isolation.html)
- [Python asyncio task cancellation](https://docs.python.org/3.13/library/asyncio-task.html)

Retrieved 2026-09-24. State ordering and the join are project decisions, not rules
mandated by these sources.
