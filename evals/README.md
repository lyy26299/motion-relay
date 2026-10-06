# Opt-in product evaluation

This directory contains product-level evaluation contracts for Motion Relay that are intentionally **not** part of the default regression suite.

The current executable slice is:

- `test_eval_p0_now.py`: 20 P0 cases covering feedback coverage/silence, interruption/control, memory isolation, and temporal routing.

Some tests intentionally fail on the current V2.2 implementation because they encode target product behavior and expose known gaps. Do not interpret a red result here as a regression unless the same behavior was already guaranteed by the existing V2.2 contract.

## Run locally

From the repository root:

```bash
uv sync --locked --extra dev

uv run python -m unittest discover \
  -s evals \
  -p 'test_eval_p0_now.py' \
  -v
```

The baseline run on 2026-10-06 produced:

- 20 executed
- 10 PASS
- 10 FAIL

Known failing areas in that run:

- current-status vs committed-history routing
- negated/historical discomfort parsing
- generic pause/resume control routing
- persistent user-speaking silence gate
- previous-session/calendar-time routing
- event-time vs session-time aggregation
- explicit timezone handling for naive datetimes

See `docs/ACTUAL_EVAL_REPORT_2026-10-06.md` for the full observed result and limitations.

## Important

These tests use deterministic local fixtures only. They do not call a live model, camera, microphone, browser audio, paid API, or human participant.

The default regression suite remains separate:

```bash
uv run python -m unittest discover -s tests -v
```

Keep this separation so product-contract failures do not silently turn the architecture regression CI red before the corresponding feature is implemented.
