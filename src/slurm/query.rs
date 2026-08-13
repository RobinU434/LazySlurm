//! The read side: everything that asks Slurm a question.
//!
//! All of it hangs off [`Slurm`], which owns the transport and the configuration
//! so that callers never thread them through by hand. Availability latches also
//! live here rather than in globals, so tests can have as many independent
//! instances as they like.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};

use chrono::{Datelike, Duration, Local, NaiveDate};

use crate::config::Config;
use crate::model::{
    job::{DetailSource, StatsSource},
    CompletedJob, FairShare, JobDetail, JobStats, NodeInfo, PartitionInfo, PartitionJob,
    PriorityInfo, RunningJob, UsageRow,
};

use super::fs::file_exists;
use super::parse;
use super::transport::CommandRunner;

/// squeue format for the user's own jobs: id, name, elapsed, partition, state,
/// time limit, nodes, cpus, memory, gres, work dir.
const SQUEUE_FORMAT: &str = "--format=%i|%j|%M|%P|%T|%l|%D|%C|%m|%b|%Z";

/// sacct fields for the terminated-jobs table.
const SACCT_FORMAT: &str = "--format=JobID,JobName,State,ExitCode,Start,End,Elapsed,Partition";

/// sacct fields for the detail fallback, when scontrol no longer knows the job.
const SACCT_DETAIL_FORMAT: &str = "--format=JobID,JobName,State,ExitCode,Partition,NodeList,\
     NCPUS,NNodes,ReqMem,Timelimit,Elapsed,Submit,Start,End,WorkDir,Account,QOS,ReqTRES,\
     AllocTRES,SubmitLine";

/// The keys of [`SACCT_DETAIL_FORMAT`], in order, spelled as the detail panel
/// expects them.
const SACCT_DETAIL_KEYS: [&str; 20] = [
    "JobID",
    "JobName",
    "State",
    "ExitCode",
    "Partition",
    "Nodelist",
    "NCPUS",
    "NNodes",
    "ReqMem",
    "Timelimit",
    "Elapsed",
    "Submit",
    "Start",
    "End",
    "WorkDir",
    "Account",
    "QoS",
    "ReqTRES",
    "AllocTRES",
    "SubmitLine",
];

/// sinfo format for the partition summary.
const SINFO_FORMAT: &str = "--format=%P|%a|%F|%C|%l|%G";

/// The long `-O` form is the only one that can report GresUsed — how many GPUs
/// of a node are actually taken, which is the question worth asking on a GPU
/// cluster. `:|` makes sinfo pad each field with `|` instead of spaces.
const SINFO_NODE_FIELDS: &str = "NodeHost:|,StateLong:|,CPUsState:|,Memory:|,FreeMem:|,\
     CPUsLoad:|,Gres:|,GresUsed:|,Reason:|";

/// Fallback for Slurm versions without those `-O` field names; loses GresUsed.
const SINFO_NODE_FORMAT: &str = "--format=%N|%T|%C|%m|%e|%O|%G||%E";

/// squeue format for other people's jobs on a partition or node.
const PARTITION_JOB_FORMAT: &str = "--format=%i|%u|%j|%T|%M|%l|%D|%C|%b|%R";

/// sstat fields for a running job's live counters.
const SSTAT_FORMAT: &str = "--format=AveCPU,AveCPUFreq,AveRSS,MaxRSS,AveVMSize,MaxVMSize,\
     AveDiskRead,AveDiskWrite,MaxDiskRead,MaxDiskWrite,MaxRSSNode,MaxRSSTask";

/// sacct fields for the accounting half of the stats panel.
///
/// JobID comes first so step rows can be told apart from the job row: sacct puts
/// ReqMem/Timelimit only on the job row and MaxRSS only on the step rows.
const SACCT_STATS_FORMAT: &str = "--format=JobID,TotalCPU,Elapsed,ReqMem,AllocTRES,ReqTRES,\
     AllocCPUS,NNodes,NTasks,Timelimit,MaxRSS";

/// sprio format: job id, priority, and the factors behind it.
const SPRIO_FORMAT: &str = "--format=%i|%Y|%A|%F|%J|%P|%Q";

/// Selectable windows for the usage panel.
pub const USAGE_WINDOWS: [UsageWindow; 3] =
    [UsageWindow::Month, UsageWindow::Last30Days, UsageWindow::Year];

/// A time span the account-usage panel can report over.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum UsageWindow {
    #[default]
    Month,
    Last30Days,
    Year,
}

