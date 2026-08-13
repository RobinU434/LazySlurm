//! The one SSH connection that remote mode runs everything through.
//!
//! See [`session`] for the design; [`pty`] is the piece that makes two-factor
//! authentication possible at all.

pub mod pty;
pub mod runner;
pub mod session;

pub use runner::RemoteRunner;
pub use session::{control_path, PromptCallback, PromptFuture, SshSession};
