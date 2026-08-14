"""Tests for widget helpers, models, and config persistence."""

from __future__ import annotations

import pytest

from lazyslurm.models import JobDetail, RunningJob
from lazyslurm.widgets.detail_view import parse_mem_bytes, sparkline
from lazyslurm.widgets import job_table
from lazyslurm.widgets.partition_view import load_bar
from lazyslurm import __version__
from lazyslurm import config as cfg


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


def test_jobdetail_empty_field_falls_back_to_the_alternative_spelling():
    # sacct emits empty columns rather than omitting them, so a present-but-empty
    # key must not win over the spelling that actually holds the value.
    d = JobDetail(job_id="1", raw={
        "NodeList": "", "Nodelist": "node42",
        "NumCPUs": "", "NCPUS": "8",
        "TRES": "", "ReqTRES": "", "AllocTRES": "cpu=4",
        "JobState": "", "State": "CANCELLED",
    })
    assert d.node_list == "node42"
    assert d.num_cpus == "8"
    assert d.tres == "cpu=4"
    assert d.state == "CANCELLED"


def test_jobdetail_all_empty_gives_na():
    d = JobDetail(job_id="1", raw={"NodeList": "", "Nodelist": ""})
    assert d.node_list == "N/A"


def test_sparkline_fixed_scale_does_not_self_normalise():
    # A series that is already a fraction must plot against 0-1, so half the
    # cores busy does not look the same as all of them.
    assert sparkline([0.5, 0.5], scale_max=1.0) == sparkline([0.5], scale_max=1.0) * 2
    assert sparkline([0.5, 0.5], scale_max=1.0) != sparkline([1.0, 1.0], scale_max=1.0)


def test_jobdetail_gres_from_tres():
    d = JobDetail(job_id="1", raw={"ReqTRES": "cpu=4,mem=8G,gres/gpu=2"})
    assert "gres/gpu=2" in d.gres


# ---------------------------------------------------------------------------
# config TOML serialization + log cache round-trip
# ---------------------------------------------------------------------------


def test_save_preserves_comments_and_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")
    (tmp_path / "config.toml").write_text(
        "# LazySlurm configuration\n"
        "editor = \"nvim\"   # my editor\n"
        "# refresh = 5.0\n"
        "future_setting = 1\n"
    )

    cfg.set_partition_order(["gpu", "cpu"])

    text = (tmp_path / "config.toml").read_text()
    assert "# LazySlurm configuration" in text
    assert "# my editor" in text
    assert "# refresh = 5.0" in text
    assert "future_setting" in text
    reloaded = cfg.load()
    assert reloaded["partition_order"] == ["gpu", "cpu"]
    assert reloaded["editor"] == "nvim"


def test_save_writes_types_toml_can_parse(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")

    cfg.save({"editor": "vim", "no_gpu": True, "days": 5,
              "partition_order": ["a", "b"], "partition_colors": {"gpu": "green"}})

    assert cfg.load() == {
        "editor": "vim", "no_gpu": True, "days": 5,
        "partition_order": ["a", "b"], "partition_colors": {"gpu": "green"},
        # A file records the LazySlurm that wrote it, so it never looks like a
        # config that needs migrating (#67).
        "config_version": __version__,
    }


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


def test_cached_command_prefers_submit_line(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "LOG_CACHE_FILE", tmp_path / "log_cache.json")

    # Both fields stored; the full submit line carries the sbatch flags resubmit needs.
    cfg.cache_job_paths(
        "777",
        command="/w/run.sh",
        work_dir="/w",
        submit_line="sbatch --array=1-4 /w/run.sh",
    )
    assert cfg.get_cached_command("777") == ("sbatch --array=1-4 /w/run.sh", "/w")

    # With only Command cached, it is still returned.
    cfg.cache_job_paths("888", command="/w/other.sh")
    assert cfg.get_cached_command("888")[0] == "/w/other.sh"


# ---------------------------------------------------------------------------
# batch script cache
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "job_id,expected",
    [
        ("123", "123"),
        ("123_11", "123"),          # array task
        ("123_[1-40]", "123"),      # pending array
        ("123+0", "123"),           # heterogeneous job
        ("123.batch", "123"),       # step, as sacct reports it
        ("  123  ", "123"),
        ("", ""),
        ("abc", ""),
        ("; rm -rf /", ""),         # never let junk reach a cache filename
    ],
)
def test_base_job_id(job_id, expected):
    assert cfg.base_job_id(job_id) == expected


