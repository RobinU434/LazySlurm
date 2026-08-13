//! Pure parsers for Slurm command output.
//!
//! Every function here is `&str -> data` with no I/O, which is what makes the
//! bulk of this crate testable against recorded cluster output. The command
//! layer in [`super::query`] owns the argv and the process; this module owns the
//! interpretation.
//!
//! Two conventions run throughout:
//!
//! - Rows that do not have enough fields are **skipped, not rejected**. Slurm
//!   versions differ in how many columns they emit, and one odd row must not
//!   empty a panel.
//! - Slurm's placeholders (`(null)`, `N/A`, `none`) are normalised to an empty
//!   string at the parser boundary, so no display code has to know about them.

use std::collections::BTreeMap;

use crate::model::{
    job::StatsSource, Aiot, CompletedJob, FairShare, JobStats, NodeInfo, PartitionInfo,
    PartitionJob, PriorityInfo, RunningJob, UsageRow,
};

/// Placeholders that mean "this field is empty".
const PLACEHOLDERS: &[&str] = &["(null)", "N/A", "none", "None"];

/// States a job can be in that mean it has not finished, so it belongs in the
/// active table rather than the terminated one.
const UNFINISHED: &[&str] = &["RUNNING", "PENDING", "REQUEUED"];

/// Normalise a Slurm placeholder to an empty string.
fn clean(value: &str) -> &str {
    let value = value.trim();
    if PLACEHOLDERS.contains(&value) {
        ""
    } else {
        value
    }
}

/// Split a `|`-separated row into trimmed fields.
fn fields(line: &str) -> Vec<&str> {
    line.split('|').map(str::trim).collect()
}

/// Read a field by index, or `""` if the row is shorter than that.
fn field(fields: &[&str], index: usize) -> String {
    fields.get(index).unwrap_or(&"").to_string()
}

/// Parse an integer that Slurm may have written as a float (`16.02` → 16).
fn as_int(value: &str) -> u32 {
    value.trim().parse::<f64>().map(|n| n as u32).unwrap_or(0)
}

/// Parse a float, defaulting to zero.
fn as_float(value: &str) -> f64 {
    value.trim().parse::<f64>().unwrap_or(0.0)
}

/// Parse Slurm's `allocated/idle/other/total` counter string.
///
/// Anything that is not exactly four parts yields zeroes rather than a partial
/// reading — a half-parsed counter would render as a plausible but wrong bar.
pub fn aiot(text: &str) -> Aiot {
    let parts: Vec<&str> = text.split('/').collect();
    if parts.len() != 4 {
        return Aiot::default();
    }
    Aiot {
        allocated: as_int(parts[0]),
        idle: as_int(parts[1]),
        other: as_int(parts[2]),
        total: as_int(parts[3]),
    }
}

// ---------------------------------------------------------------------------
// squeue — active jobs
// ---------------------------------------------------------------------------

/// Parse `squeue --format=%i|%j|%M|%P|%T|%l|%D|%C|%m|%b|%Z` output.
///
/// Rows are returned in input order; sorting is the caller's job because it
/// differs per view.
pub fn squeue_jobs(stdout: &str) -> Vec<RunningJob> {
    stdout
        .trim()
        .lines()
        .filter_map(|line| {
            let f = fields(line);
            if f.len() < 11 {
                return None;
            }
            Some(RunningJob {
                job_id: field(&f, 0),
                name: field(&f, 1),
                elapsed: field(&f, 2),
                partition: field(&f, 3),
                state: field(&f, 4),
                time_limit: field(&f, 5),
                nodes: field(&f, 6),
                cpus: field(&f, 7),
                memory: field(&f, 8),
                // Slurm prints nothing for a job with no GRES; the column reads
                // better as an explicit "None".
                gres: if f[9].is_empty() {
                    "None".to_string()
                } else {
                    field(&f, 9)
                },
                work_dir: field(&f, 10),
            })
        })
        .collect()
}

// ---------------------------------------------------------------------------
// sacct — terminated jobs
// ---------------------------------------------------------------------------

