//! Remembering where a job's logs were.
//!
//! `scontrol` reports `StdOut`/`StdErr`/`Command` only while the job is still in
//! slurmctld. After that the paths are gone, and `sacct` never had them. Caching
//! them while the job is live is what lets the detail panel still open the logs
//! of a job that finished last week.
//!
//! Every write re-reads the file and merges, because two LazySlurm sessions can
//! be running against the same cache and neither should lose the other's work.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use super::paths::{write_atomically, Paths};

/// What is known about one job.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct JobRecord {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stdout: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stderr: Option<String>,
    /// scontrol's `Command`: just the script path.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub command: Option<String>,
    /// scontrol's `SubmitLine`: the full `sbatch …` command.
    ///
    /// Stored separately from `command` so resubmission can prefer the richer
    /// one, rather than depending on which happened to be written last.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub submit_line: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub workdir: Option<String>,
    /// Unix seconds, used only for pruning.
    #[serde(default)]
    pub ts: u64,
}

impl JobRecord {
    /// Whether this record says anything worth storing.
    fn is_empty(&self) -> bool {
        self.stdout.is_none()
            && self.stderr.is_none()
            && self.command.is_none()
            && self.submit_line.is_none()
            && self.workdir.is_none()
    }

    /// Overlay the non-empty fields of `update` onto this record.
    ///
    /// A field the new observation does not carry must not erase what an earlier
    /// one knew: a `sacct`-sourced refresh has no log paths, and it should not
    /// forget the ones `scontrol` gave us while the job was running.
    fn merge(&mut self, update: &JobRecord) {
        fn keep(target: &mut Option<String>, incoming: &Option<String>) {
            if let Some(value) = incoming {
                if !value.is_empty() {
                    *target = Some(value.clone());
                }
            }
        }
        keep(&mut self.stdout, &update.stdout);
        keep(&mut self.stderr, &update.stderr);
        keep(&mut self.command, &update.command);
        keep(&mut self.submit_line, &update.submit_line);
        keep(&mut self.workdir, &update.workdir);
        self.ts = now_seconds();
    }
}

type Entries = BTreeMap<String, JobRecord>;

/// The on-disk cache of per-job log paths and submit commands.
#[derive(Debug, Clone)]
pub struct LogCache {
    path: PathBuf,
}

impl LogCache {
    pub fn new(paths: &Paths) -> Self {
        Self {
            path: paths.log_cache_file(),
        }
    }

    /// Read the cache. A missing or corrupt file reads as empty.
    fn read(&self) -> Entries {
        std::fs::read_to_string(&self.path)
            .ok()
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_default()
    }

    fn write(&self, entries: &Entries) -> std::io::Result<()> {
        let text = serde_json::to_string(entries)?;
        write_atomically(&self.path, &text, None)
    }

    /// Store what is now known about a job, merging with anything already there.
    pub fn record(&self, job_id: &str, update: &JobRecord) -> std::io::Result<()> {
        if update.is_empty() {
            return Ok(());
        }
        let mut entries = self.read();
        entries.entry(job_id.to_string()).or_default().merge(update);
        self.write(&entries)
    }

    /// The cached stdout and stderr paths for a job.
    pub fn log_paths(&self, job_id: &str) -> (Option<String>, Option<String>) {
        match self.read().get(job_id) {
            Some(entry) => (entry.stdout.clone(), entry.stderr.clone()),
            None => (None, None),
        }
    }

    /// The cached submit command and working directory for a job.
    ///
    /// Prefers the full submit line over the bare script path — it carries the
    /// sbatch flags (`--array` and friends) that resubmission needs.
    pub fn command(&self, job_id: &str) -> (Option<String>, Option<String>) {
        match self.read().get(job_id) {
            Some(entry) => (
                entry.submit_line.clone().or_else(|| entry.command.clone()),
                entry.workdir.clone(),
            ),
            None => (None, None),
        }
    }

    /// Drop entries older than `max_age_days`. `None` never prunes.
    pub fn prune(&self, max_age_days: Option<u32>) -> std::io::Result<()> {
        let Some(days) = max_age_days else {
            return Ok(());
        };
        let cutoff = now_seconds().saturating_sub(u64::from(days) * 86_400);

        let entries = self.read();
        let kept: Entries = entries
            .iter()
            .filter(|(_, entry)| entry.ts > cutoff)
            .map(|(id, entry)| (id.clone(), entry.clone()))
            .collect();

        if kept.len() < entries.len() {
            self.write(&kept)?;
        }
        Ok(())
    }
}

