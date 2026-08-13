//! One persistent SSH connection, shared by every remote command.
//!
//! Remote mode used to be one `ssh host <cmd>` per Slurm call. That re-runs
//! authentication for every command unless the keys are passwordless, which
//! makes it unusable on a cluster with two-factor authentication — and even with
//! multiplexing it forks an ssh client per command.
//!
//! So the app opens **one** connection at startup:
//!
//! 1. [`SshSession::connect`] starts an SSH *master* (`-M -N`) attached to a
//!    pty. Because it owns a pty, ssh writes its password and verification-code
//!    prompts there instead of the terminal, so they can be forwarded to a
//!    callback — the TUI shows a modal — and the answer typed back. This is
//!    where 2FA happens: once, at startup.
//! 2. Once the master is up, a *shell channel* is opened over it
//!    (`ssh <host> /bin/sh -s`, multiplexed onto the master's connection, so no
//!    second authentication). It stays open for the life of the app.
//! 3. [`SshSession::run`] writes a command into that shell and reads the output
//!    back, framed by unique markers. No new process, no new connection, no
//!    re-authentication per command.
//!
//! The channel is serialised by a lock: commands queue rather than interleave.
//! If it dies — network drop, remote logout — the next `run` transparently
//! restarts it, re-authenticating only if the master is gone too.

use std::future::Future;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use regex::Regex;
use sha2::{Digest, Sha256};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command};
use tokio::sync::{Mutex, OnceCell};

use super::pty::PtyProcess;

/// What the caller does when the cluster asks a question. Returns the answer,
/// or `None` to give up.
pub type PromptFuture = Pin<Box<dyn Future<Output = Option<String>> + Send>>;
pub type PromptCallback = Arc<dyn Fn(String, bool) -> PromptFuture + Send + Sync>;

/// Options every ssh invocation shares.
const BASE_OPTIONS: &[&str] = &[
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=3",
];

/// How long to wait for one command before giving up on it.
const COMMAND_TIMEOUT: Duration = Duration::from_secs(20);

/// How long the whole authentication may take. Generous: somebody has to find
/// their phone and read a six-digit code off it.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(120);

/// How long to wait for output before re-checking whether the master came up.
const READ_POLL: Duration = Duration::from_secs(1);

/// The shapes an interactive prompt takes.
///
/// Each anchors at the end of a line, because they are matched against the tail
/// of what the master has written to its pty. Kept as separate patterns rather
/// than one string so a new one can be added without re-reading the whole
/// alternation.
const PROMPT_PATTERNS: &[&str] = &[
    r"password[^\n]*:\s*$",
    r"passphrase[^\n]*:\s*$",
    r"passcode[^\n]*:\s*$",
    r"verification code[^\n]*:\s*$",
    r"one[- ]time password[^\n]*:\s*$",
    r"\botp\b[^\n]*:\s*$",
    r"token[_ ]?response[^\n]*:\s*$",
    r"\bpin\b[^\n]*:\s*$",
    r"duo[^\n]*:\s*$",
    r"\(yes/no(?:/\[fingerprint\])?\)\?\s*$",
    // A generic PAM fallback: "Enter <something>:".
    r"^\s*enter [^\n]*:\s*$",
];

/// Lines that say why a connection failed.
const FAILURE_PATTERNS: &[&str] = &[
    "permission denied",
    "authentication failed",
    "too many authentication failures",
    "no route to host",
    "connection (refused|closed|timed out)",
    "could not resolve",
];

/// Anything ssh or PAM asks for interactively.
fn prompt_pattern() -> &'static Regex {
    static PATTERN: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(&format!("(?im){}", PROMPT_PATTERNS.join("|")))
            .expect("the prompt patterns compile")
    })
}

/// Host-key confirmation is the one prompt that is not a secret.
fn non_secret_pattern() -> &'static Regex {
    static PATTERN: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    PATTERN.get_or_init(|| Regex::new(r"(?i)\(yes/no").expect("compiles"))
}

/// Lines that explain why a connection failed.
fn failure_pattern() -> &'static Regex {
    static PATTERN: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(&format!("(?i){}", FAILURE_PATTERNS.join("|")))
            .expect("the failure patterns compile")
    })
}