/// Parse `sacct --format=JobID,JobName,State,ExitCode,Start,End,Elapsed,Partition`.
///
/// Sub-steps (`123.batch`) and jobs that have not finished are dropped: the
/// terminated table shows one row per job, and a running job belongs in the
/// active table above it. `partition_filter` keeps only one partition when set.
pub fn sacct_jobs(stdout: &str, partition_filter: &str) -> Vec<CompletedJob> {
    stdout
        .trim()
        .lines()
        .filter_map(|line| {
            let f = fields(line);
            if f.len() < 8 {
                return None;
            }
            let job_id = field(&f, 0);
            if job_id.contains('.') {
                return None;
            }
            let state = field(&f, 2);
            if UNFINISHED.contains(&state.as_str()) {
                return None;
            }
            let partition = field(&f, 7);
            if !partition_filter.is_empty() && partition != partition_filter {
                return None;
            }
            Some(CompletedJob {
                job_id,
                name: field(&f, 1),
                state,
                exit_code: field(&f, 3),
                start: field(&f, 4),
                end: field(&f, 5),
                elapsed: field(&f, 6),
                partition,
            })
        })
        .collect()
}

// ---------------------------------------------------------------------------
// scontrol — job detail
// ---------------------------------------------------------------------------

/// The field whose value contains spaces and runs to the end of its line.
const SUBMIT_LINE: &str = "SubmitLine=";

/// Parse `scontrol show job` output into a key/value map.
///
/// Most fields are whitespace-separated `key=value` tokens, but `SubmitLine`
/// holds a value that itself contains spaces and runs to the end of its line.
/// It is captured whole so the full sbatch command survives — otherwise
/// `SubmitLine=sbatch --array=1-4 job.sh` would truncate to `sbatch` and break
/// resubmission.
pub fn scontrol(output: &str) -> BTreeMap<String, String> {
    let mut result = BTreeMap::new();

    for line in output.lines() {
        let mut head = line;
        if let Some(index) = line.find(SUBMIT_LINE) {
            let value = line[index + SUBMIT_LINE.len()..].trim();
            result.insert("SubmitLine".to_string(), value.to_string());
            // Tokens preceding it on the same line still parse normally.
            head = &line[..index];
        }
        for token in head.split_whitespace() {
            if let Some((key, value)) = token.split_once('=') {
                result.insert(key.to_string(), value.to_string());
            }
        }
    }

    result
}

// ---------------------------------------------------------------------------
// sinfo — partitions
// ---------------------------------------------------------------------------

/// Parse `sinfo --summarize --format=%P|%a|%F|%C|%l|%G` into partitions.
///
/// `--summarize` still emits one row per *node configuration*, so a partition
/// with mixed hardware (different memory or GRES) appears several times — the
/// rows are summed here so each partition shows up exactly once. Trailing fields
/// are optional, which keeps the shorter `%P|%a|%F` output working too.
pub fn sinfo(stdout: &str) -> Vec<PartitionInfo> {
    let mut partitions: Vec<PartitionInfo> = Vec::new();

    for line in stdout.trim().lines() {
        let f = fields(line);
        if f.len() < 3 {
            continue;
        }
        // The default partition carries a trailing "*".
        let name = f[0].trim_end_matches('*').to_string();
        let nodes = aiot(f[2]);
        let cpus = if f.len() > 3 {
            aiot(f[3])
        } else {
            Aiot::default()
        };
        let gres = clean(f.get(5).unwrap_or(&"")).to_string();

        match partitions.iter_mut().find(|p| p.name == name) {
            Some(existing) => {
                // Each hardware configuration's GRES is listed once.
                if !gres.is_empty() && !existing.gres.contains(&gres) {
                    if existing.gres.is_empty() {
                        existing.gres = gres;
                    } else {
                        existing.gres = format!("{},{}", existing.gres, gres);
                    }
                }
                existing.nodes.add(nodes);
                existing.cpus.add(cpus);
            }
            None => partitions.push(PartitionInfo {
                name,
                avail: field(&f, 1),
                nodes,
                cpus,
                time_limit: field(&f, 4),
                gres,
                running: 0,
                pending: 0,
            }),
        }
    }

    partitions
}