/// Seconds since the Unix epoch.
fn now_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_cache() -> (tempfile::TempDir, LogCache) {
        let dir = tempfile::tempdir().unwrap();
        let cache = LogCache::new(&Paths::rooted(dir.path()));
        (dir, cache)
    }

    fn record_with(stdout: Option<&str>, submit_line: Option<&str>) -> JobRecord {
        JobRecord {
            stdout: stdout.map(str::to_string),
            submit_line: submit_line.map(str::to_string),
            ..JobRecord::default()
        }
    }

    #[test]
    fn an_absent_cache_reads_as_empty() {
        let (_dir, cache) = temp_cache();
        assert_eq!(cache.log_paths("123"), (None, None));
        assert_eq!(cache.command("123"), (None, None));
    }

    #[test]
    fn a_corrupt_cache_reads_as_empty_rather_than_failing() {
        let (_dir, cache) = temp_cache();
        write_atomically(&cache.path, "{ not json", None).unwrap();
        assert_eq!(cache.log_paths("123"), (None, None));
        // And it can still be written to.
        cache
            .record("123", &record_with(Some("/w/o"), None))
            .unwrap();
        assert_eq!(cache.log_paths("123").0.as_deref(), Some("/w/o"));
    }

    #[test]
    fn stores_and_returns_log_paths() {
        let (_dir, cache) = temp_cache();
        cache
            .record(
                "123",
                &JobRecord {
                    stdout: Some("/work/slurm-123.out".into()),
                    stderr: Some("/work/slurm-123.err".into()),
                    ..JobRecord::default()
                },
            )
            .unwrap();

        let (out, err) = cache.log_paths("123");
        assert_eq!(out.as_deref(), Some("/work/slurm-123.out"));
        assert_eq!(err.as_deref(), Some("/work/slurm-123.err"));
    }

    #[test]
    fn prefers_the_submit_line_over_the_bare_command() {
        let (_dir, cache) = temp_cache();
        cache
            .record(
                "123",
                &JobRecord {
                    command: Some("/work/job.sh".into()),
                    submit_line: Some("sbatch --array=1-4 job.sh".into()),
                    workdir: Some("/work".into()),
                    ..JobRecord::default()
                },
            )
            .unwrap();

        let (command, workdir) = cache.command("123");
        assert_eq!(command.as_deref(), Some("sbatch --array=1-4 job.sh"));
        assert_eq!(workdir.as_deref(), Some("/work"));
    }

    #[test]
    fn falls_back_to_the_command_when_there_is_no_submit_line() {
        let (_dir, cache) = temp_cache();
        cache
            .record(
                "123",
                &JobRecord {
                    command: Some("/work/job.sh".into()),
                    ..JobRecord::default()
                },
            )
            .unwrap();
        assert_eq!(cache.command("123").0.as_deref(), Some("/work/job.sh"));
    }

    #[test]
    fn a_later_observation_never_erases_an_earlier_one() {
        let (_dir, cache) = temp_cache();
        // While running, scontrol gave us the log paths.
        cache
            .record("123", &record_with(Some("/work/o"), None))
            .unwrap();
        // Later, only a submit line is known.
        cache
            .record("123", &record_with(None, Some("sbatch j.sh")))
            .unwrap();

        assert_eq!(cache.log_paths("123").0.as_deref(), Some("/work/o"));
        assert_eq!(cache.command("123").0.as_deref(), Some("sbatch j.sh"));
    }

    #[test]
    fn recording_nothing_does_not_create_a_file() {
        let (_dir, cache) = temp_cache();
        cache.record("123", &JobRecord::default()).unwrap();
        assert!(!cache.path.exists());
    }

    #[test]
    fn keeps_entries_for_other_jobs() {
        let (_dir, cache) = temp_cache();
        cache.record("1", &record_with(Some("/a"), None)).unwrap();
        cache.record("2", &record_with(Some("/b"), None)).unwrap();

        assert_eq!(cache.log_paths("1").0.as_deref(), Some("/a"));
        assert_eq!(cache.log_paths("2").0.as_deref(), Some("/b"));
    }

    #[test]
    fn prunes_only_stale_entries() {
        let (_dir, cache) = temp_cache();

        let mut entries = Entries::new();
        entries.insert(
            "old".into(),
            JobRecord {
                stdout: Some("/old".into()),
                ts: now_seconds() - 60 * 86_400,
                ..JobRecord::default()
            },
        );
        entries.insert(
            "fresh".into(),
            JobRecord {
                stdout: Some("/fresh".into()),
                ts: now_seconds(),
                ..JobRecord::default()
            },
        );
        cache.write(&entries).unwrap();

        cache.prune(Some(30)).unwrap();

        assert_eq!(cache.log_paths("old"), (None, None));
        assert_eq!(cache.log_paths("fresh").0.as_deref(), Some("/fresh"));
    }

    #[test]
    fn pruning_is_disabled_by_none() {
        let (_dir, cache) = temp_cache();
        let mut entries = Entries::new();
        entries.insert(
            "ancient".into(),
            JobRecord {
                stdout: Some("/ancient".into()),
                ts: 0,
                ..JobRecord::default()
            },
        );
        cache.write(&entries).unwrap();

        cache.prune(None).unwrap();
        assert_eq!(cache.log_paths("ancient").0.as_deref(), Some("/ancient"));
    }
}