impl UsageWindow {
    /// How the window reads in the summary bar.
    pub fn label(self) -> &'static str {
        match self {
            Self::Month => "this month",
            Self::Last30Days => "last 30 days",
            Self::Year => "this year",
        }
    }

    /// The start date sreport should be asked for.
    ///
    /// sreport accepts `now` for the end, so no clock arithmetic is needed
    /// beyond finding the first of the month or the year.
    pub fn start(self, today: NaiveDate) -> String {
        let date = match self {
            Self::Month => today.with_day(1).unwrap_or(today),
            Self::Last30Days => today - Duration::days(30),
            Self::Year => today.with_month(1).and_then(|d| d.with_day(1)).unwrap_or(today),
        };
        date.format("%Y-%m-%d").to_string()
    }

    /// Cycle month → 30 days → year → month.
    pub fn next(self) -> Self {
        match self {
            Self::Month => Self::Last30Days,
            Self::Last30Days => Self::Year,
            Self::Year => Self::Month,
        }
    }
}

/// Whether an optional Slurm feature turned out to be unavailable.
///
/// Latched rather than retried: once `sprio` is missing it stays missing for the
/// session, and the panel says *why* the breakdown is absent instead of leaving
/// a hole the user cannot explain.
#[derive(Debug, Default)]
struct Availability {
    accounting_missing: AtomicBool,
    sprio_missing: AtomicBool,
}

/// Stderr fragments that mean accounting is not configured on this cluster.
const NO_ACCOUNTING: &[&str] = &[
    "not found",
    "no such file",
    "not configured",
    "accounting_storage",
    "slurmdbd",
];

/// The read-side facade over a Slurm cluster.
pub struct Slurm {
    runner: Box<dyn CommandRunner>,
    config: Config,
    availability: Availability,
}

impl Slurm {
    /// Build a facade over `runner`, honouring `config`.
    pub fn new(runner: Box<dyn CommandRunner>, config: Config) -> Self {
        Self {
            runner,
            config,
            availability: Availability::default(),
        }
    }

    /// The transport, for callers that run actions or read files.
    pub fn runner(&self) -> &dyn CommandRunner {
        self.runner.as_ref()
    }

    pub fn config(&self) -> &Config {
        &self.config
    }

    /// Replace the configuration, as the `,` live-reload does.
    pub fn set_config(&mut self, config: Config) {
        self.config = config;
    }

    /// False once accounting has been found to be unavailable.
    pub fn accounting_available(&self) -> bool {
        !self.availability.accounting_missing.load(Ordering::Relaxed)
    }

    /// False once `sprio` has been found to be missing.
    pub fn sprio_available(&self) -> bool {
        !self.availability.sprio_missing.load(Ordering::Relaxed)
    }

    fn note_accounting_failure(&self, stderr: &str) {
        let text = stderr.to_lowercase();
        if NO_ACCOUNTING.iter().any(|marker| text.contains(marker)) {
            self.availability
                .accounting_missing
                .store(true, Ordering::Relaxed);
        }
    }

    // -----------------------------------------------------------------------
    // Jobs
    // -----------------------------------------------------------------------

    /// The user's current jobs, newest first, with array tasks kept together.
    pub async fn running_jobs(&self) -> Vec<RunningJob> {
        let user = self.config.effective_user();
        let mut args = vec!["squeue", "-u", &user, SQUEUE_FORMAT, "--noheader", "--sort=-i"];
        if !self.config.partition.is_empty() {
            args.extend(["-p", &self.config.partition]);
        }

        let output = self.runner.run(&args).await;
        if !output.is_useful() {
            return Vec::new();
        }

        let mut jobs = parse::squeue_jobs(&output.stdout);
        sort_newest_first(&mut jobs, |job| &job.job_id);
        jobs
    }

    /// The user's finished jobs over the configured window, newest first.
    pub async fn completed_jobs(&self) -> Vec<CompletedJob> {
        let user = self.config.effective_user();
        let start = (Local::now() - Duration::days(i64::from(self.config.days)))
            .format("--starttime=%Y-%m-%dT00:00:00")
            .to_string();

        let output = self
            .runner
            .run(&[
                "sacct",
                "-u",
                &user,
                SACCT_FORMAT,
                &start,
                "--noheader",
                "--parsable2",
            ])
            .await;
        if !output.is_useful() {
            return Vec::new();
        }

        let mut jobs = parse::sacct_jobs(&output.stdout, &self.config.partition);
        sort_newest_first(&mut jobs, |job| &job.job_id);
        jobs
    }

