//! Job-shaped records, as the various Slurm commands report them.
//!
//! Each command exposes a different subset of a job, under different field
//! names, so there is one type per command rather than one merged `Job` that
//! would be mostly `None`.

use std::collections::BTreeMap;

use super::efficiency::{compute_efficiency, Efficiency};

/// The placeholder that stands in for a value Slurm did not report.
pub const NOT_AVAILABLE: &str = "N/A";

/// Values that mean "no node assigned" wherever a node list is expected.
pub const NO_NODE: &[&str] = &["N/A", "None", "(null)", ""];

/// A currently running or pending job, from `squeue`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RunningJob {
    pub job_id: String,
    pub name: String,
    pub elapsed: String,
    pub partition: String,
    pub state: String,
    pub time_limit: String,
    pub nodes: String,
    pub cpus: String,
    pub memory: String,
    pub gres: String,
    pub work_dir: String,
}

/// A completed, failed or cancelled job, from `sacct`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CompletedJob {
    pub job_id: String,
    pub name: String,
    pub state: String,
    pub exit_code: String,
    pub start: String,
    pub end: String,
    pub elapsed: String,
    pub partition: String,
}

/// A job on a partition or node, from any user (`squeue` without `-u`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PartitionJob {
    pub job_id: String,
    pub user: String,
    pub name: String,
    pub state: String,
    pub elapsed: String,
    pub time_limit: String,
    pub nodes: String,
    pub cpus: String,
    pub gres: String,
    /// Node list, or the pending reason in parentheses.
    pub nodelist: String,
}

/// Which command a [`JobDetail`] came from.
///
/// Worth tracking because `scontrol` only knows a job until `MinJobAge` seconds
/// after it ends. A `scontrol`-sourced detail means the batch script is still
/// retrievable; a `sacct`-sourced one means it is gone unless it was archived.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DetailSource {
    Scontrol,
    Sacct,
}

/// Detailed job information, parsed from `scontrol show job` or `sacct`.
///
/// The raw key/value map is kept whole: the Raw metadata tab shows all of it,
/// and the two sources disagree on field names often enough that the accessors
/// below — not the caller — should own the fallbacks. It is a `BTreeMap` because
/// that tab renders in sorted key order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JobDetail {
    pub job_id: String,
    pub raw: BTreeMap<String, String>,
    pub stdout_path: Option<String>,
    pub stderr_path: Option<String>,
    pub work_dir: String,
    pub source: DetailSource,
}

impl JobDetail {
    /// Look up the first of several possible field names that has a value.
    ///
    /// A key that is present but empty does **not** count: sacct emits empty
    /// columns for fields it does not have, so stopping at the first key that
    /// merely exists would return `""` where a later alternative holds the
    /// answer — most visibly an empty `SubmitLine` masking a good `Command`.
    fn first_of(&self, keys: &[&str]) -> &str {
        keys.iter()
            .filter_map(|key| self.raw.get(*key))
            .find(|value| !value.is_empty())
            .map_or(NOT_AVAILABLE, String::as_str)
    }

    /// The command that submitted this job.
    ///
    /// `SubmitLine` (the full `sbatch --array=1-4 job.sh`) is preferred over
    /// `Command` (just the script path), because resubmission needs the flags.
    pub fn submit_line(&self) -> &str {
        self.first_of(&["SubmitLine", "Command"])
    }

    pub fn partition(&self) -> &str {
        self.first_of(&["Partition"])
    }

    /// scontrol spells it `NodeList`, sacct `Nodelist`.
    pub fn node_list(&self) -> &str {
        self.first_of(&["NodeList", "Nodelist"])
    }

    pub fn num_cpus(&self) -> &str {
        self.first_of(&["NumCPUs", "NCPUS"])
    }

    pub fn num_nodes(&self) -> &str {
        self.first_of(&["NumNodes", "NNodes"])
    }

    pub fn memory(&self) -> &str {
        self.first_of(&["MinMemoryNode", "ReqMem"])
    }

    pub fn time_limit(&self) -> &str {
        self.first_of(&["TimeLimit", "Timelimit"])
    }

