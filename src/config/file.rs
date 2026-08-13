//! The persistent config file, `~/.config/lazyslurm/config.toml`.
//!
//! Every field is optional: an absent key means "no opinion", which is what lets
//! the file sit between the CLI and the built-in defaults without a key the user
//! never wrote silently overriding anything.
//!
//! Reads never fail. A malformed file yields defaults rather than an error,
//! because refusing to start over a stray character in a settings file is worse
//! than ignoring it — the app says so in the command log instead.

use std::collections::BTreeMap;
use std::path::Path;

use serde::Deserialize;

use super::paths::{expand_tilde, write_atomically, Paths};
use super::Config;

/// The commented template written out on first use.
const TEMPLATE: &str = include_str!("../../assets/config.toml");

/// Settings as they appear in the file.
#[derive(Debug, Clone, Default, Deserialize, PartialEq)]
#[serde(default)]
pub struct FileConfig {
    pub refresh: Option<f64>,
    pub days: Option<u32>,
    pub user: Option<String>,
    pub partition: Option<String>,
    pub no_gpu: Option<bool>,
    pub no_live: Option<bool>,
    pub remote: Option<String>,
    pub editor: Option<String>,
    pub pager: Option<String>,
    pub max_name_width: Option<usize>,
    pub max_partition_width: Option<usize>,
    pub abbreviate_states: Option<bool>,
    pub collapse_arrays: Option<bool>,
    /// Days to keep cached entries. Zero means "never prune".
    pub cache_max_age_days: Option<u32>,
    pub script_cache_dir: Option<String>,
    pub partition_order: Option<Vec<String>>,
    pub partition_colors: Option<BTreeMap<String, String>>,

    /// Anything else the file contained.
    ///
    /// Collected rather than rejected: a typo in one key must not cost the user
    /// every other setting in the file. [`Self::unknown_keys`] reports them so
    /// the mistake is still visible — a misspelled setting that silently does
    /// nothing is the hardest config problem to notice.
    #[serde(flatten)]
    pub extra: BTreeMap<String, toml::Value>,
}

/// Why a config file was not used.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoadError {
    /// No file yet — the normal state before the user runs `,`.
    Missing,
    /// The file exists but could not be read or parsed.
    Invalid(String),
}

impl std::fmt::Display for LoadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Missing => write!(f, "no config file"),
            Self::Invalid(reason) => write!(f, "ignoring config file: {reason}"),
        }
    }
}

impl FileConfig {
    /// Read the config file, if there is a readable one.
    pub fn load(paths: &Paths) -> Result<Self, LoadError> {
        let path = paths.config_file();
        if !path.exists() {
            return Err(LoadError::Missing);
        }
        Self::parse(&std::fs::read_to_string(&path).map_err(|e| LoadError::Invalid(e.to_string()))?)
    }

    /// Parse config text.
    pub fn parse(text: &str) -> Result<Self, LoadError> {
        toml::from_str(text).map_err(|error| LoadError::Invalid(error.message().to_string()))
    }

    /// Keys in the file that mean nothing to LazySlurm, usually typos.
    pub fn unknown_keys(&self) -> Vec<&str> {
        self.extra.keys().map(String::as_str).collect()
    }

    /// Layer these settings over `config`.
    pub fn apply_to(&self, config: &mut Config) {
        macro_rules! set {
            ($field:ident) => {
                if let Some(value) = self.$field.clone() {
                    config.$field = value;
                }
            };
        }

        set!(refresh);
        set!(days);
        set!(user);
        set!(partition);
        set!(no_gpu);
        set!(no_live);
        set!(remote);
        set!(editor);
        set!(pager);
        set!(max_name_width);
        set!(max_partition_width);
        set!(abbreviate_states);
        set!(collapse_arrays);
        set!(partition_order);
        set!(partition_colors);

        if let Some(dir) = &self.script_cache_dir {
            config.script_cache_dir = expand_tilde(dir);
        }
        if let Some(days) = self.cache_max_age_days {
            // TOML has no null, so the documented "never prune" needs a
            // representable value. Zero is it — and it is the safer reading
            // anyway, since a literal zero-day cutoff would delete every
            // cached script the first time the app started.
            config.cache_max_age_days = (days > 0).then_some(days);
        }
    }
}