def _script_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "SCRIPT_CACHE_DIR", tmp_path / "scripts")


def test_script_cache_round_trip(tmp_path, monkeypatch):
    _script_cache(tmp_path, monkeypatch)

    assert cfg.get_cached_script("777") is None
    path = cfg.cache_script("777", "#!/bin/bash\necho hi\n")
    assert path is not None
    assert path == tmp_path / "scripts" / "777.sh"
    assert cfg.get_cached_script("777") == path
    assert path.read_text() == "#!/bin/bash\necho hi\n"


def test_script_cache_array_task_shares_base(tmp_path, monkeypatch):
    _script_cache(tmp_path, monkeypatch)

    cfg.cache_script("777", "#!/bin/bash\n")
    # All tasks of an array resolve to the one script written for the base id.
    assert cfg.get_cached_script("777_11") == tmp_path / "scripts" / "777.sh"
    assert cfg.get_cached_script("777_[1-40]") == tmp_path / "scripts" / "777.sh"


def test_cache_script_rejects_empty(tmp_path, monkeypatch):
    _script_cache(tmp_path, monkeypatch)

    assert cfg.cache_script("777", "") is None
    assert cfg.cache_script("777", "   \n") is None
    assert cfg.get_cached_script("777") is None


def test_cache_script_rejects_bad_job_id(tmp_path, monkeypatch):
    _script_cache(tmp_path, monkeypatch)

    assert cfg.cache_script("; rm -rf /", "#!/bin/bash\n") is None
    assert cfg.script_cache_path("abc") is None


def test_cache_script_permissions(tmp_path, monkeypatch):
    import stat

    _script_cache(tmp_path, monkeypatch)
    path = cfg.cache_script("777", "#!/bin/bash\nsecret_token=abc\n")
    # Scripts hold tokens and private paths — owner-only, and not executable.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_get_cached_script_ignores_empty_file(tmp_path, monkeypatch):
    _script_cache(tmp_path, monkeypatch)

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "777.sh").write_text("")
    # A truncated write must not open as a blank buffer.
    assert cfg.get_cached_script("777") is None


def test_prune_script_cache_removes_old(tmp_path, monkeypatch):
    import os
    import time

    _script_cache(tmp_path, monkeypatch)
    old = cfg.cache_script("111", "#!/bin/bash\n")
    new = cfg.cache_script("222", "#!/bin/bash\n")
    os.utime(old, (0, time.time() - 100 * 86400))

    cfg.prune_script_cache(max_age_days=30)
    assert not old.exists()
    assert new.exists()


def test_prune_script_cache_none_never_prunes(tmp_path, monkeypatch):
    import os

    _script_cache(tmp_path, monkeypatch)
    path = cfg.cache_script("111", "#!/bin/bash\n")
    os.utime(path, (0, 0))

    cfg.prune_script_cache(max_age_days=None)
    assert path.exists()


def test_set_script_cache_dir(tmp_path, monkeypatch):
    _script_cache(tmp_path, monkeypatch)

    cfg.set_script_cache_dir(tmp_path / "custom")
    assert cfg.script_cache_path("777") == tmp_path / "custom" / "777.sh"
    # Falsy input keeps the current directory (empty config value = use default).
    cfg.set_script_cache_dir("")
    assert cfg.script_cache_path("777") == tmp_path / "custom" / "777.sh"


# ---------------------------------------------------------------------------
# Job table lookup (used to prefill the job property editor)
# ---------------------------------------------------------------------------