/// Apply a configured display order, appending anything not named in it.
pub fn order_partitions(parts: Vec<PartitionInfo>, order: &[String]) -> Vec<PartitionInfo> {
    if order.is_empty() {
        return parts;
    }
    let (mut named, rest): (Vec<_>, Vec<_>) =
        parts.into_iter().partition(|p| order.contains(&p.name));
    named.sort_by_key(|p| order.iter().position(|name| *name == p.name));
    named.into_iter().chain(rest).collect()
}

/// Count running/pending jobs per partition from `squeue --format=%P|%T`.
///
/// A pending job may list several partitions (`gpu,gpu-long`); it counts towards
/// each, since it could start on any of them.
pub fn partition_job_counts(stdout: &str) -> BTreeMap<String, (u32, u32)> {
    let mut counts: BTreeMap<String, (u32, u32)> = BTreeMap::new();

    for line in stdout.trim().lines() {
        let f = fields(line);
        if f.len() < 2 {
            continue;
        }
        let state = f[1];
        for name in f[0].split(',') {
            let name = name.trim().trim_end_matches('*');
            if name.is_empty() {
                continue;
            }
            let entry = counts.entry(name.to_string()).or_insert((0, 0));
            match state {
                "RUNNING" => entry.0 += 1,
                "PENDING" => entry.1 += 1,
                _ => {}
            }
        }
    }

    counts
}

// ---------------------------------------------------------------------------
// sinfo -N — nodes
// ---------------------------------------------------------------------------

/// Parse per-node `sinfo` output into one entry per node.
///
/// A node listed in several partitions appears more than once; the first
/// occurrence wins.
pub fn sinfo_nodes(stdout: &str) -> Vec<NodeInfo> {
    let mut nodes: Vec<NodeInfo> = Vec::new();

    for line in stdout.trim().lines() {
        let f = fields(line);
        if f.len() < 3 || f[0].is_empty() {
            continue;
        }
        let name = field(&f, 0);
        if nodes.iter().any(|n| n.name == name) {
            continue;
        }
        nodes.push(NodeInfo {
            name,
            state: field(&f, 1),
            cpus: aiot(f[2]),
            memory_mb: as_int(&field(&f, 3)),
            free_mem_mb: as_int(&field(&f, 4)),
            cpu_load: as_float(&field(&f, 5)),
            gres: clean(f.get(6).unwrap_or(&"")).to_string(),
            gres_used: clean(f.get(7).unwrap_or(&"")).to_string(),
            reason: clean(f.get(8).unwrap_or(&"")).to_string(),
        });
    }

    nodes
}

// ---------------------------------------------------------------------------
// squeue — jobs on a partition or node, across all users
// ---------------------------------------------------------------------------

/// Parse `squeue --format=%i|%u|%j|%T|%M|%l|%D|%C|%b|%R` output.
pub fn partition_jobs(stdout: &str) -> Vec<PartitionJob> {
    stdout
        .trim()
        .lines()
        .filter_map(|line| {
            let f = fields(line);
            if f.len() < 10 {
                return None;
            }
            Some(PartitionJob {
                job_id: field(&f, 0),
                user: field(&f, 1),
                name: field(&f, 2),
                state: field(&f, 3),
                elapsed: field(&f, 4),
                time_limit: field(&f, 5),
                nodes: field(&f, 6),
                cpus: field(&f, 7),
                gres: clean(f[8]).to_string(),
                nodelist: field(&f, 9),
            })
        })
        .collect()
}

// ---------------------------------------------------------------------------
// sreport / sshare — accounting
// ---------------------------------------------------------------------------