/// A short, stable control-socket path for a host.
///
/// Not ssh's own `%C` token: the path is needed before the connection exists.
/// Unix sockets are length-limited, hence the hash rather than the host name.
pub fn control_path(host: &str) -> PathBuf {
    let digest = Sha256::digest(host.as_bytes());
    let name: String = digest
        .iter()
        .flat_map(|byte| [byte >> 4, byte & 0x0f])
        .take(16)
        .map(|nibble| char::from_digit(u32::from(nibble), 16).unwrap_or('0'))
        .collect();
    control_dir().join(format!("{name}.sock"))
}

fn control_dir() -> PathBuf {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    home.join(".ssh").join("cm-lazyslurm")
}

/// The shell the commands are written into.
#[derive(Default)]
struct Channel {
    child: Option<Child>,
    stdin: Option<ChildStdin>,
    stdout: Option<BufReader<ChildStdout>>,
    stderr: Option<BufReader<ChildStderr>>,
    seq: u64,
}

impl Channel {
    fn is_open(&self) -> bool {
        self.child.is_some()
    }

    async fn kill(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _ = child.start_kill();
            let _ = child.wait().await;
        }
        self.stdin = None;
        self.stdout = None;
        self.stderr = None;
    }
}

/// One long-lived SSH connection.
pub struct SshSession {
    host: String,
    control_path: PathBuf,
    prompt: Option<PromptCallback>,
    command_timeout: Duration,
    connect_timeout: Duration,
    /// Whether to run a multiplexing master at all.
    use_master: bool,
    /// Overrides the shell channel's argv. A test seam: it runs a local
    /// `/bin/sh` rather than reaching for a cluster.
    channel_command: Option<Vec<String>>,
    /// Overrides the master's argv, for the same reason.
    master_command: Option<Vec<String>>,
    /// A file whose existence means "the master is up", replacing `ssh -O check`
    /// in tests.
    ready_file: Option<PathBuf>,

    channel: Mutex<Channel>,
    master: Mutex<Option<PtyProcess>>,
    closed: AtomicBool,
    /// Created once, on first use.
    control_dir_ready: OnceCell<()>,
}

impl SshSession {
    /// A session for `host`, not yet connected.
    pub fn new(host: impl Into<String>) -> Self {
        let host = host.into();
        Self {
            control_path: control_path(&host),
            host,
            prompt: None,
            command_timeout: COMMAND_TIMEOUT,
            connect_timeout: CONNECT_TIMEOUT,
            use_master: true,
            channel_command: None,
            master_command: None,
            ready_file: None,
            channel: Mutex::new(Channel::default()),
            master: Mutex::new(None),
            closed: AtomicBool::new(false),
            control_dir_ready: OnceCell::new(),
        }
    }

    /// Answer the cluster's questions with this callback.
    pub fn with_prompt(mut self, prompt: PromptCallback) -> Self {
        self.prompt = Some(prompt);
        self
    }

    pub fn with_command_timeout(mut self, timeout: Duration) -> Self {
        self.command_timeout = timeout;
        self
    }

    pub fn with_connect_timeout(mut self, timeout: Duration) -> Self {
        self.connect_timeout = timeout;
        self
    }

    /// Run this instead of `ssh host /bin/sh -s`, and skip the master.
    ///
    /// A test seam: it exercises the whole command path against a local shell.
    pub fn with_channel_command(mut self, argv: Vec<String>) -> Self {
        self.channel_command = Some(argv);
        self.use_master = false;
        self
    }

    /// Run this instead of `ssh -M -N`, and treat `ready` as "the master is up".
    pub fn with_master_command(mut self, argv: Vec<String>, ready: PathBuf) -> Self {
        self.master_command = Some(argv);
        self.ready_file = Some(ready);
        self.use_master = true;
        self
    }

    pub fn host(&self) -> &str {
        &self.host
    }

    pub fn control_path(&self) -> &Path {
        &self.control_path
    }

    /// Whether the shell channel is open.
    pub async fn connected(&self) -> bool {
        self.channel.lock().await.is_open()
    }

    /// Authenticate once, then open the shared shell channel.
    pub async fn connect(&self) -> Result<String, String> {
        self.closed.store(false, Ordering::Relaxed);
        if self.use_master {
            self.start_master().await?;
        }
        let mut channel = self.channel.lock().await;
        self.start_channel(&mut channel).await?;
        Ok(format!("Connected to {}", self.host))
    }

    // -- the master ---------------------------------------------------------

