"""Tests for pure parsing/formatting logic in slurmtop.

These cover the code paths that do not require a live Slurm cluster:
CLI-output parsing, node-spec handling, and the resubmit argument builder.
Async functions are exercised by monkeypatching the transport layer
(`slurm._run_cmd`).
"""

from __future__ import annotations

import asyncio

import pytest

from slurmtop import slurm
from slurmtop.models import Config, RunningJob


# ---------------------------------------------------------------------------
# _first_node
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("node001", "node001"),
        ("node001,node002", "node001"),
        ("node[001-003]", "node001"),
        ("node[005,007]", "node005"),
        ("gpu[01-04],gpu[10]", "gpu01"),
        ("", ""),
    ],
)
def test_first_node(spec, expected):
    assert slurm._first_node(spec) == expected


# ---------------------------------------------------------------------------
# _parse_scontrol
# ---------------------------------------------------------------------------


def test_parse_scontrol_basic():
    out = (
        "JobId=123 JobName=train UserId=me(1000)\n"
        "   StdOut=/work/slurm-123.out StdErr=/work/slurm-123.err\n"
        "   WorkDir=/work Command=/work/run.sh\n"
    )
    parsed = slurm._parse_scontrol(out)
    assert parsed["JobId"] == "123"
    assert parsed["JobName"] == "train"
    assert parsed["StdOut"] == "/work/slurm-123.out"
    assert parsed["Command"] == "/work/run.sh"


def test_parse_scontrol_ignores_tokens_without_equals():
    parsed = slurm._parse_scontrol("plain text no equals here")
    assert parsed == {}


def test_parse_scontrol_captures_submitline_with_spaces():
    # Regression: SubmitLine holds a space-separated command that runs to the
    # end of the line. It must be captured whole, not truncated to "sbatch".
    out = (
        "JobId=123 JobName=train WorkDir=/work Command=/work/job.sh\n"
        "   SubmitLine=sbatch --array=1-4 --time=1:00:00 job.sh\n"
    )
    parsed = slurm._parse_scontrol(out)
    assert parsed["SubmitLine"] == "sbatch --array=1-4 --time=1:00:00 job.sh"
    assert parsed["Command"] == "/work/job.sh"
    assert parsed["JobId"] == "123"


def test_submit_line_from_scontrol_survives_into_resubmit(monkeypatch):
    # End-to-end (no cluster): scontrol detail -> submit_line -> resubmit args.
    from slurmtop.models import JobDetail

    raw = slurm._parse_scontrol(
        "JobId=9 WorkDir=/w Command=/w/j.sh SubmitLine=sbatch --array=1-4 j.sh"
    )
    detail = JobDetail(job_id="9", raw=raw, work_dir="/w", source="scontrol")
    assert detail.submit_line == "sbatch --array=1-4 j.sh"

    fake, calls = _capture_run_cmd()
    monkeypatch.setattr(slurm, "_run_cmd", fake)
    ok, _ = asyncio.run(slurm.resubmit_job(detail.submit_line, detail.work_dir))
    assert ok
    assert calls["args"] == ("sbatch", "--chdir", "/w", "--array=1-4", "j.sh")


# ---------------------------------------------------------------------------
# squeue / sacct parsing via monkeypatched transport
# ---------------------------------------------------------------------------


def _fake_run_cmd(stdout: str, rc: int = 0):
    async def _fake(*args):
        return stdout, "", rc
    return _fake


def test_get_running_jobs_parses_and_sorts(monkeypatch):
    rows = "\n".join([
        "101|jobA|1:00|gpu|RUNNING|2:00|1|4|8G|gpu:1|/work/a",
        "205|jobB|0:10|cpu|PENDING|1:00|1|2|4G|None|/work/b",
    ])
    monkeypatch.setattr(slurm, "_run_cmd", _fake_run_cmd(rows))
    jobs = asyncio.run(slurm.get_running_jobs(Config()))
    assert [j.job_id for j in jobs] == ["205", "101"]  # descending
    assert jobs[1].name == "jobA"
    assert jobs[1].gres == "gpu:1"


def test_get_running_jobs_empty(monkeypatch):
    monkeypatch.setattr(slurm, "_run_cmd", _fake_run_cmd("", rc=0))
    assert asyncio.run(slurm.get_running_jobs(Config())) == []