/// Parse `sreport cluster AccountUtilizationByUser ... -P` output.
///
/// sreport prints a banner of dashes and a title before the parsable rows, and
/// the column header itself is parsable-looking, so rows are recognised by
/// content rather than position: a header starts with `Cluster`, banners have no
/// separator, and the hours column must be a number.
pub fn sreport(stdout: &str) -> Vec<UsageRow> {
    stdout
        .lines()
        .filter_map(|line| {
            let line = line.trim();
            if line.is_empty() || !line.contains('|') || line.starts_with('-') {
                return None;
            }
            let f = fields(line);
            if f.len() < 5 {
                return None;
            }
            let first = f[0].to_lowercase();
            if first == "cluster" || first == "cluster/account/user" {
                return None;
            }
            // The hours column is the discriminator: a header row fails here.
            let hours = f[4].replace(',', "").parse::<f64>().ok()?;
            Some(UsageRow {
                account: field(&f, 1),
                user: field(&f, 2),
                name: field(&f, 3),
                hours,
            })
        })
        .collect()
}

/// Parse `sshare -P -o Account,User,RawShares,NormShares,RawUsage,EffectvUsage,FairShare`.
pub fn sshare(stdout: &str) -> Vec<FairShare> {
    stdout
        .lines()
        .filter_map(|line| {
            let line = line.trim();
            if line.is_empty() || !line.contains('|') {
                return None;
            }
            let f = fields(line);
            if f.len() < 7 || f[0].to_lowercase() == "account" {
                return None;
            }
            Some(FairShare {
                account: field(&f, 0),
                user: field(&f, 1),
                raw_shares: field(&f, 2),
                norm_shares: as_float(f[3]),
                raw_usage: as_float(f[4]),
                effective_usage: as_float(f[5]),
                // Account rows leave the FairShare column empty.
                fairshare: f[6].parse::<f64>().ok(),
            })
        })
        .collect()
}

/// Pull one job's priority factors, and its rank, out of `sprio` output.
///
/// The command is asked for a whole partition rather than a single job, so the
/// same output yields both the breakdown and the job's position in the queue —
/// one call instead of two.
pub fn sprio(stdout: &str, job_id: &str) -> Option<PriorityInfo> {
    let target = job_id.trim();
    let mut totals: Vec<i64> = Vec::new();
    let mut found: Option<PriorityInfo> = None;

    for line in stdout.trim().lines() {
        let f = fields(line);
        if f.len() < 7 || f[0].is_empty() || f[0].to_uppercase() == "JOBID" {
            continue;
        }
        // sprio may print factors as floats; the UI wants whole numbers.
        let number = |index: usize| -> i64 {
            f.get(index)
                .and_then(|v| v.parse::<f64>().ok())
                .map(|n| n as i64)
                .unwrap_or(0)
        };

        totals.push(number(1));
        if f[0] == target {
            found = Some(PriorityInfo {
                job_id: field(&f, 0),
                total: number(1),
                age: number(2),
                fairshare: number(3),
                job_size: number(4),
                partition: number(5),
                qos: number(6),
                rank: 0,
                queued: 0,
            });
        }
    }

    let mut info = found?;
    info.queued = totals.len() as u32;
    info.rank = totals.iter().filter(|total| **total > info.total).count() as u32 + 1;
    Some(info)
}

// ---------------------------------------------------------------------------
// sstat / sacct — resource usage
// ---------------------------------------------------------------------------

/// Parse `sstat` output into live counters for a running job.
pub fn sstat(stdout: &str, job_id: &str) -> Option<JobStats> {
    let line = stdout.trim().lines().next()?;
    let f = fields(line);
    if f.len() < 12 {
        return None;
    }

    let value = |index: usize| -> String {
        let text = field(&f, index);
        if text.is_empty() {
            crate::model::job::NOT_AVAILABLE.to_string()
        } else {
            text
        }
    };

    let mut stats = JobStats::empty(job_id, StatsSource::Sstat);
    stats.ave_cpu = value(0);
    stats.ave_cpu_freq = value(1);
    stats.ave_rss = value(2);
    stats.max_rss = value(3);
    stats.ave_vm_size = value(4);
    stats.max_vm_size = value(5);
    stats.ave_disk_read = value(6);
    stats.ave_disk_write = value(7);
    stats.max_disk_read = value(8);
    stats.max_disk_write = value(9);
    stats.max_rss_node = value(10);
    stats.max_rss_task = value(11);
    Some(stats)
}