def test_get_job_returns_dataclass_or_none():
    table = job_table.ActiveJobTable()
    jobs = [
        RunningJob("1", "a", "0:10", "gpu", "RUNNING"),
        RunningJob("2", "b", "0:00", "cpu", "PENDING", time_limit="1:00:00"),
    ]
    table._all_jobs = jobs  # bypass _rebuild, which needs a mounted DataTable
    assert table.get_job("2") is jobs[1]
    assert table.get_job("2").time_limit == "1:00:00"
    assert table.get_job("999") is None


# ---------------------------------------------------------------------------
# Partition load bar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fraction,expected_bar,expected_style",
    [
        (0.0, "░" * 10, "green"),
        (0.5, "█" * 5 + "░" * 5, "green"),
        (0.75, "█" * 8 + "░" * 2, "yellow"),
        (1.0, "█" * 10, "red"),
        (1.5, "█" * 10, "red"),   # clamped
        (-0.5, "░" * 10, "green"),  # clamped
    ],
)
def test_load_bar(fraction, expected_bar, expected_style):
    text = load_bar(fraction)
    assert text.plain.startswith(expected_bar)
    assert text.plain.endswith("%")
    assert text.spans[0].style == expected_style


# ---------------------------------------------------------------------------
# cache_max_age_days: TOML has no null, so 0/false must mean "never"
# ---------------------------------------------------------------------------


def test_cache_max_age_zero_means_never_not_delete_everything():
    from lazyslurm.__main__ import parse_cache_max_age

    assert parse_cache_max_age(0) is None
    assert parse_cache_max_age(False) is None
    assert parse_cache_max_age(None) is None
    assert parse_cache_max_age(-1) is None
    assert parse_cache_max_age(30) == 30
    assert parse_cache_max_age("7") == 7
    assert parse_cache_max_age("nonsense") == 30


def test_cache_job_paths_skips_the_rewrite_when_nothing_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "LOG_CACHE_FILE", tmp_path / "log_cache.json")

    writes = []
    real_save = cfg._save_log_cache
    monkeypatch.setattr(
        cfg, "_save_log_cache",
        lambda cache: (writes.append(1), real_save(cache))[1],
    )

    cfg.cache_job_paths("42", stdout_path="/w/out", work_dir="/w")
    assert len(writes) == 1
    # Same values again — the file must not be rewritten.
    cfg.cache_job_paths("42", stdout_path="/w/out", work_dir="/w")
    assert len(writes) == 1
    # A changed value still goes through.
    cfg.cache_job_paths("42", stdout_path="/w/other.out", work_dir="/w")
    assert len(writes) == 2
    assert cfg.get_cached_log_paths("42") == ("/w/other.out", None)


# ---------------------------------------------------------------------------
# unknown config keys are reported, never rejected
# ---------------------------------------------------------------------------


def test_unknown_config_keys_flags_typos_only():
    from lazyslurm.__main__ import unknown_config_keys

    saved = {
        "refresh": 2.0,                        # CLI-backed key
        "editor": "nvim",                      # file-only key
        "partition_colors": {"gpu": "green"},  # nested table, checked by name
        "reffresh": 2.0,                       # typo
        "editr": "nvim",                       # typo
    }
    assert unknown_config_keys(saved) == [
        "ignoring unknown setting: editr",
        "ignoring unknown setting: reffresh",
    ]


def test_every_documented_template_key_is_known():
    # The shipped template must not advertise a setting the loader ignores.
    import re
    from importlib.resources import files
    from lazyslurm.__main__ import KNOWN_CONFIG_KEYS

    text = files("lazyslurm").joinpath("templ", "config.toml").read_text()
    # A commented-out setting is "# key = value" with a single space after the
    # "#"; prose that happens to contain an "=" is indented further, so the
    # exact-one-space rule separates the two.
    documented = set()
    for line in text.splitlines():
        table = re.match(r"# \[([a-z_]+)\]$", line)
        if table:
            # Keys below a table header belong to it (gpu = "green"), and the
            # unknown-key check only looks at table names anyway.
            documented.add(table.group(1))
            break
        key = re.match(r"# ([a-z_]+) = ", line)
        if key:
            documented.add(key.group(1))
    assert documented, "no settings found in the template"
    assert documented <= set(KNOWN_CONFIG_KEYS), documented - set(KNOWN_CONFIG_KEYS)


