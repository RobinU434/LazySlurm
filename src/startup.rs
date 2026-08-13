//! Composing the settings the app starts with.
//!
//! Three layers, lowest first: built-in defaults, the config file, then the
//! command line. Kept in its own module because it is the one place that needs
//! to know about both [`crate::cli`] and [`crate::config`], and neither should
//! have to know about the other.

use std::path::PathBuf;

use crate::cli::{Args, Override};
use crate::config::{Config, FileConfig, JobRecord, LoadError, LogCache, Paths, ScriptCache};
use crate::model::JobDetail;
use crate::slurm::{DetailCache, ScriptStore};

// The two trait implementations below live here, in the composition layer,
// rather than in either module: `slurm` should not know where files go, and
// `config` should not know what Slurm needs. This is the seam between them.

impl DetailCache for LogCache {
    fn remember(&self, job_id: &str, detail: &JobDetail) {
        let record = JobRecord {
            stdout: detail.stdout_path.clone(),
            stderr: detail.stderr_path.clone(),
            command: detail.raw.get("Command").cloned(),
            submit_line: detail.raw.get("SubmitLine").cloned(),
            workdir: (!detail.work_dir.is_empty()).then(|| detail.work_dir.clone()),
            ts: 0, // Stamped on write.
        };
        // A cache miss costs a guessed log path, never correctness, so a write
        // failure here must not interrupt loading the job.
        let _ = self.record(job_id, &record);
    }

    fn recall_log_paths(&self, job_id: &str) -> (Option<String>, Option<String>) {
        self.log_paths(job_id)
    }

    fn recall_command(&self, job_id: &str) -> (Option<String>, Option<String>) {
        self.command(job_id)
    }
}

impl ScriptStore for ScriptCache {
    fn archived(&self, job_id: &str) -> Option<PathBuf> {
        self.get(job_id)
    }

    fn archive(&self, job_id: &str, text: &str) -> Option<PathBuf> {
        self.store(job_id, text).ok().flatten()
    }
}

/// Everything resolved at startup.
pub struct Settings {
    pub config: Config,
    pub paths: Paths,
    /// Settings the command line took from the config file, for the startup log.
    pub overrides: Vec<Override>,
    /// Things worth telling the user about the load itself.
    pub notes: Vec<String>,
}

impl Settings {
    /// Resolve defaults, then the config file, then the command line.
    pub fn resolve(args: &Args, mut paths: Paths) -> Self {
        let mut config = Config::default();
        let mut notes = Vec::new();

        match FileConfig::load(&paths) {
            Ok(file) => {
                file.apply_to(&mut config);
                // A misspelled setting silently does nothing, which is the
                // hardest config mistake to spot. Name it.
                let unknown = file.unknown_keys();
                if !unknown.is_empty() {
                    notes.push(format!(
                        "config file: ignoring unknown setting{} {}",
                        if unknown.len() == 1 { "" } else { "s" },
                        unknown.join(", ")
                    ));
                }
            }
            // A missing file is the normal state before the user runs `,`.
            Err(LoadError::Missing) => {}
            // A broken file is worth saying out loud: the alternative is a user
            // wondering why a setting they wrote has no effect.
            Err(error @ LoadError::Invalid(_)) => notes.push(error.to_string()),
        }

        let overrides = args.apply_to(&mut config);
        paths.set_script_cache_dir(&config.script_cache_dir);

        Self {
            config,
            paths,
            overrides,
            notes,
        }
    }

    /// Resolve against the user's real config directory.
    pub fn discover(args: &Args) -> Self {
        Self::resolve(args, Paths::discover())
    }

    pub fn log_cache(&self) -> LogCache {
        LogCache::new(&self.paths)
    }

    pub fn script_cache(&self) -> ScriptCache {
        ScriptCache::new(self.paths.script_cache_dir.clone())
    }