/// Fold sacct's job row and its step rows into one set of numbers.
///
/// sacct splits what the efficiency report needs across rows: the job row
/// carries the request (ReqMem, Timelimit, AllocCPUS) but no MaxRSS, while each
/// step row carries a MaxRSS and nothing about the request. The peak across
/// steps is the job's memory high-water mark, which is what `seff` reports and
/// what `.batch` alone would understate.
pub fn sacct_stats(stdout: &str) -> Option<BTreeMap<String, String>> {
    const KEYS: [&str; 9] = [
        "TotalCPU",
        "Elapsed",
        "ReqMem",
        "AllocTRES",
        "ReqTRES",
        "AllocCPUS",
        "NNodes",
        "NTasks",
        "Timelimit",
    ];

    let mut result: BTreeMap<String, String> = BTreeMap::new();
    let mut peak_rss = 0.0_f64;
    let mut peak_text = String::new();

    for line in stdout.trim().lines() {
        let f = fields(line);
        if f.len() < 11 || f[0].is_empty() || f[0].to_uppercase() == "JOBID" {
            continue;
        }
        let is_job_row = !f[0].contains('.');

        if is_job_row && result.is_empty() {
            for (offset, key) in KEYS.iter().enumerate() {
                result.insert((*key).to_string(), field(&f, offset + 1));
            }
        }

        if let Some(size) = crate::model::parse_mem_bytes(f[10]) {
            if size > peak_rss {
                peak_rss = size;
                peak_text = field(&f, 10);
            }
        }
    }

    if result.is_empty() {
        return None;
    }
    if !peak_text.is_empty() {
        result.insert("MaxRSS".to_string(), peak_text);
    }
    Some(result)
}

// ---------------------------------------------------------------------------
// Node specs
// ---------------------------------------------------------------------------