    /// Argv for the background master connection.
    fn master_argv(&self) -> Vec<String> {
        if let Some(argv) = &self.master_command {
            return argv.clone();
        }

        let mut argv = vec![
            "ssh".to_string(),
            "-N".to_string(),
            "-M".to_string(),
            "-o".to_string(),
            format!("ControlPath={}", self.control_path.display()),
            "-o".to_string(),
            "ControlPersist=no".to_string(),
        ];
        argv.extend(BASE_OPTIONS.iter().map(|option| (*option).to_string()));

        if self.prompt.is_none() {
            // Nobody can answer a prompt: fail fast rather than hang forever.
            argv.extend(["-o".to_string(), "BatchMode=yes".to_string()]);
        } else {
            argv.extend(["-o".to_string(), "NumberOfPasswordPrompts=3".to_string()]);
        }
        argv.push(self.host.clone());
        argv
    }

    /// Ask ssh whether the multiplexing master is up.
    async fn master_alive(&self) -> bool {
        if let Some(ready) = &self.ready_file {
            return ready.exists();
        }
        Command::new("ssh")
            .args([
                "-o",
                &format!("ControlPath={}", self.control_path.display()),
                "-O",
                "check",
                &self.host,
            ])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await
            .map(|status| status.success())
            .unwrap_or(false)
    }

    /// Spawn the master on a pty and answer its prompts.
    async fn start_master(&self) -> Result<String, String> {
        if self.master_alive().await {
            return Ok("Reusing existing SSH master".to_string());
        }

        // A test that supplies its own master has no control socket to make.
        if self.ready_file.is_none() {
            self.ensure_control_dir().await?;
        }

        let pty = PtyProcess::spawn(&self.master_argv())
            .map_err(|error| format!("Failed to start ssh: {error}"))?;
        let mut guard = self.master.lock().await;
        *guard = Some(pty);

        match tokio::time::timeout(self.connect_timeout, self.drive_auth(&mut guard)).await {
            Ok(result) => result,
            Err(_) => {
                if let Some(pty) = guard.as_mut() {
                    pty.kill().await;
                }
                *guard = None;
                Err(format!("Timed out connecting to {}", self.host))
            }
        }
    }

