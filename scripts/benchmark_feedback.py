"""Compare receipt paths; no network, audio, threads, or on-disk durability measurement.

Run with python -m scripts.benchmark_feedback in the current checkout. The same
harness can load an archived --source-root; module hashes reject source mixing.
"""
from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
from pathlib import Path

from scripts.benchmark_architecture import measure, source_module_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-revision", default="working-tree")
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.iterations < 100:
        parser.error("--iterations must be at least 100")
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from coach.memory.store import MemoryStore

    n = args.iterations
    metrics = {}
    invariants = {}
    with MemoryStore(":memory:") as store:
        store.ensure_user("u")
        store.create_session("u", session_id="s")
        ids = [f"legacy-{i}" for i in range(n + 100)]
        for fid in ids:
            store.record_feedback("u", "s", feedback_id=fid, cue_text="fixture", status="accepted")
        legacy_ids = iter(ids)
        legacy_regressions = 0
        def legacy_cycle():
            nonlocal legacy_regressions
            fid = next(legacy_ids)
            store.update_feedback("u", fid, status="generated", response_id="r")
            store.update_feedback("u", fid, status="interrupted", playback_state="interrupted")
            result = store.update_feedback("u", fid, status="queued", playback_state="unknown")
            legacy_regressions += result["status"] != "interrupted"
        metrics["legacy_three_observation_cycle"] = measure(legacy_cycle, n)
        invariants["legacy_terminal_regressions_including_warmups"] = legacy_regressions
        # Never import an optional module absent in the requested checkout:
        # an editable install could otherwise fall back to the current source.
        if not all((root / path).is_file() for path in (
            "coach/feedback_state.py", "coach/memory/feedback.py",
        )):
            metrics["guarded_three_observation_cycle"] = {"available": False,
                                                         "reason": "absent from source root"}
            metrics["feedback_status_join"] = {"available": False,
                                               "reason": "absent from source root"}
        else:
            from coach.feedback_state import merge_feedback_status
            from coach.memory.feedback import FeedbackRecorder
            recorder = FeedbackRecorder(store, "u", "s", store.get_memory_epoch("u"))
            ids = [f"guarded-{i}" for i in range(n + 100)]
            for fid in ids:
                store.record_feedback("u", "s", feedback_id=fid, cue_text="fixture", status="accepted")
            guarded_ids = iter(ids)
            guarded_regressions = 0
            def guarded_cycle():
                nonlocal guarded_regressions
                fid = next(guarded_ids)
                recorder.apply(fid, "generated", "r")
                recorder.apply(fid, "interrupted", "r")
                result = recorder.apply(fid, "queued")
                guarded_regressions += result.status != "interrupted"
            metrics["guarded_three_observation_cycle"] = measure(guarded_cycle, n)
            metrics["feedback_status_join"] = measure(
                lambda: merge_feedback_status("interrupted", "queued"), n)
            invariants["guarded_terminal_regressions_including_warmups"] = guarded_regressions
            assert guarded_regressions == 0, "receipt invariant regressed"
    result = {
        "schema_version": "coach.feedback_benchmark.v1",
        "source_revision": args.source_revision,
        "source_modules": source_module_manifest(root),
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "machine": platform.machine(), "sqlite": sqlite3.sqlite_version},
        "method": "100 warmups + measured samples; nearest-rank percentiles; precreated receipts",
        "invariant_observations_per_path": n + 100,
        "metrics": metrics,
        "invariants": invariants,
        "limitations": [
            "legacy and guarded cycles have different correctness contracts, not a speedup contest",
            "in-memory SQLite; no disk fsync, contention, executor scheduling or power-loss test",
            "no camera, network, LLM, TTS, speaker, actual playback acknowledgment or user study",
            "legacy low-level API intentionally unchanged; application must use the scoped recorder",
        ],
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
