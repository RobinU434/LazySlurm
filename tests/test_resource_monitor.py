"""The htop/nvtop-style resource monitor (#59).

Everything the node sends back is parsed here rather than on the node, so these
drive the parsers with the exact text the sampling script emits — including the
shapes that go wrong: a cgroup with no limit, an nvidia-smi that reports
[N/A] for power, an SSH fallback that sees the whole machine.
"""

from __future__ import annotations

import asyncio

import pytest

from lazyslurm import slurm
from lazyslurm.app import LazySlurmApp
from lazyslurm.__main__ import parse_resource_monitor
from lazyslurm.models import (
    Config,
    CoreSample,
    GpuReading,
    GpuSample,
    NodeSample,
    RESOURCE_MONITOR_MODES,
    RunningJob,
)
from lazyslurm.slurm import parse_cpu_list, parse_gpu_sample, parse_node_sample
from lazyslurm.widgets.detail_view import (
    DetailView,
    meter_bar,
    render_cpu_monitor,
    render_gpu_monitor,
)


# ---------------------------------------------------------------------------
# parse_cpu_list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("0-3", [0, 1, 2, 3]),
        ("0-3,8", [0, 1, 2, 3, 8]),
        ("5", [5]),
        ("2,4,6", [2, 4, 6]),
        (" 0-1 , 4 ", [0, 1, 4]),
        ("", []),
        ("nonsense", []),
    ],
)
def test_parse_cpu_list(spec, expected):
    assert parse_cpu_list(spec) == expected


# ---------------------------------------------------------------------------
# parse_node_sample
# ---------------------------------------------------------------------------


def _stat(cpu: int, user: float, idle: float) -> str:
    # user nice system idle iowait irq softirq steal
    return f"cpu{cpu} {user:.0f} 0 0 {idle:.0f} 0 0 0 0"


def _sample_text(
    first: list[str],
    second: list[str],
    affinity: str = "0-1",
    cgroup: str = "used 1073741824\nlimit 4294967296",
    meminfo: str = "MemTotal:       16000000 kB\nMemAvailable:    8000000 kB",
    load: str = "1.50 2.00 2.50 3/900 12345",
) -> str:
    return "\n".join([
        "##stat1", *first,
        "##affinity", f"Cpus_allowed_list:\t{affinity}",
        "##load", load,
        "##meminfo", meminfo,
        "##cgroup", cgroup,
        "##stat2", *second,
    ])


def test_per_core_utilisation_is_the_delta_between_the_two_snapshots():
    text = _sample_text(
        first=[_stat(0, 100, 100), _stat(1, 100, 100)],
        # cpu0 spent every new jiffy busy, cpu1 spent a quarter of them busy.
        second=[_stat(0, 200, 100), _stat(1, 125, 175)],
    )
    sample = parse_node_sample(text, node="node042", scope="job")

    assert [c.cpu for c in sample.cores] == [0, 1]
    assert sample.cores[0].busy == pytest.approx(1.0)
    assert sample.cores[1].busy == pytest.approx(0.25)
    assert sample.busy == pytest.approx(0.625)
    assert sample.node == "node042"
    assert sample.scope == "job"


def test_only_the_allocated_cores_are_reported():
    """The affinity mask is what makes this the job's cores, not the node's."""
    text = _sample_text(
        first=[_stat(c, 100, 100) for c in range(4)],
        second=[_stat(c, 200, 100) for c in range(4)],
        affinity="1,3",
    )
    assert [c.cpu for c in parse_node_sample(text).cores] == [1, 3]


def test_every_core_shows_when_the_affinity_section_is_missing():
    text = "\n".join([
        "##stat1", _stat(0, 100, 100), _stat(1, 100, 100),
        "##stat2", _stat(0, 150, 150), _stat(1, 150, 150),
    ])
    assert [c.cpu for c in parse_node_sample(text).cores] == [0, 1]


def test_the_aggregate_cpu_line_is_not_a_core():
    text = "\n".join([
        "##stat1", "cpu  400 0 0 400 0 0 0 0", _stat(0, 100, 100),
        "##stat2", "cpu  800 0 0 400 0 0 0 0", _stat(0, 200, 100),
    ])
    assert [c.cpu for c in parse_node_sample(text).cores] == [0]


def test_a_counter_that_did_not_move_reads_as_idle_not_as_saturated():
    """A zero delta says nothing; reporting it as 100% would be a false alarm."""
    text = _sample_text(
        first=[_stat(0, 100, 100)],
        second=[_stat(0, 100, 100)],
        affinity="0",
    )
    assert parse_node_sample(text).cores[0].busy == 0.0