    /// Drop cache entries the user no longer wants kept.
    ///
    /// Failures are reported rather than propagated: a cache that cannot be
    /// pruned is untidy, not fatal.
    pub fn prune_caches(&self) -> Vec<String> {
        let mut problems = Vec::new();
        let max_age = self.config.cache_max_age_days;

        if let Err(error) = self.log_cache().prune(max_age) {
            problems.push(format!("could not prune the log cache: {error}"));
        }
        if let Err(error) = self.script_cache().prune(max_age) {
            problems.push(format!("could not prune the script cache: {error}"));
        }
        problems
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;

    fn args_from(argv: &[&str]) -> Args {
        let mut full = vec!["lazyslurm"];
        full.extend_from_slice(argv);
        Args::try_parse_from(full).expect("arguments parse")
    }

    fn temp_paths() -> (tempfile::TempDir, Paths) {
        let dir = tempfile::tempdir().unwrap();
        let paths = Paths::rooted(dir.path());
        paths.ensure_config_dir().unwrap();
        (dir, paths)
    }

    #[test]
    fn uses_defaults_when_there_is_no_file() {
        let (_dir, paths) = temp_paths();
        let settings = Settings::resolve(&args_from(&[]), paths);

        assert_eq!(settings.config, Config::default());
        assert!(settings.notes.is_empty());
    }

    #[test]
    fn the_file_beats_the_defaults() {
        let (_dir, paths) = temp_paths();
        std::fs::write(paths.config_file(), "days = 21\nrefresh = 2.0\n").unwrap();

        let settings = Settings::resolve(&args_from(&[]), paths);
        assert_eq!(settings.config.days, 21);
        assert_eq!(settings.config.refresh, 2.0);
    }

    #[test]
    fn the_command_line_beats_the_file() {
        let (_dir, paths) = temp_paths();
        std::fs::write(paths.config_file(), "days = 21\nrefresh = 2.0\n").unwrap();

        let settings = Settings::resolve(&args_from(&["--days", "3"]), paths);
        assert_eq!(settings.config.days, 3);
        // Untouched by the command line, so the file still decides.
        assert_eq!(settings.config.refresh, 2.0);

        assert_eq!(settings.overrides.len(), 1);
        assert_eq!(settings.overrides[0].key, "days");
    }

    #[test]
    fn a_typo_is_named_and_the_rest_of_the_file_still_applies() {
        let (_dir, paths) = temp_paths();
        std::fs::write(paths.config_file(), "reffresh = 5\ndays = 21\n").unwrap();

        let settings = Settings::resolve(&args_from(&[]), paths);
        assert_eq!(settings.config.days, 21);
        assert_eq!(settings.notes.len(), 1);
        assert!(
            settings.notes[0].contains("reffresh"),
            "{:?}",
            settings.notes
        );
    }

    #[test]
    fn a_broken_file_is_reported_and_then_ignored() {
        let (_dir, paths) = temp_paths();
        std::fs::write(paths.config_file(), "days = = 3").unwrap();

        let settings = Settings::resolve(&args_from(&[]), paths);
        assert_eq!(settings.config, Config::default());
        assert_eq!(settings.notes.len(), 1);
        assert!(settings.notes[0].contains("ignoring config file"));
    }

    #[test]
    fn a_configured_script_cache_dir_moves_the_archive() {
        let (_dir, paths) = temp_paths();
        std::fs::write(
            paths.config_file(),
            r#"script_cache_dir = "/scratch/my-scripts""#,
        )
        .unwrap();

        let settings = Settings::resolve(&args_from(&[]), paths);
        assert_eq!(
            settings.script_cache().dir(),
            std::path::Path::new("/scratch/my-scripts")
        );
    }

    #[test]
    fn the_default_script_cache_lives_beside_the_config() {
        let (_dir, paths) = temp_paths();
        let expected = paths.config_dir.join("scripts");

        let settings = Settings::resolve(&args_from(&[]), paths);
        assert_eq!(settings.script_cache().dir(), expected);
    }

    #[test]
    fn pruning_an_untouched_installation_reports_no_problems() {
        let (_dir, paths) = temp_paths();
        let settings = Settings::resolve(&args_from(&[]), paths);
        assert!(settings.prune_caches().is_empty());
    }
}