    pub fn run_time(&self) -> &str {
        self.first_of(&["RunTime", "Elapsed"])
    }

    pub fn submit_time(&self) -> &str {
        self.first_of(&["SubmitTime", "Submit"])
    }

    pub fn start_time(&self) -> &str {
        self.first_of(&["StartTime", "Start"])
    }

    pub fn end_time(&self) -> &str {
        self.first_of(&["EndTime", "End"])
    }

    pub fn state(&self) -> &str {
        self.first_of(&["JobState", "State"])
    }

    pub fn account(&self) -> &str {
        self.first_of(&["Account"])
    }

    pub fn qos(&self) -> &str {
        self.first_of(&["QOS", "QoS"])
    }

    pub fn tres(&self) -> &str {
        self.first_of(&["TRES", "ReqTRES", "AllocTRES"])
    }

    /// The GPU part of the TRES string, or whatever `Gres` holds.
    pub fn gres(&self) -> &str {
        let tres = self.tres();
        if tres.to_lowercase().contains("gres/gpu") {
            if let Some(part) = tres
                .split(',')
                .find(|part| part.to_lowercase().contains("gres/gpu"))
            {
                return part.trim();
            }
        }
        self.raw.get("Gres").map(String::as_str).unwrap_or("None")
    }

    /// Whether the job is waiting to start, which gates the Pending panel.
    pub fn is_pending(&self) -> bool {
        self.state().to_uppercase().starts_with("PENDING")
    }

    /// The pending reason code, if Slurm reported one.
    pub fn reason(&self) -> &str {
        self.raw
            .get("Reason")
            .map(|r| r.trim())
            .filter(|r| !r.is_empty())
            .unwrap_or(NOT_AVAILABLE)
    }
}

/// Which commands contributed to a [`JobStats`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StatsSource {
    /// Live counters for a running job.
    Sstat,
    /// Accounting records, available after the job ends.
    Sacct,
    /// Both, merged.
    Combined,
}

impl StatsSource {
    pub fn label(self) -> &'static str {
        match self {
            Self::Sstat => "sstat",
            Self::Sacct => "sacct",
            Self::Combined => "combined",
        }
    }
}

/// Resource usage for one job, merged from `sstat` and `sacct`.
///
/// Fields stay as Slurm's own strings because the stats panel shows most of them
/// verbatim; [`Self::efficiency`] is where they get interpreted numerically.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JobStats {
    pub job_id: String,

    // CPU
    pub ave_cpu: String,
    pub ave_cpu_freq: String,
    pub total_cpu: String,
    pub elapsed: String,

    // Memory
    pub ave_rss: String,
    pub max_rss: String,
    pub ave_vm_size: String,
    pub max_vm_size: String,
    pub req_mem: String,
    pub max_rss_node: String,
    pub max_rss_task: String,

    // Disk I/O
    pub ave_disk_read: String,
    pub ave_disk_write: String,
    pub max_disk_read: String,
    pub max_disk_write: String,

    // GPU, from the sacct TRES strings
    pub gpu_alloc: String,
    pub gpu_tres: String,

    // Denominators for the efficiency report
    pub alloc_cpus: u32,
    pub nnodes: u32,
    pub ntasks: u32,
    pub time_limit: String,

    pub source: StatsSource,
}

impl JobStats {
    /// An otherwise-empty record for `job_id`, with every field unavailable.
    pub fn empty(job_id: impl Into<String>, source: StatsSource) -> Self {
        let na = || NOT_AVAILABLE.to_string();
        Self {
            job_id: job_id.into(),
            ave_cpu: na(),
            ave_cpu_freq: na(),
            total_cpu: na(),
            elapsed: na(),
            ave_rss: na(),
            max_rss: na(),
            ave_vm_size: na(),
            max_vm_size: na(),
            req_mem: na(),
            max_rss_node: na(),
            max_rss_task: na(),
            ave_disk_read: na(),
            ave_disk_write: na(),
            max_disk_read: na(),
            max_disk_write: na(),
            gpu_alloc: na(),
            gpu_tres: na(),
            alloc_cpus: 0,
            nnodes: 0,
            ntasks: 0,
            time_limit: na(),
            source,
        }
    }