def test_cgroup_memory_is_preferred_over_the_nodes():
    sample = parse_node_sample(_sample_text([_stat(0, 1, 1)], [_stat(0, 2, 2)]))
    assert sample.mem_used == 1024 ** 3
    assert sample.mem_total == 4 * 1024 ** 3
    assert sample.mem_scope == "job"
    assert sample.mem_ratio == pytest.approx(0.25)


def test_an_unlimited_cgroup_falls_back_to_node_memory():
    """A step cgroup with no limit of its own must not read as 100% used."""
    sample = parse_node_sample(_sample_text(
        [_stat(0, 1, 1)], [_stat(0, 2, 2)],
        cgroup="used 1073741824\nlimit 9223372036854771712",
    ))
    assert sample.mem_scope == "node"
    assert sample.mem_total == 16000000 * 1024
    assert sample.mem_used == (16000000 - 8000000) * 1024


def test_load_average_is_read_from_proc_loadavg():
    sample = parse_node_sample(_sample_text([_stat(0, 1, 1)], [_stat(0, 2, 2)]))
    assert sample.load == (1.50, 2.00, 2.50)


def test_missing_sections_do_not_shift_the_others():
    text = "\n".join([
        "##stat1", _stat(0, 100, 100),
        "##affinity", "Cpus_allowed_list:\t0",
        "##load",
        "##meminfo",
        "##cgroup",
        "##stat2", _stat(0, 200, 100),
    ])
    sample = parse_node_sample(text)
    assert sample.cores[0].busy == pytest.approx(1.0)
    assert sample.load is None
    assert sample.mem_total is None
    assert sample.mem_ratio is None


# ---------------------------------------------------------------------------
# parse_gpu_sample
# ---------------------------------------------------------------------------


_GPU_CSV = (
    "0, NVIDIA A100-SXM4-80GB, 97, 40960, 81920, 62, 310.55, 400.00\n"
    "1, NVIDIA A100-SXM4-80GB, 0, 4, 81920, 33, 58.10, 400.00\n"
)


def test_parse_gpu_sample():
    reading = parse_gpu_sample(_GPU_CSV, node="node042", scope="job")
    assert [g.index for g in reading.gpus] == [0, 1]

    first = reading.gpus[0]
    assert first.name == "NVIDIA A100-SXM4-80GB"
    assert first.util == pytest.approx(0.97)
    assert first.mem_used == 40960 * 1024 ** 2
    assert first.mem_total == 81920 * 1024 ** 2
    assert first.mem_ratio == pytest.approx(0.5)
    assert first.temperature == 62
    assert first.power == pytest.approx(310.55)
    assert first.power_limit == pytest.approx(400.0)
    assert reading.scope == "job"


def test_unsupported_gpu_fields_become_none_not_zero():
    """[N/A] power on a consumer card must not render as a 0W draw."""
    csv = "0, Tesla T4, 55, 1024, 15360, [N/A], [Not Supported], [N/A]"
    gpu = parse_gpu_sample(csv).gpus[0]
    assert gpu.util == pytest.approx(0.55)
    assert gpu.temperature is None
    assert gpu.power is None
    assert gpu.power_limit is None


def test_gpu_noise_lines_are_ignored():
    csv = "Failed to initialize NVML: Driver/library version mismatch\n"
    assert parse_gpu_sample(csv).gpus == []


# ---------------------------------------------------------------------------
# Fetching: srun first, SSH as the fallback
# ---------------------------------------------------------------------------


def _patch_calls(monkeypatch, srun_result, ssh_result):
    calls: list[str] = []

    async def _run_cmd(*args):
        calls.append(args[0])
        return srun_result

    async def _ssh_cmd(node, cmd):
        calls.append("ssh")
        return ssh_result

    monkeypatch.setattr(slurm, "_run_cmd", _run_cmd)
    monkeypatch.setattr(slurm, "_ssh_cmd", _ssh_cmd)
    return calls


def test_node_sample_prefers_srun_and_costs_one_round_trip(monkeypatch):
    text = _sample_text([_stat(0, 100, 100)], [_stat(0, 200, 100)], affinity="0")
    calls = _patch_calls(monkeypatch, (text, "", 0), ("", 1))

    sample = asyncio.run(slurm.get_node_sample("node042", "4815"))

    assert calls == ["srun"]  # per-core, memory and load all ride one call
    assert sample.scope == "job"
    assert len(sample.cores) == 1


def test_node_sample_falls_back_to_ssh_and_says_so(monkeypatch):
    text = _sample_text([_stat(0, 100, 100)], [_stat(0, 200, 100)], affinity="0")
    calls = _patch_calls(monkeypatch, ("", "srun: error", 1), (text, 0))

    sample = asyncio.run(slurm.get_node_sample("node042", "4815"))

    assert calls == ["srun", "ssh"]
    assert sample.scope == "node"
    assert "whole machine" in render_cpu_monitor(sample)