/// Extract the first node name from a Slurm node specification.
///
/// Handles `node001`, `node[001-003]` and `node001,node002`. Used to pick which
/// node to SSH into for a multi-node job.
pub fn first_node(node_spec: &str) -> String {
    if node_spec.contains(',') && !node_spec.contains('[') {
        return node_spec.split(',').next().unwrap_or_default().to_string();
    }
    if let Some((prefix, rest)) = node_spec.split_once('[') {
        let inside = rest.trim_end_matches(']');
        let first_range = inside.split(',').next().unwrap_or_default();
        let first_num = first_range.split('-').next().unwrap_or_default();
        return format!("{prefix}{first_num}");
    }
    node_spec.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use rstest::rstest;

    // Real-world shape: --summarize still emits one row per node
    // *configuration*, so a partition with mixed hardware appears more than once.
    const SINFO_OUT: &str = concat!(
        "a100*|up|19/1/1/21|778/502/64/1344|3-00:00:00|gpu:a100:8\n",
        "a100|up|3/0/0/3|366/402/0/768|3-00:00:00|gpu:a100:9\n",
        "cpu|up|2/0/0/2|13/51/0/64|30-00:00:00|(null)\n",
        "maint|down|0/0/4/4|0/0/256/256|1:00:00|(null)",
    );

    const SINFO_NODES: &str = concat!(
        "gpu-node01|mixed|58/6/0/64|948865|324779|16.02|gpu:a100:8(S:0-1)|gpu:a100:5(IDX:0-4)|none|\n",
        "gpu-node02|allocated|64/0/0/64|948863|499782|11.52|gpu:a100:8(S:0-1)|gpu:a100:8(IDX:0-7)|none|\n",
        "gpu-node03|drained*|0/0/64/64|948863|1025717|0.01|gpu:a100:8(S:0-1)|gpu:a100:0(IDX:N/A)|kernel patch|\n",
        "gpu-node01|mixed|58/6/0/64|948865|324779|16.02|gpu:a100:8(S:0-1)|gpu:a100:5(IDX:0-4)|none|",
    );

    #[rstest]
    #[case("node001", "node001")]
    #[case("node001,node002", "node001")]
    #[case("node[001-003]", "node001")]
    #[case("node[005,007]", "node005")]
    #[case("gpu[01-04],gpu[10]", "gpu01")]
    #[case("", "")]
    fn finds_first_node(#[case] spec: &str, #[case] expected: &str) {
        assert_eq!(first_node(spec), expected);
    }

    #[test]
    fn parses_scontrol_key_values() {
        let parsed = scontrol(concat!(
            "JobId=123 JobName=train UserId=me(1000)\n",
            "   StdOut=/work/slurm-123.out StdErr=/work/slurm-123.err\n",
            "   WorkDir=/work Command=/work/run.sh\n",
        ));
        assert_eq!(parsed["JobId"], "123");
        assert_eq!(parsed["JobName"], "train");
        assert_eq!(parsed["StdOut"], "/work/slurm-123.out");
        assert_eq!(parsed["Command"], "/work/run.sh");
    }

    #[test]
    fn ignores_tokens_without_equals() {
        assert!(scontrol("plain text no equals here").is_empty());
    }

    #[test]
    fn captures_submit_line_with_spaces() {
        // Regression: SubmitLine holds a space-separated command that runs to
        // the end of the line. It must be captured whole, not truncated.
        let parsed = scontrol(concat!(
            "JobId=123 JobName=train WorkDir=/work Command=/work/job.sh\n",
            "   SubmitLine=sbatch --array=1-4 --time=1:00:00 job.sh\n",
        ));
        assert_eq!(parsed["SubmitLine"], "sbatch --array=1-4 --time=1:00:00 job.sh");
        assert_eq!(parsed["Command"], "/work/job.sh");
        assert_eq!(parsed["JobId"], "123");
    }

    #[test]
    fn parses_squeue_rows() {
        let jobs = squeue_jobs(concat!(
            "101|jobA|1:00|gpu|RUNNING|2:00|1|4|8G|gpu:1|/work/a\n",
            "205|jobB|0:10|cpu|PENDING|1:00|1|2|4G|None|/work/b",
        ));
        assert_eq!(jobs.len(), 2);
        assert_eq!(jobs[0].name, "jobA");
        assert_eq!(jobs[0].gres, "gpu:1");
        assert_eq!(jobs[0].work_dir, "/work/a");
    }

    #[test]
    fn squeue_reports_missing_gres_as_none() {
        let jobs = squeue_jobs("101|j|1:00|cpu|RUNNING|2:00|1|4|8G||/work");
        assert_eq!(jobs[0].gres, "None");
    }

    #[test]
    fn sacct_drops_substeps_and_unfinished_jobs() {
        let jobs = sacct_jobs(
            concat!(
                "300|jobX|COMPLETED|0:0|s|e|1:00|gpu\n",
                "300.batch|batch|COMPLETED|0:0|s|e|1:00|gpu\n",
                "301|jobY|RUNNING|0:0|s|e|0:30|gpu\n",
                "302|jobZ|FAILED|1:0|s|e|0:05|cpu",
            ),
            "",
        );
        let ids: Vec<&str> = jobs.iter().map(|j| j.job_id.as_str()).collect();
        assert_eq!(ids, vec!["300", "302"]);
    }

    #[test]
    fn sacct_honours_the_partition_filter() {
        let jobs = sacct_jobs(
            concat!(
                "300|jobX|COMPLETED|0:0|s|e|1:00|gpu\n",
                "302|jobZ|FAILED|1:0|s|e|0:05|cpu",
            ),
            "cpu",
        );
        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0].job_id, "302");
    }

    #[test]
    fn aggregates_sinfo_rows_per_partition() {
        let parts = sinfo(SINFO_OUT);
        let names: Vec<&str> = parts.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["a100", "cpu", "maint"]); // trailing "*" stripped

        let a100 = &parts[0];
        assert_eq!(a100.nodes.display(), "22/1/1/24");
        assert_eq!(a100.cpus.display(), "1144/904/64/2112");
        assert_eq!(a100.time_limit, "3-00:00:00");
        assert_eq!(a100.gres, "gpu:a100:8,gpu:a100:9"); // both configs listed
        assert_eq!(parts[1].gres, ""); // "(null)" normalised away
        assert_eq!(parts[2].avail, "down");
    }

    #[test]
    fn tolerates_the_short_sinfo_format() {
        // The cluster bar's older 3-field format must still parse.
        let parts = sinfo("gpu|up|10/5/0/15");
        assert_eq!(parts.len(), 1);
        assert_eq!(parts[0].nodes.display(), "10/5/0/15");
        assert_eq!(parts[0].cpus.display(), "0/0/0/0");
    }

    #[test]
    fn partition_load_excludes_drained_cpus() {
        // 100 allocated of 200 usable — the 800 "other" CPUs are not counted.
        let parts = sinfo("gpu|up|1/0/1/2|100/100/800/1000");
        assert!((parts[0].load() - 0.5).abs() < f64::EPSILON);
    }

    #[test]
    fn orders_partitions_then_appends_the_rest() {
        let parts = sinfo(SINFO_OUT);
        let order = vec!["cpu".to_string(), "a100".to_string()];
        let names: Vec<String> = order_partitions(parts, &order)
            .into_iter()
            .map(|p| p.name)
            .collect();
        assert_eq!(names, vec!["cpu", "a100", "maint"]);
    }

    #[test]
    fn counts_a_multi_partition_pending_job_towards_each() {
        let counts = partition_job_counts(concat!(
            "gpu|RUNNING\n",
            "gpu|RUNNING\n",
            "gpu,gpu-long|PENDING\n",
            "cpu|COMPLETING",
        ));
        assert_eq!(counts["gpu"], (2, 1));
        assert_eq!(counts["gpu-long"], (0, 1));
        assert_eq!(counts["cpu"], (0, 0));
    }

    #[test]
    fn parses_partition_jobs() {
        let jobs = partition_jobs(concat!(
            "2735316|rvy895|train|RUNNING|14:28:36|2-06:00:00|1|8|gres/gpu:1|galvani-cn059\n",
            "2735270|pba175|vsv100|PENDING|0:00|3-00:00:00|1|8|N/A|(Dependency)",
        ));
        assert_eq!(jobs[0].user, "rvy895");
        assert_eq!(jobs[0].gres, "gres/gpu:1");
        assert_eq!(jobs[1].gres, ""); // "N/A" normalised away
        assert_eq!(jobs[1].nodelist, "(Dependency)"); // pending reason
    }

    #[test]
    fn parses_and_dedupes_nodes() {
        let nodes = sinfo_nodes(SINFO_NODES);
        let names: Vec<&str> = nodes.iter().map(|n| n.name.as_str()).collect();
        assert_eq!(names, vec!["gpu-node01", "gpu-node02", "gpu-node03"]);

        let first = &nodes[0];
        assert_eq!(first.state, "mixed");
        assert_eq!(first.cpus.display(), "58/6/0/64");
        assert!((first.cpu_load - 16.02).abs() < 1e-9); // float, not truncated
        assert_eq!(first.mem_used_mb(), 948_865 - 324_779);
        assert_eq!(
            (first.gpus_used(), first.gpus_total(), first.gpus_free()),
            (5, 8, 3)
        );
        assert_eq!(first.reason, ""); // "none" normalised away

        let drained = &nodes[2];
        assert_eq!(drained.base_state(), "drained"); // trailing flag stripped
        assert!(drained.is_unresponsive());
        assert_eq!(drained.reason, "kernel patch");
        assert_eq!(drained.gpus_used(), 0);
    }

    #[test]
    fn parses_the_short_node_fallback_format() {
        // The %-format fallback has an empty GresUsed column.
        let nodes = sinfo_nodes("cpu-node01|idle|0/64/0/64|128000|127000|0.05|(null)||none");
        assert_eq!(nodes.len(), 1);
        assert_eq!(nodes[0].base_state(), "idle");
        assert_eq!(nodes[0].gres, "");
        assert_eq!(nodes[0].gpus_total(), 0);
        assert_eq!(nodes[0].cpus.idle, 64);
    }

    #[test]
    fn parses_sreport_skipping_banners_and_headers() {
        let rows = sreport(concat!(
            "--------------------------------------------------\n",
            "Cluster/Account/User Utilization 2026-01-01 - 2026-01-31\n",
            "Cluster|Account|Login|Proper Name|Used|Energy|\n",
            "galvani|physics||| 68364|0|\n",
            "galvani|physics|rvy895|Robin U| 2472|0|\n",
            "galvani|physics|pba175|Pat B| 1,234|0|",
        ));
        assert_eq!(rows.len(), 3);
        assert!(rows[0].is_account_total());
        assert_eq!(rows[0].hours, 68_364.0);
        assert_eq!(rows[1].user, "rvy895");
        assert_eq!(rows[2].hours, 1234.0); // thousands separator stripped
    }

    #[test]
    fn parses_sshare_with_empty_account_factors() {
        let shares = sshare(concat!(
            "Account|User|RawShares|NormShares|RawUsage|EffectvUsage|FairShare|\n",
            "physics||100|0.250000|123456|0.300000||\n",
            "physics|rvy895|parent|0.050000|12345|0.030000|0.712500|",
        ));
        assert_eq!(shares.len(), 2);
        assert_eq!(shares[0].fairshare, None); // account row leaves it empty
        assert_eq!(shares[1].user, "rvy895");
        assert_eq!(shares[1].fairshare, Some(0.7125));
        assert_eq!(shares[1].raw_shares, "parent");
    }

    #[test]
    fn derives_priority_rank_from_the_partition_queue() {
        let info = sprio(
            concat!(
                "JOBID|PRIORITY|AGE|FAIRSHARE|JOBSIZE|PARTITION|QOS\n",
                "100|5000|100|4000|10|500|0\n",
                "200|3000|50|2500|10|400|0\n",
                "300|1000|10|500|10|400|0",
            ),
            "200",
        )
        .expect("job 200 is in the output");

        assert_eq!(info.total, 3000);
        assert_eq!(info.fairshare, 2500);
        assert_eq!(info.queued, 3);
        assert_eq!(info.rank, 2); // one job outranks it
        assert_eq!(info.ahead(), 1);
    }

    #[test]
    fn returns_no_priority_when_the_job_is_absent() {
        assert!(sprio("100|5000|100|4000|10|500|0", "999").is_none());
    }

    #[test]
    fn folds_sacct_job_and_step_rows() {
        // The job row has the request; the step rows have the memory peaks.
        let stats = sacct_stats(concat!(
            "123|02:00:00|01:00:00|16Gn|cpu=8,gres/gpu=2|cpu=8|8|1|1|04:00:00|\n",
            "123.batch|01:00:00|01:00:00|||||1|1||2.5G\n",
            "123.0|01:00:00|01:00:00|||||1|1||7.5G",
        ))
        .expect("a job row is present");

        assert_eq!(stats["ReqMem"], "16Gn");
        assert_eq!(stats["Timelimit"], "04:00:00");
        assert_eq!(stats["AllocCPUS"], "8");
        // The peak across steps, not the .batch row alone.
        assert_eq!(stats["MaxRSS"], "7.5G");
    }

    #[test]
    fn returns_no_stats_without_a_job_row() {
        assert!(sacct_stats("123.batch|01:00:00|01:00:00|||||1|1||2.5G").is_none());
    }

    #[test]
    fn parses_sstat_counters() {
        let stats = sstat(
            "00:10:00|2.5GHz|1.2G|2.4G|8G|9G|100M|200M|150M|250M|node01|0",
            "123",
        )
        .expect("twelve fields are present");
        assert_eq!(stats.max_rss, "2.4G");
        assert_eq!(stats.max_rss_node, "node01");
        assert_eq!(stats.source, StatsSource::Sstat);
    }

    #[test]
    fn rejects_short_sstat_rows() {
        assert!(sstat("00:10:00|2.5GHz", "123").is_none());
    }
}
