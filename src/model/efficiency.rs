//! How much of what a job asked for it actually used.
//!
//! The definitions match `seff`, deliberately: users compare the two, and a
//! LazySlurm number that disagrees with `seff` reads as a bug even when the
//! alternative definition is more defensible.

use super::format::{parse_duration, parse_mem_bytes, parse_req_mem};
use super::job::JobStats;

/// Below this fraction of the request, a resource counts as over-provisioned
/// and earns a sizing hint.
const OVER_REQUESTED: f64 = 0.5;

/// Headroom multiplier applied to actual usage when suggesting a new request.
const HEADROOM: f64 = 1.3;

/// Efficiency ratios for one job.
///
/// Every ratio is a fraction of 1.0, or `None` when Slurm did not record the
/// numbers needed for it — a job too old for sacct, a step that never ran, or a
/// partition with no default time limit. `None` and `0.0` mean different things
/// and must not be conflated: the first is "unknown", the second is "used none".
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Efficiency {
    pub cpu: Option<f64>,
    pub memory: Option<f64>,
    pub walltime: Option<f64>,

    /// Core-equivalents actually busy.
    pub cpu_used: f64,
    pub cpu_alloc: u32,
    /// Bytes, peak of any one task.
    pub mem_used: f64,
    /// Bytes, per node.
    pub mem_request: f64,
    /// Seconds.
    pub elapsed: f64,
    /// Seconds.
    pub time_limit: f64,
    pub gpus: u32,
    pub nnodes: u32,
}

impl Efficiency {
    /// Whether any ratio could be computed at all.
    pub fn has_any(&self) -> bool {
        self.cpu.is_some() || self.memory.is_some() || self.walltime.is_some()
    }

    /// Peak memory at or above the request — the next run may be killed.
    pub fn oom_risk(&self) -> bool {
        self.memory.is_some_and(|ratio| ratio >= 1.0)
    }
}

/// Derive CPU / memory / walltime efficiency from a [`JobStats`].
///
/// CPU is `TotalCPU / (cores × elapsed)`, the same definition `seff` uses.
/// Memory compares the peak RSS of one task against the request *per node*, so a
/// multi-node job is not credited with memory it never touched on one node.
pub fn compute_efficiency(stats: &JobStats) -> Efficiency {
    let nnodes = stats.nnodes.max(1);
    let mut eff = Efficiency {
        cpu_alloc: stats.alloc_cpus,
        nnodes,
        gpus: stats.gpu_count(),
        ..Efficiency::default()
    };

    let elapsed = parse_duration(&stats.elapsed);
    let total_cpu = parse_duration(&stats.total_cpu);
    let limit = parse_duration(&stats.time_limit);
    let used_mem = parse_mem_bytes(&stats.max_rss);
    let request = parse_req_mem(&stats.req_mem, stats.alloc_cpus, stats.nnodes);

    // A zero elapsed time makes every rate undefined, so treat it as missing.
    if let Some(elapsed) = elapsed.filter(|e| *e > 0.0) {
        eff.elapsed = elapsed;
        if let Some(total_cpu) = total_cpu.filter(|_| stats.alloc_cpus > 0) {
            eff.cpu_used = total_cpu / elapsed;
            eff.cpu = Some(total_cpu / (elapsed * f64::from(stats.alloc_cpus)));
        }
        if let Some(limit) = limit.filter(|l| *l > 0.0) {
            eff.time_limit = limit;
            eff.walltime = Some(elapsed / limit);
        }
    }

    if let (Some(used), Some(request)) = (used_mem, request.filter(|r| *r > 0.0)) {
        let per_node = request / f64::from(nnodes);
        eff.mem_used = used;
        eff.mem_request = per_node;
        if per_node > 0.0 {
            eff.memory = Some(used / per_node);
        }
    }

    eff
}

/// What to ask for next time, when the job was clearly over-provisioned.
///
/// Suggests roughly a third more than the job actually used, so a rerun has
/// headroom. Empty when the numbers are missing or the request was reasonable —
/// a hint on a well-sized job is noise, and users stop reading them.
pub fn sizing_hint(eff: &Efficiency) -> String {
    let mut suggestions: Vec<String> = Vec::new();

    if let Some(ratio) = eff.memory {
        if ratio < OVER_REQUESTED && eff.mem_used > 0.0 {
            suggestions.push(memory_suggestion(eff.mem_used * HEADROOM));
        }
    }

    if let Some(ratio) = eff.cpu {
        if ratio < OVER_REQUESTED && eff.cpu_alloc > 1 {
            let cores = (eff.cpu_used * HEADROOM).ceil().max(1.0) as u32;
            if cores < eff.cpu_alloc {
                suggestions.push(format!("--cpus-per-task={cores}"));
            }
        }
    }

    if let Some(ratio) = eff.walltime {
        if ratio < OVER_REQUESTED && eff.time_limit > 0.0 {
            let target = (eff.elapsed * 1.5) as i64;
            let (hours, rest) = (target / 3600, target % 3600);
            suggestions.push(format!("--time={:02}:{:02}:00", hours, rest / 60));
        }
    }

    if suggestions.is_empty() {
        String::new()
    } else {
        format!("next time try {}", suggestions.join(" "))
    }
}