def test_node_sample_reports_an_error_when_both_paths_fail(monkeypatch):
    _patch_calls(monkeypatch, ("", "boom", 1), ("", 1))
    sample = asyncio.run(slurm.get_node_sample("node042", "4815"))
    assert "Could not sample" in sample.error
    assert render_cpu_monitor(sample) == sample.error


def test_node_sample_without_a_node():
    assert "No node assigned" in asyncio.run(slurm.get_node_sample("")).error


def test_gpu_sample_falls_back_when_srun_reports_no_gpus(monkeypatch):
    calls = _patch_calls(monkeypatch, ("no devices found\n", "", 0), (_GPU_CSV, 0))
    reading = asyncio.run(slurm.get_gpu_sample("node042", "4815"))
    assert calls == ["srun", "ssh"]
    assert len(reading.gpus) == 2
    assert reading.scope == "node"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _node_sample(cores: list[float], **kwargs) -> NodeSample:
    return NodeSample(
        node="node042",
        scope="job",
        cores=[CoreSample(cpu=i, busy=b) for i, b in enumerate(cores)],
        **kwargs,
    )


def test_meter_bar_fills_proportionally():
    assert meter_bar(1.0, 10).count("█") == 10
    assert meter_bar(0.0, 10).count("█") == 0
    assert meter_bar(0.5, 10).count("█") == 5


def test_a_barely_busy_core_still_shows_one_block():
    """An empty bar next to "1%" reads as a bug, not as a nearly idle core."""
    assert meter_bar(0.01, 20).count("█") == 1


def test_meter_bar_colours_by_load():
    assert "[green]" in meter_bar(0.2, 10)
    assert "[yellow]" in meter_bar(0.7, 10)
    assert "[red]" in meter_bar(0.95, 10)


def test_meter_mode_shows_one_bar_per_core_and_no_history():
    sample = _node_sample([1.0, 0.5], mem_used=2 * 1024 ** 3, mem_total=4 * 1024 ** 3,
                          mem_scope="job", load=(1.0, 2.0, 3.0))
    out = render_cpu_monitor(sample, {"cpu": [0.5] * 10}, width=100, graph=False)

    assert "node042" in out
    assert "2 cores, allocated to this job" in out
    assert out.count("▏") == 3          # two cores plus the memory meter
    assert "100%" in out and " 50%" in out
    assert "2.0G/4.0G" in out
    assert "1.00  2.00  3.00" in out
    assert "History" not in out         # meters only, that is the mode


def test_graph_mode_adds_a_band_per_core_and_the_aggregates():
    sample = _node_sample([0.9, 0.1], mem_used=1024 ** 3, mem_total=4 * 1024 ** 3)
    history = {
        "cores": {0: [0.1, 0.9], 1: [0.0, 0.1]},
        "cpu": [0.5, 0.5],
        "mem": [0.25, 0.25],
    }
    out = render_cpu_monitor(sample, history, width=120, graph=True)

    assert "History" in out
    assert "2 samples" in out
    # A band per core, plus the two aggregate bands underneath.
    assert out.count("·") > 0
    assert any(ch in out for ch in "▁▂▃▄▅▆▇█")


def test_a_narrow_terminal_still_renders_every_core():
    out = render_cpu_monitor(_node_sample([0.5] * 8), width=40, graph=False)
    for cpu in range(8):
        assert f"{cpu} ▏" in out or f"{cpu}[dim]▏" in out or f" {cpu} " in out


def test_gpu_meters_show_utilisation_memory_and_the_extras():
    reading = GpuReading(node="node042", scope="job", gpus=[
        GpuSample(index=0, name="A100", util=0.97, mem_used=40 * 1024 ** 3,
                  mem_total=80 * 1024 ** 3, temperature=62.0, power=310.0,
                  power_limit=400.0),
    ])
    out = render_gpu_monitor(reading, width=100, graph=False)

    assert "GPU 0" in out and "A100" in out
    assert "62°C" in out
    assert "310W/400W" in out
    assert "40G/80G" in out
    assert " 97%" in out


def test_gpu_extras_are_omitted_when_nvidia_smi_does_not_report_them():
    reading = GpuReading(gpus=[GpuSample(index=0, name="T4", util=0.5)])
    out = render_gpu_monitor(reading, width=80)
    assert "°C" not in out and "W" not in out
    assert "  ? " in out  # memory is unknown, and says so rather than showing 0%


def test_gpu_graph_mode_bands_each_device():
    reading = GpuReading(gpus=[
        GpuSample(index=0, name="A100", util=0.9, mem_used=1.0, mem_total=2.0),
        GpuSample(index=1, name="A100", util=0.1, mem_used=1.0, mem_total=2.0),
    ])
    history = {"gpu_util": {0: [0.9, 0.9], 1: [0.1]}, "gpu_mem": {0: [0.5, 0.5]}}
    out = render_gpu_monitor(reading, history, width=100, graph=True)
    assert out.count("util") == 2
    assert out.count("mem ") == 2


