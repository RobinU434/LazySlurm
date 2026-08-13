//! Entry point for the `lazyslurm` binary.

use lazyslurm::cli::Args;
use lazyslurm::config::Config;
use lazyslurm::slurm::{LocalRunner, Slurm};
use lazyslurm::VERSION;

#[tokio::main]
async fn main() {
    let args = Args::parse_args();

    // TODO(P3): load ~/.config/lazyslurm/config.toml as the middle layer.
    let mut config = Config::default();
    let overrides = args.apply_to(&mut config);

    // TODO(P8): a remote target needs the SSH session as its transport.
    let slurm = Slurm::new(Box::new(LocalRunner), config);

    // TODO(P4): replace this with the terminal UI. Until then the binary is a
    // working front end for the query layer, which is what makes it testable
    // against a real cluster while the UI is being built.
    println!("lazyslurm {VERSION} (Rust) — TUI not yet implemented");
    for entry in &overrides {
        println!("  config override: {entry}");
    }

    let jobs = slurm.running_jobs().await;
    println!("\n{} active job(s) for {}", jobs.len(), slurm.config().effective_user());
    for job in jobs.iter().take(20) {
        println!(
            "  {:<14} {:<20} {:<10} {:<10} {}",
            job.job_id, job.name, job.state, job.elapsed, job.partition
        );
    }
}
