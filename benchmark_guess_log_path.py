"""Benchmark the _guess_log_path candidate list before/after deduplication.

Run with `python benchmark_guess_log_path.py` from the repo root.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from lazyslurm import slurm


def count_candidates(work_dir: str, job_id: str, suffix: str, job_name: str) -> int:
    """Return the number of stat probes _guess_log_path would issue."""
    calls = 0

    async def _count(path: str) -> bool:
        nonlocal calls
        calls += 1
        return False

    original = slurm._file_exists
    slurm._file_exists = _count
    try:
        asyncio.run(slurm._guess_log_path(work_dir, job_id, suffix, job_name))
    finally:
        slurm._file_exists = original
    return calls


def benchmark_candidate_generation(runs: int = 1000) -> None:
    times: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        n = count_candidates("/work", "12345", "out", "train")
        t1 = time.perf_counter()
        times.append(t1 - t0)

    print(f"Candidates per call (suffix='out', job_name='train'): {n}")
    print(f"Generation time per call: {statistics.mean(times)*1e6:.2f} µs")
    print(f"Best: {min(times)*1e6:.2f} µs  Worst: {max(times)*1e6:.2f} µs")


if __name__ == "__main__":
    benchmark_candidate_generation()
