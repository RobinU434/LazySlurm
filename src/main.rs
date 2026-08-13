//! Entry point for the `lazyslurm` binary.

use std::process::ExitCode;
use std::sync::Arc;

use lazyslurm::cli::Args;
use lazyslurm::slurm::{LocalRunner, Slurm};
use lazyslurm::startup::Settings;
use lazyslurm::ui::{run, App};

#[tokio::main]
async fn main() -> ExitCode {
    let args = Args::parse_args();
    let settings = Settings::discover(&args);

    // Anything worth telling the user about startup is shown in the command log
    // rather than printed, since the screen is about to be taken over.
    let mut notes = settings.notes.clone();
    notes.extend(settings.prune_caches());
    notes.extend(settings.overrides.iter().map(|entry| entry.to_string()));

    // TODO(P8): a remote target needs the SSH session as its transport.
    let slurm = Arc::new(
        Slurm::new(Box::new(LocalRunner), settings.config.clone())
            .with_cache(Arc::new(settings.log_cache())),
    );

    let app = App::new(settings.config);
    match run(slurm, app, notes).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            // The terminal has been restored by now, so this is visible.
            eprintln!("lazyslurm: {error:#}");
            ExitCode::FAILURE
        }
    }
}
