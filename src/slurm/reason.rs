//! Explaining, in words, why a job is not running yet.
//!
//! Slurm answers "why is my job pending?" with a reason code and a backfill
//! estimate. Both are terse to the point of being useless to anyone who has not
//! memorised them, so this module turns them into sentences.

use std::collections::BTreeMap;

use chrono::{Local, NaiveDateTime};

use crate::model::PriorityInfo;

/// Reason codes matched exactly.
const EXACT: &[(&str, &str)] = &[
    ("Resources", "waiting for enough free nodes to become available"),
    ("Priority", "other jobs are ahead of it in the queue"),
    ("Dependency", "waiting for another job to finish"),
    (
        "DependencyNeverSatisfied",
        "its dependency can never be satisfied — cancel it",
    ),
    ("BeginTime", "held until its requested start time"),
    (
        "JobHeldUser",
        "held by you — release it with `scontrol release`",
    ),
    ("JobHeldAdmin", "held by an administrator"),
    ("Licenses", "waiting for a software license"),
    (
        "ReqNodeNotAvail",
        "the nodes it asked for are down, drained or reserved",
    ),
    ("PartitionDown", "its partition is down"),
    ("PartitionInactive", "its partition is inactive"),
    (
        "PartitionNodeLimit",
        "it asks for more nodes than the partition allows",
    ),
    (
        "PartitionTimeLimit",
        "its time limit is longer than the partition allows",
    ),
    ("NodeDown", "a node it needs is down"),
    (
        "Cleaning",
        "a previous job is still being cleaned up on its nodes",
    ),
    ("None", "it should start shortly"),
];

/// Reason codes matched by prefix, which covers the whole `QOSMax*`/`AssocMax*`
/// family without listing every variant.
///
/// Order matters: the more specific prefix must come first, or `AssocMaxJobs`
/// would be swallowed by `AssocMax`.
const PREFIXES: &[(&str, &str)] = &[
    ("QOSMaxGRES", "you are at your QOS limit for GPUs"),
    ("QOSMaxCpu", "you are at your QOS limit for CPUs"),
    ("QOSMaxNode", "you are at your QOS limit for nodes"),
    ("QOSMaxMem", "you are at your QOS limit for memory"),
    ("QOSMaxJobs", "you are at your QOS limit for running jobs"),
    ("QOSMaxSubmit", "you are at your QOS limit for submitted jobs"),
    ("QOSMaxWall", "its time limit is longer than the QOS allows"),
    ("QOSGrp", "your QOS group is at its resource limit"),
    ("QOSMin", "it asks for less than the QOS minimum"),
    ("QOSNotAllowed", "that QOS is not allowed on this partition"),
    ("QOSResourceLimit", "the QOS resource limit is reached"),
    (
        "AssocMaxJobs",
        "you are at your account's limit for running jobs",
    ),
    (
        "AssocMaxWall",
        "its time limit is longer than your account allows",
    ),
    ("AssocGrp", "your account group is at its resource limit"),
    ("AssocMax", "you are at an account resource limit"),
    (
        "ReqNodeNotAvail",
        "the nodes it asked for are down, drained or reserved",
    ),
    ("Reservation", "waiting for its reservation to begin"),
];

/// Values that mean Slurm reported no reason at all.
const NO_REASON: &[&str] = &["", "N/A", "none"];

/// Values that mean Slurm could not estimate a start time.
const NO_ESTIMATE: &[&str] = &["", "Unknown", "N/A", "None", "(null)"];