    /// Everything Slurm still knows about one job.
    ///
    /// `scontrol` is tried first because it is richer and includes the log paths;
    /// once the job ages out of slurmctld, `sacct` is the only source left and the
    /// log paths have to be guessed.
    pub async fn job_detail(&self, job_id: &str) -> Option<JobDetail> {
        let output = self.runner.run(&["scontrol", "show", "job", job_id]).await;

        if output.is_useful() && !output.stdout.contains("Invalid job id") {
            let raw = parse::scontrol(&output.stdout);
            return Some(JobDetail {
                job_id: job_id.to_string(),
                stdout_path: raw.get("StdOut").cloned(),
                stderr_path: raw.get("StdErr").cloned(),
                work_dir: raw.get("WorkDir").cloned().unwrap_or_default(),
                raw,
                source: DetailSource::Scontrol,
            });
        }

        self.job_detail_from_sacct(job_id).await
    }

    async fn job_detail_from_sacct(&self, job_id: &str) -> Option<JobDetail> {
        let output = self
            .runner
            .run(&[
                "sacct",
                "-j",
                job_id,
                SACCT_DETAIL_FORMAT,
                "--noheader",
                "--parsable2",
            ])
            .await;
        if !output.is_useful() {
            return None;
        }

        // Take the job row; step rows carry the same id with a `.suffix`.
        let line = output
            .stdout
            .trim()
            .lines()
            .map(|line| line.split('|').map(str::trim).collect::<Vec<_>>())
            .find(|fields| fields.len() >= 20 && !fields[0].contains('.'))?;

        let raw: BTreeMap<String, String> = SACCT_DETAIL_KEYS
            .iter()
            .zip(line.iter())
            .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
            .collect();

        let work_dir = raw.get("WorkDir").cloned().unwrap_or_default();
        let job_name = raw.get("JobName").cloned().unwrap_or_default();

        let stdout_path = self.guess_log_path(&work_dir, job_id, "out", &job_name).await;
        let mut stderr_path = self.guess_log_path(&work_dir, job_id, "err", &job_name).await;
        // Many clusters merge stdout and stderr into one .out file.
        if stderr_path.is_none() {
            stderr_path = stdout_path.clone();
        }

        Some(JobDetail {
            job_id: job_id.to_string(),
            raw,
            stdout_path,
            stderr_path,
            work_dir,
            source: DetailSource::Sacct,
        })
    }

    /// Try the log-file naming patterns different clusters use.
    ///
    /// Only reached once a job has left slurmctld, since `scontrol` reports the
    /// real paths. Every candidate costs a stat (or a remote `test -f`), so the
    /// list stays short and ordered by how common each pattern is.
    async fn guess_log_path(
        &self,
        work_dir: &str,
        job_id: &str,
        suffix: &str,
        job_name: &str,
    ) -> Option<String> {
        if work_dir.is_empty() {
            return None;
        }
        let extension = if suffix == "out" { "out" } else { "err" };

        let mut candidates = vec![
            format!("{work_dir}/slurm-{job_id}.{extension}"),
            format!("{work_dir}/slurm-{job_id}.{suffix}"),
        ];
        if !job_name.is_empty() {
            candidates.extend([
                format!("{work_dir}/{job_name}-{job_id}.{extension}"),
                format!("{work_dir}/{job_name}_{job_id}.{extension}"),
                format!("{work_dir}/{job_name}.{extension}"),
            ]);
        }
        candidates.extend([
            format!("{work_dir}/logs/slurm-{job_id}.{extension}"),
            format!("{work_dir}/log/slurm-{job_id}.{extension}"),
        ]);

        for candidate in candidates {
            if file_exists(self.runner(), &candidate).await {
                return Some(candidate);
            }
        }
        None
    }

    /// Resource usage for one job, merging live and accounting counters.
    pub async fn job_stats(&self, job_id: &str) -> Option<JobStats> {
        let step = format!("{job_id}.batch");
        // Bound to locals so the slices outlive the borrows `join!` holds.
        let sstat_args = ["sstat", "-j", &step, SSTAT_FORMAT, "--noheader", "--parsable2"];
        let sacct_args = [
            "sacct",
            "-j",
            job_id,
            SACCT_STATS_FORMAT,
            "--noheader",
            "--parsable2",
        ];
        let (live, accounting) =
            tokio::join!(self.runner.run(&sstat_args), self.runner.run(&sacct_args));

        let live = live
            .is_useful()
            .then(|| parse::sstat(&live.stdout, job_id))
            .flatten();
        let accounting = accounting
            .is_useful()
            .then(|| parse::sacct_stats(&accounting.stdout))
            .flatten();

        match (live, accounting) {
            (None, None) => None,
            (live, accounting) => {
                let had_live = live.is_some();
                let mut stats =
                    live.unwrap_or_else(|| JobStats::empty(job_id, StatsSource::Sacct));

                if let Some(fields) = accounting {
                    apply_accounting(&mut stats, &fields);
                    stats.source = if had_live {
                        StatsSource::Combined
                    } else {
                        StatsSource::Sacct
                    };
                }
                Some(stats)
            }
        }
    }

