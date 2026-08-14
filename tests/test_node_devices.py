"""Folding a node row open into its GPUs (#66).

The free/busy half of this is parsed out of what sinfo already returns, so most
of these drive `gres_devices` with the exact strings a real cluster produces --
including the ones that go wrong: a drained node reporting IDX:N/A, a GRES with
no model, an index list whose commas sit inside the parentheses.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult

from lazyslurm import slurm
from lazyslurm.__main__ import parse_gpu_column, parse_node_expand
from lazyslurm.models import (
    Config,
    GPU_COLUMN_MODES,
    NODE_EXPAND_MODES,
    NodeDevice,
    NodeInfo,
    gres_devices,
    gres_indices,
)
from lazyslurm.slurm import (
    expand_node_spec,
    get_device_owners,
    parse_job_allocation,
)
from lazyslurm.widgets.partition_view import (
    NodeTable,
    device_key,
    device_marks,
    device_of,
    node_of,
    set_node_display,
)


@pytest.fixture(autouse=True)
def _default_display():
    set_node_display()
    yield
    set_node_display()


def _node(name="cn1", gres="gpu:a100:8(S:0-1)", used="gpu:a100:0(IDX:N/A)",
          state="mixed") -> NodeInfo:
    return NodeInfo(name=name, state=state, gres=gres, gres_used=used,
                    cpus_total=64, memory_mb=512000)


# --- which devices are taken -----------------------------------------------


def test_the_allocated_indices_are_read_not_just_the_count():
    # "7/8" cannot say which one is free. IDX can.
    devices = gres_devices("gpu:rtx2080ti:8(S:0-1)", "gpu:rtx2080ti:7(IDX:0,2-7)")

    assert len(devices) == 8
    assert [d.index for d in devices if not d.busy] == [1]
    assert all(d.model == "rtx2080ti" for d in devices)


def test_a_comma_inside_the_index_list_does_not_split_the_entry():
    # Splitting the GRES string on "," lands in the middle of (IDX:0-4,6-7).
    devices = gres_devices("gpu:a100:8(S:0-1)", "gpu:a100:7(IDX:0-4,6-7)")
    assert [d.index for d in devices if not d.busy] == [5]


def test_nothing_allocated_reads_as_nothing_not_as_device_zero():
    """`IDX:N/A` is what an idle node reports; index 0 is not busy."""
    devices = gres_devices("gpu:rtx2080ti:8(S:0-1)", "gpu:rtx2080ti:0(IDX:N/A)")
    assert [d.busy for d in devices] == [False] * 8


def test_a_gres_without_a_model_still_counts():
    devices = gres_devices("gpu:8", "gpu:2(IDX:3,5)")
    assert [d.index for d in devices if d.busy] == [3, 5]
    assert devices[0].model == ""


def test_other_gres_kinds_are_ignored():
    devices = gres_devices("gpu:a100:4,mps:100", "gpu:a100:1(IDX:2)")
    assert len(devices) == 4
    assert [d.index for d in devices if d.busy] == [2]


@pytest.mark.parametrize("gres", ["", "(null)", "N/A"])
def test_a_node_with_no_gpus_has_no_devices(gres):
    assert gres_devices(gres, gres) == []


def test_gres_indices_shapes():
    assert gres_indices("IDX:0-3") == [0, 1, 2, 3]
    assert gres_indices("IDX:0,2-3,7") == [0, 2, 3, 7]
    assert gres_indices("IDX:N/A") == []
    assert gres_indices("S:0-1") == []


# --- who holds them --------------------------------------------------------


_SCONTROL = """JobId=2743116 JobName=train
   NumNodes=1 NumCPUs=4 NumTasks=1 CPUs/Task=4
   JOB_GRES=gpu:a100:1
     Nodes=galvani-cn240 CPU_IDs=6-9 Mem=32000 GRES=gpu:a100:1(IDX:2)
   TresPerJob=gres/gpu:1