/// A `--mem=` suggestion: whole GiB above 1 GiB, 256 MB steps below it.
fn memory_suggestion(target_bytes: f64) -> String {
    const GIB: f64 = 1024.0 * 1024.0 * 1024.0;
    const MIB: f64 = 1024.0 * 1024.0;
    const STEP_MB: u64 = 256;

    let gib = target_bytes / GIB;
    if gib >= 1.0 {
        format!("--mem={}G", (gib.ceil() as u64).max(1))
    } else {
        let mb = (target_bytes / MIB).ceil() as u64;
        // Round up to the next 256 MB so the suggestion is a round number.
        let rounded = mb.div_ceil(STEP_MB) * STEP_MB;
        format!("--mem={}M", rounded.max(STEP_MB))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::job::StatsSource;

    fn stats(build: impl FnOnce(&mut JobStats)) -> JobStats {
        let mut stats = JobStats::empty("1", StatsSource::Combined);
        build(&mut stats);
        stats
    }

    #[test]
    fn computes_cpu_efficiency_like_seff() {
        // 4 cores for 1 hour = 4 core-hours available; 2 core-hours used = 50%.
        let stats = stats(|s| {
            s.elapsed = "01:00:00".into();
            s.total_cpu = "02:00:00".into();
            s.alloc_cpus = 4;
        });
        let eff = compute_efficiency(&stats);
        assert_eq!(eff.cpu, Some(0.5));
        assert_eq!(eff.cpu_used, 2.0);
    }

    #[test]
    fn memory_ratio_is_per_node() {
        // 64G requested per node across 2 nodes; peak 32G on one node = 50%.
        let stats = stats(|s| {
            s.max_rss = "32G".into();
            s.req_mem = "64Gn".into();
            s.nnodes = 2;
            s.alloc_cpus = 8;
        });
        let eff = compute_efficiency(&stats);
        assert_eq!(eff.memory, Some(0.5));
    }

    #[test]
    fn missing_data_yields_none_not_zero() {
        let eff = compute_efficiency(&stats(|_| {}));
        assert_eq!(eff.cpu, None);
        assert_eq!(eff.memory, None);
        assert_eq!(eff.walltime, None);
        assert!(!eff.has_any());
    }

    #[test]
    fn flags_oom_risk_at_or_above_the_request() {
        let stats = stats(|s| {
            s.max_rss = "16G".into();
            s.req_mem = "16G".into();
            s.nnodes = 1;
        });
        assert!(compute_efficiency(&stats).oom_risk());
    }

    #[test]
    fn walltime_uses_the_time_limit_as_denominator() {
        let stats = stats(|s| {
            s.elapsed = "01:00:00".into();
            s.time_limit = "04:00:00".into();
        });
        assert_eq!(compute_efficiency(&stats).walltime, Some(0.25));
    }

    #[test]
    fn unlimited_time_limit_is_not_a_denominator() {
        let stats = stats(|s| {
            s.elapsed = "01:00:00".into();
            s.time_limit = "UNLIMITED".into();
        });
        assert_eq!(compute_efficiency(&stats).walltime, None);
    }

    #[test]
    fn suggests_smaller_request_when_over_provisioned() {
        let eff = Efficiency {
            memory: Some(0.1),
            mem_used: 2.0 * 1024.0 * 1024.0 * 1024.0,
            cpu: Some(0.2),
            cpu_used: 1.0,
            cpu_alloc: 16,
            walltime: Some(0.1),
            elapsed: 3600.0,
            time_limit: 36_000.0,
            ..Efficiency::default()
        };
        let hint = sizing_hint(&eff);
        assert!(hint.contains("--mem=3G"), "{hint}");
        assert!(hint.contains("--cpus-per-task=2"), "{hint}");
        assert!(hint.contains("--time=01:30:00"), "{hint}");
    }

    #[test]
    fn stays_quiet_when_the_request_was_reasonable() {
        let eff = Efficiency {
            memory: Some(0.8),
            mem_used: 1.0,
            cpu: Some(0.9),
            cpu_alloc: 4,
            walltime: Some(0.7),
            ..Efficiency::default()
        };
        assert_eq!(sizing_hint(&eff), "");
    }

    #[test]
    fn small_memory_suggestions_round_to_256mb() {
        let eff = Efficiency {
            memory: Some(0.1),
            mem_used: 300.0 * 1024.0 * 1024.0,
            ..Efficiency::default()
        };
        assert_eq!(sizing_hint(&eff), "next time try --mem=512M");
    }
}