/// Write the commented template, if no config file exists yet.
///
/// Returns whether it created one. The template is almost entirely comments, so
/// this is what makes `,` land the user in a documented file rather than an
/// empty buffer.
pub fn write_template_if_missing(paths: &Paths) -> std::io::Result<bool> {
    let path = paths.config_file();
    if path.exists() {
        return Ok(false);
    }
    paths.ensure_config_dir()?;
    write_atomically(&path, TEMPLATE, None)?;
    Ok(true)
}

/// Persist the partition display order, preserving the rest of the file.
///
/// Uses a format-preserving edit rather than rewriting the file: the template is
/// mostly comments explaining every setting, and regenerating it would throw
/// away both those and anything else the user wrote.
pub fn save_partition_order(paths: &Paths, order: &[String]) -> std::io::Result<()> {
    let path = paths.config_file();
    let mut document = read_document(&path);

    let mut array = toml_edit::Array::new();
    for name in order {
        array.push(name.as_str());
    }
    document["partition_order"] = toml_edit::value(array);

    paths.ensure_config_dir()?;
    write_atomically(&path, &document.to_string(), None)
}

/// Parse `path` as an editable document, falling back to an empty one.
fn read_document(path: &Path) -> toml_edit::DocumentMut {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|text| text.parse().ok())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A `Paths` rooted at a fresh temporary directory.
    fn temp_paths() -> (tempfile::TempDir, Paths) {
        let dir = tempfile::tempdir().unwrap();
        let paths = Paths::rooted(dir.path());
        (dir, paths)
    }

    #[test]
    fn a_missing_file_is_not_an_error_worth_reporting() {
        let (_dir, paths) = temp_paths();
        assert_eq!(FileConfig::load(&paths), Err(LoadError::Missing));
    }

    #[test]
    fn a_malformed_file_falls_back_to_defaults() {
        let (_dir, paths) = temp_paths();
        paths.ensure_config_dir().unwrap();
        std::fs::write(paths.config_file(), "this is not = = toml").unwrap();

        let error = FileConfig::load(&paths).unwrap_err();
        assert!(matches!(error, LoadError::Invalid(_)));
        // The app still starts, on defaults.
        let mut config = Config::default();
        FileConfig::default().apply_to(&mut config);
        assert_eq!(config, Config::default());
    }

    #[test]
    fn absent_keys_leave_the_config_alone() {
        let file = FileConfig::parse("days = 14").unwrap();
        let mut config = Config {
            refresh: 2.0,
            editor: "nano".into(),
            ..Config::default()
        };
        file.apply_to(&mut config);

        assert_eq!(config.days, 14);
        // Untouched by the file.
        assert_eq!(config.refresh, 2.0);
        assert_eq!(config.editor, "nano");
    }

    #[test]
    fn reads_every_scalar_setting() {
        let file = FileConfig::parse(
            r#"
            refresh = 3.5
            days = 14
            user = "rvy895"
            partition = "gpu"
            no_gpu = true
            no_live = true
            remote = "me@login.hpc.edu"
            editor = "nvim"
            pager = "bat"
            max_name_width = 24
            max_partition_width = 8
            abbreviate_states = true
            collapse_arrays = false
            "#,
        )
        .unwrap();

        let mut config = Config::default();
        file.apply_to(&mut config);

        assert_eq!(config.refresh, 3.5);
        assert_eq!(config.days, 14);
        assert_eq!(config.user, "rvy895");
        assert_eq!(config.partition, "gpu");
        assert!(config.no_gpu && config.no_live);
        assert_eq!(config.remote, "me@login.hpc.edu");
        assert_eq!(config.editor, "nvim");
        assert_eq!(config.pager, "bat");
        assert_eq!(config.max_name_width, 24);
        assert_eq!(config.max_partition_width, 8);
        assert!(config.abbreviate_states);
        assert!(!config.collapse_arrays);
    }

    #[test]
    fn reads_partition_order_and_colours() {
        let file = FileConfig::parse(
            r#"
            partition_order = ["gpu", "cpu", "fat"]

            [partition_colors]
            gpu = "green"
            cpu = "cyan"
            "#,
        )
        .unwrap();

        let mut config = Config::default();
        file.apply_to(&mut config);

        assert_eq!(config.partition_order, vec!["gpu", "cpu", "fat"]);
        assert_eq!(config.partition_colors["gpu"], "green");
        assert_eq!(config.partition_colors["cpu"], "cyan");
    }

    #[test]
    fn zero_cache_age_means_never_prune() {
        let mut config = Config::default();
        FileConfig::parse("cache_max_age_days = 0")
            .unwrap()
            .apply_to(&mut config);
        assert_eq!(config.cache_max_age_days, None);
    }

    #[test]
    fn a_cache_age_is_kept_as_given() {
        let mut config = Config::default();
        FileConfig::parse("cache_max_age_days = 7")
            .unwrap()
            .apply_to(&mut config);
        assert_eq!(config.cache_max_age_days, Some(7));
    }

    #[test]
    fn expands_a_tilde_in_the_script_cache_dir() {
        std::env::set_var("HOME", "/home/tester");
        let mut config = Config::default();
        FileConfig::parse(r#"script_cache_dir = "~/scratch/scripts""#)
            .unwrap()
            .apply_to(&mut config);
        assert_eq!(config.script_cache_dir, "/home/tester/scratch/scripts");
    }

    #[test]
    fn an_unknown_key_is_reported_but_costs_nothing_else() {
        // A typo in a setting name is the most likely config mistake, and one
        // the user cannot otherwise see: the setting just never takes effect.
        // Reporting it must not discard the settings that were spelled right.
        let file = FileConfig::parse("reffresh = 5\ndays = 21\n").unwrap();

        assert_eq!(file.unknown_keys(), vec!["reffresh"]);

        let mut config = Config::default();
        file.apply_to(&mut config);
        assert_eq!(config.days, 21);
        assert_eq!(config.refresh, crate::config::DEFAULT_REFRESH);
    }

    #[test]
    fn a_correct_file_reports_no_unknown_keys() {
        let file = FileConfig::parse("days = 21\nrefresh = 2.0\n").unwrap();
        assert!(file.unknown_keys().is_empty());
    }

    #[test]
    fn the_shipped_template_parses_and_changes_nothing() {
        // Every setting in it is commented out, so a fresh install runs on
        // defaults; if a line were accidentally uncommented this would catch it.
        let file = FileConfig::parse(TEMPLATE).expect("the template is valid TOML");
        assert_eq!(file, FileConfig::default());
    }

    #[test]
    fn writes_the_template_only_once() {
        let (_dir, paths) = temp_paths();

        assert!(write_template_if_missing(&paths).unwrap());
        std::fs::write(paths.config_file(), "days = 3").unwrap();
        // A second call must not overwrite the user's edits.
        assert!(!write_template_if_missing(&paths).unwrap());
        assert_eq!(
            std::fs::read_to_string(paths.config_file()).unwrap(),
            "days = 3"
        );
    }

    #[test]
    fn saving_partition_order_preserves_comments_and_settings() {
        let (_dir, paths) = temp_paths();
        paths.ensure_config_dir().unwrap();
        std::fs::write(
            paths.config_file(),
            "# my settings\ndays = 14\n\n# how often to poll\nrefresh = 3.0\n",
        )
        .unwrap();

        save_partition_order(&paths, &["gpu".into(), "cpu".into()]).unwrap();

        let text = std::fs::read_to_string(paths.config_file()).unwrap();
        assert!(text.contains("# my settings"), "{text}");
        assert!(text.contains("# how often to poll"), "{text}");
        assert!(text.contains("days = 14"), "{text}");

        let reloaded = FileConfig::parse(&text).unwrap();
        assert_eq!(
            reloaded.partition_order,
            Some(vec!["gpu".to_string(), "cpu".to_string()])
        );
        assert_eq!(reloaded.days, Some(14));
    }

    #[test]
    fn saving_partition_order_creates_a_file_when_there_is_none() {
        let (_dir, paths) = temp_paths();
        save_partition_order(&paths, &["gpu".into()]).unwrap();

        let file = FileConfig::load(&paths).unwrap();
        assert_eq!(file.partition_order, Some(vec!["gpu".to_string()]));
    }
}
