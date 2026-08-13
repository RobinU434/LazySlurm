//! Running Slurm commands over the shared SSH session.
//!
//! This is the whole of what remote mode changes. Every query and action above
//! the transport is written once and works either way, which is what the
//! [`CommandRunner`] trait was for.

use std::sync::Arc;

use crate::slurm::transport::{quote_argv, CommandRunner, Output, OutputFuture};

use super::SshSession;

/// Runs commands on the login node, through one long-lived connection.
pub struct RemoteRunner {
    session: Arc<SshSession>,
}

impl RemoteRunner {
    pub fn new(session: Arc<SshSession>) -> Self {
        Self { session }
    }

    pub fn session(&self) -> &Arc<SshSession> {
        &self.session
    }
}

impl CommandRunner for RemoteRunner {
    fn run<'a>(&'a self, args: &'a [&'a str]) -> OutputFuture<'a> {
        Box::pin(async move {
            // The channel is a shell, so the argv becomes one command line.
            let (stdout, stderr, code) = self.session.run(&quote_argv(args)).await;
            Output {
                stdout,
                stderr,
                code,
            }
        })
    }

    fn is_remote(&self) -> bool {
        true
    }

    fn control_path(&self) -> Option<String> {
        Some(self.session.control_path().display().to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A runner whose "connection" is a local shell.
    async fn local_runner() -> RemoteRunner {
        let session = Arc::new(
            SshSession::new("test@localhost")
                .with_channel_command(vec!["/bin/sh".to_string(), "-s".to_string()]),
        );
        session.connect().await.expect("the local shell starts");
        RemoteRunner::new(session)
    }

    #[tokio::test]
    async fn runs_a_command_and_reports_its_output() {
        let runner = local_runner().await;
        let output = runner.run(&["echo", "hello"]).await;

        assert_eq!(output.stdout, "hello\n");
        assert_eq!(output.code, 0);
        runner.session().close().await;
    }

    #[tokio::test]
    async fn arguments_are_quoted_into_one_command_line() {
        // The channel is a shell, so anything with a space or a quote in it has
        // to survive being written as a single line.
        let runner = local_runner().await;
        let output = runner.run(&["echo", "a b", "c'd"]).await;

        assert_eq!(output.stdout, "a b c'd\n");
        runner.session().close().await;
    }

    #[tokio::test]
    async fn a_failing_command_reports_its_status_and_stderr() {
        let runner = local_runner().await;
        let output = runner.run(&["sh", "-c", "echo oops 1>&2; exit 7"]).await;

        assert_eq!(output.code, 7);
        assert_eq!(output.stderr, "oops\n");
        runner.session().close().await;
    }

    #[tokio::test]
    async fn it_identifies_itself_as_remote_and_offers_its_control_path() {
        let runner = local_runner().await;

        assert!(runner.is_remote());
        assert!(runner
            .control_path()
            .expect("a remote runner has one")
            .ends_with(".sock"));
        runner.session().close().await;
    }
}
