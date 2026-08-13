"""The cursor must survive a refresh in every table that is rebuilt (#43).

These tables are refreshed by clearing and re-adding their rows, and clear()
resets the cursor to row 0. On the partition and node screens that happens on
a timer, so without this the pane cannot be browsed at all: every scroll is
undone a few seconds later.
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult

from lazyslurm.models import NodeInfo, PartitionInfo, PartitionJob, UsageRow
from lazyslurm.widgets.partition_view import NodeTable, PartitionJobTable, PartitionTable
from lazyslurm.widgets.usage_view import UsageTable


def _jobs(ids) -> list[PartitionJob]:
    return [
        PartitionJob(
            job_id=str(i), user="bob", name=f"job{i}", state="RUNNING",
            elapsed="1:00", time_limit="2:00:00", nodes="1", cpus="8",
            gres="gpu:1", nodelist="node01",
        )
        for i in ids
    ]


def _partitions(names) -> list[PartitionInfo]:
    return [PartitionInfo(name=n, nodes_total=10, cpus_total=100) for n in names]


def _nodes(names) -> list[NodeInfo]:
    return [NodeInfo(name=n, state="idle", cpus_total=8, memory_mb=1024) for n in names]


def _usage(users) -> list[UsageRow]:
    return [UsageRow(user=u, account="acct", hours=float(10 * (i + 1)))
            for i, u in enumerate(users)]


def cursor_key(table) -> str | None:
    """The row key under the cursor, via the plain DataTable API.

    Deliberately not `KeyedTable.selected_key`: these tests must fail against
    the old code because the cursor moved, not because a method is missing.
    """
    if table.row_count == 0:
        return None
    row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
    return str(row_key.value)


class _Harness(App):
    def __init__(self, table) -> None:
        super().__init__()
        self.table = table

    def compose(self) -> ComposeResult:
        yield self.table


def _run(table, fill, row, refill):
    """Fill, park the cursor on `row`, refill, and report the cursor row."""
    result = {}

    async def scenario():
        app = _Harness(table)
        async with app.run_test(size=(120, 30)) as pilot:
            fill()
            await pilot.pause()
            table.move_cursor(row=row)
            await pilot.pause()
            result["before"] = cursor_key(table)
            refill()
            await pilot.pause()
            result["after"] = cursor_key(table)
            result["row"] = table.cursor_coordinate.row

    asyncio.run(scenario())
    return result


def test_partition_job_list_keeps_the_cursor_across_a_refresh():
    table = PartitionJobTable(user="bob")
    jobs = _jobs(range(1, 21))
    out = _run(table, lambda: table.update_jobs(jobs), 12,
               lambda: table.update_jobs(jobs))
    assert out["before"] == "13"
    assert out["after"] == "13", "the job list jumped back to the top"
    assert out["row"] == 12


def test_cursor_follows_its_job_when_earlier_rows_disappear():
    """The key moves, not the index: jobs above the cursor can end."""
    table = PartitionJobTable(user="bob")
    out = _run(table, lambda: table.update_jobs(_jobs(range(1, 11))), 5,
               lambda: table.update_jobs(_jobs(range(4, 11))))
    assert out["before"] == "6"
    assert out["after"] == "6"      # same job...
    assert out["row"] == 2          # ...three rows higher


def test_cursor_stays_put_when_its_job_ends():
    table = PartitionJobTable(user="bob")
    out = _run(table, lambda: table.update_jobs(_jobs(range(1, 11))), 4,
               lambda: table.update_jobs(_jobs([1, 2, 3])))
    assert out["before"] == "5"
    assert out["after"] in {"1", None}   # the row is gone; no crash


def test_node_table_keeps_the_cursor():
    table = NodeTable()
    nodes = _nodes([f"node{i:02d}" for i in range(20)])
    out = _run(table, lambda: table.update_nodes(nodes), 9,
               lambda: table.update_nodes(nodes))
    assert out["before"] == "node09"
    assert out["after"] == "node09"


def test_partition_table_keeps_the_cursor():
    table = PartitionTable()
    parts = _partitions(["gpu", "cpu", "fat", "debug"])
    out = _run(table, lambda: table.update_partitions(parts), 2,
               lambda: table.update_partitions(parts))
    assert out["before"] == "fat"
    assert out["after"] == "fat"


def test_usage_table_keeps_the_cursor():
    table = UsageTable(user="bob")
    rows = _usage(["ann", "bob", "cid", "dee"])
    out = _run(table, lambda: table.update_rows(rows), 2,
               lambda: table.update_rows(rows))
    assert out["before"] == "cid"
    assert out["after"] == "cid"


def test_refill_on_an_empty_table_is_harmless():
    table = PartitionJobTable(user="bob")

    async def scenario():
        app = _Harness(table)
        async with app.run_test(size=(120, 30)) as pilot:
            table.update_jobs([])
            await pilot.pause()
            assert cursor_key(table) is None
            table.update_jobs(_jobs([1, 2]))
            await pilot.pause()
            assert table.row_count == 2

    asyncio.run(scenario())