    // -----------------------------------------------------------------------
    // Partitions and nodes
    // -----------------------------------------------------------------------

    /// Every partition's node/CPU state, with job counts filled in.
    ///
    /// Unlike the cluster bar this keeps unavailable ("down") partitions — the
    /// monitor screen shows them greyed out rather than hiding them.
    pub async fn partitions(&self) -> Vec<PartitionInfo> {
        let (info, counts) = tokio::join!(
            self.runner
                .run(&["sinfo", "--noheader", "--summarize", SINFO_FORMAT]),
            self.partition_job_counts(),
        );
        if !info.is_useful() {
            return Vec::new();
        }

        let mut partitions = parse::sinfo(&info.stdout);
        for partition in &mut partitions {
            if let Some((running, pending)) = counts.get(&partition.name) {
                partition.running = *running;
                partition.pending = *pending;
            }
        }
        parse::order_partitions(partitions, &self.config.partition_order)
    }

    /// Running/pending job counts per partition, across all users.
    async fn partition_job_counts(&self) -> BTreeMap<String, (u32, u32)> {
        let output = self
            .runner
            .run(&[
                "squeue",
                "--noheader",
                "--format=%P|%T",
                "--states=RUNNING,PENDING",
            ])
            .await;
        if !output.is_useful() {
            return BTreeMap::new();
        }
        parse::partition_job_counts(&output.stdout)
    }

    /// Per-partition availability strings for the cluster bar, e.g. `gpu:10/5/0/15`.
    ///
    /// Down partitions are dropped here — the one-line bar has no room to explain
    /// a partition nobody can submit to.
    pub async fn partition_availability(&self) -> Vec<String> {
        let output = self
            .runner
            .run(&["sinfo", "--noheader", "--summarize", SINFO_FORMAT])
            .await;
        if output.stdout.trim().is_empty() {
            return Vec::new();
        }

        let up: Vec<PartitionInfo> = parse::sinfo(&output.stdout)
            .into_iter()
            .filter(PartitionInfo::is_up)
            .collect();

        parse::order_partitions(up, &self.config.partition_order)
            .iter()
            .map(PartitionInfo::availability_summary)
            .collect()
    }

    /// Every node of one partition, sorted by name.
    pub async fn partition_nodes(&self, partition: &str) -> Vec<NodeInfo> {
        if partition.is_empty() {
            return Vec::new();
        }

        let mut output = self
            .runner
            .run(&[
                "sinfo",
                "-N",
                "-p",
                partition,
                "--noheader",
                "-O",
                SINFO_NODE_FIELDS,
            ])
            .await;

        if !output.is_useful() {
            // Older Slurm: retry with the short format, losing GresUsed.
            output = self
                .runner
                .run(&[
                    "sinfo",
                    "-N",
                    "-p",
                    partition,
                    "--noheader",
                    SINFO_NODE_FORMAT,
                ])
                .await;
            if !output.is_useful() {
                return Vec::new();
            }
        }

        let mut nodes = parse::sinfo_nodes(&output.stdout);
        nodes.sort_by(|a, b| a.name.cmp(&b.name));
        nodes
    }

    /// All users' jobs currently running on one node.
    pub async fn node_jobs(&self, node: &str) -> Vec<PartitionJob> {
        if node.is_empty() {
            return Vec::new();
        }
        let output = self
            .runner
            .run(&[
                "squeue",
                "-w",
                node,
                "--noheader",
                PARTITION_JOB_FORMAT,
                "--states=RUNNING",
            ])
            .await;
        if !output.is_useful() {
            return Vec::new();
        }

        let mut jobs = parse::partition_jobs(&output.stdout);
        sort_newest_first(&mut jobs, |job| &job.job_id);
        jobs
    }

    /// All users' jobs on one partition: running first, then newest first.
    pub async fn partition_jobs(&self, partition: &str, states: &str) -> Vec<PartitionJob> {
        if partition.is_empty() {
            return Vec::new();
        }
        let states = format!("--states={states}");
        let output = self
            .runner
            .run(&[
                "squeue",
                "-p",
                partition,
                "--noheader",
                PARTITION_JOB_FORMAT,
                &states,
            ])
            .await;
        if !output.is_useful() {
            return Vec::new();
        }

        let mut jobs = parse::partition_jobs(&output.stdout);
        jobs.sort_by(|a, b| {
            let running = |job: &PartitionJob| job.state != "RUNNING";
            running(a)
                .cmp(&running(b))
                .then_with(|| crate::model::sort_key(&b.job_id).cmp(&crate::model::sort_key(&a.job_id)))
        });
        jobs
    }