/// Say why a job is pending, in a sentence.
///
/// Falls back to the raw code when Slurm reports something unrecognised — better
/// a code than a confidently wrong explanation.
pub fn explain(
    reason: &str,
    raw: Option<&BTreeMap<String, String>>,
    priority: Option<&PriorityInfo>,
) -> String {
    // Slurm appends detail in parentheses: "Resources(something)".
    let code = reason.trim().split('(').next().unwrap_or_default().trim();
    if NO_REASON.contains(&code) {
        return "no reason reported".to_string();
    }

    let text = EXACT
        .iter()
        .find(|(key, _)| *key == code)
        .or_else(|| PREFIXES.iter().find(|(prefix, _)| code.starts_with(prefix)))
        .map(|(_, text)| *text);

    let Some(text) = text else {
        return format!("Slurm says: {code}");
    };

    // Fill in the specifics Slurm hands us elsewhere.
    if code == "Priority" {
        if let Some(ahead) = priority.map(PriorityInfo::ahead).filter(|a| *a > 0) {
            let plural = if ahead == 1 { "job" } else { "jobs" };
            return format!("{ahead} {plural} ahead of it in the queue");
        }
    } else if code.starts_with("Dependency") {
        if let Some(dependency) = raw
            .and_then(|raw| raw.get("Dependency"))
            .map(|d| d.trim())
            .filter(|d| !d.is_empty() && *d != "(null)" && *d != "None")
        {
            return format!("waiting on {dependency}");
        }
    }

    text.to_string()
}

/// Turn scontrol's `StartTime` into `~14:20 (in 2h10m)`.
///
/// Slurm fills `StartTime` for a pending job with its backfill estimate, so no
/// extra `squeue --start` call is needed. It says `Unknown` when it cannot
/// estimate — usually because the job is blocked rather than merely queued.
pub fn format_start_estimate(raw: &str, now: NaiveDateTime) -> String {
    let value = raw.trim();
    if NO_ESTIMATE.contains(&value) {
        return "not estimated yet — Slurm cannot schedule it while it is blocked".to_string();
    }

    let Ok(start) = NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S") else {
        // Anything we cannot read is shown as Slurm wrote it.
        return value.to_string();
    };

    let when = if start.date() == now.date() {
        start.format("%H:%M").to_string()
    } else {
        start.format("%b %d %H:%M").to_string()
    };

    let seconds = (start - now).num_seconds();
    if seconds <= 0 {
        format!("~{when} (due now)")
    } else {
        format!("~{when} (in {})", humanize(seconds))
    }
}

/// The current local time, as the start-estimate formatter wants it.
pub fn now() -> NaiveDateTime {
    Local::now().naive_local()
}

