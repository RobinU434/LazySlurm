"""Tests for pure parsing/formatting logic in lazyslurm.

These cover the code paths that do not require a live Slurm cluster:
CLI-output parsing, node-spec handling, and the resubmit argument builder.
Async functions are exercised by monkeypatching the transport layer
(`slurm._run_cmd`).
"""

from __future__ import annotations

import asyncio

import pytest

from lazyslurm import slurm
from lazyslurm.models import Config, RunningJob


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
    from lazyslurm.models import JobDetail

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
# _script_token_index
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tokens,expected",
    [
        (["job.sh"], 0),
        (["--array=1-4", "job.sh"], 1),
        (["--array", "1-4", "job.sh"], 2),        # separate-form option value skipped
        (["-J", "myname", "job.sh"], 2),
        (["--array=1-4", "-J", "n", "job.sh"], 3),
        (["--hold"], None),                        # flags only, no script
        ([], None),
    ],
)
def test_script_token_index(tokens, expected):
    assert slurm._script_token_index(tokens) == expected


# ---------------------------------------------------------------------------
# batch script fetch + archive
# ---------------------------------------------------------------------------


def test_get_batch_script_success(monkeypatch):
    calls = {}

    async def _fake(*args):
        calls["args"] = args
        return "#!/bin/bash\necho hi\n", "", 0

    monkeypatch.setattr(slurm, "_run_cmd", _fake)
    text = asyncio.run(slurm.get_batch_script("123"))
    assert text == "#!/bin/bash\necho hi\n"
    # The trailing "-" is what sends the script to stdout instead of a file in cwd.
    assert calls["args"] == ("scontrol", "write", "batch_script", "123", "-")


def test_get_batch_script_failure_rc_zero(monkeypatch):
    """scontrol exits 0 even when retrieval fails, so rc must not be trusted."""

    async def _fake(*args):
        return "", "job script retrieval failed: Invalid job id specified", 0

    monkeypatch.setattr(slurm, "_run_cmd", _fake)
    assert asyncio.run(slurm.get_batch_script("123")) is None


def test_archive_batch_script_writes_cache(tmp_path, monkeypatch):
    from lazyslurm import config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "SCRIPT_CACHE_DIR", tmp_path / "scripts")

    async def _fake(*args):
        return "#!/bin/bash\necho hi\n", "", 0

    monkeypatch.setattr(slurm, "_run_cmd", _fake)
    path = asyncio.run(slurm.archive_batch_script("123"))
    assert path == tmp_path / "scripts" / "123.sh"
    assert path.read_text() == "#!/bin/bash\necho hi\n"


def test_archive_batch_script_uses_cache(tmp_path, monkeypatch):
    from lazyslurm import config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "SCRIPT_CACHE_DIR", tmp_path / "scripts")
    cfg.cache_script("123", "#!/bin/bash\ncached\n")

    async def _fail(*args):
        raise AssertionError("should not hit Slurm when the script is cached")

    monkeypatch.setattr(slurm, "_run_cmd", _fail)
    # An array task resolves to the same cached script as its base id.
    path = asyncio.run(slurm.archive_batch_script("123_7"))
    assert path.read_text() == "#!/bin/bash\ncached\n"


def test_archive_batch_script_unavailable(tmp_path, monkeypatch):
    from lazyslurm import config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "SCRIPT_CACHE_DIR", tmp_path / "scripts")

    async def _fake(*args):
        return "", "job script retrieval failed: Invalid job id specified", 0

    monkeypatch.setattr(slurm, "_run_cmd", _fake)
    assert asyncio.run(slurm.archive_batch_script("123")) is None


# ---------------------------------------------------------------------------
# resubmit falls back to the archived script
# ---------------------------------------------------------------------------


def test_resubmit_falls_back_to_archive(tmp_path, monkeypatch):
    from lazyslurm import config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "SCRIPT_CACHE_DIR", tmp_path / "scripts")
    archived = cfg.cache_script("123", "#!/bin/bash\n")

    slurm.set_config(Config())  # local mode
    fake, calls = _capture_run_cmd()
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    ok, msg = asyncio.run(
        slurm.resubmit_job("sbatch --array=1-4 /gone/job.sh", "/work", "123")
    )
    assert ok
    # The missing script token is replaced; flags and --chdir are untouched.
    assert calls["args"] == (
        "sbatch", "--chdir", "/work", "--array=1-4", str(archived),
    )
    assert "archived copy" in msg


