//! Talking to Slurm.
//!
//! The module is layered so that almost none of it needs a cluster to test:
//!
//! - [`parse`] turns recorded command output into [`crate::model`] types. Pure.
//! - [`reason`] turns Slurm's pending-reason codes into sentences. Pure.
//! - [`transport`] runs a command, locally or over the shared SSH session.
//! - [`query`] asks questions; [`action`] changes things; [`fs`] reads files.
//!
//! Everything stateful hangs off [`Slurm`], so there are no module-level globals
//! and a test can hold as many independent instances as it wants.

pub mod action;
pub mod cache;
pub mod fs;
pub mod live;
pub mod parse;
pub mod query;
pub mod reason;
pub mod transport;

pub use action::{
    archive_batch_script, build_update_args, cancel_job, get_batch_script, normalize_memory,
    resubmit_job, script_token_index, EditableField, Outcome, ScriptFallback, EDITABLE_FIELDS,
};
pub use cache::{DetailCache, ScriptStore};
pub use fs::{read_log_file, tail_file, TAIL_LINES};
pub use live::{gpu_status, node_processes};
pub use query::{Slurm, UsageWindow, USAGE_WINDOWS};
pub use reason::{explain, format_start_estimate};
pub use transport::{CommandRunner, LocalRunner, Output};
