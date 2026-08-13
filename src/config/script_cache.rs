//! Archiving sbatch scripts.
//!
//! Slurm only keeps a job's batch script until `MinJobAge` seconds after it ends
//! (300s on many clusters), so `scontrol write batch_script` fails for anything
//! older. Archiving the *text* — rather than the path, which the user may edit,
//! move or delete — is what makes an old job's script recoverable at all.
//!
//! Layout: `<dir>/<base job id>.sh`, so every task of an array resolves to the
//! one script they share.

use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use super::paths::write_atomically;
use crate::model::base_job_id;

/// Archived scripts routinely hold tokens and private paths, so both the
/// directory and the files stay owner-only. No exec bit: this copy is for
/// reading, not running.
const DIR_MODE: u32 = 0o700;
const FILE_MODE: u32 = 0o600;

/// The archive of batch-script text, on disk.
#[derive(Debug, Clone)]
pub struct ScriptCache {
    dir: PathBuf,
}

impl ScriptCache {
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into() }
    }

    /// The directory scripts are archived into.
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Where this job's script is, or would be, archived.
    ///
    /// `None` when the id has no numeric base — which also keeps arbitrary text
    /// out of cache filenames.
    pub fn path_for(&self, job_id: &str) -> Option<PathBuf> {
        base_job_id(job_id).map(|base| self.dir.join(format!("{base}.sh")))
    }

    /// The archived script for a job, if there is a usable one.
    ///
    /// A zero-length file does not count: a truncated write must not open as a
    /// blank buffer that looks like the job had an empty script.
    pub fn get(&self, job_id: &str) -> Option<PathBuf> {
        let path = self.path_for(job_id)?;
        let usable = std::fs::metadata(&path).is_ok_and(|meta| meta.len() > 0);
        usable.then_some(path)
    }

    /// Archive a job's batch-script text.
    pub fn store(&self, job_id: &str, text: &str) -> std::io::Result<Option<PathBuf>> {
        let Some(path) = self.path_for(job_id) else {
            return Ok(None);
        };
        if text.trim().is_empty() {
            return Ok(None);
        }

        std::fs::create_dir_all(&self.dir)?;
        set_mode(&self.dir, DIR_MODE)?;
        write_atomically(&path, text, Some(FILE_MODE))?;
        Ok(Some(path))
    }

    /// Delete archived scripts older than `max_age_days`. `None` never prunes.
    pub fn prune(&self, max_age_days: Option<u32>) -> std::io::Result<()> {
        let Some(days) = max_age_days else {
            return Ok(());
        };
        let Ok(entries) = std::fs::read_dir(&self.dir) else {
            return Ok(()); // Nothing archived yet.
        };

        let max_age = Duration::from_secs(u64::from(days) * 86_400);
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().is_none_or(|ext| ext != "sh") {
                continue;
            }
            // The file's own mtime is the record here — there is no sidecar
            // index that could disagree with it.
            let stale = entry
                .metadata()
                .and_then(|meta| meta.modified())
                .map(|modified| {
                    SystemTime::now()
                        .duration_since(modified)
                        .is_ok_and(|age| age >= max_age)
                })
                .unwrap_or(false);
            if stale {
                let _ = std::fs::remove_file(&path);
            }
        }
        Ok(())
    }
}

fn set_mode(path: &Path, mode: u32) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn temp_cache() -> (tempfile::TempDir, ScriptCache) {
        let dir = tempfile::tempdir().unwrap();
        let cache = ScriptCache::new(dir.path().join("scripts"));
        (dir, cache)
    }

    #[test]
    fn stores_and_returns_a_script() {
        let (_dir, cache) = temp_cache();
        let path = cache
            .store("123", "#!/bin/bash\necho hi\n")
            .unwrap()
            .expect("a numeric job id archives");

        assert!(path.ends_with("123.sh"));
        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            "#!/bin/bash\necho hi\n"
        );
        assert_eq!(cache.get("123").as_deref(), Some(path.as_path()));
    }

    #[test]
    fn every_task_of_an_array_shares_one_script() {
        let (_dir, cache) = temp_cache();
        cache.store("123", "#!/bin/bash\ncached\n").unwrap();

        // An array task, a pending range and a step all resolve to the base id.
        for job_id in ["123_7", "123_[1-40]", "123.batch"] {
            let path = cache.get(job_id).unwrap_or_else(|| panic!("{job_id}"));
            assert_eq!(
                std::fs::read_to_string(path).unwrap(),
                "#!/bin/bash\ncached\n"
            );
        }
    }

    #[test]
    fn refuses_a_job_id_that_is_not_numeric() {
        let (_dir, cache) = temp_cache();
        assert_eq!(cache.store("../../etc/passwd", "text").unwrap(), None);
        assert_eq!(cache.path_for("nonsense"), None);
        assert_eq!(cache.get("nonsense"), None);
    }

    #[test]
    fn refuses_to_archive_empty_text() {
        let (_dir, cache) = temp_cache();
        assert_eq!(cache.store("123", "   \n").unwrap(), None);
        assert_eq!(cache.get("123"), None);
    }

    #[test]
    fn a_truncated_archive_does_not_count_as_cached() {
        let (_dir, cache) = temp_cache();
        std::fs::create_dir_all(cache.dir()).unwrap();
        std::fs::write(cache.dir().join("123.sh"), "").unwrap();

        assert_eq!(cache.get("123"), None);
    }

    #[test]
    fn archives_are_owner_only() {
        let (_dir, cache) = temp_cache();
        let path = cache.store("123", "#!/bin/bash\n").unwrap().unwrap();

        let file_mode = std::fs::metadata(&path).unwrap().permissions().mode();
        let dir_mode = std::fs::metadata(cache.dir()).unwrap().permissions().mode();
        assert_eq!(file_mode & 0o777, FILE_MODE);
        assert_eq!(dir_mode & 0o777, DIR_MODE);
    }

    #[test]
    fn overwrites_an_existing_archive() {
        let (_dir, cache) = temp_cache();
        cache.store("123", "old\n").unwrap();
        let path = cache.store("123", "new\n").unwrap().unwrap();
        assert_eq!(std::fs::read_to_string(path).unwrap(), "new\n");
    }

    #[test]
    fn prunes_only_stale_scripts() {
        let (_dir, cache) = temp_cache();
        let fresh = cache.store("111", "#!/bin/bash\n").unwrap().unwrap();
        let stale = cache.store("222", "#!/bin/bash\n").unwrap().unwrap();

        // Backdate one of them by 60 days.
        let long_ago = SystemTime::now() - Duration::from_secs(60 * 86_400);
        filetime_set(&stale, long_ago);

        cache.prune(Some(30)).unwrap();

        assert!(fresh.exists());
        assert!(!stale.exists());
    }

    #[test]
    fn pruning_is_disabled_by_none() {
        let (_dir, cache) = temp_cache();
        let path = cache.store("222", "#!/bin/bash\n").unwrap().unwrap();
        filetime_set(&path, SystemTime::now() - Duration::from_secs(365 * 86_400));

        cache.prune(None).unwrap();
        assert!(path.exists());
    }

    #[test]
    fn pruning_an_empty_cache_is_not_an_error() {
        let (_dir, cache) = temp_cache();
        cache.prune(Some(30)).unwrap();
    }

    /// Set a file's mtime, so pruning can be tested without waiting 30 days.
    fn filetime_set(path: &Path, when: SystemTime) {
        let file = std::fs::File::options().write(true).open(path).unwrap();
        file.set_modified(when).unwrap();
    }
}
