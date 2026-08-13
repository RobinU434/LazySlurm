//! Entry point for the `lazyslurm` binary.

use std::sync::Arc;

use lazyslurm::cli::Args;
use lazyslurm::slurm::{LocalRunner, Slurm};
use lazyslurm::startup::Settings;
use lazyslurm::VERSION;

#[tokio::main]
async fn main() {
    let args = Args::parse_args();
    let settings = Settings::discover(&args);

    // TODO(P8): a remote target needs the SSH session as its transport.
    let slurm = Slurm::new(Box::new(LocalRunner), settings.config.clone())
        .with_cache(Arc::new(settings.log_cache()));

    // TODO(P4): replace this with the terminal UI. Until then the binary is a
    // working front end for the query layer, which is what makes it testable
    // against a real cluster while the UI is being built.
    println!("lazyslurm {VERSION} (Rust) — TUI not yet implemented");

    for note in settings.notes.iter().chain(settings.prune_caches().iter()) {
        println!("  {note}");
    }
    for entry in &settings.overrides {
        println!("  config override: {entry}");
    }

    let jobs = slurm.running_jobs().await;
    println!(
        "\n{} active job(s) for {}",
        jobs.len(),
        settings.config.effective_user()
    );
    for job in jobs.iter().take(20) {
        println!(
            "  {:<14} {:<20} {:<10} {:<10} {}",
            job.job_id, job.name, job.state, job.elapsed, job.partition
        );
    }
}