    /// Create the multiplexing socket's directory, once.
    ///
    /// At the point of use rather than at startup: nothing should touch
    /// `~/.ssh` just because the program was launched.
    async fn ensure_control_dir(&self) -> Result<(), String> {
        self.control_dir_ready
            .get_or_try_init(|| async {
                let dir = control_dir();
                std::fs::create_dir_all(&dir)
                    .map_err(|error| format!("Cannot create {}: {error}", dir.display()))?;
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    let _ = std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700));
                }
                Ok::<(), String>(())
            })
            .await
            .copied()
    }

    /// Pump the master's pty: forward prompts out, write answers back.
    async fn drive_auth(&self, master: &mut Option<PtyProcess>) -> Result<String, String> {
        let mut buffer = String::new();

        loop {
            let Some(pty) = master.as_mut() else {
                return Err("the master went away".to_string());
            };

            if pty.exited() {
                // ssh exited: either it failed, or — rarely — the master is up
                // anyway. Check before reporting an error.
                return if self.master_alive().await {
                    Ok("SSH master ready".to_string())
                } else {
                    Err(self.auth_error(&buffer))
                };
            }
            if self.master_alive().await {
                return Ok("SSH master ready".to_string());
            }

            let chunk = match tokio::time::timeout(READ_POLL, pty.read()).await {
                // Nothing said yet — loop back and re-check the master.
                Err(_) => continue,
                Ok(None) => {
                    return if self.master_alive().await {
                        Ok("SSH master ready".to_string())
                    } else {
                        Err(self.auth_error(&buffer))
                    }
                }
                Ok(Some(chunk)) => chunk,
            };
            buffer.push_str(&chunk);

            let cleaned = buffer.replace('\r', "");
            let Some(found) = prompt_pattern().find(&cleaned) else {
                continue;
            };
            let question = found.as_str().trim().to_string();
            let secret = !non_secret_pattern().is_match(&question);

            let Some(prompt) = &self.prompt else {
                pty.kill().await;
                *master = None;
                return Err(format!(
                    "{} asked for '{question}' but no input is available",
                    self.host
                ));
            };

            match prompt(question, secret).await {
                Some(answer) => {
                    if let Some(pty) = master.as_mut() {
                        pty.write_line(&answer)
                            .map_err(|error| format!("Could not answer the prompt: {error}"))?;
                    }
                    // Consumed: do not match this prompt again.
                    buffer.clear();
                }
                None => {
                    if let Some(pty) = master.as_mut() {
                        pty.kill().await;
                    }
                    *master = None;
                    return Err("Authentication cancelled".to_string());
                }
            }
        }
    }

    /// Turn the master's pty output into one useful error line.
    pub fn auth_error(&self, buffer: &str) -> String {
        let text = buffer.replace('\r', "");
        for line in text.lines().rev() {
            if failure_pattern().is_match(line) {
                return line.trim().to_string();
            }
        }
        match text.lines().next_back().map(str::trim) {
            Some(last) if !last.is_empty() => last.to_string(),
            _ => format!("Could not connect to {}", self.host),
        }
    }

    // -- the channel --------------------------------------------------------

    /// Open the shell that every command is then written into.
    async fn start_channel(&self, channel: &mut Channel) -> Result<(), String> {
        let argv = self.channel_command.clone().unwrap_or_else(|| {
            let mut argv = vec![
                "ssh".to_string(),
                "-o".to_string(),
                format!("ControlPath={}", self.control_path.display()),
                "-o".to_string(),
                "ControlMaster=no".to_string(),
                // The master has already authenticated.
                "-o".to_string(),
                "BatchMode=yes".to_string(),
            ];
            argv.extend(BASE_OPTIONS.iter().map(|option| (*option).to_string()));
            argv.push(self.host.clone());
            argv.push("/bin/sh -s".to_string());
            argv
        });

        let (program, args) = argv.split_first().ok_or("empty channel command")?;
        let mut child = Command::new(program)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|error| format!("Failed to open SSH channel: {error}"))?;

        channel.stdin = child.stdin.take();
        channel.stdout = child.stdout.take().map(BufReader::new);
        channel.stderr = child.stderr.take().map(BufReader::new);
        channel.child = Some(child);
        Ok(())
    }

    /// Bring the channel back, re-authenticating only if the master died too.
    async fn reconnect(&self, channel: &mut Channel) -> Result<(), String> {
        channel.kill().await;
        if self.use_master && !self.master_alive().await {
            self.start_master().await?;
        }
        self.start_channel(channel).await
    }

    /// Run a command in the shared shell.
    pub async fn run(&self, command: &str) -> (String, String, i32) {
        if self.closed.load(Ordering::Relaxed) {
            return (String::new(), "SSH session closed".to_string(), 1);
        }

        let mut channel = self.channel.lock().await;
        if !channel.is_open() {
            if let Err(error) = self.reconnect(&mut channel).await {
                return (String::new(), error, 1);
            }
        }

        match tokio::time::timeout(self.command_timeout, exchange(&mut channel, command)).await {
            Ok(Ok(result)) => result,
            Ok(Err(error)) => {
                channel.kill().await;
                (String::new(), format!("SSH channel lost: {error}"), 1)
            }
            Err(_) => {
                // The shell is mid-command and out of sync — drop it. The next
                // run() opens a fresh one over the same connection.
                channel.kill().await;
                (String::new(), format!("Timed out running: {command}"), 1)
            }
        }
    }

    /// Close the channel and tear the connection down.
    pub async fn close(&self) {
        self.closed.store(true, Ordering::Relaxed);
        self.channel.lock().await.kill().await;

        if self.use_master && self.ready_file.is_none() {
            // Ask ssh to close the master cleanly before killing anything.
            let _ = Command::new("ssh")
                .args([
                    "-o",
                    &format!("ControlPath={}", self.control_path.display()),
                    "-O",
                    "exit",
                    &self.host,
                ])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .await;
        }
        if let Some(pty) = self.master.lock().await.as_mut() {
            pty.kill().await;
        }
    }
}