def test_parse_interactive_shell_validates_and_warns():
    from lazyslurm.__main__ import parse_interactive_shell

    assert parse_interactive_shell("ssh") == ("ssh", "")
    assert parse_interactive_shell("srun") == ("srun", "")
    assert parse_interactive_shell(" SRUN ") == ("srun", "")
    value, warning = parse_interactive_shell("telnet")
    assert value == "ssh"                      # falls back, never fails
    assert "telnet" in warning and "ssh | srun" in warning


# ---------------------------------------------------------------------------
# _truncate measures terminal columns, not code points (#37)
# ---------------------------------------------------------------------------


def test_truncate_counts_columns_not_characters():
    from rich.cells import cell_len

    # Four CJK characters are eight columns wide, so a 5-column budget fits
    # two of them plus the ellipsis.
    assert job_table._truncate("実験実験", 5) == "実験…"
    assert cell_len(job_table._truncate("実験実験", 4)) <= 4


def test_truncate_never_overflows_its_column():
    from rich.cells import cell_len

    for text in ("実験-sweep", "🚀-run", "éxperiment", "plain-name", "実"):
        for width in range(1, 14):
            out = job_table._truncate(text, width)
            assert cell_len(out) <= max(width, cell_len(text)), (text, width, out)
            if cell_len(text) > width:
                assert cell_len(out) <= width, (text, width, out)


def test_truncate_leaves_text_that_fits_alone():
    # A name that fits must come back byte-identical, not padded.
    assert job_table._truncate("実験", 4) == "実験"
    assert job_table._truncate("train", 5) == "train"
    assert job_table._truncate("train", 0) == "train"   # 0 = no limit


def test_truncate_does_not_split_a_wide_character_in_half():
    from rich.cells import cell_len

    # An odd budget cannot fit half of a 2-column character; the result still
    # has to occupy exactly the space the column reserves.
    out = job_table._truncate("実験実験", 4)
    assert cell_len(out) == 4
    assert "\ufffd" not in out


# ---------------------------------------------------------------------------
# unknown free memory is not "full" (#48)
# ---------------------------------------------------------------------------


def test_unreported_free_memory_is_unknown_not_full():
    from lazyslurm import slurm

    # sinfo prints N/A for FreeMem on a node that is down or unreachable.
    node = slurm.parse_sinfo_nodes(
        "node07|down*|0/0/8/8|515000|N/A|N/A|gpu:a100:8|(null)|Not responding"
    )[0]
    assert node.free_mem_mb is None
    assert node.mem_used_mb is None
    assert node.mem_used is None


def test_reported_free_memory_still_computes():
    from lazyslurm import slurm

    node = slurm.parse_sinfo_nodes(
        "node01|mixed|4/4/0/8|515000|260000|3.5|gpu:a100:8|gpu:a100:2|"
    )[0]
    assert node.free_mem_mb == 260000
    assert node.mem_used_mb == 255000
    assert node.mem_used == pytest.approx(255000 / 515000)


def test_node_row_shows_a_dash_for_unknown_memory():
    from lazyslurm import slurm
    from lazyslurm.widgets.partition_view import NodeTable

    table = NodeTable()
    unknown = slurm.parse_sinfo_nodes(
        "node07|down*|0/0/8/8|515000|N/A|N/A|gpu:a100:8|(null)|down"
    )[0]
    known = slurm.parse_sinfo_nodes(
        "node02|allocated|8/0/0/8|515000|20000|7.9|gpu:a100:8|gpu:a100:8|"
    )[0]

    unknown_cell = table._row_for(unknown)[4].plain
    known_cell = table._row_for(known)[4].plain
    assert "—" in unknown_cell and "503" in unknown_cell   # capacity still shown
    assert "503/503" not in unknown_cell                   # ...but not as "full"
    assert known_cell.strip() == "483/503G"
