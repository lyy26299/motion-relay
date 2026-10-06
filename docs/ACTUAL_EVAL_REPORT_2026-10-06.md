# Motion Relay P0 Evaluation Run — 2026-10-06

## Scope

Source: GitHub Actions artifact for branch `research/agent-architecture-v2`, workflow run 35975751955. The artifact's `source.zip` was used because the local container could not resolve github.com directly. The workflow head is `fb2fb76a5c95cef869134e0ced758c1689e9398e`; the artifact's internal `commit.txt` contains `bb4f3fa60211e8d960d7463c2bdb4914975b63ee`, so this run records that provenance mismatch instead of silently treating them as identical.

No network model, camera, microphone, browser audio, or human participant was used.

## Environment

- Python: 3.13.5
- uv: 0.10.0
- Dependency sync: attempted, blocked by container DNS when downloading `pyyaml==6.0.3`

## Existing regression slice

Five deterministic suites were run with the available environment:

- `test_agent_architecture_v2.py`: 17/17 PASS
- `test_runtime_boundaries.py`: 9/9 PASS
- `test_memory_retrieval.py`: 4/4 PASS
- `test_feedback_receipts.py`: 33/33 PASS
- `test_native_output_admission.py`: 9/9 PASS

Total: **72/72 PASS**.

This supports that the existing V2.2 protections exercised by those suites still hold in this environment.

## Full original-suite attempt

Before adding the new evaluation slice, `python -m unittest discover -s tests -v` discovered 141 runnable/import-placeholder tests in this container:

- 134 passed
- 0 assertion failures
- 7 errors caused by unavailable dependencies/imports

Blocked modules/dependencies included `av`, `mcp`, `getstream`, and `vision_agents`. This is an environment/dependency limitation, not evidence that those 7 product behaviors failed.

## New product-level P0 slice

20 executable tests were added locally without modifying production code.

Result: **10 PASS / 10 FAIL**.

### PASS

- C01: idle required rep feedback reaches mock voice once
- I03: local discomfort pause does not wait for SQLite receipt path
- Q03: duplicate motion event is not spoken twice
- M01: same query shape remains scoped to each user
- M03: cross-user session ID returns no private rows
- M08: old retrieval service is invalid after delete/recreate epoch change
- F06: deadline equal to now is stale
- F10: new camera epoch cannot complete old partial rep
- I11(runtime invariant): explicit application pause stays latched across new frames
- M12: multiple-people observation does not create a person-owned rep

### FAIL / observed gaps

1. **C02/T01 — Current count routes to committed history instead of live truth.**
   Fixture: live WorkingMemory = completed 5 / valid 4, SQLite = completed 3 / valid 2. Query: `我现在完成多少次了`. The captured prompt contained `completed_reps=3, valid_reps=2`, not live 5/4.

2. **I09 — Negated discomfort false-positive.**
   `我现在没有不舒服` paused the current runtime because substring matching treats `不舒服` as affirmative.

3. **I10 — Historical/negated discomfort false-positive.**
   `昨天不舒服，现在已经没有了` paused the current runtime.

4. **I11(control route) — Generic pause intent is not wired to runtime pause.**
   `暂停一下` did not set the application pause state.

5. **I12 — Resume intent is not wired to runtime resume.**
   After explicit pause, `继续训练` did not release the pause.

6. **Q01 — Routine rep feedback can speak while user-speaking state is active.**
   After `on_user_speech_started()`, a real Runtime→FSM→rep event still reached mock voice once.

7. **T02 — “Last session” evidence includes the current active session.**
   Query `上一次训练有效几次` passed both `A_NOW` and `A_PREV` into the model prompt.

8. **T03 — “Yesterday” evidence includes today’s active session.**
   Query `昨天有效几次` also passed `A_NOW` into the model prompt; no explicit temporal range was routed.

9. **T08 — Cross-midnight event-time semantics are unsupported by `query_training`.**
   A session started before midnight with 2 reps before and 2 after midnight returned 4 reps for the previous-day session-start range; the new evaluation contract for “昨天完成了几次” expects 2 by `rep.completed_at`. This is a capability mismatch, not a claim that the existing `query_training` API violates its documented per-session semantics.

10. **T17 — Naive ISO datetime is accepted without explicit timezone.**
    `_finite_time("2026-10-05T00:00:00")` did not reject the timezone-less value, so absolute interpretation can depend on host timezone.

## Interpretation

The failures cluster into three product-level gaps rather than random regressions:

- **Control/intent semantics:** pause/resume and negation/history handling are not yet a structured control router.
- **Silence policy:** VAD invalidates old output, but there is no persistent `user_speaking` gate preventing a new routine feedback turn before final transcription.
- **Temporal/source routing:** “current”, “last session”, and calendar-time queries are routed through one generic historical query with fixed `exercise=squat, metric=all, limit=5`; event-time aggregation and explicit timezone semantics are not yet implemented.

The memory scope/epoch and core race protections in the tested slice remained intact.

## Not Run / Unobservable

The following portions of the broader evaluation design remain NOT RUN or UNOBSERVABLE in this environment:

- final spoken-content correctness through the real model
- strict client-request ↔ provider-response causal attribution
- browser/physical playback confirmation and acoustic interruption latency
- real camera / microphone / AEC behavior
- online Qwen/other provider behavior
- human interaction study
- full multi-person identity binding beyond the deterministic `multiple_people` fail-closed case

## Reproduction commands

```bash
uv sync --locked --extra dev

uv run python -m unittest discover -s evals -p 'test_eval_p0_now.py' -v

uv run python -m unittest discover -s tests -p 'test_agent_architecture_v2.py' -v
uv run python -m unittest discover -s tests -p 'test_runtime_boundaries.py' -v
uv run python -m unittest discover -s tests -p 'test_memory_retrieval.py' -v
uv run python -m unittest discover -s tests -p 'test_feedback_receipts.py' -v
uv run python -m unittest discover -s tests -p 'test_native_output_admission.py' -v
```

## Expected current behavior

The P0 evaluation file is intentionally opt-in. On the V2.2 implementation used for this run, expect roughly:

- 20 tests executed
- 10 PASS
- 10 FAIL

If Codex or local changes reduce these failures, compare the failure list against the product contracts above rather than simply forcing the suite green.
