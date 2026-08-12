## Node view

Press `Enter` on a partition in the monitor (`p`) to drill into its individual nodes.

<img src="https://raw.githubusercontent.com/RobinU434/LazySlurm/main/img/nodes.png" alt="Nodes of a partition" width="100%">

The upper panel gives one row per node:

| Column | Meaning |
|--------|---------|
| State | `idle`, `mixed`, `allocated`, `drained`, `down`, … A trailing `*` means the node is not responding |
| CPUs A/I/O/T | allocated / idle / other / total cores |
| Load | the node's load average over its core count — *actual* usage, unlike the allocation counters beside it |
| Memory | in use (configured minus free) over configured, red past 90% |
| GPUs | GRES in use over GRES configured; green while any are free, red when the node is full |
| Reason | why Slurm drained or downed the node (`kernel patch`, `Faulty GPU #7`, …) |

The bar at the top counts nodes by state and totals GPUs in use across the partition.

The lower panel lists **all users'** jobs running on the highlighted node, so you can see
who you would be sharing it with before you queue.

`Up`/`Down` moves between nodes and the job list follows, `Tab` switches panels, `r`
refreshes, `Escape` or `q` returns to the partition list. The screen re-polls on your
`--refresh` interval while it is open.

### Compatibility

GPU occupancy comes from `sinfo -N -O …,GresUsed,…`, which the short `%`-format cannot
report. On Slurm builds that reject those output field names, LazySlurm falls back to the
short query automatically — everything except the GPU column still works.

---

**Install / upgrade**

```bash
pip install --upgrade lazyslurm-py
```

Full changelog: [CHANGELOG.md](https://github.com/RobinU434/LazySlurm/blob/main/CHANGELOG.md)
