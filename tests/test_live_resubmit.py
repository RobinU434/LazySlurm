"""Live integration test for the resubmission feature against a real Slurm cluster.

This test actually submits jobs, so it is guarded twice:

  1. The Slurm client binaries (sbatch/squeue/scontrol/scancel) must be present.
  2. The opt-in env var ``SLURMTOP_LIVE_TESTS=1`` must be set.

The second guard exists so a plain ``pytest`` run — e.g. on a login node or in
CI — never submits jobs by accident. To run it deliberately:

    SLURMTOP_LIVE_TESTS=1 uv run --with pytest python -m pytest tests/test_live_resubmit.py -v

All submitted jobs use ``--hold`` so they stay PENDING and consume **no**
compute, and every job created is cancelled in a ``finally`` block regardless
of outcome.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess

import pytest

from slurmtop import slurm
from slurmtop.models import Config

_BINS = ("sbatch", "squeue", "scontrol", "scancel")
_HAVE_SLURM = all(shutil.which(b) for b in _BINS)
_OPTED_IN = os.environ.get("SLURMTOP_LIVE_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not (_HAVE_SLURM and _OPTED_IN),
    reason="requires a Slurm cluster and SLURMTOP_LIVE_TESTS=1 (submits real jobs)",
)

_SUBMITTED_RE = re.compile(r"Submitted batch job (\d+)")


def _job_id(sbatch_output: str) -> str | None:
    m = _SUBMITTED_RE.search(sbatch_output)
    return m.group(1) if m else None


def _scancel(job_id: str) -> None:
    if job_id:
        subprocess.run(["scancel", job_id], capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _local_config():
    """Ensure slurm uses local (non-remote) transport for these tests."""
    slurm.set_config(Config(user=os.environ.get("USER", "")))
    yield
    slurm.set_config(Config())


def test_resubmit_roundtrip_live(tmp_path):
    """Submit a held job, read its detail, resubmit from that detail, verify."""
    script = tmp_path / "resubmit_test.sh"
    script.write_text("#!/bin/bash\n#SBATCH --job-name=slurmtop_test\ntrue\n")
    script.chmod(0o755)

    created: list[str] = []
    try:
        # 1. Submit the original job on hold (never runs, no compute used).
        ok, msg = asyncio.run(
            slurm.resubmit_job(f"sbatch --hold {script}", str(tmp_path))
        )
        if not ok:
            pytest.skip(f"cluster rejected submission (no default partition/account?): {msg}")
        original = _job_id(msg)
        assert original, f"could not parse job id from: {msg!r}"
        created.append(original)

        # 2. Read the job detail — this is what the 's' key path uses.
        detail = asyncio.run(slurm.get_job_detail(original))
        assert detail is not None, "get_job_detail returned None for a live job"

        # The submit line must be recoverable and NOT truncated to bare 'sbatch'.
        submit_line = detail.submit_line
        assert submit_line not in ("", "N/A", "sbatch"), (
            f"submit_line not usable for resubmit: {submit_line!r}"
        )
        assert "resubmit_test.sh" in submit_line or detail.work_dir, (
            f"submit_line lost the script reference: {submit_line!r}"
        )

        # 3. Resubmit exactly as the app does, from the recovered detail.
        ok2, msg2 = asyncio.run(
            slurm.resubmit_job(detail.submit_line, detail.work_dir)
        )
        assert ok2, f"resubmit failed: {msg2}"
        resubmitted = _job_id(msg2)
        assert resubmitted and resubmitted != original
        created.append(resubmitted)

        # 4. The resubmitted job should be visible in the user's queue.
        running = asyncio.run(slurm.get_running_jobs(Config(user=os.environ.get("USER", ""))))
        queued_ids = {j.job_id for j in running}
        assert resubmitted in queued_ids, (
            f"resubmitted job {resubmitted} not found in squeue {queued_ids}"
        )
    finally:
        for jid in created:
            _scancel(jid)


def test_resubmit_empty_is_rejected_live():
    """The empty-command guard should never reach sbatch."""
    ok, msg = asyncio.run(slurm.resubmit_job("", "/tmp"))
    assert not ok and "empty" in msg.lower()
