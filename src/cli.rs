//! Command-line arguments.
//!
//! Every option here can also be set in the config file, so each one is an
//! `Option` — `None` means "not given on the command line", which is what lets
//! [`Args::apply_to`] layer CLI over file over default without a flag that was
//! merely defaulted overwriting a configured value.

use clap::Parser;

use crate::config::Config;

/// A TUI for monitoring Slurm HPC jobs.
#[derive(Debug, Default, Parser)]
#[command(name = "lazyslurm", version, about, long_about = None)]
pub struct Args {
    /// Auto-refresh interval in seconds. Use 0 or 'off' to disable.
    #[arg(short, long, value_name = "SEC")]
    pub refresh: Option<String>,

    /// How many days back to show terminated jobs.
    #[arg(short, long, value_name = "N")]
    pub days: Option<u32>,

    /// Slurm user to monitor (default: current user).
    #[arg(short, long)]
    pub user: Option<String>,

    /// Filter jobs by partition.
    #[arg(short, long)]
    pub partition: Option<String>,

    /// Disable the live GPU monitoring tab (nvidia-smi).
    #[arg(long)]
    pub no_gpu: bool,

    /// Disable live CPU/GPU monitoring tabs (no SSH to compute nodes).
    #[arg(long)]
    pub no_live: bool,

    /// Comma-separated partition display order, e.g. gpu,cpu,fat.
    #[arg(long, value_name = "P1,P2,...")]
    pub partition_order: Option<String>,

    /// SSH target for remote mode, e.g. user@login.hpc.edu.
    #[arg(short = 'H', long, value_name = "HOST")]
    pub remote: Option<String>,
}

/// A setting the command line overrode, for the startup log.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Override {
    pub key: String,
    pub from: String,
    pub to: String,
}

impl std::fmt::Display for Override {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: config={} -> cli={}", self.key, self.from, self.to)
    }
}

impl Args {
    /// Parse the process arguments.
    pub fn parse_args() -> Self {
        Self::parse()
    }

    /// Layer these arguments over `config`, reporting what they changed.
    ///
    /// The returned overrides are shown in the command log at startup, so a user
    /// who forgot they passed a flag can see why the session disagrees with
    /// their config file.
    pub fn apply_to(&self, config: &mut Config) -> Vec<Override> {
        let mut overrides = Vec::new();

        if let Some(refresh) = self.refresh_seconds() {
            record(&mut overrides, "refresh", config.refresh, refresh);
            config.refresh = refresh;
        }
        if let Some(days) = self.days {
            record(&mut overrides, "days", config.days, days);
            config.days = days;
        }
        if let Some(user) = &self.user {
            record(&mut overrides, "user", &config.user, user);
            config.user.clone_from(user);
        }
        if let Some(partition) = &self.partition {
            record(&mut overrides, "partition", &config.partition, partition);
            config.partition.clone_from(partition);
        }
        // These are flags: absent means "no opinion", never "off".
        if self.no_gpu {
            record(&mut overrides, "no_gpu", config.no_gpu, true);
            config.no_gpu = true;
        }
        if self.no_live {
            record(&mut overrides, "no_live", config.no_live, true);
            config.no_live = true;
        }
        if let Some(remote) = &self.remote {
            record(&mut overrides, "remote", &config.remote, remote);
            config.remote.clone_from(remote);
        }
        if let Some(order) = self.partition_order() {
            record(
                &mut overrides,
                "partition_order",
                config.partition_order.join(","),
                order.join(","),
            );
            config.partition_order = order;
        }

        // `--remote user@host` names the user, unless one was given explicitly.
        if config.user.is_empty() {
            if let Some((user, _)) = config.remote.split_once('@') {
                config.user = user.to_string();
            }
        }

        overrides
    }

    /// The refresh interval, accepting the words that mean "off".
    ///
    /// Returns `None` when the flag was not given, so the config file still
    /// decides. An unparseable value is also `None` rather than an error: a typo
    /// should not stop the app from starting.
    fn refresh_seconds(&self) -> Option<f64> {
        let raw = self.refresh.as_deref()?.trim().to_lowercase();
        if matches!(raw.as_str(), "off" | "none" | "null" | "0") {
            return Some(0.0);
        }
        raw.parse().ok()
    }