def test_resubmit_prefers_existing_original(tmp_path, monkeypatch):
    from lazyslurm import config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "SCRIPT_CACHE_DIR", tmp_path / "scripts")
    cfg.cache_script("123", "#!/bin/bash\n")

    original = tmp_path / "job.sh"
    original.write_text("#!/bin/bash\n")

    slurm.set_config(Config())
    fake, calls = _capture_run_cmd()
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    ok, msg = asyncio.run(slurm.resubmit_job(f"sbatch {original}", "/work", "123"))
    assert ok
    # Original still there, so the archive is ignored.
    assert calls["args"] == ("sbatch", "--chdir", "/work", str(original))
    assert "archived" not in msg


def test_resubmit_missing_script_no_archive(tmp_path, monkeypatch):
    from lazyslurm import config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "SCRIPT_CACHE_DIR", tmp_path / "scripts")

    slurm.set_config(Config())
    fake, _calls = _capture_run_cmd()
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    ok, msg = asyncio.run(slurm.resubmit_job("sbatch /gone/job.sh", "/work", "123"))
    assert not ok
    assert "no archived copy" in msg


def test_resubmit_without_job_id_skips_fallback(monkeypatch):
    """Existing callers that pass no job_id must not gain a file-existence check."""
    slurm.set_config(Config())
    fake, calls = _capture_run_cmd()
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    ok, _ = asyncio.run(slurm.resubmit_job("sbatch /gone/job.sh", "/work"))
    assert ok
    assert calls["args"] == ("sbatch", "--chdir", "/work", "/gone/job.sh")


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


# ---------------------------------------------------------------------------
# scontrol update – editing pending job properties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("40G", "40960"),
        ("4000M", "4000"),
        ("512", "512"),
        ("40Gn", "40960"),   # squeue's per-node marker
        ("2000Mc", "2000"),  # squeue's per-cpu marker
        ("1T", "1048576"),
        ("", ""),
        ("garbage", "garbage"),  # passed through for Slurm to reject
    ],
)
def test_normalize_memory(value, expected):
    assert slurm.normalize_memory(value) == expected


def test_build_update_args_maps_and_skips_blanks():
    args = slurm.build_update_args({
        "time_limit": "2-00:00:00",
        "partition": "gpu",
        "nodes": "",        # blank -> unchanged
        "cpus": "8",
        "memory": "40G",
    })
    assert args == [
        "TimeLimit=2-00:00:00",
        "Partition=gpu",
        "NumCPUs=8",
        "MinMemoryNode=40960",
    ]


def test_update_job_builds_scontrol_command(monkeypatch):
    fake, calls = _capture_run_cmd()
    monkeypatch.setattr(slurm, "_run_cmd", fake)
    ok, msg = asyncio.run(slurm.update_job("1234", {"time_limit": "4:00:00"}))
    assert ok
    assert calls["args"] == ("scontrol", "update", "jobid=1234", "TimeLimit=4:00:00")
    assert "1234" in msg


def test_update_job_no_changes_is_a_noop(monkeypatch):
    fake, calls = _capture_run_cmd()
    monkeypatch.setattr(slurm, "_run_cmd", fake)
    ok, msg = asyncio.run(slurm.update_job("1234", {"partition": "   "}))
    assert not ok
    assert "args" not in calls  # scontrol never invoked
    assert "nothing to update" in msg


def test_update_job_reports_failure(monkeypatch):
    async def _fail(*args):
        return "", "Invalid partition name specified", 1

    monkeypatch.setattr(slurm, "_run_cmd", _fail)
    ok, msg = asyncio.run(slurm.update_job("1234", {"partition": "nope"}))
    assert not ok
    assert "Invalid partition name" in msg


# ---------------------------------------------------------------------------
# Partitions (sinfo) and per-partition job lists
# ---------------------------------------------------------------------------

# Real-world shape: --summarize still emits one row per node *configuration*,
# so a partition with mixed hardware appears more than once.
_SINFO_OUT = "\n".join([
    "a100*|up|19/1/1/21|778/502/64/1344|3-00:00:00|gpu:a100:8",
    "a100|up|3/0/0/3|366/402/0/768|3-00:00:00|gpu:a100:9",
    "cpu|up|2/0/0/2|13/51/0/64|30-00:00:00|(null)",
    "maint|down|0/0/4/4|0/0/256/256|1:00:00|(null)",
])