    // -----------------------------------------------------------------------
    // Accounting
    // -----------------------------------------------------------------------

    /// Per-user hours in the account over a window, largest first.
    pub async fn account_usage(&self, window: UsageWindow, account: &str) -> Vec<UsageRow> {
        let start = format!("start={}", window.start(Local::now().date_naive()));
        let mut args = vec![
            "sreport",
            "cluster",
            "AccountUtilizationByUser",
            &start,
            "end=now",
            "-t",
            "hours",
            "-P",
            "--noheader",
        ];
        let account_arg = format!("account={account}");
        if !account.is_empty() {
            args.push(&account_arg);
        }

        let output = self.runner.run(&args).await;
        if output.code != 0 {
            self.note_accounting_failure(&output.stderr);
            return Vec::new();
        }

        let mut rows = parse::sreport(&output.stdout);
        // Account totals last, then biggest consumer first.
        rows.sort_by(|a, b| {
            a.is_account_total()
                .cmp(&b.is_account_total())
                .then_with(|| b.hours.total_cmp(&a.hours))
        });
        rows
    }

    /// Fair-share rows for a user's own associations.
    pub async fn fairshare(&self, user: &str) -> Vec<FairShare> {
        let mut args = vec![
            "sshare",
            "-P",
            "-o",
            "Account,User,RawShares,NormShares,RawUsage,EffectvUsage,FairShare",
        ];
        if user.is_empty() {
            args.push("-U");
        } else {
            args.extend(["-u", user]);
        }

        let output = self.runner.run(&args).await;
        if output.code != 0 {
            self.note_accounting_failure(&output.stderr);
            return Vec::new();
        }
        parse::sshare(&output.stdout)
    }

    /// Priority factors and queue position for a pending job.
    ///
    /// `None` when sprio is missing, priority accounting is off, or the job is no
    /// longer pending — the caller shows a plain message instead.
    pub async fn job_priority(&self, job_id: &str, partition: &str) -> Option<PriorityInfo> {
        if job_id.is_empty() {
            return None;
        }

        let mut args = vec!["sprio", "--noheader", SPRIO_FORMAT];
        if !partition.is_empty() && partition != "N/A" && partition != "None" {
            args.extend(["-p", partition]);
        }

        let output = self.runner.run(&args).await;
        if output.code != 0 {
            let text = output.stderr.to_lowercase();
            if text.contains("not found") || text.contains("no such file") {
                self.availability.sprio_missing.store(true, Ordering::Relaxed);
            }
            return None;
        }
        if output.stdout.trim().is_empty() {
            return None;
        }
        parse::sprio(&output.stdout, job_id)
    }
}

/// Sort newest first, keeping each array's tasks together and ascending.
fn sort_newest_first<T>(items: &mut [T], id: impl Fn(&T) -> &str) {
    items.sort_by(|a, b| crate::model::sort_key(id(b)).cmp(&crate::model::sort_key(id(a))));
}

