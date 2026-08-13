//! How a Slurm command actually gets run.
//!
//! Local mode spawns the binary directly. Remote mode sends a shell line down
//! the one long-lived SSH session (see the `ssh` module). Both are hidden behind
//! [`CommandRunner`] so that everything above this layer — every query, every
//! action — is written once and tested against a recorded-output stub.

use std::future::Future;
use std::pin::Pin;
use std::process::Stdio;

use tokio::process::Command;

/// What a command produced.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Output {
    pub stdout: String,
    pub stderr: String,
    pub code: i32,
}

impl Output {
    /// A failure with a message but no output, for errors before the spawn.
    pub fn failure(message: impl Into<String>) -> Self {
        Self {
            stdout: String::new(),
            stderr: message.into(),
            code: 1,
        }
    }

    /// Whether the command exited cleanly *and* said something.
    ///
    /// Most Slurm commands report an empty result and a zero exit code when
    /// there is simply nothing to show, so callers almost always want both
    /// conditions rather than the exit code alone.
    pub fn is_useful(&self) -> bool {
        self.code == 0 && !self.stdout.trim().is_empty()
    }
}

/// A boxed future, so [`CommandRunner`] stays object-safe and the transport can
/// be swapped at runtime.
pub type OutputFuture<'a> = Pin<Box<dyn Future<Output = Output> + Send + 'a>>;

/// Anything that can run a Slurm command and hand back its output.
pub trait CommandRunner: Send + Sync {
    /// Run `args` as a single command, waiting for it to finish.
    fn run<'a>(&'a self, args: &'a [&'a str]) -> OutputFuture<'a>;

    /// Whether commands are executed on a remote host.
    ///
    /// A few callers genuinely need to know — reading a file, for instance, is a
    /// local `open` in one mode and a `tail` over SSH in the other.
    fn is_remote(&self) -> bool {
        false
    }
}

/// Shared ownership of a runner, so a caller can keep inspecting one after
/// handing it to a [`crate::slurm::Slurm`].
impl<T: CommandRunner + ?Sized> CommandRunner for std::sync::Arc<T> {
    fn run<'a>(&'a self, args: &'a [&'a str]) -> OutputFuture<'a> {
        (**self).run(args)
    }

    fn is_remote(&self) -> bool {
        (**self).is_remote()
    }
}

/// Runs commands as child processes on this machine.
#[derive(Debug, Default, Clone, Copy)]
pub struct LocalRunner;

impl CommandRunner for LocalRunner {
    fn run<'a>(&'a self, args: &'a [&'a str]) -> OutputFuture<'a> {
        Box::pin(async move {
            let Some((program, rest)) = args.split_first() else {
                return Output::failure("empty command");
            };

            let spawned = Command::new(program)
                .args(rest)
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .output()
                .await;

            match spawned {
                Ok(output) => Output {
                    // Slurm output is usually UTF-8, but a job name can hold
                    // anything; replacing bad bytes beats losing the row.
                    stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
                    stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
                    code: output.status.code().unwrap_or(1),
                },
                // A missing binary is the common case here (no Slurm on this
                // host), and the message is what the UI shows.
                Err(error) => Output::failure(format!("{program}: {error}")),
            }
        })
    }
}

/// Join an argv list into one shell command line, for the remote shell.
pub fn quote_argv(args: &[&str]) -> String {
    args.iter()
        .map(|arg| shell_words::quote(arg).into_owned())
        .collect::<Vec<_>>()
        .join(" ")
}

#[cfg(test)]
pub(crate) mod testing {
    //! A runner that replays canned output, so query and action code can be
    //! tested without a cluster.

    use super::*;
    use std::sync::Mutex;

    /// Decides what to reply with, given the argv it was handed.
    type Responder = Box<dyn Fn(&[&str]) -> Output + Send + Sync>;

    /// Records the commands it was asked to run and answers from a script.
    pub struct StubRunner {
        responder: Responder,
        calls: Mutex<Vec<Vec<String>>>,
        remote: bool,
    }

    impl StubRunner {
        /// Reply to every command with the same stdout.
        pub fn with_stdout(stdout: impl Into<String>) -> Self {
            let stdout = stdout.into();
            Self::new(move |_| Output {
                stdout: stdout.clone(),
                stderr: String::new(),
                code: 0,
            })
        }

        /// Reply based on the argv.
        pub fn new(responder: impl Fn(&[&str]) -> Output + Send + Sync + 'static) -> Self {
            Self {
                responder: Box::new(responder),
                calls: Mutex::new(Vec::new()),
                remote: false,
            }
        }

        /// Pretend to be a remote transport.
        pub fn remote(mut self) -> Self {
            self.remote = true;
            self
        }

        /// Every argv this runner was asked to run, in order.
        pub fn calls(&self) -> Vec<Vec<String>> {
            self.calls.lock().unwrap().clone()
        }

        /// The single argv this runner was asked to run.
        ///
        /// Panics if it was called any number of times other than once, which
        /// is itself usually the assertion a test wants.
        pub fn only_call(&self) -> Vec<String> {
            let calls = self.calls();
            assert_eq!(
                calls.len(),
                1,
                "expected exactly one command, got {calls:?}"
            );
            calls.into_iter().next().unwrap()
        }
    }

    impl CommandRunner for StubRunner {
        fn run<'a>(&'a self, args: &'a [&'a str]) -> OutputFuture<'a> {
            self.calls
                .lock()
                .unwrap()
                .push(args.iter().map(|a| (*a).to_string()).collect());
            let output = (self.responder)(args);
            Box::pin(async move { output })
        }

        fn is_remote(&self) -> bool {
            self.remote
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn runs_a_local_command() {
        let output = LocalRunner.run(&["echo", "hello"]).await;
        assert_eq!(output.code, 0);
        assert_eq!(output.stdout.trim(), "hello");
    }

    #[tokio::test]
    async fn reports_a_missing_binary_instead_of_panicking() {
        let output = LocalRunner.run(&["definitely-not-a-real-binary-xyz"]).await;
        assert_ne!(output.code, 0);
        assert!(output.stderr.contains("definitely-not-a-real-binary-xyz"));
    }

    #[tokio::test]
    async fn reports_a_nonzero_exit_code() {
        let output = LocalRunner.run(&["false"]).await;
        assert_eq!(output.code, 1);
    }

    #[test]
    fn quotes_arguments_for_a_remote_shell() {
        assert_eq!(
            quote_argv(&["squeue", "-u", "me", "--format=%i|%j"]),
            "squeue -u me '--format=%i|%j'"
        );
    }

    #[test]
    fn empty_output_is_not_useful_even_when_successful() {
        assert!(!Output::default().is_useful());
        assert!(Output {
            stdout: "data".into(),
            ..Output::default()
        }
        .is_useful());
    }
}