def test_parse_sinfo_aggregates_rows_per_partition():
    parts = {p.name: p for p in slurm.parse_sinfo(_SINFO_OUT)}
    assert set(parts) == {"a100", "cpu", "maint"}  # trailing "*" stripped
    a100 = parts["a100"]
    assert a100.nodes_aiot == "22/1/1/24"
    assert a100.cpus_aiot == "1144/904/64/2112"
    assert a100.time_limit == "3-00:00:00"
    assert a100.gres == "gpu:a100:8,gpu:a100:9"  # both configs listed
    assert parts["cpu"].gres == ""  # "(null)" normalized away
    assert parts["maint"].avail == "down"


def test_parse_sinfo_tolerates_short_rows():
    # The cluster bar's older 3-field format must still parse.
    parts = slurm.parse_sinfo("gpu|up|10/5/0/15")
    assert len(parts) == 1
    assert parts[0].nodes_aiot == "10/5/0/15"
    assert parts[0].cpus_aiot == "0/0/0/0"


def test_partition_load_excludes_drained_cpus():
    part = slurm.parse_sinfo("gpu|up|1/0/1/2|100/100/800/1000")[0]
    # 100 allocated of 200 usable — the 800 "other" CPUs are not counted
    assert part.load == pytest.approx(0.5)


def test_partition_load_zero_when_nothing_usable():
    assert slurm.parse_sinfo("gpu|up|0/0/2/2|0/0/128/128")[0].load == 0.0


def test_order_partitions_honors_config_then_appends_rest():
    parts = slurm.parse_sinfo(_SINFO_OUT)
    ordered = slurm.order_partitions(parts, Config(partition_order=["cpu", "a100"]))
    assert [p.name for p in ordered] == ["cpu", "a100", "maint"]


def test_get_partitions_fills_job_counts(monkeypatch):
    async def _fake(*args):
        if args[0] == "sinfo":
            return _SINFO_OUT, "", 0
        return "a100|RUNNING\na100|PENDING\ncpu|RUNNING\n", "", 0

    monkeypatch.setattr(slurm, "_run_cmd", _fake)
    parts = {p.name: p for p in asyncio.run(slurm.get_partitions(Config()))}
    assert (parts["a100"].running, parts["a100"].pending) == (1, 1)
    assert (parts["cpu"].running, parts["cpu"].pending) == (1, 0)
    assert (parts["maint"].running, parts["maint"].pending) == (0, 0)


def test_get_partition_availability_keeps_only_up_partitions(monkeypatch):
    monkeypatch.setattr(slurm, "_run_cmd", _fake_run_cmd(_SINFO_OUT))
    info = asyncio.run(slurm.get_partition_availability(Config()))
    assert info == ["a100:22/1/1/24", "cpu:2/0/0/2"]  # aggregated, maint dropped


def test_parse_partition_job_counts_multi_partition_pending():
    counts = slurm.parse_partition_job_counts("\n".join([
        "gpu|RUNNING",
        "gpu|RUNNING",
        "gpu,gpu-long|PENDING",   # counts towards both
        "cpu|COMPLETING",         # neither running nor pending
    ]))
    assert counts == {"gpu": (2, 1), "gpu-long": (0, 1), "cpu": (0, 0)}


def test_parse_partition_jobs():
    jobs = slurm.parse_partition_jobs(
        "2735316|rvy895|train|RUNNING|14:28:36|2-06:00:00|1|8|gres/gpu:1|galvani-cn059\n"
        "2735270|pba175|vsv100|PENDING|0:00|3-00:00:00|1|8|N/A|(Dependency)\n"
    )
    assert [j.job_id for j in jobs] == ["2735316", "2735270"]
    assert jobs[0].user == "rvy895"
    assert jobs[0].gres == "gres/gpu:1"
    assert jobs[1].gres == ""  # "N/A" normalized away
    assert jobs[1].nodelist == "(Dependency)"  # pending reason


