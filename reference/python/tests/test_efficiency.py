"""Tests for the job efficiency report (issue #9).

The maths is checked against numbers taken from a real cluster (the sacct rows
in SACCT_OUT below are a verbatim capture), plus the degenerate cases where
Slurm gives us nothing to divide by.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from lazyslurm import slurm
from lazyslurm.models import (
    Efficiency,
    JobStats,
    compute_efficiency,
    format_bytes,
    format_duration,
    parse_duration,
    parse_mem_bytes,
    parse_req_mem,
    sizing_hint,
)
from lazyslurm.widgets.detail_view import DetailView, efficiency_bar, efficiency_style


def _run(coro):
    return asyncio.run(coro)


def plain(text: str) -> str:
    """Strip Rich markup so assertions read like the rendered line."""
    return re.sub(r"\[/?[a-z ]*\]", "", text)


# Verbatim from `sacct -j 2736118_0 --format=JobID,TotalCPU,Elapsed,ReqMem,
# AllocTRES,ReqTRES,AllocCPUS,NNodes,NTasks,Timelimit,MaxRSS --parsable2`:
# the job row carries the request, the step rows carry the memory peaks.
SACCT_OUT = "\n".join([
    "2736118_0|00:43.900|00:17:02|64G|billing=11,cpu=8,gres/gpu:a100=1,gres/gpu=1,"
    "mem=64G,node=1|billing=3,cpu=8,gres/gpu=1,mem=64G,node=1|8|1||12:00:00|",
    "2736118_0.batch|00:43.900|00:17:02||cpu=8,gres/gpu:a100=1,gres/gpu=1,mem=64G,"
    "node=1||8|1|1||177548K",
    "2736118_0.extern|00:00:00|00:17:02||billing=11,cpu=8,gres/gpu:a100=1,"
    "gres/gpu=1,mem=64G,node=1||8|1|1||264K",
    "2736118_0.0|00:00:00|00:17:02||cpu=8,gres/gpu:a100=1,gres/gpu=1,mem=64G,"
    "node=1||8|1|1||2746264K",
    "2736118_0.1|00:00:00|00:00:00||cpu=8,gres/gpu:a100=1,gres/gpu=1,mem=64G,"
    "node=1||8|1|1||22744K",
])


# ---------------------------------------------------------------------------
# Duration and memory parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,seconds",
    [
        ("00:43.900", 43.9),          # sacct writes TotalCPU as MM:SS.mmm
        ("00:17:02", 1022),
        ("1-04:09:36", 101376),
        ("12:00:00", 43200),
        ("2-00:00:00", 172800),
        ("30", 30),
    ],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == pytest.approx(seconds)


@pytest.mark.parametrize(
    "text", ["", "   ", "N/A", "UNLIMITED", "Partition_Limit", "Unknown", "INVALID", "abc",
             "1:2:3:4"],
)
def test_parse_duration_rejects_non_durations(text):
    assert parse_duration(text) is None


@pytest.mark.parametrize(
    "raw,cpus,nodes,expected",
    [
        ("64G", 8, 1, 64 * 1024 ** 3),          # modern sacct: already a total
        ("64Gn", 8, 2, 128 * 1024 ** 3),        # per node
        ("4Gc", 8, 1, 32 * 1024 ** 3),          # per cpu
        ("4000M", 4, 1, 4000 * 1024 ** 2),
        ("0", 8, 1, None),
        ("", 8, 1, None),
        ("N/A", 8, 1, None),
    ],
)
def test_parse_req_mem(raw, cpus, nodes, expected):
    result = parse_req_mem(raw, cpus, nodes)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_parse_req_mem_per_node_without_a_node_count():
    """Never multiply by zero nodes and report a request of nothing."""
    assert parse_req_mem("8Gn", 0, 0) == pytest.approx(8 * 1024 ** 3)


# ---------------------------------------------------------------------------
# Efficiency maths
# ---------------------------------------------------------------------------


def _stats(**kwargs) -> JobStats:
    base = dict(
        job_id="1", total_cpu="00:43.900", elapsed="00:17:02", req_mem="64G",
        max_rss="2746264K", alloc_cpus=8, nnodes=1, time_limit="12:00:00",
        gpu_alloc="billing=3,cpu=8,gres/gpu=1,mem=64G,node=1",
    )
    base.update(kwargs)
    return JobStats(**base)


def test_efficiency_matches_hand_computation():
    """43.9 CPU-seconds over 8 cores x 1022 s, 2.62 GiB of 64 GiB, 1022 s of 43200."""
    eff = compute_efficiency(_stats())
    assert eff.cpu == pytest.approx(43.9 / (1022 * 8))       # 0.537%
    assert eff.memory == pytest.approx(2746264 * 1024 / (64 * 1024 ** 3))  # 4.09%
    assert eff.walltime == pytest.approx(1022 / 43200)       # 2.37%
    assert eff.cpu_used == pytest.approx(43.9 / 1022)
    assert eff.gpus == 1


def test_efficiency_of_a_well_sized_job():
    eff = compute_efficiency(_stats(
        total_cpu="5-08:00:00", elapsed="16:00:00", req_mem="40G", max_rss="34G",
        time_limit="20:00:00",
    ))
    assert eff.cpu == pytest.approx(1.0)
    assert eff.memory == pytest.approx(34 / 40)
    assert eff.walltime == pytest.approx(0.8)
    assert not eff.oom_risk


def test_memory_compares_per_node_on_a_multi_node_job():
    """A 4-node job asking 64G/node is not credited with 256G of headroom."""
    eff = compute_efficiency(_stats(req_mem="64Gn", nnodes=4, max_rss="32G"))
    assert eff.mem_request == pytest.approx(64 * 1024 ** 3)   # per node, not 256G
    assert eff.memory == pytest.approx(0.5)


def test_memory_at_the_request_is_flagged_as_oom_risk():
    eff = compute_efficiency(_stats(req_mem="4G", max_rss="4G"))
    assert eff.memory == pytest.approx(1.0)
    assert eff.oom_risk


@pytest.mark.parametrize(
    "kwargs",
    [
        {"elapsed": "00:00:00"},        # never ran: no denominator
        {"alloc_cpus": 0},              # sacct did not report cores
        {"total_cpu": "N/A"},
    ],
)
def test_cpu_efficiency_absent_without_a_denominator(kwargs):
    assert compute_efficiency(_stats(**kwargs)).cpu is None


@pytest.mark.parametrize(
    "kwargs",
    [{"req_mem": "N/A"}, {"req_mem": "0"}, {"max_rss": "N/A"}],
)
def test_memory_efficiency_absent_without_the_numbers(kwargs):
    assert compute_efficiency(_stats(**kwargs)).memory is None


@pytest.mark.parametrize("limit", ["UNLIMITED", "Partition_Limit", "N/A"])
def test_walltime_efficiency_absent_without_a_limit(limit):
    assert compute_efficiency(_stats(time_limit=limit)).walltime is None


def test_efficiency_of_an_empty_stats_object_has_nothing():
    eff = compute_efficiency(JobStats("1"))
    assert (eff.cpu, eff.memory, eff.walltime) == (None, None, None)
    assert not eff.has_any
    assert not eff.oom_risk


def test_gpu_count_from_tres():
    assert _stats().gpu_count == 1
    assert JobStats("1", gpu_tres="billing=8,cpu=32,gres/gpu=4,node=2").gpu_count == 4
    assert JobStats("1").gpu_count == 0


# ---------------------------------------------------------------------------
# Colours and gauge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ratio,style",
    [
        (0.90, "green"), (0.60, "green"),
        (0.59, "yellow"), (0.25, "yellow"),
        (0.24, "red"), (0.0, "red"),
        (1.0, "red bold"), (1.4, "red bold"),   # used its whole request
        (None, "dim"),
    ],
)
def test_efficiency_thresholds(ratio, style):
    assert efficiency_style(ratio) == style


def test_efficiency_bar_scales_and_clamps():
    assert efficiency_bar(0.0, 8) == "▁" * 8
    assert efficiency_bar(0.5, 8) == "▆" * 4 + "▁" * 4
    assert efficiency_bar(1.0, 8) == "▆" * 8
    assert efficiency_bar(2.0, 8) == "▆" * 8       # clamped, never overflows
    assert efficiency_bar(None, 8) == " " * 8


# ---------------------------------------------------------------------------
# Sizing hint
# ---------------------------------------------------------------------------


def test_sizing_hint_suggests_smaller_everything():
    hint = sizing_hint(compute_efficiency(_stats()))
    assert "--mem=4G" in hint            # 2.6 GiB used + a third
    assert "--cpus-per-task=1" in hint
    assert "--time=00:25:00" in hint     # 17 minutes + half


def test_sizing_hint_is_silent_for_a_well_sized_job():
    eff = compute_efficiency(_stats(
        total_cpu="5-08:00:00", elapsed="16:00:00", req_mem="40G", max_rss="34G",
        time_limit="20:00:00",
    ))
    assert sizing_hint(eff) == ""


def test_sizing_hint_never_suggests_more_cpus_than_were_asked_for():
    eff = compute_efficiency(_stats(alloc_cpus=1, total_cpu="00:00:01"))
    assert "--cpus-per-task" not in sizing_hint(eff)


def test_sizing_hint_without_data_says_nothing():
    assert sizing_hint(Efficiency()) == ""


# ---------------------------------------------------------------------------
# sacct row folding
# ---------------------------------------------------------------------------


def test_parse_sacct_stats_takes_the_peak_across_steps():
    fields = slurm.parse_sacct_stats(SACCT_OUT)
    assert fields is not None
    assert fields["ReqMem"] == "64G"           # only on the job row
    assert fields["Timelimit"] == "12:00:00"
    assert fields["AllocCPUS"] == "8"
    assert fields["MaxRSS"] == "2746264K"      # step .0, not .batch's 177548K


def test_parse_sacct_stats_without_step_rows():
    job_row = SACCT_OUT.splitlines()[0]
    fields = slurm.parse_sacct_stats(job_row)
    assert fields is not None and "MaxRSS" not in fields


def test_parse_sacct_stats_ignores_headers_and_junk():
    noisy = "JobID|TotalCPU|" + "|" * 9 + "\n\n" + SACCT_OUT + "\ngarbage\n"
    assert slurm.parse_sacct_stats(noisy)["MaxRSS"] == "2746264K"


def test_parse_sacct_stats_on_empty_output():
    assert slurm.parse_sacct_stats("") is None


def test_get_job_stats_fills_the_efficiency_denominators(monkeypatch):
    async def _fake(*args):
        if args[0] == "sstat":
            return "", "", 1            # finished job: no live stats
        return SACCT_OUT, "", 0

    monkeypatch.setattr(slurm, "_run_cmd", _fake)
    stats = _run(slurm.get_job_stats("2736118_0"))
    assert stats is not None
    assert (stats.alloc_cpus, stats.nnodes) == (8, 1)
    assert stats.time_limit == "12:00:00"
    assert stats.max_rss == "2746264K"     # taken from the steps, since sstat gave nothing
    eff = stats.efficiency
    assert eff.cpu == pytest.approx(43.9 / (1022 * 8))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_report_renders_every_row():
    text = plain(DetailView._efficiency_section(_stats()))
    assert "Efficiency" in text
    assert "CPU" in text and "8 cores" in text
    assert "2.6G / 64G" in text
    assert "17:02 / 12:00:00" in text
    assert "GPU" in text
    assert "over-requested" in text
    assert "next time try" in text


def test_report_never_rounds_a_fraction_of_a_percent_up():
    """0.54% must not read as 1%."""
    assert "<1%" in plain(DetailView._efficiency_section(_stats()))


def test_report_marks_memory_at_the_limit():
    text = plain(DetailView._efficiency_section(_stats(req_mem="4G", max_rss="4G")))
    assert "risks OOM" in text


def test_report_says_unavailable_when_slurm_has_nothing():
    text = plain(DetailView._efficiency_section(JobStats("1")))
    assert "unavailable" in text
    assert "next time try" not in text


def test_report_labels_the_request_per_node_on_multi_node_jobs():
    text = plain(DetailView._efficiency_section(_stats(req_mem="64Gn", nnodes=4, max_rss="32G")))
    assert "64G/node" in text


def test_report_omits_gpu_row_for_a_cpu_job():
    text = plain(DetailView._efficiency_section(_stats(gpu_alloc="N/A", gpu_tres="N/A")))
    assert "GPU" not in text


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,text",
    [(2746264 * 1024, "2.6G"), (64 * 1024 ** 3, "64G"), (177548 * 1024, "173M"),
     (512, "512B"), (1024 ** 4 * 2, "2.0T")],
)
def test_format_bytes(value, text):
    assert format_bytes(value) == text


@pytest.mark.parametrize(
    "seconds,text",
    [(1022, "17:02"), (43200, "12:00:00"), (43.9, "43s"), (0, "0s")],
)
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text
