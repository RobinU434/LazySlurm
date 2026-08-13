//! Data types describing what Slurm reports, plus the pure helpers that
//! interpret them.
//!
//! Everything here is I/O-free and therefore directly unit-testable. Fields
//! hold Slurm's own text rather than parsed values wherever the UI displays them
//! verbatim: re-formatting a duration that Slurm already formatted only creates
//! opportunities to disagree with `squeue`.

pub mod accounting;
pub mod efficiency;
pub mod format;
pub mod job;
pub mod job_id;
pub mod partition;

pub use accounting::{FairShare, PriorityInfo, UsageRow};
pub use efficiency::{compute_efficiency, sizing_hint, Efficiency};
pub use format::{
    elapsed_seconds, format_bytes, format_duration, format_hours, gres_count, parse_duration,
    parse_mem_bytes, parse_req_mem,
};
pub use job::{
    CompletedJob, DetailSource, JobDetail, JobStats, PartitionJob, RunningJob, StatsSource,
};
pub use job_id::{array_index_span, array_task_count, base_job_id, sort_key};
pub use partition::{Aiot, NodeInfo, PartitionInfo};