/// Write one command and read back its framed stdout, stderr and status.
async fn exchange(channel: &mut Channel, command: &str) -> std::io::Result<(String, String, i32)> {
    channel.seq += 1;
    let marker = format!("__LAZYSLURM_{}__", channel.seq);

    // The command runs, then both streams are stamped with the marker. The
    // stdout stamp carries the exit status; the stderr stamp just tells the
    // reader where this command's stderr ends.
    let payload = format!(
        "{command}\n__lzs_rc=$?\nprintf '%s%s\\n' '{marker}' \"$__lzs_rc\"\nprintf '%s\\n' '{marker}' 1>&2\n"
    );

    let stdin = channel
        .stdin
        .as_mut()
        .ok_or_else(|| std::io::Error::other("no channel stdin"))?;
    stdin.write_all(payload.as_bytes()).await?;
    stdin.flush().await?;

    // Both streams must be read at once: waiting for one while the other fills
    // its pipe buffer would deadlock.
    let Channel { stdout, stderr, .. } = channel;
    let (out, err) = tokio::join!(
        read_until_marker(stdout.as_mut(), &marker),
        read_until_marker(stderr.as_mut(), &marker),
    );

    let (stdout_text, status) = out?;
    let (stderr_text, _) = err?;
    let code = status.trim().parse::<i32>().unwrap_or(1);
    Ok((stdout_text, stderr_text, code))
}