"""


def test_the_held_index_and_cpu_count_come_from_scontrol():
    cpus, indices = parse_job_allocation(_SCONTROL, "galvani-cn240")
    assert indices == [2]
    assert cpus == "4"          # CPU_IDs=6-9


def test_another_node_of_the_same_job_is_not_claimed():
    assert parse_job_allocation(_SCONTROL, "galvani-cn999") == ("", [])


def test_a_multi_node_job_is_matched_through_its_host_range():
    # Matching the spec literally would drop every owner of a job spanning
    # more than one node.
    text = ("     Nodes=cn[101-103] CPU_IDs=0-7 Mem=64000 "
            "GRES=gpu:a100:2(IDX:0-1)\n")
    cpus, indices = parse_job_allocation(text, "cn102")
    assert indices == [0, 1]
    assert cpus == "8"


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("cn1", ["cn1"]),
        ("cn1,cn2", ["cn1", "cn2"]),
        ("cn[1-3]", ["cn1", "cn2", "cn3"]),
        ("cn[01-03]", ["cn01", "cn02", "cn03"]),
        ("cn[1,5]", ["cn1", "cn5"]),
        ("cn[1-2],cn9", ["cn1", "cn2", "cn9"]),
    ],
)
def test_expand_node_spec(spec, expected):
    assert expand_node_spec(spec) == expected


def test_owners_are_one_scontrol_per_job(monkeypatch):
    calls: list[tuple] = []

    async def _run_cmd(*args):
        calls.append(args)
        return _SCONTROL.replace("2743116", args[-1]), "", 0

    monkeypatch.setattr(slurm, "_run_cmd", _run_cmd)
    owners = asyncio.run(get_device_owners(
        "galvani-cn240", [("100", "alice", "train"), ("101", "bob", "eval")],
    ))

    assert [c[0] for c in calls] == ["scontrol", "scontrol"]
    # Both jobs claim index 2 in this stub; the last one wins, which is all the
    # mapping promises -- one device, one owner.
    assert owners[2][1] in ("alice", "bob")


def test_a_failing_scontrol_skips_that_job(monkeypatch):
    async def _run_cmd(*args):
        return "", "Invalid job id specified", 1

    monkeypatch.setattr(slurm, "_run_cmd", _run_cmd)
    assert asyncio.run(get_device_owners("cn1", [("100", "alice", "train")])) == {}


# --- the table -------------------------------------------------------------


def test_row_keys_carry_their_parent_node():
    key = device_key("galvani-cn240", 3)
    assert node_of(key) == "galvani-cn240"
    assert device_of(key) == 3
    assert node_of("galvani-cn240") == "galvani-cn240"
    assert device_of("galvani-cn240") is None


def test_marks_are_green_while_something_is_free():
    devices = [NodeDevice(index=0, busy=True), NodeDevice(index=1, busy=False)]
    assert "green" in str(device_marks(devices).style)
    assert "red" in str(device_marks([NodeDevice(index=0, busy=True)]).style)


def test_a_drained_node_is_dim_however_idle_its_gpus_look():
    """Eight idle GPUs nothing can be scheduled onto are not eight free GPUs."""
    devices = [NodeDevice(index=i, busy=False) for i in range(8)]
    assert str(device_marks(devices, usable=False).style) == "dim"


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield NodeTable(id="nodes")


def _drive(scenario):
    async def run():
        app = _Harness()
        async with app.run_test(size=(140, 30)) as pilot:
            await scenario(app.query_one("#nodes", NodeTable), pilot)

    asyncio.run(run())


def test_expanding_adds_one_row_per_device():
    async def scenario(table, pilot):
        table.update_nodes([_node(used="gpu:a100:1(IDX:3)")])
        await pilot.pause()
        assert table.row_count == 1

        table.toggle_expand("cn1")
        await pilot.pause()
        assert table.row_count == 9          # the node plus its eight GPUs

        table.toggle_expand("cn1")
        await pilot.pause()
        assert table.row_count == 1

    _drive(scenario)


def test_a_node_without_gpus_does_not_expand():
    async def scenario(table, pilot):
        table.update_nodes([_node(gres="", used="")])
        await pilot.pause()
        assert table.toggle_expand("cn1") is False
        assert table.row_count == 1

    _drive(scenario)


def test_expansion_is_off_when_the_config_says_so():
    set_node_display(node_expand="off")

    async def scenario(table, pilot):
        table.update_nodes([_node()])
        await pilot.pause()
        assert table.toggle_expand("cn1") is False

    _drive(scenario)


def test_the_cursor_on_a_device_still_selects_its_node():
    """The jobs panel follows NodeSelected, and must not lose the node."""
    async def scenario(table, pilot):
        table.update_nodes([_node()])
        table.toggle_expand("cn1")
        await pilot.pause()
        table.move_cursor(row=table.get_row_index(device_key("cn1", 4)))
        await pilot.pause()
        assert table.get_selected_node() == "cn1"
        assert table.get_selected_device() == 4

    _drive(scenario)


def test_expansion_survives_a_refresh():
    async def scenario(table, pilot):
        table.update_nodes([_node()])
        table.toggle_expand("cn1")
        await pilot.pause()
        table.update_nodes([_node(used="gpu:a100:1(IDX:0)")])   # a poll
        await pilot.pause()
        assert table.row_count == 9

    _drive(scenario)


def test_a_node_that_leaves_the_partition_is_forgotten():
    async def scenario(table, pilot):
        table.update_nodes([_node("cn1"), _node("cn2")])
        table.toggle_expand("cn2")
        await pilot.pause()
        table.update_nodes([_node("cn1")])
        await pilot.pause()
        assert "cn2" not in table._expanded
        assert table.row_count == 1

    _drive(scenario)


def test_resolved_owners_survive_a_poll_but_busy_state_is_refetched():
    """sinfo stays the authority on what is taken; `g` only adds what it cannot say."""
    async def scenario(table, pilot):
        table.update_nodes([_node(used="gpu:a100:1(IDX:0)")])
        table.toggle_expand("cn1")
        await pilot.pause()

        devices = table.devices_of(table.get_node("cn1"))
        devices[0].job_id, devices[0].user = "4815", "alice"
        table.set_devices("cn1", devices)
        await pilot.pause()

        # A later poll: GPU 1 is taken too, and GPU 0's owner is still known.
        table.update_nodes([_node(used="gpu:a100:2(IDX:0-1)")])
        fresh = table.devices_of(table.get_node("cn1"))
        assert fresh[0].job_id == "4815"
        assert fresh[1].busy and fresh[1].job_id == ""

    _drive(scenario)


def test_the_gpus_column_switches_between_a_count_and_marks():
    async def scenario(table, pilot):
        table.update_nodes([_node(used="gpu:a100:1(IDX:0)")])
        await pilot.pause()
        assert "1/8" in str(table.get_row("cn1")[5])

        set_node_display(gpu_column="glyphs")
        table.update_nodes([_node(used="gpu:a100:1(IDX:0)")])
        await pilot.pause()
        assert str(table.get_row("cn1")[5]) == "▣▢▢▢▢▢▢▢"

    _drive(scenario)


def test_too_many_devices_fall_back_to_the_count():
    """Sixteen marks would push Reason off the screen."""
    set_node_display(gpu_column="glyphs")

    async def scenario(table, pilot):
        table.update_nodes([_node(gres="gpu:a100:16", used="gpu:a100:1(IDX:0)")])
        await pilot.pause()
        assert "1/16" in str(table.get_row("cn1")[5])

    _drive(scenario)


# --- config ----------------------------------------------------------------


@pytest.mark.parametrize("value", NODE_EXPAND_MODES)
def test_every_documented_expand_mode_is_accepted(value):
    assert parse_node_expand(value) == (value, "")


@pytest.mark.parametrize("value", GPU_COLUMN_MODES)
def test_every_documented_column_mode_is_accepted(value):
    assert parse_gpu_column(value) == (value, "")


def test_an_unknown_mode_warns_and_falls_back():
    value, warning = parse_node_expand("devices")
    assert value == "gpu"
    assert "not one of" in warning

    value, warning = parse_gpu_column("marks")
    assert value == "count"
    assert "not one of" in warning


def test_the_defaults_match_the_documented_ones():
    assert Config().node_expand == "gpu"
    assert Config().gpu_column == "count"


def test_a_device_row_shows_what_it_is_drawing():
    device = NodeDevice(index=0, model="a100", busy=True, util=0.9,
                        power=310.0, power_limit=400.0)
    cell = NodeTable._power_cell(device)
    assert "310" in str(cell) and "400W" in str(cell)


def test_the_model_holds_the_column_until_there_is_a_reading():
    cell = NodeTable._power_cell(NodeDevice(index=0, model="a100", busy=True))
    assert str(cell) == "a100"


def test_power_without_a_limit_still_renders():
    cell = NodeTable._power_cell(NodeDevice(index=0, busy=True, power=88.0))
    assert str(cell) == "88W"