# ---------------------------------------------------------------------------
# Configuration and the runtime toggle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", RESOURCE_MONITOR_MODES)
def test_every_documented_mode_is_accepted(value):
    assert parse_resource_monitor(value) == (value, "")


def test_an_unknown_mode_warns_and_falls_back():
    value, warning = parse_resource_monitor("meters")
    assert value == "graph"
    assert "not one of" in warning


def test_the_default_config_starts_on_graph():
    assert Config().resource_monitor == "graph"


def _job(job_id: str) -> RunningJob:
    return RunningJob(
        job_id=job_id, name="train", elapsed="1:00", partition="gpu",
        state="RUNNING", time_limit="2:00:00", nodes="1", cpus="8",
        memory="4G", gres="gpu:1", work_dir="/w",
    )


def _app(monkeypatch, **config):
    async def _running(*a, **k):
        return [_job("4815")]

    async def _empty(*a, **k):
        return []

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(slurm, "get_running_jobs", _running)
    monkeypatch.setattr(slurm, "get_completed_jobs", _empty)
    monkeypatch.setattr(slurm, "get_partition_availability", _empty)
    monkeypatch.setattr(slurm, "get_job_detail", _none)
    monkeypatch.setattr(slurm, "get_job_stats", _none)
    return LazySlurmApp(config=Config(refresh=0, no_gpu=True, **config))


def test_shift_m_cycles_the_mode(monkeypatch):
    async def scenario():
        app = _app(monkeypatch, no_live=True, resource_monitor="text")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._monitor_mode == "text"
            await pilot.press("M")
            assert app._monitor_mode == "meter"
            await pilot.press("M")
            assert app._monitor_mode == "graph"
            await pilot.press("M")
            assert app._monitor_mode == "text"

    asyncio.run(scenario())


def test_an_unknown_configured_mode_does_not_break_the_cycle(monkeypatch):
    async def scenario():
        app = _app(monkeypatch, no_live=True, resource_monitor="nonsense")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._monitor_mode == "graph"

    asyncio.run(scenario())


def test_the_text_mode_still_runs_ps_and_nvidia_smi(monkeypatch):
    """Raw output stays reachable — it carries what the meters cannot show."""
    async def scenario():
        app = _app(monkeypatch, resource_monitor="text")
        called: list[str] = []

        async def _ps(node, user=""):
            called.append("ps")
            return "raw ps output"

        async def _sample(node, job_id=""):
            called.append("sample")
            return NodeSample()

        monkeypatch.setattr(slurm, "get_node_processes", _ps)
        monkeypatch.setattr(slurm, "get_node_sample", _sample)

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_job_id = "4815"
            app._selected_node = "node042"
            await app._load_cpu_monitor()
            assert called == ["ps"]

    asyncio.run(scenario())


def test_the_meter_modes_accumulate_history_per_job(monkeypatch):
    async def scenario():
        app = _app(monkeypatch, resource_monitor="graph")
        samples = iter([
            _node_sample([0.2, 0.4], mem_used=1.0, mem_total=4.0),
            _node_sample([0.6, 0.8], mem_used=2.0, mem_total=4.0),
        ])

        async def _sample(node, job_id=""):
            return next(samples)

        monkeypatch.setattr(slurm, "get_node_sample", _sample)

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_job_id = "4815"
            app._selected_node = "node042"
            await app._load_cpu_monitor()
            await app._load_cpu_monitor()

            history = app._monitor_history["4815"]
            assert history["cores"][0] == [0.2, 0.6]
            assert history["cores"][1] == [0.4, 0.8]
            assert history["cpu"] == [pytest.approx(0.3), pytest.approx(0.7)]
            assert history["mem"] == [0.25, 0.5]

    asyncio.run(scenario())


def test_history_is_capped_and_keeps_the_newest_samples(monkeypatch):
    app = _app(monkeypatch, resource_monitor="graph")
    for i in range(200):
        app._record_node_sample("4815", _node_sample([i / 200]))
    series = app._monitor_history["4815"]["cores"][0]
    assert len(series) == 60
    assert series[-1] == pytest.approx(199 / 200)


def test_history_is_dropped_when_the_job_ends(monkeypatch):
    """Otherwise a long session keeps a series per job ever highlighted."""
    async def scenario():
        app = _app(monkeypatch, no_live=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._record_node_sample("4815", _node_sample([0.5]))
            app._record_node_sample("9999", _node_sample([0.5]))
            await app._poll_jobs()  # only 4815 is still running
            assert "4815" in app._monitor_history
            assert "9999" not in app._monitor_history

    asyncio.run(scenario())


def test_the_monitor_width_survives_an_unlaid_out_widget():
    view = DetailView()
    assert view.monitor_width >= 40