    /// The partition order, split and cleaned.
    fn partition_order(&self) -> Option<Vec<String>> {
        let raw = self.partition_order.as_deref()?;
        let order: Vec<String> = raw
            .split(',')
            .map(str::trim)
            .filter(|name| !name.is_empty())
            .map(str::to_string)
            .collect();
        (!order.is_empty()).then_some(order)
    }
}

/// Note an override, but only when the value actually changed.
fn record<T, U>(overrides: &mut Vec<Override>, key: &str, from: T, to: U)
where
    T: std::fmt::Display,
    U: std::fmt::Display,
{
    let (from, to) = (from.to_string(), to.to_string());
    if from != to {
        overrides.push(Override {
            key: key.to_string(),
            from,
            to,
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args_from(argv: &[&str]) -> Args {
        let mut full = vec!["lazyslurm"];
        full.extend_from_slice(argv);
        Args::try_parse_from(full).expect("arguments parse")
    }

    #[test]
    fn absent_arguments_leave_the_config_alone() {
        let mut config = Config {
            refresh: 3.0,
            days: 14,
            ..Config::default()
        };
        let overrides = args_from(&[]).apply_to(&mut config);

        assert_eq!(config.refresh, 3.0);
        assert_eq!(config.days, 14);
        assert!(overrides.is_empty());
    }

    #[test]
    fn the_command_line_wins_and_is_reported() {
        let mut config = Config {
            days: 14,
            ..Config::default()
        };
        let overrides = args_from(&["--days", "3"]).apply_to(&mut config);

        assert_eq!(config.days, 3);
        assert_eq!(overrides.len(), 1);
        assert_eq!(overrides[0].to_string(), "days: config=14 -> cli=3");
    }

    #[test]
    fn setting_the_same_value_is_not_an_override() {
        let mut config = Config {
            days: 3,
            ..Config::default()
        };
        assert!(args_from(&["--days", "3"]).apply_to(&mut config).is_empty());
    }

    #[test]
    fn refresh_accepts_the_words_for_off() {
        for word in ["off", "none", "null", "0", "OFF"] {
            let mut config = Config::default();
            args_from(&["--refresh", word]).apply_to(&mut config);
            assert_eq!(config.refresh, 0.0, "{word}");
            assert!(!config.auto_refresh());
        }
    }

    #[test]
    fn refresh_accepts_a_fractional_interval() {
        let mut config = Config::default();
        args_from(&["-r", "2.5"]).apply_to(&mut config);
        assert_eq!(config.refresh, 2.5);
    }

    #[test]
    fn an_unparseable_refresh_does_not_stop_startup() {
        let mut config = Config::default();
        args_from(&["-r", "soon"]).apply_to(&mut config);
        assert_eq!(config.refresh, crate::config::DEFAULT_REFRESH);
    }

    #[test]
    fn partition_order_is_split_and_cleaned() {
        let mut config = Config::default();
        args_from(&["--partition-order", "gpu, cpu ,,fat"]).apply_to(&mut config);
        assert_eq!(config.partition_order, vec!["gpu", "cpu", "fat"]);
    }

    #[test]
    fn remote_names_the_user_when_none_was_given() {
        let mut config = Config::default();
        args_from(&["--remote", "rvy895@login.hpc.edu"]).apply_to(&mut config);
        assert_eq!(config.user, "rvy895");
        assert!(config.is_remote());
    }

    #[test]
    fn an_explicit_user_survives_a_remote_target() {
        let mut config = Config::default();
        args_from(&["--remote", "rvy895@login.hpc.edu", "--user", "someone"]).apply_to(&mut config);
        assert_eq!(config.user, "someone");
    }

    #[test]
    fn flags_are_never_turned_off_by_being_absent() {
        // The config file said yes; not passing the flag must not undo that.
        let mut config = Config {
            no_gpu: true,
            ..Config::default()
        };
        args_from(&[]).apply_to(&mut config);
        assert!(config.no_gpu);
    }
}
