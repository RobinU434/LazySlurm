"""Tests for widget helpers, models, and config persistence."""

from __future__ import annotations

import pytest

from slurmtop.models import JobDetail
from slurmtop.widgets.detail_view import parse_mem_bytes, sparkline
from slurmtop.widgets import job_table
from slurmtop import config as cfg


# ---------------------------------------------------------------------------
# parse_mem_bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "s,expected",
    [
        ("1024K", 1024 * 1024),
        ("512M", 512 * 1024**2),
        ("2.5G", 2.5 * 1024**3),
        ("1T", 1024**4),
        ("100", 100.0),
        ("N/A", None),
        ("", None),
        ("garbage", None),
    ],
)
def test_parse_mem_bytes(s, expected):
    assert parse_mem_bytes(s) == expected


# ---------------------------------------------------------------------------
# sparkline
# ---------------------------------------------------------------------------


def test_sparkline_empty():
    assert sparkline([]) == ""


def test_sparkline_all_zero():
    assert sparkline([0, 0, 0]) == "▁▁▁"


def test_sparkline_monotonic_uses_full_range():
    out = sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    assert len(out) == 8
    assert out[0] == "▁"   # smallest
    assert out[-1] == "█"  # max maps to full block


# ---------------------------------------------------------------------------
# job_table helpers
# ---------------------------------------------------------------------------


def test_truncate():
    assert job_table._truncate("short", 10) == "short"
    assert job_table._truncate("longname12345", 6) == "longn…"
    assert job_table._truncate("anything", 0) == "anything"  # 0 = no limit


def test_partition_style_deterministic():
    # Same partition always maps to the same color
    assert job_table._partition_style("gpu") == job_table._partition_style("gpu")
    assert job_table._partition_style("") == ""


def test_partition_style_custom_override():
    job_table.set_partition_colors({"gpu": "bright_red"})
    try:
        assert job_table._partition_style("gpu") == "bright_red"
    finally:
        job_table.set_partition_colors(None)


def test_styled_state_abbreviation():
    job_table.set_display_config(abbreviate=True)
    try:
        txt = job_table._styled_state("OUT_OF_MEMORY")
        assert txt.plain == "OOM"
    finally:
        job_table.set_display_config(abbreviate=False)
    # Without abbreviation the full state is shown
    assert job_table._styled_state("COMPLETED").plain == "COMPLETED"


# ---------------------------------------------------------------------------
# JobDetail.submit_line — prefers SubmitLine over Command
# ---------------------------------------------------------------------------


def test_submit_line_prefers_submitline():
    d = JobDetail(job_id="1", raw={"SubmitLine": "sbatch --array=1-4 j.sh", "Command": "/w/j.sh"})
    assert d.submit_line == "sbatch --array=1-4 j.sh"


def test_submit_line_falls_back_to_command():
    d = JobDetail(job_id="1", raw={"SubmitLine": "", "Command": "/w/j.sh"})
    assert d.submit_line == "/w/j.sh"


def test_submit_line_default():
    assert JobDetail(job_id="1", raw={}).submit_line == "N/A"


def test_jobdetail_scontrol_and_sacct_key_aliases():
    # scontrol-style keys
    d1 = JobDetail(job_id="1", raw={"NumCPUs": "8", "NumNodes": "2", "QOS": "hi"})
    assert d1.num_cpus == "8" and d1.num_nodes == "2" and d1.qos == "hi"
    # sacct-style keys
    d2 = JobDetail(job_id="1", raw={"NCPUS": "4", "NNodes": "1", "QoS": "lo"})
    assert d2.num_cpus == "4" and d2.num_nodes == "1" and d2.qos == "lo"


def test_jobdetail_gres_from_tres():
    d = JobDetail(job_id="1", raw={"ReqTRES": "cpu=4,mem=8G,gres/gpu=2"})
    assert "gres/gpu=2" in d.gres


# ---------------------------------------------------------------------------
# config TOML serialization + log cache round-trip
# ---------------------------------------------------------------------------


def test_toml_value_formatting():
    assert cfg._toml_value("hello") == '"hello"'
    assert cfg._toml_value(True) == "true"
    assert cfg._toml_value(False) == "false"
    assert cfg._toml_value(["a", "b"]) == '["a", "b"]'
    assert cfg._toml_value(5) == "5"


def test_log_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "LOG_CACHE_FILE", tmp_path / "log_cache.json")

    cfg.cache_job_paths(
        "777",
        stdout_path="/w/out",
        stderr_path="/w/err",
        command="/w/run.sh",
        work_dir="/w",
    )
    assert cfg.get_cached_log_paths("777") == ("/w/out", "/w/err")
    assert cfg.get_cached_command("777") == ("/w/run.sh", "/w")
    # Unknown job
    assert cfg.get_cached_log_paths("000") == (None, None)


def test_prune_log_cache_removes_old(tmp_path, monkeypatch):
    import json
    import time

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "LOG_CACHE_FILE", tmp_path / "log_cache.json")

    old_ts = time.time() - 100 * 86400
    new_ts = time.time()
    (tmp_path / "log_cache.json").write_text(json.dumps({
        "old": {"stdout": "/o", "ts": old_ts},
        "new": {"stdout": "/n", "ts": new_ts},
    }))
    cfg.prune_log_cache(max_age_days=30)
    remaining = cfg._load_log_cache()
    assert "new" in remaining and "old" not in remaining


def test_prune_log_cache_none_never_prunes(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "LOG_CACHE_FILE", tmp_path / "log_cache.json")
    (tmp_path / "log_cache.json").write_text(json.dumps({
        "x": {"stdout": "/x", "ts": 0},
    }))
    cfg.prune_log_cache(max_age_days=None)
    assert "x" in cfg._load_log_cache()