/// `43870` → `12h11m`, `300` → `5m`, `200000` → `2d7h`.
///
/// Two units at most: an estimate accurate to the second would be false
/// precision, since it is a backfill guess that moves every poll.
fn humanize(seconds: i64) -> String {
    let (days, rest) = (seconds / 86_400, seconds % 86_400);
    let (hours, rest) = (rest / 3600, rest % 3600);
    let minutes = rest / 60;

    if days > 0 {
        if hours > 0 {
            format!("{days}d{hours}h")
        } else {
            format!("{days}d")
        }
    } else if hours > 0 {
        if minutes > 0 {
            format!("{hours}h{minutes:02}m")
        } else {
            format!("{hours}h")
        }
    } else if minutes > 0 {
        format!("{minutes}m")
    } else {
        "<1m".to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rstest::rstest;

    fn now_fixture() -> NaiveDateTime {
        NaiveDateTime::parse_from_str("2026-08-12T12:10:00", "%Y-%m-%dT%H:%M:%S").unwrap()
    }

    #[rstest]
    #[case("2026-08-12T21:20:09", "~21:20 (in 9h10m)")]
    #[case("2026-08-12T12:40:00", "~12:40 (in 30m)")]
    #[case("2026-08-13T09:05:00", "~Aug 13 09:05 (in 20h55m)")]
    #[case("2026-08-15T09:05:00", "~Aug 15 09:05 (in 2d20h)")]
    #[case("2026-08-12T12:10:30", "~12:10 (in <1m)")]
    // An estimate that has already passed.
    #[case("2026-08-12T12:09:00", "~12:09 (due now)")]
    fn formats_start_estimate(#[case] raw: &str, #[case] expected: &str) {
        assert_eq!(format_start_estimate(raw, now_fixture()), expected);
    }

    #[rstest]
    #[case("Unknown")]
    #[case("N/A")]
    #[case("")]
    #[case("   ")]
    #[case("(null)")]
    #[case("None")]
    fn reports_when_slurm_cannot_estimate(#[case] raw: &str) {
        assert!(format_start_estimate(raw, now_fixture()).contains("not estimated"));
    }

    #[test]
    fn passes_unparseable_start_times_through() {
        assert_eq!(format_start_estimate("sometime", now_fixture()), "sometime");
    }

    #[rstest]
    #[case("Resources", "free nodes")]
    #[case("Priority", "ahead of it")]
    #[case("QOSMaxGRESPerUser", "QOS limit for GPUs")]
    #[case("QOSMaxCpuPerUserLimit", "QOS limit for CPUs")]
    #[case("QOSMaxWallDurationPerJobLimit", "longer than the QOS allows")]
    #[case("AssocGrpCpuLimit", "account group")]
    #[case("AssocMaxJobsLimit", "account's limit")]
    #[case("BeginTime", "requested start time")]
    #[case("JobHeldUser", "held by you")]
    #[case("ReqNodeNotAvail", "down, drained or reserved")]
    #[case("PartitionTimeLimit", "longer than the partition")]
    #[case("DependencyNeverSatisfied", "never be satisfied")]
    #[case("Reservation", "reservation")]
    fn speaks_plainly(#[case] code: &str, #[case] fragment: &str) {
        let explanation = explain(code, None, None);
        assert!(
            explanation.contains(fragment),
            "{code} explained as {explanation:?}, expected to contain {fragment:?}"
        );
    }

    #[test]
    fn counts_the_jobs_ahead() {
        let priority = PriorityInfo {
            rank: 8,
            queued: 40,
            ..PriorityInfo::default()
        };
        assert_eq!(
            explain("Priority", None, Some(&priority)),
            "7 jobs ahead of it in the queue"
        );
    }

    #[test]
    fn uses_the_singular_for_one_job_ahead() {
        let priority = PriorityInfo {
            rank: 2,
            queued: 10,
            ..PriorityInfo::default()
        };
        assert!(explain("Priority", None, Some(&priority)).contains("1 job ahead"));
    }

    #[test]
    fn falls_back_when_nothing_is_ahead() {
        let priority = PriorityInfo {
            rank: 1,
            queued: 5,
            ..PriorityInfo::default()
        };
        assert_eq!(
            explain("Priority", None, Some(&priority)),
            "other jobs are ahead of it in the queue"
        );
    }

    #[test]
    fn names_the_dependency() {
        let raw = BTreeMap::from([(
            "Dependency".to_string(),
            "afterok:4815162(unfulfilled)".to_string(),
        )]);
        assert_eq!(
            explain("Dependency", Some(&raw), None),
            "waiting on afterok:4815162(unfulfilled)"
        );
    }

    #[test]
    fn ignores_a_null_dependency() {
        let raw = BTreeMap::from([("Dependency".to_string(), "(null)".to_string())]);
        assert_eq!(
            explain("Dependency", Some(&raw), None),
            "waiting for another job to finish"
        );
    }

    #[test]
    fn keeps_unknown_codes_verbatim() {
        assert_eq!(
            explain("SomeFutureCode", None, None),
            "Slurm says: SomeFutureCode"
        );
    }

    #[rstest]
    #[case("")]
    #[case("N/A")]
    #[case("none")]
    fn reports_nothing_to_explain(#[case] code: &str) {
        assert_eq!(explain(code, None, None), "no reason reported");
    }

    #[test]
    fn strips_slurm_parentheses() {
        assert!(explain("Resources(something)", None, None).contains("free nodes"));
    }

    #[rstest]
    #[case(43_870, "12h11m")]
    #[case(300, "5m")]
    #[case(200_000, "2d7h")]
    #[case(3600, "1h")]
    #[case(30, "<1m")]
    #[case(172_800, "2d")]
    fn humanizes_durations(#[case] seconds: i64, #[case] expected: &str) {
        assert_eq!(humanize(seconds), expected);
    }
}