/// Fold sacct's numbers into stats that may already hold live sstat counters.
fn apply_accounting(stats: &mut JobStats, fields: &BTreeMap<String, String>) {
    let get = |key: &str| {
        fields
            .get(key)
            .cloned()
            .unwrap_or_else(|| crate::model::job::NOT_AVAILABLE.to_string())
    };
    let number = |key: &str| -> u32 {
        fields
            .get(key)
            .and_then(|value| value.trim().parse().ok())
            .unwrap_or(0)
    };

    stats.total_cpu = get("TotalCPU");
    stats.elapsed = get("Elapsed");
    stats.req_mem = get("ReqMem");
    stats.time_limit = get("Timelimit");
    stats.alloc_cpus = number("AllocCPUS");
    stats.nnodes = number("NNodes");
    stats.ntasks = number("NTasks");

    // sstat has no MaxRSS for a finished job; sacct's step rows do.
    if crate::model::job::is_missing(&stats.max_rss) {
        if let Some(peak) = fields.get("MaxRSS") {
            stats.max_rss = peak.clone();
        }
    }

    // The GPU allocation is buried in whichever TRES string is populated.
    for key in ["AllocTRES", "ReqTRES"] {
        let tres = fields.get(key).cloned().unwrap_or_default();
        if tres.to_lowercase().contains("gres/gpu") {
            if let Some(part) = tres
                .split(',')
                .find(|part| part.to_lowercase().contains("gres/gpu"))
            {
                stats.gpu_alloc = part.trim().to_string();
            }
            break;
        }
    }
    stats.gpu_tres = fields
        .get("AllocTRES")
        .or_else(|| fields.get("ReqTRES"))
        .cloned()
        .unwrap_or_else(|| crate::model::job::NOT_AVAILABLE.to_string());
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::slurm::transport::testing::StubRunner;
    use crate::slurm::transport::Output;
    use std::sync::Arc;

    const SINFO_OUT: &str = concat!(
        "a100*|up|19/1/1/21|778/502/64/1344|3-00:00:00|gpu:a100:8\n",
        "a100|up|3/0/0/3|366/402/0/768|3-00:00:00|gpu:a100:9\n",
        "cpu|up|2/0/0/2|13/51/0/64|30-00:00:00|(null)\n",
        "maint|down|0/0/4/4|0/0/256/256|1:00:00|(null)",
    );

    /// A `Slurm` over a stub that answers every command with `stdout`.
    fn with_stdout(stdout: &str) -> Slurm {
        Slurm::new(Box::new(StubRunner::with_stdout(stdout)), Config::default())
    }

    #[tokio::test]
    async fn lists_running_jobs_newest_first() {
        let slurm = with_stdout(concat!(
            "101|jobA|1:00|gpu|RUNNING|2:00|1|4|8G|gpu:1|/work/a\n",
            "205|jobB|0:10|cpu|PENDING|1:00|1|2|4G|None|/work/b",
        ));
        let jobs = slurm.running_jobs().await;
        let ids: Vec<&str> = jobs.iter().map(|j| j.job_id.as_str()).collect();
        assert_eq!(ids, vec!["205", "101"]);
    }

    #[tokio::test]
    async fn keeps_array_tasks_together() {
        // Regression: array ids are not integers, so a naive sort scattered them.
        let rows: String = ["500_3", "600", "500_11", "499", "500_1"]
            .iter()
            .map(|id| format!("{id}|arr|0:10|gpu|PENDING|1:00|1|2|4G|None|/work\n"))
            .collect();
        let slurm = with_stdout(&rows);

        let ids: Vec<String> = slurm
            .running_jobs()
            .await
            .into_iter()
            .map(|j| j.job_id)
            .collect();
        assert_eq!(ids, vec!["600", "500_1", "500_3", "500_11", "499"]);
    }

    #[tokio::test]
    async fn returns_nothing_when_squeue_is_empty() {
        assert!(with_stdout("").running_jobs().await.is_empty());
    }

    #[tokio::test]
    async fn completed_jobs_keep_array_tasks_together() {
        let rows: String = ["500_3", "600", "500_11", "499", "500_1"]
            .iter()
            .map(|id| format!("{id}|arr|COMPLETED|0:0|s|e|1:00|gpu\n"))
            .collect();
        let ids: Vec<String> = with_stdout(&rows)
            .completed_jobs()
            .await
            .into_iter()
            .map(|j| j.job_id)
            .collect();
        assert_eq!(ids, vec!["600", "500_1", "500_3", "500_11", "499"]);
    }

    #[tokio::test]
    async fn fills_partition_job_counts() {
        let runner = StubRunner::new(|args| {
            let stdout = if args[0] == "sinfo" {
                SINFO_OUT
            } else {
                "a100|RUNNING\na100|PENDING\ncpu|RUNNING\n"
            };
            Output {
                stdout: stdout.to_string(),
                stderr: String::new(),
                code: 0,
            }
        });
        let slurm = Slurm::new(Box::new(runner), Config::default());

        let parts = slurm.partitions().await;
        let by_name: BTreeMap<&str, &PartitionInfo> =
            parts.iter().map(|p| (p.name.as_str(), p)).collect();
        assert_eq!((by_name["a100"].running, by_name["a100"].pending), (1, 1));
        assert_eq!((by_name["cpu"].running, by_name["cpu"].pending), (1, 0));
        assert_eq!((by_name["maint"].running, by_name["maint"].pending), (0, 0));
    }

    #[tokio::test]
    async fn availability_drops_down_partitions_and_aggregates() {
        let slurm = with_stdout(SINFO_OUT);
        assert_eq!(
            slurm.partition_availability().await,
            vec!["a100:22/1/1/24", "cpu:2/0/0/2"]
        );
    }

    #[tokio::test]
    async fn availability_honours_the_configured_order() {
        let config = Config {
            partition_order: vec!["cpu".into(), "a100".into()],
            ..Config::default()
        };
        let slurm = Slurm::new(Box::new(StubRunner::with_stdout(SINFO_OUT)), config);
        assert_eq!(
            slurm.partition_availability().await,
            vec!["cpu:2/0/0/2", "a100:22/1/1/24"]
        );
    }

    #[tokio::test]
    async fn partition_jobs_sort_running_first_then_newest() {
        let slurm = with_stdout(concat!(
            "100|a|j1|PENDING|0:00|1:00|1|1|N/A|(Priority)\n",
            "200|b|j2|RUNNING|1:00|1:00|1|1|N/A|node1\n",
            "300|c|j3|PENDING|0:00|1:00|1|1|N/A|(Priority)\n",
            "150|d|j4|RUNNING|1:00|1:00|1|1|N/A|node2",
        ));
        let ids: Vec<String> = slurm
            .partition_jobs("gpu", "RUNNING,PENDING")
            .await
            .into_iter()
            .map(|j| j.job_id)
            .collect();
        assert_eq!(ids, vec!["200", "150", "300", "100"]);
    }

    #[tokio::test]
    async fn partition_jobs_order_arrays_after_running_first() {
        let slurm = with_stdout(concat!(
            "500_11|u|j|PENDING|0:00|1:00|1|1|N/A|(Priority)\n",
            "500_2|u|j|PENDING|0:00|1:00|1|1|N/A|(Priority)\n",
            "600|u|j|RUNNING|1:00|1:00|1|1|N/A|node1\n",
            "500_1|u|j|RUNNING|1:00|1:00|1|1|N/A|node2",
        ));
        let ids: Vec<String> = slurm
            .partition_jobs("gpu", "RUNNING,PENDING")
            .await
            .into_iter()
            .map(|j| j.job_id)
            .collect();
        assert_eq!(ids, vec!["600", "500_1", "500_2", "500_11"]);
    }

    #[tokio::test]
    async fn no_partition_means_no_command() {
        let runner = Arc::new(StubRunner::with_stdout(""));
        let slurm = Slurm::new(Box::new(runner.clone()), Config::default());
        assert!(slurm.partition_jobs("", "RUNNING").await.is_empty());
        assert!(slurm.partition_nodes("").await.is_empty());
        assert!(runner.calls().is_empty());
    }

    #[tokio::test]
    async fn falls_back_to_the_short_node_format() {
        let runner = StubRunner::new(|args| {
            // Pretend this Slurm has no -O field names.
            if args.contains(&"-O") {
                return Output {
                    stdout: String::new(),
                    stderr: "invalid field".into(),
                    code: 1,
                };
            }
            Output {
                stdout: "cpu-node01|idle|0/64/0/64|128000|127000|0.05|(null)||none".into(),
                stderr: String::new(),
                code: 0,
            }
        });
        let slurm = Slurm::new(Box::new(runner), Config::default());

        let nodes = slurm.partition_nodes("cpu").await;
        assert_eq!(nodes.len(), 1);
        assert_eq!(nodes[0].name, "cpu-node01");
    }

    #[tokio::test]
    async fn node_jobs_ask_for_everyone_on_that_node() {
        let runner = Arc::new(StubRunner::with_stdout(
            "500_1|u|j|RUNNING|1:00|2:00|1|4|gres/gpu:1|gpu-node01",
        ));
        let slurm = Slurm::new(Box::new(runner.clone()), Config::default());

        assert_eq!(slurm.node_jobs("gpu-node01").await.len(), 1);

        let call = runner.only_call();
        assert!(call.contains(&"-w".to_string()));
        assert!(call.contains(&"gpu-node01".to_string()));
        assert!(call.contains(&"--states=RUNNING".to_string()));
        // Everyone's jobs, not just ours.
        assert!(!call.contains(&"-u".to_string()));
    }

    #[tokio::test]
    async fn merges_live_and_accounting_stats() {
        let runner = StubRunner::new(|args| {
            let stdout = match args[0] {
                "sstat" => "00:10:00|2.5GHz|1.2G|2.4G|8G|9G|100M|200M|150M|250M|node01|0",
                _ => "123|02:00:00|01:00:00|16Gn|cpu=8,gres/gpu=2|cpu=8|8|1|1|04:00:00|\n\
                      123.batch|01:00:00|01:00:00|||||1|1||7.5G",
            };
            Output {
                stdout: stdout.to_string(),
                stderr: String::new(),
                code: 0,
            }
        });
        let slurm = Slurm::new(Box::new(runner), Config::default());

        let stats = slurm.job_stats("123").await.expect("both sources answered");
        assert_eq!(stats.source, StatsSource::Combined);
        // sstat's live MaxRSS wins while the job is running.
        assert_eq!(stats.max_rss, "2.4G");
        assert_eq!(stats.req_mem, "16Gn");
        assert_eq!(stats.alloc_cpus, 8);
        assert_eq!(stats.gpu_count(), 2);
    }

    #[tokio::test]
    async fn uses_sacct_alone_for_a_finished_job() {
        let runner = StubRunner::new(|args| {
            if args[0] == "sstat" {
                return Output::failure("no steps running");
            }
            Output {
                stdout: "123|02:00:00|01:00:00|16Gn|cpu=8|cpu=8|8|1|1|04:00:00|\n\
                         123.batch|01:00:00|01:00:00|||||1|1||7.5G"
                    .into(),
                stderr: String::new(),
                code: 0,
            }
        });
        let slurm = Slurm::new(Box::new(runner), Config::default());

        let stats = slurm.job_stats("123").await.expect("sacct answered");
        assert_eq!(stats.source, StatsSource::Sacct);
        // With no live counters, the step peak fills MaxRSS.
        assert_eq!(stats.max_rss, "7.5G");
    }

    #[tokio::test]
    async fn reports_no_stats_when_neither_source_answers() {
        let slurm = Slurm::new(
            Box::new(StubRunner::new(|_| Output::failure("nope"))),
            Config::default(),
        );
        assert!(slurm.job_stats("123").await.is_none());
    }

    #[tokio::test]
    async fn latches_missing_sprio() {
        let slurm = Slurm::new(
            Box::new(StubRunner::new(|_| Output::failure("sprio: command not found"))),
            Config::default(),
        );
        assert!(slurm.sprio_available());
        assert!(slurm.job_priority("1", "gpu").await.is_none());
        assert!(!slurm.sprio_available());
    }

    #[tokio::test]
    async fn latches_missing_accounting() {
        let slurm = Slurm::new(
            Box::new(StubRunner::new(|_| {
                Output::failure("sreport: error: slurmdbd is not configured")
            })),
            Config::default(),
        );
        assert!(slurm.accounting_available());
        assert!(slurm.account_usage(UsageWindow::Month, "").await.is_empty());
        assert!(!slurm.accounting_available());
    }

    #[tokio::test]
    async fn sorts_usage_biggest_first_with_the_total_last() {
        let slurm = with_stdout(concat!(
            "galvani|physics||| 68364|0|\n",
            "galvani|physics|pba175|Pat B| 1234|0|\n",
            "galvani|physics|rvy895|Robin U| 2472|0|",
        ));
        let rows = slurm.account_usage(UsageWindow::Month, "").await;
        let users: Vec<&str> = rows.iter().map(|r| r.user.as_str()).collect();
        assert_eq!(users, vec!["rvy895", "pba175", ""]);
    }

    #[test]
    fn usage_windows_cycle() {
        assert_eq!(UsageWindow::Month.next(), UsageWindow::Last30Days);
        assert_eq!(UsageWindow::Last30Days.next(), UsageWindow::Year);
        assert_eq!(UsageWindow::Year.next(), UsageWindow::Month);
    }

    #[test]
    fn usage_windows_start_where_expected() {
        let today = NaiveDate::from_ymd_opt(2026, 8, 13).unwrap();
        assert_eq!(UsageWindow::Month.start(today), "2026-08-01");
        assert_eq!(UsageWindow::Year.start(today), "2026-01-01");
        assert_eq!(UsageWindow::Last30Days.start(today), "2026-07-14");
    }

    #[tokio::test]
    async fn prefers_scontrol_for_job_detail() {
        let slurm = with_stdout(
            "JobId=123 JobName=train WorkDir=/work StdOut=/work/o StdErr=/work/e \
             JobState=RUNNING",
        );
        let detail = slurm.job_detail("123").await.expect("scontrol answered");
        assert_eq!(detail.source, DetailSource::Scontrol);
        assert_eq!(detail.stdout_path.as_deref(), Some("/work/o"));
        assert_eq!(detail.work_dir, "/work");
    }

    #[tokio::test]
    async fn falls_back_to_sacct_for_an_aged_out_job() {
        let runner = StubRunner::new(|args| {
            if args[0] == "scontrol" {
                return Output {
                    stdout: "Invalid job id specified".into(),
                    stderr: String::new(),
                    code: 0,
                };
            }
            if args[0] == "test" {
                // No guessed log path exists.
                return Output::failure("");
            }
            Output {
                stdout: "123|train|COMPLETED|0:0|gpu|node01|8|1|16Gn|04:00:00|01:00:00|\
                         s|s|e|/work|physics|normal|cpu=8|cpu=8|sbatch job.sh"
                    .into(),
                stderr: String::new(),
                code: 0,
            }
        });
        let slurm = Slurm::new(Box::new(runner), Config::default());

        let detail = slurm.job_detail("123").await.expect("sacct answered");
        assert_eq!(detail.source, DetailSource::Sacct);
        assert_eq!(detail.submit_line(), "sbatch job.sh");
        assert_eq!(detail.node_list(), "node01");
        assert_eq!(detail.qos(), "normal");
    }
}