def test_get_partition_jobs_sorts_running_first_then_newest(monkeypatch):
    rows = "\n".join([
        "100|a|j1|PENDING|0:00|1:00|1|1|N/A|(Priority)",
        "200|b|j2|RUNNING|1:00|1:00|1|1|N/A|node1",
        "300|c|j3|PENDING|0:00|1:00|1|1|N/A|(Priority)",
        "150|d|j4|RUNNING|1:00|1:00|1|1|N/A|node2",
    ])
    monkeypatch.setattr(slurm, "_run_cmd", _fake_run_cmd(rows))
    jobs = asyncio.run(slurm.get_partition_jobs("gpu", Config()))
    assert [j.job_id for j in jobs] == ["200", "150", "300", "100"]


def test_get_partition_jobs_without_partition_skips_squeue(monkeypatch):
    fake, calls = _capture_run_cmd()
    monkeypatch.setattr(slurm, "_run_cmd", fake)
    assert asyncio.run(slurm.get_partition_jobs("", Config())) == []
    assert "args" not in calls


# ---------------------------------------------------------------------------
# Log tailing — must cost the size of the tail, not the size of the log
# ---------------------------------------------------------------------------


def test_tail_file_returns_the_last_lines(tmp_path):
    log = tmp_path / "job.out"
    log.write_text("".join(f"line {i}\n" for i in range(1000)))
    out = slurm.tail_file(str(log), tail_lines=3)
    assert out == "line 997\nline 998\nline 999\n"


def test_tail_file_shorter_than_requested(tmp_path):
    log = tmp_path / "job.out"
    log.write_text("a\nb\n")
    assert slurm.tail_file(str(log), tail_lines=500) == "a\nb\n"


def test_tail_file_empty(tmp_path):
    log = tmp_path / "job.out"
    log.write_text("")
    assert slurm.tail_file(str(log), tail_lines=500) == ""


def test_tail_file_without_trailing_newline(tmp_path):
    log = tmp_path / "job.out"
    log.write_text("first\nlast line, no newline")
    assert slurm.tail_file(str(log), tail_lines=2) == "first\nlast line, no newline"


def test_tail_file_never_splits_a_line_mid_way(tmp_path):
    """Reading backwards in blocks must not emit a half line at the top."""
    log = tmp_path / "job.out"
    # Lines much longer than one 64 KiB read block.
    log.write_text("".join(f"{i}:{'x' * 100_000}\n" for i in range(5)))
    out = slurm.tail_file(str(log), tail_lines=2)
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("3:") and lines[1].startswith("4:")
    assert all(len(line) == 100_002 for line in lines)  # whole lines


def test_tail_file_caps_a_single_enormous_line(tmp_path, monkeypatch):
    """A log that is one giant line (progress bars) must still read fast."""
    monkeypatch.setattr(slurm, "_TAIL_MAX_BYTES", 128 * 1024)
    log = tmp_path / "job.out"
    log.write_text("x" * (2 * 1024 * 1024))  # 2 MB, no newline at all
    out = slurm.tail_file(str(log), tail_lines=500)
    assert "truncated" in out
    assert len(out) < 200 * 1024  # nowhere near the whole file


def test_tail_file_reads_only_the_end(tmp_path):
    """Guard against the O(filesize) regression this replaced."""
    log = tmp_path / "job.out"
    log.write_text("".join(f"line {i}\n" for i in range(200_000)))  # ~2.4 MB

    read_bytes = 0
    real_open = open

    class _CountingFile:
        def __init__(self, f):
            self._f = f

        def read(self, n=-1):
            nonlocal read_bytes
            data = self._f.read(n)
            read_bytes += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self._f, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._f.__exit__(*exc)

    import builtins
    builtins.open = lambda *a, **k: _CountingFile(real_open(*a, **k))
    try:
        out = slurm.tail_file(str(log), tail_lines=10)
    finally:
        builtins.open = real_open

    assert out.splitlines()[-1] == "line 199999"
    assert read_bytes < 200 * 1024  # a couple of blocks, not the 2.4 MB file


def test_read_log_file_reports_unreadable_files(tmp_path, monkeypatch):
    monkeypatch.setattr(slurm, "_config", Config())
    log = tmp_path / "job.out"
    log.write_text("data\n")
    log.chmod(0o000)
    try:
        out = asyncio.run(slurm.read_log_file(str(log)))
    finally:
        log.chmod(0o644)
    assert "could not read" in out.lower()
