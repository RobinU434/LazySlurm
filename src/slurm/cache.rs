//! What the Slurm layer needs remembered for it.
//!
//! Slurm forgets a job shortly after it ends: `scontrol` stops answering once
//! `MinJobAge` passes, taking the log paths and the batch script with it. Both
//! are recoverable only if something wrote them down while the job was live.
//!
//! These traits say *what* has to be remembered without saying where. The
//! implementations live with the on-disk caches, which keeps this module free of
//! any dependency on the filesystem layout — and lets tests substitute an
//! in-memory one.

use std::path::PathBuf;

use crate::model::JobDetail;

/// Remembers the parts of a job detail that Slurm will stop reporting.
pub trait DetailCache: Send + Sync {
    /// Record what a fresh `scontrol` reading knows.
    fn remember(&self, job_id: &str, detail: &JobDetail);

    /// The remembered stdout and stderr paths, if any.
    fn recall_log_paths(&self, job_id: &str) -> (Option<String>, Option<String>);

    /// The remembered submit command and working directory, if any.
    fn recall_command(&self, job_id: &str) -> (Option<String>, Option<String>);
}

/// Stores the text of batch scripts, keyed by job id.
pub trait ScriptStore: Send + Sync {
    /// The archived script for a job, if one is held.
    fn archived(&self, job_id: &str) -> Option<PathBuf>;

    /// Archive script text, returning where it landed.
    fn archive(&self, job_id: &str, text: &str) -> Option<PathBuf>;
}

#[cfg(test)]
pub(crate) mod testing {
    //! In-memory implementations, for tests that should not touch the disk.

    use super::*;
    use std::collections::BTreeMap;
    use std::sync::Mutex;

    /// What a [`MemoryCache`] holds for one job.
    #[derive(Default, Clone)]
    struct Remembered {
        stdout: Option<String>,
        stderr: Option<String>,
        command: Option<String>,
    }

    #[derive(Default)]
    pub struct MemoryCache {
        entries: Mutex<BTreeMap<String, Remembered>>,
    }

    impl MemoryCache {
        /// Pre-seed what the cache knows about a job.
        pub fn seed(
            &self,
            job_id: &str,
            stdout: Option<&str>,
            stderr: Option<&str>,
            command: Option<&str>,
        ) {
            self.entries.lock().unwrap().insert(
                job_id.to_string(),
                Remembered {
                    stdout: stdout.map(str::to_string),
                    stderr: stderr.map(str::to_string),
                    command: command.map(str::to_string),
                },
            );
        }

        /// Whether anything has been remembered for a job.
        pub fn knows(&self, job_id: &str) -> bool {
            self.entries.lock().unwrap().contains_key(job_id)
        }
    }

    /// Holds archived script text in memory.
    #[derive(Default)]
    pub struct MemoryScriptStore {
        scripts: Mutex<BTreeMap<String, String>>,
    }

    impl MemoryScriptStore {
        /// Pre-seed an already-archived script.
        pub fn seed(&self, job_id: &str, text: &str) {
            self.scripts
                .lock()
                .unwrap()
                .insert(job_id.to_string(), text.to_string());
        }

        /// The stored text for a job, if any.
        pub fn text(&self, job_id: &str) -> Option<String> {
            self.scripts.lock().unwrap().get(job_id).cloned()
        }
    }

    impl ScriptStore for MemoryScriptStore {
        fn archived(&self, job_id: &str) -> Option<PathBuf> {
            let base = crate::model::base_job_id(job_id)?;
            self.scripts
                .lock()
                .unwrap()
                .contains_key(base)
                .then(|| PathBuf::from(format!("/archive/{base}.sh")))
        }

        fn archive(&self, job_id: &str, text: &str) -> Option<PathBuf> {
            let base = crate::model::base_job_id(job_id)?.to_string();
            self.scripts
                .lock()
                .unwrap()
                .insert(base.clone(), text.to_string());
            Some(PathBuf::from(format!("/archive/{base}.sh")))
        }
    }

    impl DetailCache for MemoryCache {
        fn remember(&self, job_id: &str, detail: &JobDetail) {
            self.entries.lock().unwrap().insert(
                job_id.to_string(),
                Remembered {
                    stdout: detail.stdout_path.clone(),
                    stderr: detail.stderr_path.clone(),
                    command: Some(detail.submit_line().to_string()),
                },
            );
        }

        fn recall_log_paths(&self, job_id: &str) -> (Option<String>, Option<String>) {
            match self.entries.lock().unwrap().get(job_id) {
                Some(entry) => (entry.stdout.clone(), entry.stderr.clone()),
                None => (None, None),
            }
        }

        fn recall_command(&self, job_id: &str) -> (Option<String>, Option<String>) {
            match self.entries.lock().unwrap().get(job_id) {
                Some(entry) => (entry.command.clone(), None),
                None => (None, None),
            }
        }
    }
}
