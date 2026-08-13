//! Entry point for the `lazyslurm` binary.

use std::process::ExitCode;
use std::sync::Arc;

use lazyslurm::cli::Args;
use lazyslurm::slurm::{CommandRunner, LocalRunner, Slurm};
use lazyslurm::ssh::{RemoteRunner, SshSession};
use lazyslurm::startup::Settings;
use lazyslurm::ui::app::RemoteSession;
use lazyslurm::ui::{run, App};

#[tokio::main]
async fn main() -> ExitCode {
    let args = Args::parse_args();
    if args.print_completions() {
        return ExitCode::SUCCESS;
    }
    let settings = Settings::discover(&args);

    // Anything worth telling the user about startup is shown in the command log
    // rather than printed, since the screen is about to be taken over.
    let mut notes = settings.notes.clone();
    notes.extend(settings.prune_caches());
    notes.extend(settings.overrides.iter().map(|entry| entry.to_string()));

    // Remote mode runs every command through one SSH session; local mode spawns
    // the Slurm binaries directly. Nothing above the transport knows which.
    let (runner, session): (Box<dyn CommandRunner>, _) = if settings.config.is_remote() {
        let (prompts, prompt_receiver) = tokio::sync::mpsc::unbounded_channel();
        let session = Arc::new(SshSession::new(settings.config.remote.clone()).with_prompt(
            Arc::new(move |question, secret| {
                let prompts = prompts.clone();
                Box::pin(async move {
                    // The modal lives on the main loop, so the question goes
                    // there and the answer comes back down a oneshot.
                    let (reply, answer) = tokio::sync::oneshot::channel();
                    if prompts.send((question, secret, reply)).is_err() {
                        return None;
                    }
                    answer.await.unwrap_or(None)
                })
            }),
        ));
        (
            Box::new(RemoteRunner::new(session.clone())),
            Some((session, prompt_receiver)),
        )
    } else {
        (Box::new(LocalRunner), None)
    };

    let slurm = Arc::new(
        Slurm::new(runner, settings.config.clone()).with_cache(Arc::new(settings.log_cache())),
    );

    let app = App::new(settings.config.clone());
    let remote = session.map(|(session, prompts)| RemoteSession { session, prompts });

    match run(slurm, app, settings, notes, remote).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            // The terminal has been restored by now, so this is visible.
            eprintln!("lazyslurm: {error:#}");
            ExitCode::FAILURE
        }
    }
}
