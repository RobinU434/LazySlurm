//! Partitions and the nodes behind them, from `sinfo`.

use super::format::gres_count;

/// Slurm's trailing node-state flags: `*` unresponsive, `$` reserved, and the
/// rest. Stripped to get at the base state a colour is chosen from.
const STATE_FLAGS: &[char] = &['*', '$', '~', '#', '!', '%', '@', '^', '-'];

/// Node states meaning the node is not usable, so its counters say nothing.
const OUT_OF_SERVICE: &[&str] = &["down", "drained", "draining", "fail", "failing", "maint"];

/// Slurm's allocated / idle / other / total counters.
///
/// "Other" is down, drained, or otherwise unavailable. It is excluded from load
/// denominators on purpose — see [`PartitionInfo::load`].
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Aiot {
    pub allocated: u32,
    pub idle: u32,
    pub other: u32,
    pub total: u32,
}

impl Aiot {
    /// Sum another set of counters into this one.
    ///
    /// `sinfo --summarize` emits one row per node *configuration*, so a
    /// partition with mixed hardware appears several times and its rows must be
    /// added together.
    pub fn add(&mut self, other: Aiot) {
        self.allocated += other.allocated;
        self.idle += other.idle;
        self.other += other.other;
        self.total += other.total;
    }

    /// The `A/I/O/T` string the tables display.
    pub fn display(&self) -> String {
        format!(
            "{}/{}/{}/{}",
            self.allocated, self.idle, self.other, self.total
        )
    }

    /// Fraction of *usable* capacity currently allocated (0.0–1.0).
    ///
    /// Unavailable capacity is excluded from the denominator: a partition with
    /// half its nodes drained is fully loaded when the remaining half is busy,
    /// not 50% loaded.
    pub fn load(&self) -> f64 {
        let usable = self.allocated + self.idle;
        if usable == 0 {
            0.0
        } else {
            f64::from(self.allocated) / f64::from(usable)
        }
    }
}

/// Aggregated state of one partition.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PartitionInfo {
    pub name: String,
    pub avail: String,
    pub nodes: Aiot,
    pub cpus: Aiot,
    pub time_limit: String,
    pub gres: String,
    /// Job counts across all users, filled in from a separate `squeue` call.
    pub running: u32,
    pub pending: u32,
}

impl PartitionInfo {
    /// Whether the partition is accepting work.
    pub fn is_up(&self) -> bool {
        self.avail == "up"
    }

    /// CPU load, which is what the partition table's bar shows.
    pub fn load(&self) -> f64 {
        self.cpus.load()
    }

    /// `gpu:10/5/0/15` — the compact form used in the cluster bar.
    pub fn availability_summary(&self) -> String {
        format!("{}:{}", self.name, self.nodes.display())
    }
}

/// One compute node of a partition, from `sinfo -N`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct NodeInfo {
    pub name: String,
    /// `idle`, `mixed`, `allocated`, `drained*`, …
    pub state: String,
    pub cpus: Aiot,
    /// Configured memory.
    pub memory_mb: u32,
    pub free_mem_mb: u32,
    /// Absolute load average, as Slurm reports it.
    pub cpu_load: f64,
    /// Configured GRES.
    pub gres: String,
    pub gres_used: String,
    /// Why the node is down or drained.
    pub reason: String,
}

impl NodeInfo {
    /// State without Slurm's trailing flags.
    pub fn base_state(&self) -> &str {
        self.state.trim_end_matches(STATE_FLAGS)
    }

    /// Slurm marks a node it cannot reach with a trailing `*`.
    pub fn is_unresponsive(&self) -> bool {
        self.state.ends_with('*')
    }

    /// Whether the node is down, drained or under maintenance.
    ///
    /// Such a node's load and memory counters are stale, so the UI shows a dash
    /// instead of a misleading bar.
    pub fn is_out_of_service(&self) -> bool {
        OUT_OF_SERVICE.contains(&self.base_state())
    }

    /// Load average over total CPUs. Can exceed 1.0 when oversubscribed.
    pub fn load(&self) -> f64 {
        if self.cpus.total == 0 {
            0.0
        } else {
            self.cpu_load / f64::from(self.cpus.total)
        }
    }

    pub fn mem_used_mb(&self) -> u32 {
        self.memory_mb.saturating_sub(self.free_mem_mb)
    }

    /// Fraction of configured memory in use (0.0–1.0).
    pub fn mem_used(&self) -> f64 {
        if self.memory_mb == 0 {
            0.0
        } else {
            f64::from(self.mem_used_mb()) / f64::from(self.memory_mb)
        }
    }

    pub fn gpus_total(&self) -> u32 {
        gres_count(&self.gres)
    }

    pub fn gpus_used(&self) -> u32 {
        gres_count(&self.gres_used)
    }

    pub fn gpus_free(&self) -> u32 {
        self.gpus_total().saturating_sub(self.gpus_used())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rstest::rstest;

    #[test]
    fn load_excludes_unavailable_capacity() {
        // Half the CPUs are drained; the usable half is fully busy.
        let counters = Aiot {
            allocated: 50,
            idle: 0,
            other: 50,
            total: 100,
        };
        assert_eq!(counters.load(), 1.0);
    }

    #[test]
    fn load_is_zero_when_nothing_is_usable() {
        let counters = Aiot {
            allocated: 0,
            idle: 0,
            other: 10,
            total: 10,
        };
        assert_eq!(counters.load(), 0.0);
    }

    #[test]
    fn counters_sum_across_node_configurations() {
        let mut total = Aiot::default();
        total.add(Aiot {
            allocated: 1,
            idle: 2,
            other: 0,
            total: 3,
        });
        total.add(Aiot {
            allocated: 4,
            idle: 0,
            other: 1,
            total: 5,
        });
        assert_eq!(total.display(), "5/2/1/8");
    }

    #[rstest]
    #[case("idle", "idle", false)]
    #[case("mixed", "mixed", false)]
    #[case("drained*", "drained", true)]
    #[case("down*", "down", true)]
    #[case("allocated", "allocated", false)]
    fn strips_state_flags(
        #[case] state: &str,
        #[case] base: &str,
        #[case] unresponsive: bool,
    ) {
        let node = NodeInfo {
            state: state.into(),
            ..NodeInfo::default()
        };
        assert_eq!(node.base_state(), base);
        assert_eq!(node.is_unresponsive(), unresponsive);
    }

    #[test]
    fn counts_gpus_from_gres_strings() {
        let node = NodeInfo {
            gres: "gpu:a100:8".into(),
            gres_used: "gpu:a100:3(IDX:0-2)".into(),
            ..NodeInfo::default()
        };
        assert_eq!(node.gpus_total(), 8);
        assert_eq!(node.gpus_used(), 3);
        assert_eq!(node.gpus_free(), 5);
    }

    #[test]
    fn memory_use_never_goes_negative() {
        // FreeMem can exceed the configured total on a misreporting node.
        let node = NodeInfo {
            memory_mb: 1000,
            free_mem_mb: 1200,
            ..NodeInfo::default()
        };
        assert_eq!(node.mem_used_mb(), 0);
    }
}