def test_get_completed_jobs_filters_substeps_and_running(monkeypatch):
    rows = "\n".join([
        "300|jobX|COMPLETED|0:0|s|e|1:00|gpu",
        "300.batch|batch|COMPLETED|0:0|s|e|1:00|gpu",  # sub-step, dropped
        "301|jobY|RUNNING|0:0|s|e|0:30|gpu",           # still running, dropped
        "302|jobZ|FAILED|1:0|s|e|0:05|cpu",
    ])
    monkeypatch.setattr(slurm, "_run_cmd", _fake_run_cmd(rows))
    jobs = asyncio.run(slurm.get_completed_jobs(Config()))
    assert [j.job_id for j in jobs] == ["302", "300"]
    assert jobs[1].state == "COMPLETED"


def test_get_completed_jobs_partition_filter(monkeypatch):
    rows = "\n".join([
        "300|jobX|COMPLETED|0:0|s|e|1:00|gpu",
        "302|jobZ|FAILED|1:0|s|e|0:05|cpu",
    ])
    monkeypatch.setattr(slurm, "_run_cmd", _fake_run_cmd(rows))
    jobs = asyncio.run(slurm.get_completed_jobs(Config(partition="cpu")))
    assert [j.job_id for j in jobs] == ["302"]


# ---------------------------------------------------------------------------
# resubmit_job argument construction
# ---------------------------------------------------------------------------


def _capture_run_cmd():
    calls = {}

    async def _fake(*args):
        calls["args"] = args
        return "Submitted batch job 999", "", 0

    return _fake, calls


def test_resubmit_full_submit_line(monkeypatch):
    fake, calls = _capture_run_cmd()
    monkeypatch.setattr(slurm, "_run_cmd", fake)
    ok, msg = asyncio.run(
        slurm.resubmit_job("sbatch --array=1-4 job.sh", "/work")
    )
    assert ok
    # sbatch stripped, --chdir injected, original flags preserved
    assert calls["args"] == ("sbatch", "--chdir", "/work", "--array=1-4", "job.sh")


def test_resubmit_bare_script(monkeypatch):
    fake, calls = _capture_run_cmd()
    monkeypatch.setattr(slurm, "_run_cmd", fake)
    ok, _ = asyncio.run(slurm.resubmit_job("/work/run.sh", "/work"))
    assert ok
    assert calls["args"] == ("sbatch", "--chdir", "/work", "/work/run.sh")


def test_resubmit_no_workdir(monkeypatch):
    fake, calls = _capture_run_cmd()
    monkeypatch.setattr(slurm, "_run_cmd", fake)
    ok, _ = asyncio.run(slurm.resubmit_job("sbatch job.sh", ""))
    assert ok
    assert calls["args"] == ("sbatch", "job.sh")


def test_resubmit_empty_command():
    ok, msg = asyncio.run(slurm.resubmit_job("", "/work"))
    assert not ok
    assert "empty" in msg.lower()


# ---------------------------------------------------------------------------
# cluster summary: partition availability (sinfo) + count formatting
# ---------------------------------------------------------------------------


def _rj(job_id, state):
    return RunningJob(
        job_id=job_id, name="j", elapsed="", partition="gpu", state=state,
    )


def test_get_partition_availability_filters_down_and_orders(monkeypatch):
    sinfo = "\n".join([
        "cpu*|up|10/5/0/15",
        "gpu|up|2/2/0/4",
        "maint|down|0/0/4/4",  # not "up" -> dropped
    ])
    monkeypatch.setattr(slurm, "_run_cmd", _fake_run_cmd(sinfo))
    info = asyncio.run(
        slurm.get_partition_availability(Config(partition_order=["gpu", "cpu"]))
    )
    assert info == ["gpu:2/2/0/4", "cpu:10/5/0/15"]  # ordered, maint excluded


def test_format_cluster_summary_counts_from_running_list():
    running = [_rj("1", "RUNNING"), _rj("2", "RUNNING"), _rj("3", "PENDING")]
    out = slurm.format_cluster_summary(running, ["gpu:2/2/0/4"], Config(user="bob"))
    assert "bob" in out
    assert "2[/] running" in out
    assert "1[/] pending" in out
    assert "gpu:2/2/0/4" in out