/// Read lines until the marker line.
///
/// Returns the text before it, and whatever the marker line carried after the
/// marker itself — the exit status on stdout, nothing on stderr.
async fn read_until_marker<R>(
    reader: Option<&mut BufReader<R>>,
    marker: &str,
) -> std::io::Result<(String, String)>
where
    R: tokio::io::AsyncRead + Unpin,
{
    let Some(reader) = reader else {
        return Ok((String::new(), String::new()));
    };

    let mut collected = String::new();
    loop {
        let mut line = Vec::new();
        // Bytes rather than a string: a job name can hold anything, and losing
        // the whole command to one invalid sequence would be worse.
        if reader.read_until(b'\n', &mut line).await? == 0 {
            // EOF: the channel died mid-command.
            return Ok((collected, String::new()));
        }
        let text = String::from_utf8_lossy(&line);

        if let Some(index) = text.find(marker) {
            collected.push_str(&text[..index]);
            return Ok((collected, text[index + marker.len()..].to_string()));
        }
        collected.push_str(&text);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A session whose "ssh channel" is a local shell — no network involved.
    fn local_session() -> SshSession {
        SshSession::new("test@localhost")
            .with_channel_command(vec!["/bin/sh".to_string(), "-s".to_string()])
    }

    #[test]
    fn prompt_patterns_match_real_ssh_prompts() {
        for text in [
            "user@login's password: ",
            "Password: ",
            "Enter passphrase for key '/home/u/.ssh/id_ed25519': ",
            "Verification code: ",
            "Duo two-factor login for user\n\nPasscode or option (1-3): ",
            "One-time password (OATH-TOTP): ",
            "OTP: ",
            "Token_Response: ",
            "Enter PIN: ",
            "Are you sure you want to continue connecting (yes/no/[fingerprint])? ",
        ] {
            assert!(
                prompt_pattern().is_match(&text.replace('\r', "")),
                "should have matched: {text:?}"
            );
        }
    }

    #[test]
    fn prompt_patterns_ignore_ordinary_output() {
        for text in [
            "Last login: Tue Aug 12 09:00:00 2026 from 10.0.0.1",
            "Welcome to the cluster!",
            "debug1: Authenticating to login:22 as 'user'",
        ] {
            assert!(!prompt_pattern().is_match(text), "matched: {text:?}");
        }
    }

    #[test]
    fn the_host_key_prompt_is_not_treated_as_a_secret() {
        assert!(non_secret_pattern().is_match("(yes/no/[fingerprint])?"));
        assert!(!non_secret_pattern().is_match("Verification code:"));
    }

    #[test]
    fn the_control_path_is_stable_and_per_host() {
        let a = control_path("user@login.hpc.edu");
        assert_eq!(a, control_path("user@login.hpc.edu"));
        assert_ne!(a, control_path("user@other.hpc.edu"));
        assert!(a.to_string_lossy().ends_with(".sock"));
    }

    #[test]
    fn the_auth_error_picks_the_meaningful_line() {
        let session = local_session();
        assert_eq!(
            session.auth_error(
                "debug1: connecting\r\nPermission denied (publickey,keyboard-interactive).\r\n"
            ),
            "Permission denied (publickey,keyboard-interactive)."
        );
        // With nothing to go on it still says something.
        assert!(session.auth_error("").contains("Could not connect"));
    }

    #[test]
    fn without_a_callback_the_master_runs_in_batch_mode() {
        // Nothing can answer a prompt, so it must fail fast rather than block.
        let silent = SshSession::new("user@login");
        assert!(silent.master_argv().contains(&"BatchMode=yes".to_string()));

        let interactive =
            SshSession::new("user@login").with_prompt(Arc::new(|_, _| Box::pin(async { None })));
        assert!(!interactive
            .master_argv()
            .contains(&"BatchMode=yes".to_string()));
    }

    #[tokio::test]
    async fn commands_share_one_channel_and_keep_shell_state() {
        let session = local_session();
        session.connect().await.expect("the local shell starts");

        assert_eq!(
            session.run("echo hello").await,
            ("hello\n".to_string(), String::new(), 0)
        );

        // State set by one command is visible to the next: proof they run in the
        // same shell, not in separate ssh invocations.
        session.run("MARKER=persisted").await;
        let (out, _, _) = session.run("echo $MARKER").await;
        assert_eq!(out, "persisted\n");

        session.close().await;
    }

    #[tokio::test]
    async fn stdout_and_stderr_stay_separate_and_the_status_is_reported() {
        let session = local_session();
        session.connect().await.unwrap();

        let (out, err, code) = session.run("echo to-out; echo to-err 1>&2; (exit 3)").await;
        assert_eq!(out, "to-out\n");
        assert_eq!(err, "to-err\n");
        assert_eq!(code, 3);

        session.close().await;
    }

    #[tokio::test]
    async fn output_that_looks_like_a_marker_is_not_confused_for_one() {
        let session = local_session();
        session.connect().await.unwrap();

        let (out, _, code) = session.run("echo '__LAZYSLURM_99__ in my data'").await;
        assert_eq!(out, "__LAZYSLURM_99__ in my data\n");
        assert_eq!(code, 0);

        // The counter has moved on, so a later command still frames correctly.
        let (out, _, code) = session.run("echo after").await;
        assert_eq!((out.as_str(), code), ("after\n", 0));

        session.close().await;
    }

    #[tokio::test]
    async fn multiline_output_comes_back_whole() {
        let session = local_session();
        session.connect().await.unwrap();

        let (out, _, code) = session.run("printf 'a\\nb\\nc\\n'").await;
        assert_eq!(out, "a\nb\nc\n");
        assert_eq!(code, 0);

        session.close().await;
    }

    #[tokio::test]
    async fn commands_are_serialised_rather_than_interleaved() {
        let session = Arc::new(local_session());
        session.connect().await.unwrap();

        let mut handles = Vec::new();
        for index in 0..8 {
            let session = session.clone();
            handles.push(tokio::spawn(async move {
                session.run(&format!("echo job{index}")).await
            }));
        }

        for (index, handle) in handles.into_iter().enumerate() {
            let (out, _, _) = handle.await.unwrap();
            assert_eq!(out, format!("job{index}\n"));
        }
        session.close().await;
    }

    #[tokio::test]
    async fn a_timeout_recycles_the_channel_and_later_commands_work() {
        let session = local_session().with_command_timeout(Duration::from_millis(500));
        session.connect().await.unwrap();

        let (_, err, code) = session.run("sleep 5").await;
        assert_eq!(code, 1);
        assert!(err.contains("Timed out"), "{err}");
        // Dropped, because it is out of sync.
        assert!(!session.connected().await);

        // The next call transparently reopens it.
        let (out, _, code) = session.run("echo recovered").await;
        assert_eq!((out.as_str(), code), ("recovered\n", 0));

        session.close().await;
    }

    #[tokio::test]
    async fn a_dead_channel_is_recovered_on_the_next_command() {
        let session = local_session();
        session.connect().await.unwrap();
        session.run("echo warm").await;

        // The remote end goes away.
        session.channel.lock().await.kill().await;

        let (out, _, code) = session.run("echo back").await;
        assert_eq!((out.as_str(), code), ("back\n", 0));

        session.close().await;
    }

    /// Stands in for ssh: prompts on its pty exactly as a 2FA login does, and
    /// "becomes reachable" — touches the ready file — only once both answers
    /// are right.
    const FAKE_SSH: &str = r#"
ready="$1"
echo "debug1: Authenticating to login:22"
printf "jdoe@login's password: "
read password
printf "\nDuo two-factor login for jdoe\n\nPasscode or option (1-3): "
read code
if [ "$password" = "hunter2" ] && [ "$code" = "123456" ]; then
  : > "$ready"
  printf "\nAuthenticated.\n"
  # Stay up briefly, so the "master still running" path is the one tested.
  sleep 2
else
  printf "\nPermission denied (keyboard-interactive).\n"
  exit 255
fi
"#;

    /// A session whose master is a local script that prompts like a 2FA login.
    fn fake_auth_session(dir: &tempfile::TempDir, prompt: PromptCallback) -> (SshSession, PathBuf) {
        let script = dir.path().join("fake_ssh.sh");
        std::fs::write(&script, FAKE_SSH).unwrap();
        let ready = dir.path().join("ready");

        let session = SshSession::new("jdoe@login")
            .with_channel_command(vec!["/bin/sh".to_string(), "-s".to_string()])
            .with_master_command(
                vec![
                    "/bin/sh".to_string(),
                    script.display().to_string(),
                    ready.display().to_string(),
                ],
                ready.clone(),
            )
            .with_prompt(prompt)
            .with_connect_timeout(Duration::from_secs(20));
        (session, ready)
    }

    /// The questions a callback was asked, and whether each was masked.
    type SeenPrompts = Arc<std::sync::Mutex<Vec<(String, bool)>>>;

    /// A callback that records what it was asked and answers from a table.
    fn recording_prompt(
        answers: Vec<(&'static str, &'static str)>,
    ) -> (PromptCallback, SeenPrompts) {
        let seen = Arc::new(std::sync::Mutex::new(Vec::new()));
        let recorder = seen.clone();

        let callback: PromptCallback = Arc::new(move |question: String, secret: bool| {
            recorder.lock().unwrap().push((question.clone(), secret));
            let lowered = question.to_lowercase();
            let answer = answers
                .iter()
                .find(|(needle, _)| lowered.contains(needle))
                .map(|(_, answer)| (*answer).to_string());
            Box::pin(async move { answer }) as PromptFuture
        });
        (callback, seen)
    }

    #[tokio::test]
    async fn two_factor_prompts_are_forwarded_and_answered() {
        // The password *and* the verification code reach the callback, in order.
        let dir = tempfile::tempdir().unwrap();
        let (prompt, seen) =
            recording_prompt(vec![("password", "hunter2"), ("passcode", "123456")]);
        let (session, _ready) = fake_auth_session(&dir, prompt);

        session.connect().await.expect("both answers are right");

        // And the session works for commands afterwards.
        let (out, _, code) = session.run("echo authenticated").await;
        assert_eq!((out.as_str(), code), ("authenticated\n", 0));
        session.close().await;

        let seen = seen.lock().unwrap();
        assert_eq!(seen.len(), 2, "{seen:?}");
        assert!(seen[0].0.to_lowercase().contains("password"), "{seen:?}");
        assert!(seen[0].1, "a password must be masked");
        assert!(seen[1].0.to_lowercase().contains("passcode"), "{seen:?}");
        assert!(seen[1].1, "a verification code must be masked");
    }

    #[tokio::test]
    async fn a_wrong_code_reports_the_servers_own_error() {
        let dir = tempfile::tempdir().unwrap();
        let (prompt, _) = recording_prompt(vec![("password", "hunter2"), ("passcode", "000000")]);
        let (session, _ready) = fake_auth_session(&dir, prompt);

        let error = session.connect().await.expect_err("the code is wrong");
        session.close().await;

        assert!(error.contains("Permission denied"), "{error}");
    }

    #[tokio::test]
    async fn cancelling_a_prompt_aborts_the_connection() {
        let dir = tempfile::tempdir().unwrap();
        // The user pressed Escape.
        let (prompt, _) = recording_prompt(vec![]);
        let (session, _ready) = fake_auth_session(&dir, prompt);

        let error = session.connect().await.expect_err("the user gave up");
        session.close().await;

        assert!(error.to_lowercase().contains("cancelled"), "{error}");
    }

    #[tokio::test]
    async fn running_after_close_fails_rather_than_reconnecting() {
        let session = local_session();
        session.connect().await.unwrap();
        session.close().await;

        let (out, err, code) = session.run("echo nope").await;
        assert_eq!((out.as_str(), code), ("", 1));
        assert!(err.contains("closed"), "{err}");
    }
}
