//! Runtime configuration.
//!
//! [`Config`] is the resolved settings the whole app reads: CLI arguments layered
//! over the persistent config file layered over defaults. Resolution itself lives
//! in the CLI and file modules; this is just the result.

use std::collections::BTreeMap;

/// How often to poll, in seconds, when the user has not said otherwise.
pub const DEFAULT_REFRESH: f64 = 5.0;

/// How many days of terminated jobs to show by default.
pub const DEFAULT_DAYS: u32 = 7;

/// Resolved runtime settings.
#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    /// Auto-refresh interval in seconds. Zero disables polling entirely.
    pub refresh: f64,
    /// How far back the terminated-jobs table reaches.
    pub days: u32,
    /// Slurm user to monitor. Empty means the current user.
    pub user: String,
    /// Restrict every view to one partition. Empty means all.
    pub partition: String,
    /// Hide the live GPU tab.
    pub no_gpu: bool,
    /// Hide both live monitoring tabs, so nothing ever SSHes to a compute node.
    pub no_live: bool,
    /// SSH target for remote mode, e.g. `user@login.hpc.edu`. Empty means local.
    pub remote: String,
    /// Display order for partitions; anything unlisted follows in its own order.
    pub partition_order: Vec<String>,
    /// Explicit partition colours, overriding the automatic hash-based ones.
    pub partition_colors: BTreeMap<String, String>,
    /// Editor used for logs, scripts and the config file itself.
    pub editor: String,
    /// Pager used to browse whole logs.
    pub pager: String,
    /// Maximum width of the job name column. Zero means unlimited.
    pub max_name_width: usize,
    /// Maximum width of the partition column. Zero means unlimited.
    pub max_partition_width: usize,
    /// Show short state names (`COMP`, `FAIL`, `OOM`, …).
    pub abbreviate_states: bool,
    /// Fold a job array's tasks into one expandable row.
    pub collapse_arrays: bool,
    /// Prune cache entries older than this. `None` never prunes.
    pub cache_max_age_days: Option<u32>,
    /// Where to archive sbatch scripts. Empty means `<config dir>/scripts`.
    pub script_cache_dir: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            refresh: DEFAULT_REFRESH,
            days: DEFAULT_DAYS,
            user: String::new(),
            partition: String::new(),
            no_gpu: false,
            no_live: false,
            remote: String::new(),
            partition_order: Vec::new(),
            partition_colors: BTreeMap::new(),
            editor: "vim".to_string(),
            pager: "less".to_string(),
            max_name_width: 16,
            max_partition_width: 16,
            abbreviate_states: false,
            collapse_arrays: true,
            cache_max_age_days: Some(30),
            script_cache_dir: String::new(),
        }
    }
}

impl Config {
    /// Whether commands run over SSH rather than locally.
    pub fn is_remote(&self) -> bool {
        !self.remote.is_empty()
    }

    /// Whether polling is enabled.
    pub fn auto_refresh(&self) -> bool {
        self.refresh > 0.0
    }

    /// The user whose jobs to show, falling back to the login of this session.
    pub fn effective_user(&self) -> String {
        if !self.user.is_empty() {
            return self.user.clone();
        }
        current_user()
    }
}

/// The login name of the current session, or an empty string if unknown.
pub fn current_user() -> String {
    std::env::var("USER")
        .or_else(|_| std::env::var("LOGNAME"))
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_local_with_polling_on() {
        let config = Config::default();
        assert!(!config.is_remote());
        assert!(config.auto_refresh());
        assert_eq!(config.refresh, DEFAULT_REFRESH);
    }

    #[test]
    fn a_zero_refresh_disables_polling() {
        let config = Config {
            refresh: 0.0,
            ..Config::default()
        };
        assert!(!config.auto_refresh());
    }

    #[test]
    fn an_explicit_user_wins_over_the_environment() {
        let config = Config {
            user: "someone".into(),
            ..Config::default()
        };
        assert_eq!(config.effective_user(), "someone");
    }
}
