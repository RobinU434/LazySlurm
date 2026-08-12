"""Render the README screenshots headlessly, from synthetic data.

Run with:  uv run --with cairosvg python scripts/make_screenshots.py

Everything shown is invented — no real user names, job names or paths ever
reach the images. The Slurm layer is stubbed out, so this needs neither a
cluster nor a terminal; Textual renders each screen to SVG, which is then
converted to PNG (PyPI does not render SVG in a project description).
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

from textual.widgets import Footer

from lazyslurm import slurm
from lazyslurm.app import EditJobScreen, HelpScreen, LazySlurmApp, SSHPromptScreen
from lazyslurm.models import (
    CompletedJob,
    Config,
    JobDetail,
    JobStats,
    NodeInfo,
    PartitionInfo,
    PartitionJob,
    RunningJob,
)

OUT = Path(__file__).resolve().parent.parent / "img"
SIZE = (160, 42)


# ---------------------------------------------------------------------------
# Fake cluster
# ---------------------------------------------------------------------------

RUNNING = [
    RunningJob("4815162", "train-resnet50", "6:42:11", "gpu", "RUNNING",
               time_limit="1-00:00:00", nodes="1", cpus="8", memory="40G",
               gres="gres/gpu:2", work_dir="/scratch/jdoe/vision"),
    RunningJob("4815160", "train-vit-base", "8:15:03", "gpu", "RUNNING",
               time_limit="1-00:00:00", nodes="1", cpus="8", memory="64G",
               gres="gres/gpu:4", work_dir="/scratch/jdoe/vision"),
    RunningJob("4815094", "preprocess", "1:03:47", "cpu", "RUNNING",
               time_limit="4:00:00", nodes="1", cpus="16", memory="16G",
               gres="None", work_dir="/scratch/jdoe/data"),
    RunningJob("4815201_0", "sweep-lr", "0:00", "gpu", "PENDING",
               time_limit="6:00:00", nodes="1", cpus="4", memory="24G",
               gres="gres/gpu:1", work_dir="/scratch/jdoe/sweeps"),
    RunningJob("4815201_1", "sweep-lr", "0:00", "gpu", "PENDING",
               time_limit="6:00:00", nodes="1", cpus="4", memory="24G",
               gres="gres/gpu:1", work_dir="/scratch/jdoe/sweeps"),
    RunningJob("4815201_2", "sweep-lr", "0:00", "gpu", "PENDING",
               time_limit="6:00:00", nodes="1", cpus="4", memory="24G",
               gres="gres/gpu:1", work_dir="/scratch/jdoe/sweeps"),
]

COMPLETED = [
    CompletedJob("4814977", "train-resnet18", "COMPLETED", "0:0",
                 "2026-08-11T22:14:05", "2026-08-12T04:51:32", "6:37:27", "gpu"),
    CompletedJob("4814903", "eval-imagenet", "COMPLETED", "0:0",
                 "2026-08-11T20:02:11", "2026-08-11T20:39:44", "0:37:33", "gpu"),
    CompletedJob("4814871", "train-vit-large", "OUT_OF_MEMORY", "0:125",
                 "2026-08-11T18:30:00", "2026-08-11T19:12:18", "0:42:18", "gpu"),
    CompletedJob("4814802", "sweep-wd", "TIMEOUT", "0:1",
                 "2026-08-11T12:00:00", "2026-08-11T18:00:04", "6:00:04", "gpu"),
    CompletedJob("4814766", "prepare-shards", "FAILED", "1:0",
                 "2026-08-11T11:41:09", "2026-08-11T11:43:52", "0:02:43", "cpu"),
    CompletedJob("4814701", "smoke-test", "CANCELLED", "0:0",
                 "2026-08-11T10:15:00", "2026-08-11T10:16:20", "0:01:20", "cpu"),
]

DETAIL = JobDetail(
    job_id="4815162",
    raw={
        "JobId": "4815162", "JobName": "train-resnet50", "JobState": "RUNNING",
        "Partition": "gpu", "NodeList": "gpu-node07", "NumNodes": "1",
        "NumCPUs": "8", "MinMemoryNode": "40G", "TimeLimit": "1-00:00:00",
        "RunTime": "6:42:11", "SubmitTime": "2026-08-12T03:11:44",
        "StartTime": "2026-08-12T03:12:02", "EndTime": "2026-08-13T03:12:02",
        "Account": "vision-lab", "QOS": "normal",
        "TRES": "cpu=8,mem=40G,node=1,billing=8,gres/gpu=2",
        "WorkDir": "/scratch/jdoe/vision",
        "StdOut": "/scratch/jdoe/vision/logs/train-resnet50-4815162.out",
        "StdErr": "/scratch/jdoe/vision/logs/train-resnet50-4815162.err",
        "SubmitLine": "sbatch --gres=gpu:2 --mem=40G train.sh --arch resnet50",
    },
    stdout_path="/scratch/jdoe/vision/logs/train-resnet50-4815162.out",
    stderr_path="/scratch/jdoe/vision/logs/train-resnet50-4815162.err",
    work_dir="/scratch/jdoe/vision",
)

STATS = JobStats(
    job_id="4815162", ave_cpu="06:31:12", ave_cpu_freq="2.4 GHz",
    ave_rss="21.4G", max_rss="27.9G", ave_vm_size="38.2G", max_vm_size="41.0G",
    req_mem="40G", ave_disk_read="1.2G", ave_disk_write="512M",
    max_disk_read="3.4G", max_disk_write="1.1G",
    gpu_alloc="2", gpu_tres="gres/gpu=2", total_cpu="2-04:09:36",
    elapsed="6:42:11", max_rss_node="gpu-node07", max_rss_task="0",
    source="combined",
)

STDOUT = "\n".join([
    "Epoch 11/90  loss 1.8421  top1 58.02  top5 81.44  lr 0.0400  4m12s",
    "Epoch 12/90  loss 1.7903  top1 59.31  top5 82.10  lr 0.0400  4m09s",
    "Epoch 13/90  loss 1.7488  top1 60.24  top5 82.77  lr 0.0400  4m11s",
    "Epoch 14/90  loss 1.7104  top1 61.08  top5 83.35  lr 0.0400  4m10s",
    "Epoch 15/90  loss 1.6742  top1 61.92  top5 83.90  lr 0.0400  4m13s",
    "  checkpoint saved -> checkpoints/resnet50/epoch_15.pt",
    "Epoch 16/90  loss 1.6431  top1 62.55  top5 84.31  lr 0.0400  4m08s",
    "Epoch 17/90  loss 1.6118  top1 63.20  top5 84.79  lr 0.0400  4m12s",
])

PARTITIONS = [
    PartitionInfo("gpu", "up", 21, 3, 2, 26, 812, 196, 64, 1072,
                  "1-00:00:00", "gpu:a100:8", running=64, pending=37),
    PartitionInfo("gpu-long", "up", 11, 1, 0, 12, 396, 84, 0, 480,
                  "7-00:00:00", "gpu:a100:8", running=22, pending=51),
    PartitionInfo("cpu", "up", 34, 26, 0, 60, 1088, 832, 0, 1920,
                  "30-00:00:00", "", running=113, pending=8),
    PartitionInfo("fat", "up", 2, 2, 0, 4, 96, 96, 0, 192,
                  "3-00:00:00", "", running=3, pending=0),
    PartitionInfo("maint", "down", 0, 0, 8, 8, 0, 0, 256, 256,
                  "1:00:00", "", running=0, pending=0),
]

NODES = [
    NodeInfo("gpu-node01", "mixed", 58, 6, 0, 64, 948865, 324779, 16.02,
             "gpu:a100:8(S:0-1)", "gpu:a100:5(IDX:0-4)"),
    NodeInfo("gpu-node02", "allocated", 64, 0, 0, 64, 948863, 128400, 51.4,
             "gpu:a100:8(S:0-1)", "gpu:a100:8(IDX:0-7)"),
    NodeInfo("gpu-node03", "mixed", 40, 24, 0, 64, 948863, 669673, 22.8,
             "gpu:a100:8(S:0-1)", "gpu:a100:6(IDX:0-5)"),
    NodeInfo("gpu-node04", "idle", 0, 64, 0, 64, 948863, 940120, 0.04,
             "gpu:a100:8(S:0-1)", "gpu:a100:0(IDX:N/A)"),
    NodeInfo("gpu-node05", "drained*", 0, 0, 64, 64, 948863, 942200, 0.01,
             "gpu:a100:8(S:0-1)", "gpu:a100:0(IDX:N/A)", reason="Faulty GPU #7"),
    NodeInfo("gpu-node06", "mixed", 52, 12, 0, 64, 948863, 96539, 44.6,
             "gpu:a100:8(S:0-1)", "gpu:a100:7(IDX:0-6)"),
]

NODE_JOBS = [
    PartitionJob("4815162", "jdoe", "train-resnet50", "RUNNING", "6:42:11",
                 "1-00:00:00", "1", "8", "gres/gpu:2", "gpu-node01"),
    PartitionJob("4815155", "asmith", "diffusion-ft", "RUNNING", "11:02:39",
                 "1-00:00:00", "2", "16", "gres/gpu:8", "gpu-node01"),
    PartitionJob("4815120", "cwang", "eval-sweep", "RUNNING", "3:11:47",
                 "6:00:00", "1", "4", "gres/gpu:1", "gpu-node01"),
]

PARTITION_JOBS = [
    PartitionJob("4815162", "jdoe", "train-resnet50", "RUNNING", "6:42:11",
                 "1-00:00:00", "1", "8", "gres/gpu:2", "gpu-node07"),
    PartitionJob("4815160", "jdoe", "train-vit-base", "RUNNING", "8:15:03",
                 "1-00:00:00", "1", "8", "gres/gpu:4", "gpu-node04"),
    PartitionJob("4815155", "asmith", "diffusion-ft", "RUNNING", "11:02:39",
                 "1-00:00:00", "2", "16", "gres/gpu:8", "gpu-node[01-02]"),
    PartitionJob("4815140", "bpatel", "rl-humanoid", "RUNNING", "1-03:18:52",
                 "2-00:00:00", "1", "6", "gres/gpu:1", "gpu-node09"),
    PartitionJob("4815098", "cwang", "nerf-recon", "RUNNING", "2:47:05",
                 "12:00:00", "1", "4", "gres/gpu:1", "gpu-node11"),
    PartitionJob("4815201_0", "jdoe", "sweep-lr", "PENDING", "0:00",
                 "6:00:00", "1", "4", "gres/gpu:1", "(Resources)"),
    PartitionJob("4815201_1", "jdoe", "sweep-lr", "PENDING", "0:00",
                 "6:00:00", "1", "4", "gres/gpu:1", "(Resources)"),
    PartitionJob("4815188", "asmith", "llm-pretrain", "PENDING", "0:00",
                 "1-00:00:00", "4", "32", "gres/gpu:8", "(QOSMaxGRESPerUser)"),
    PartitionJob("4815177", "dmuller", "ablation-3", "PENDING", "0:00",
                 "6:00:00", "1", "2", "gres/gpu:1", "(Priority)"),
]

CLUSTER_BAR = ["gpu:21/3/2/26", "gpu-long:11/1/0/12", "cpu:34/26/0/60", "fat:2/2/0/4"]


def install_fakes() -> None:
    async def _running(*a, **k):
        return RUNNING

    async def _completed(*a, **k):
        return COMPLETED

    async def _availability(*a, **k):
        return CLUSTER_BAR

    async def _detail(job_id, *a, **k):
        return DETAIL

    async def _stats(*a, **k):
        return STATS

    async def _log(path, *a, **k):
        return STDOUT if path and path.endswith(".out") else "(no errors)\n"

    async def _partitions(*a, **k):
        return PARTITIONS

    async def _partition_jobs(name, *a, **k):
        return PARTITION_JOBS if name == "gpu" else []

    async def _archive(*a, **k):
        return None

    async def _nodes(partition, *a, **k):
        return NODES

    async def _node_jobs(node, *a, **k):
        return NODE_JOBS if node == "gpu-node01" else []

    slurm.get_running_jobs = _running
    slurm.get_completed_jobs = _completed
    slurm.get_partition_availability = _availability
    slurm.get_job_detail = _detail
    slurm.get_job_stats = _stats
    slurm.read_log_file = _log
    slurm.get_partitions = _partitions
    slurm.get_partition_jobs = _partition_jobs
    slurm.get_partition_nodes = _nodes
    slurm.get_node_jobs = _node_jobs
    slurm.archive_batch_script = _archive
    slurm.USER = "jdoe"
    # The app warns when the hostname contains "login"; keep the real machine
    # name out of the pictures.
    socket.gethostname = lambda: "workstation"


def to_png(svg: str, path: Path) -> None:
    """Rasterize, forcing a font whose block glyphs are one cell wide.

    Textual asks for Fira Code; without it the renderer falls back per glyph
    and the load bars end up wider than their column.
    """
    import cairosvg

    svg = svg.replace("font-family: Fira Code, monospace",
                      "font-family: DejaVu Sans Mono, monospace")
    cairosvg.svg2png(bytestring=svg.encode(), write_to=str(path), scale=1.4)


def save(app: LazySlurmApp, name: str) -> None:
    # Textual's headless SVG export draws the key bar as an empty strip (the
    # FooterKey widgets render fine in a real terminal, but not here), so hide
    # it rather than publish a blank bar. The keys are in the help screenshot.
    for footer in app.screen.query(Footer):
        footer.display = False
    svg = app.export_screenshot(title=f"LazySlurm — {name.replace('-', ' ')}")
    path = OUT / f"{name}.png"
    to_png(svg, path)
    print(f"wrote {path.relative_to(path.parent.parent)}")


async def main() -> None:
    install_fakes()
    OUT.mkdir(exist_ok=True)
    config = Config(user="jdoe", refresh=0, no_live=True, no_gpu=True)

    app = LazySlurmApp(config=config)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await app._poll_jobs()
        await app._load_job_details("4815162")
        # A little sparkline history, as a long-running job would accumulate.
        app._resource_history["4815162"] = {
            "memory": [x * 1024 ** 3 for x in
                       (8.1, 11.4, 14.0, 16.8, 18.2, 19.9, 21.0, 22.6, 24.1,
                        25.0, 26.2, 27.1, 27.6, 27.9)],
            "cpu": [],
        }
        app._selected_job_id = "4815162"
        await pilot.pause()
        save(app, "main")

        # Job details, stats tab
        await app._load_job_details("4815162")
        app.query_one("#detail-view").switch_tab(4)
        await pilot.pause()
        save(app, "stats")

        # Partition monitor
        await pilot.press("p")
        await pilot.pause()
        await pilot.pause()
        save(app, "partitions")

        # Node view: Enter on the highlighted partition
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        save(app, "nodes")
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        # Pending-job editor
        app.push_screen(EditJobScreen(["4815201_0"], {
            "time_limit": "6:00:00", "partition": "gpu",
            "nodes": "1", "cpus": "4", "memory": "24G",
        }))
        await pilot.pause()
        save(app, "edit-job")
        await pilot.press("escape")
        await pilot.pause()

        # Help screen — the key bindings the footer would show
        await pilot.press("question_mark")
        await pilot.pause()
        save(app, "help")
        await pilot.press("escape")
        await pilot.pause()

        # Two-factor prompt
        app.push_screen(SSHPromptScreen(
            "jdoe@login.hpc.edu",
            "Duo two-factor login for jdoe\n\nPasscode or option (1-3):",
        ))
        await pilot.pause()
        save(app, "two-factor")
        await pilot.press("escape")
        await pilot.pause()


if __name__ == "__main__":
    asyncio.run(main())