    /// GPUs allocated, from the TRES string sacct reports.
    pub fn gpu_count(&self) -> u32 {
        for spec in [&self.gpu_alloc, &self.gpu_tres] {
            for part in spec.split(',') {
                if part.to_lowercase().contains("gres/gpu=") {
                    if let Some(count) = part.rsplit('=').next().and_then(|n| n.trim().parse().ok())
                    {
                        return count;
                    }
                }
            }
        }
        0
    }

    /// How much of what the job asked for it actually used.
    pub fn efficiency(&self) -> Efficiency {
        compute_efficiency(self)
    }
}

/// Whether a value is one Slurm uses to mean "not reported".
pub fn is_missing(value: &str) -> bool {
    value.trim().is_empty() || value == NOT_AVAILABLE
}

/// Whether a node list actually names a node.
pub fn has_node(node_spec: &str) -> bool {
    !NO_NODE.contains(&node_spec.trim())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn detail_from(pairs: &[(&str, &str)], source: DetailSource) -> JobDetail {
        JobDetail {
            job_id: "1".into(),
            raw: pairs
                .iter()
                .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
                .collect(),
            stdout_path: None,
            stderr_path: None,
            work_dir: String::new(),
            source,
        }
    }

    #[test]
    fn prefers_submit_line_over_command() {
        let detail = detail_from(
            &[
                ("Command", "/home/me/job.sh"),
                ("SubmitLine", "sbatch --array=1-4 job.sh"),
            ],
            DetailSource::Scontrol,
        );
        assert_eq!(detail.submit_line(), "sbatch --array=1-4 job.sh");
    }

    #[test]
    fn falls_back_to_command_when_no_submit_line() {
        let detail = detail_from(&[("Command", "/home/me/job.sh")], DetailSource::Sacct);
        assert_eq!(detail.submit_line(), "/home/me/job.sh");
    }

    #[test]
    fn an_empty_field_falls_through_to_the_next_alternative() {
        // sacct emits empty columns rather than omitting them, so a present but
        // blank SubmitLine must not mask a usable Command.
        let detail = detail_from(
            &[("SubmitLine", ""), ("Command", "/home/me/job.sh")],
            DetailSource::Sacct,
        );
        assert_eq!(detail.submit_line(), "/home/me/job.sh");
    }

    #[test]
    fn reports_not_available_when_every_alternative_is_empty() {
        let detail = detail_from(&[("SubmitLine", ""), ("Command", "")], DetailSource::Sacct);
        assert_eq!(detail.submit_line(), NOT_AVAILABLE);
    }

    #[test]
    fn accepts_either_spelling_of_node_list() {
        let scontrol = detail_from(&[("NodeList", "gpu01")], DetailSource::Scontrol);
        let sacct = detail_from(&[("Nodelist", "gpu02")], DetailSource::Sacct);
        assert_eq!(scontrol.node_list(), "gpu01");
        assert_eq!(sacct.node_list(), "gpu02");
    }

    #[test]
    fn extracts_gpu_from_tres() {
        let detail = detail_from(
            &[("TRES", "cpu=8,mem=32G,node=1,billing=8,gres/gpu=2")],
            DetailSource::Scontrol,
        );
        assert_eq!(detail.gres(), "gres/gpu=2");
    }

    #[test]
    fn detects_pending_state() {
        let pending = detail_from(&[("JobState", "PENDING")], DetailSource::Scontrol);
        let running = detail_from(&[("JobState", "RUNNING")], DetailSource::Scontrol);
        assert!(pending.is_pending());
        assert!(!running.is_pending());
    }

    #[test]
    fn counts_gpus_from_alloc_tres() {
        let mut stats = JobStats::empty("1", StatsSource::Sacct);
        stats.gpu_tres = "cpu=8,mem=32G,gres/gpu=4".into();
        assert_eq!(stats.gpu_count(), 4);
    }

    #[test]
    fn reports_no_gpus_when_tres_has_none() {
        let stats = JobStats::empty("1", StatsSource::Sacct);
        assert_eq!(stats.gpu_count(), 0);
    }
}
