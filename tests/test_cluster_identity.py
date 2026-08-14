"""Per-cluster caches (#61).

Slurm numbers jobs from 1 on every cluster, so job 4815 exists on all of them
and means something different on each. Without a cluster level the caches hand
one cluster's log paths -- and, worse, its batch script -- to another's job of
the same number. These drive that collision directly.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from lazyslurm import config as cfg
from lazyslurm import slurm
from lazyslurm.models import Config


@pytest.fixture
def caches(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "LOG_CACHE_FILE", tmp_path / "log_cache.json")
    monkeypatch.setattr(cfg, "SCRIPT_CACHE_DIR", tmp_path / "scripts")
    return tmp_path


# --- naming a cluster ------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("galvani", "galvani"),
        ("national/gpu", "national-gpu"),      # never a path separator
        ("../evil", "evil"),                   # never an escape from the dir
        ("..", "local"),
        ("", "local"),
        ("   ", "local"),
    ],
)
def test_a_cluster_name_is_safe_to_use_as_a_directory(raw, expected):
    assert cfg.cluster_key(raw) == expected


def test_slurms_own_name_is_preferred(monkeypatch):
    async def _run_cmd(*args):
        return "ClusterName             = galvani\nSlurmUser = slurm\n", "", 0

    monkeypatch.setattr(slurm, "_run_cmd", _run_cmd)
    assert asyncio.run(slurm.get_cluster_name(Config(remote="me@login.hpc.edu"))) == "galvani"


def test_the_configured_name_wins_over_slurms(monkeypatch):
    """The escape hatch has to beat everything, or it is not one."""
    async def _run_cmd(*args):  # pragma: no cover - must not be reached
        raise AssertionError("scontrol should not be asked")

    monkeypatch.setattr(slurm, "_run_cmd", _run_cmd)
    assert asyncio.run(slurm.get_cluster_name(Config(cluster_name="hpc-fat"))) == "hpc-fat"


def test_the_remote_host_is_the_fallback(monkeypatch):
    async def _run_cmd(*args):
        return "", "scontrol: command not found", 127

    monkeypatch.setattr(slurm, "_run_cmd", _run_cmd)
    name = asyncio.run(slurm.get_cluster_name(Config(remote="Me@Login.HPC.edu:22")))
    assert name == "login.hpc.edu"


def test_local_mode_without_slurm_is_named_local(monkeypatch):
    async def _run_cmd(*args):
        return "", "", 127

    monkeypatch.setattr(slurm, "_run_cmd", _run_cmd)
    assert asyncio.run(slurm.get_cluster_name(Config())) == "local"


def test_the_name_is_resolved_once_per_session(monkeypatch):
    calls = []

    async def _run_cmd(*args):
        calls.append(args)
        return "ClusterName = galvani\n", "", 0

    monkeypatch.setattr(slurm, "_run_cmd", _run_cmd)
    asyncio.run(slurm.get_cluster_name(Config()))
    asyncio.run(slurm.get_cluster_name(Config()))
    assert len(calls) == 1


# --- the collision ---------------------------------------------------------


def test_two_clusters_keep_separate_log_paths(caches):
    """The bug: same job id, two clusters, one entry."""
    cfg.set_cluster("cluster-a")
    cfg.cache_job_paths("4815", stdout_path="/a/out", work_dir="/a")

    cfg.set_cluster("cluster-b")
    cfg.cache_job_paths("4815", stdout_path="/b/out", work_dir="/b")
    assert cfg.get_cached_log_paths("4815") == ("/b/out", None)

    cfg.set_cluster("cluster-a")
    assert cfg.get_cached_log_paths("4815") == ("/a/out", None)
    assert cfg.get_cached_command("4815") == (None, "/a")


def test_two_clusters_keep_separate_scripts(caches):
    """The sharper half: resubmit falls back to the archive, so a collision here
    submits another cluster's script."""
    cfg.set_cluster("cluster-a")
    cfg.cache_script("4815", "#!/bin/bash\necho A\n")

    cfg.set_cluster("cluster-b")
    assert cfg.get_cached_script("4815") is None      # not B's script

    cfg.cache_script("4815", "#!/bin/bash\necho B\n")
    assert cfg.get_cached_script("4815").read_text().endswith("echo B\n")

    cfg.set_cluster("cluster-a")
    assert cfg.get_cached_script("4815").read_text().endswith("echo A\n")


def test_a_job_unknown_on_this_cluster_is_a_miss_not_someone_elses_entry(caches):
    cfg.set_cluster("cluster-a")
    cfg.cache_job_paths("4815", stdout_path="/a/out")
    cfg.set_cluster("cluster-b")

    assert cfg.get_cached_log_paths("4815") == (None, None)
    assert cfg.get_cached_command("4815") == (None, None)


