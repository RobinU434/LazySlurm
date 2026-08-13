//! LazySlurm — a terminal UI for monitoring Slurm HPC jobs.
//!
//! The crate is split so that every layer can be tested without the one above
//! it:
//!
//! - [`model`] — plain data types and pure formatting/parsing helpers. No I/O.
//! - `slurm` — builds Slurm command lines and turns their output into [`model`]
//!   types. Its parsers are pure functions; only its thin command layer does I/O.
//! - `ssh` — the single long-lived connection that remote mode runs through.
//! - `config` — CLI arguments, the persistent config file, and the on-disk caches.
//! - `ui` — terminal rendering and event handling.
//!
//! Nothing below `ui` knows that a terminal exists, which is what keeps the
//! interesting logic unit-testable.

pub mod cli;
pub mod config;
pub mod model;
pub mod slurm;
pub mod startup;
pub mod ui;

/// The crate version, as published to both crates.io and PyPI.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
