"""Small local-path benchmark. No live model, camera, audio or external service.

Run the SAME script/environment against --source-root for a fair baseline.
Fixtures are constructed before timing; percentile estimator is nearest rank.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import queue
import sqlite3
import statistics
import sys
import time
from pathlib import Path


def summarize(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {
        "n": len(samples), "unit": "us",
        "p50": ordered[math.ceil(.50 * len(ordered)) - 1],
        "p95": ordered[math.ceil(.95 * len(ordered)) - 1],
        "p99": ordered[math.ceil(.99 * len(ordered)) - 1],
        "mean": statistics.fmean(samples),
        "measured_calls_per_second": 1_000_000 / statistics.fmean(samples),
    }


def measure(callback, n: int) -> dict:
    for _ in range(100):
        callback()
    samples = []
    for _ in range(n):
        started = time.perf_counter_ns()
        callback()
        samples.append((time.perf_counter_ns() - started) / 1000)
    return summarize(samples)


async def agent_measure(n: int) -> dict:
    from coach.agent_loop import AgentLoop, AgentTrigger, DecisionDraft
    from coach.working_memory import WorkingMemory
    async def decide(context):
        return DecisionDraft(action="abstain")
    async def act(*args):
        raise AssertionError("abstain must never act")
    loop = AgentLoop(user_id="u", session_epoch=0, memory=WorkingMemory(session_id="s"),
                     tools={}, decider=decide, actor=act)
    values = []
    for i in range(n + 100):
        started = time.perf_counter_ns()
        result = await loop.run(AgentTrigger(kind="benchmark"))
        elapsed = (time.perf_counter_ns() - started) / 1000
        assert result.status == "abstained"
        if i >= 100:
            values.append(elapsed)
    if hasattr(loop, "close"):
        await loop.close()
    return summarize(values)



def source_module_manifest(root: Path, modules=None) -> dict:
    """Reject editable-install fallbacks outside the requested source checkout.

    Run after timing so file hashing is not included in benchmark samples.
    """
    root = root.resolve()
    selected = dict(sys.modules if modules is None else modules)
    manifest = {}
    for name, module in sorted(selected.items()):
        if name != "coach" and not name.startswith("coach."):
            continue
        filename = getattr(module, "__file__", None)
        if filename is None:
            raise RuntimeError(f"source module has no file: {name}")
        path = Path(filename).resolve()
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"benchmark imported {name} outside --source-root") from exc
        manifest[name] = {"path": relative.as_posix(),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-revision", default="working-tree")
    parser.add_argument("--iterations", type=int, default=5000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.iterations < 100:
        parser.error("--iterations must be at least 100")
    sys.path.insert(0, str(args.source_root.resolve()))
    from coach.exercises.squat import SquatFSM
    from coach.memory.retrieval import RetrievalService
    from coach.memory.store import MemoryStore
    from coach.models import PoseSnapshot, ProjectedAngles
    from coach.runtime import MotionRuntime
    from coach.working_memory import WorkingMemory
    n = args.iterations
    poses = [PoseSnapshot(
        "s", 1, i+1, i*.3, i*.3, i*.3, 640, 480, "fixture", .5, (),
        ProjectedAngles((175,145,110,130,170)[i%5], (175,145,110,130,170)[i%5],160,160),
        "observable", 0,
    ) for i in range(n+101)]
    fsm, runtime, memory = SquatFSM(session_id="s"), MotionRuntime(session_id="s"), WorkingMemory(session_id="s")
    metrics = {"clock_and_callable_overhead": measure(lambda: None, n)}
    for name, callback in (("fsm_update", fsm.update), ("motion_ingest", runtime.ingest),
                           ("working_memory_add_pose", memory.add_pose)):
        stream = iter(poses)
        metrics[name] = measure(lambda: callback(next(stream)), n)
    metrics["working_memory_view"] = measure(runtime.memory.view, n)
    events = queue.Queue(maxsize=256)
    def queue_roundtrip():
        events.put_nowait(1)
        events.get_nowait()
        events.task_done()
    metrics["bounded_queue_roundtrip"] = measure(queue_roundtrip, n)
    with MemoryStore(":memory:") as store:
        store.ensure_user("u")
        for i in range(5):
            store.create_session("u", session_id=f"history-{i}")
        retrieval = RetrievalService(store, "u")
        metrics["memory_query_training_5_empty_sessions"] = measure(
            lambda: retrieval.query_training(limit=5), min(n, 1000))
    metrics["agent_abstain_scheduling"] = asyncio.run(agent_measure(min(n, 500)))
    try:
        from coach.arbiter import FeedbackArbiter, FeedbackKind
    except ImportError:
        metrics["feedback_arbitration"] = {"available": False, "reason": "not implemented in baseline"}
    else:
        arbiter = FeedbackArbiter()
        counter = iter(range(n+101))
        def arbitrate():
            lease = arbiter.admit(FeedbackKind.ANSWER, str(next(counter)), deadline=time.monotonic()+1).lease
            assert lease is not None
            arbiter.finish(lease)
        metrics["feedback_arbitration"] = measure(arbitrate, n)
    # Editable installation finders may fall back to the current checkout
    # when an optional module is absent from the requested baseline tree.
    if not (args.source_root / "coach" / "voice_state.py").is_file():
        metrics["voice_response_identity_cycle"] = {
            "available": False, "reason": "not implemented in baseline"}
    else:
        from coach.voice_state import ResponseWindow
        window = ResponseWindow()
        response_ids = iter([f"response-{i}" for i in range(n+101)])
        def voice_identity_cycle():
            response_id = next(response_ids)
            assert window.begin(response_id) == "started"
            assert window.accepts(response_id, audio=True)
            assert window.finish_audio(response_id)
            assert window.finish(response_id)
        metrics["voice_response_identity_cycle"] = measure(voice_identity_cycle, n)
    result = {
        "schema_version": "coach.benchmark.v1", "source_revision": args.source_revision,
        "source_modules": source_module_manifest(args.source_root),
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "machine": platform.machine(), "sqlite": sqlite3.sqlite_version},
        "method": "100 warmups, nearest-rank percentiles, prebuilt poses, perf_counter_ns",
        "limitations": ["synthetic local microbenchmark", "no camera/inference/network/LLM/TTS",
                        "SQLite in-memory fixture has five sessions and no reps",
                        "not a user experiment or production latency guarantee"],
        "metrics": metrics,
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text+"\n")
    print(text)


if __name__ == "__main__":
    main()