def test_writing_one_cluster_leaves_the_others_alone(caches):
    cfg.set_cluster("cluster-a")
    cfg.cache_job_paths("1", stdout_path="/a/1")
    cfg.set_cluster("cluster-b")
    cfg.cache_job_paths("2", stdout_path="/b/2")

    document = json.loads((caches / "log_cache.json").read_text())
    assert set(document["clusters"]) == {"cluster-a", "cluster-b"}
    assert document["clusters"]["cluster-a"]["1"]["stdout"] == "/a/1"
    assert document["clusters"]["cluster-b"]["2"]["stdout"] == "/b/2"


# --- migrating what is already on disk -------------------------------------


def test_an_old_flat_cache_is_read_as_this_clusters(caches):
    """Whoever wrote it had one cluster: the one they are on now."""
    (caches / "log_cache.json").write_text(json.dumps({
        "4815": {"stdout": "/w/out", "workdir": "/w", "ts": time.time()},
    }))
    cfg.set_cluster("galvani")

    assert cfg.get_cached_log_paths("4815") == ("/w/out", None)


def test_migrating_the_flat_cache_does_not_lose_it(caches):
    (caches / "log_cache.json").write_text(json.dumps({
        "4815": {"stdout": "/w/out", "ts": time.time()},
    }))
    cfg.set_cluster("galvani")
    cfg.cache_job_paths("4816", stdout_path="/w/out2")

    document = json.loads((caches / "log_cache.json").read_text())
    assert document["version"] == cfg.LOG_CACHE_VERSION
    assert set(document["clusters"]["galvani"]) == {"4815", "4816"}


def test_a_corrupt_cache_is_rebuilt_not_fatal(caches):
    (caches / "log_cache.json").write_text("{ this is not json")
    cfg.set_cluster("galvani")

    assert cfg.get_cached_log_paths("4815") == (None, None)
    cfg.cache_job_paths("4815", stdout_path="/w/out")
    assert cfg.get_cached_log_paths("4815") == ("/w/out", None)


def test_loose_scripts_move_under_the_current_cluster(caches):
    scripts = caches / "scripts"
    scripts.mkdir()
    (scripts / "4815.sh").write_text("#!/bin/bash\necho old\n")
    cfg.set_cluster("galvani")

    assert cfg.migrate_script_cache() == 1
    assert cfg.get_cached_script("4815").read_text().endswith("echo old\n")
    assert not (scripts / "4815.sh").exists()


def test_migrating_the_script_cache_is_idempotent(caches):
    scripts = caches / "scripts"
    scripts.mkdir()
    (scripts / "4815.sh").write_text("#!/bin/bash\n")
    cfg.set_cluster("galvani")

    assert cfg.migrate_script_cache() == 1
    assert cfg.migrate_script_cache() == 0


def test_a_loose_script_never_overwrites_one_already_placed(caches):
    scripts = caches / "scripts"
    (scripts / "galvani").mkdir(parents=True)
    (scripts / "galvani" / "4815.sh").write_text("kept\n")
    (scripts / "4815.sh").write_text("loose\n")
    cfg.set_cluster("galvani")

    cfg.migrate_script_cache()
    assert (scripts / "galvani" / "4815.sh").read_text() == "kept\n"


# --- pruning ---------------------------------------------------------------


def test_pruning_reaches_every_cluster_not_just_this_one(caches):
    """A cluster not connected to today would otherwise never age out."""
    old, new = time.time() - 100 * 86400, time.time()
    (caches / "log_cache.json").write_text(json.dumps({
        "version": cfg.LOG_CACHE_VERSION,
        "clusters": {
            "cluster-a": {"1": {"stdout": "/a", "ts": old}},
            "cluster-b": {"2": {"stdout": "/b", "ts": new}},
        },
    }))
    cfg.set_cluster("cluster-b")
    cfg.prune_log_cache(max_age_days=30)

    document = json.loads((caches / "log_cache.json").read_text())
    assert "cluster-a" not in document["clusters"]      # emptied, so dropped
    assert set(document["clusters"]["cluster-b"]) == {"2"}


def test_pruning_scripts_reaches_both_layouts(caches):
    import os

    scripts = caches / "scripts"
    (scripts / "galvani").mkdir(parents=True)
    old = time.time() - 100 * 86400
    for path in (scripts / "loose.sh", scripts / "galvani" / "4815.sh"):
        path.write_text("#!/bin/bash\n")
        os.utime(path, (old, old))
    fresh = scripts / "galvani" / "4816.sh"
    fresh.write_text("#!/bin/bash\n")

    cfg.set_cluster("galvani")
    cfg.prune_script_cache(max_age_days=30)

    assert not (scripts / "loose.sh").exists()
    assert not (scripts / "galvani" / "4815.sh").exists()
    assert fresh.exists()
