//! Where LazySlurm keeps its files.
//!
//! Paths are carried in a [`Paths`] value rather than resolved from the
//! environment at each call site, so tests can point the whole config layer at a
//! temporary directory without touching the user's real one.

use std::path::{Path, PathBuf};

/// The application's directory name under the config root.
const APP_DIR: &str = "lazyslurm";

/// Resolved locations for the config file and the caches.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Paths {
    /// `~/.config/lazyslurm`, or `$XDG_CONFIG_HOME/lazyslurm`.
    pub config_dir: PathBuf,
    /// Where archived sbatch scripts live. Defaults inside `config_dir`.
    pub script_cache_dir: PathBuf,
}

impl Paths {
    /// Resolve from the environment, honouring `XDG_CONFIG_HOME`.
    pub fn discover() -> Self {
        let root = std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home_dir().join(".config"));
        Self::rooted(root.join(APP_DIR))
    }

    /// Use `config_dir` for everything, with the default script cache inside it.
    pub fn rooted(config_dir: impl Into<PathBuf>) -> Self {
        let config_dir = config_dir.into();
        Self {
            script_cache_dir: config_dir.join("scripts"),
            config_dir,
        }
    }

    /// The TOML settings file.
    pub fn config_file(&self) -> PathBuf {
        self.config_dir.join("config.toml")
    }

    /// The JSON cache of per-job log paths and submit commands.
    pub fn log_cache_file(&self) -> PathBuf {
        self.config_dir.join("log_cache.json")
    }

    /// Point the script cache at a custom directory.
    ///
    /// An empty path leaves the default in place, which is what an unset
    /// `script_cache_dir` in the config file means.
    pub fn set_script_cache_dir(&mut self, path: &str) {
        let path = path.trim();
        if !path.is_empty() {
            self.script_cache_dir = PathBuf::from(expand_tilde(path));
        }
    }

    /// Create the config directory if it does not exist.
    pub fn ensure_config_dir(&self) -> std::io::Result<()> {
        std::fs::create_dir_all(&self.config_dir)
    }
}

impl Default for Paths {
    fn default() -> Self {
        Self::discover()
    }
}

/// The user's home directory, or the current directory if it cannot be found.
fn home_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Expand a leading `~` the way a shell would.
///
/// Config values are typed by hand and `~/scratch/scripts` is the natural way to
/// write a path; nothing else expands it for us.
pub fn expand_tilde(path: &str) -> String {
    match path.strip_prefix('~') {
        Some(rest) => {
            let rest = rest.strip_prefix('/').unwrap_or(rest);
            home_dir().join(rest).to_string_lossy().into_owned()
        }
        None => path.to_string(),
    }
}

/// Write `contents` to `path` without ever leaving a half-written file behind.
///
/// The TUI and a resubmit can both touch the caches, so a reader must never see
/// a truncated file. Writing to a sibling temp file and renaming makes the swap
/// atomic within the directory.
pub fn write_atomically(path: &Path, contents: &str, mode: Option<u32>) -> std::io::Result<()> {
    let parent = path.parent().unwrap_or(Path::new("."));
    std::fs::create_dir_all(parent)?;

    let temp = parent.join(format!(
        ".{}.tmp{}",
        path.file_name().unwrap_or_default().to_string_lossy(),
        std::process::id()
    ));
    std::fs::write(&temp, contents)?;

    if let Some(mode) = mode {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&temp, std::fs::Permissions::from_mode(mode))?;
    }

    std::fs::rename(&temp, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn derives_every_path_from_the_config_dir() {
        let paths = Paths::rooted("/tmp/example");
        assert_eq!(paths.config_file(), Path::new("/tmp/example/config.toml"));
        assert_eq!(
            paths.log_cache_file(),
            Path::new("/tmp/example/log_cache.json")
        );
        assert_eq!(paths.script_cache_dir, Path::new("/tmp/example/scripts"));
    }

    #[test]
    fn an_empty_script_cache_dir_keeps_the_default() {
        let mut paths = Paths::rooted("/tmp/example");
        paths.set_script_cache_dir("   ");
        assert_eq!(paths.script_cache_dir, Path::new("/tmp/example/scripts"));
    }

    #[test]
    fn a_custom_script_cache_dir_replaces_the_default() {
        let mut paths = Paths::rooted("/tmp/example");
        paths.set_script_cache_dir("/scratch/scripts");
        assert_eq!(paths.script_cache_dir, Path::new("/scratch/scripts"));
    }

    #[test]
    fn expands_a_leading_tilde() {
        std::env::set_var("HOME", "/home/tester");
        assert_eq!(expand_tilde("~/scratch"), "/home/tester/scratch");
        assert_eq!(expand_tilde("/absolute"), "/absolute");
        assert_eq!(expand_tilde("relative"), "relative");
    }

    #[test]
    fn writes_atomically_and_creates_parents() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nested/file.json");

        write_atomically(&path, "{}", None).unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "{}");

        // No temp file is left behind.
        let leftovers: Vec<_> = std::fs::read_dir(path.parent().unwrap())
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().contains(".tmp"))
            .collect();
        assert!(leftovers.is_empty());
    }

    #[test]
    fn applies_the_requested_permissions() {
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("secret.sh");
        write_atomically(&path, "#!/bin/sh\n", Some(0o600)).unwrap();

        let mode = std::fs::metadata(&path).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
    }
}
