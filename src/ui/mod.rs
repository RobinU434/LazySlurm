//! The terminal interface.
//!
//! Split so that the interesting half needs no terminal: [`filter`] and
//! [`job_table`] hold every state transition — filtering, array grouping,
//! expansion, cursor movement — and are asserted directly in tests. Rendering
//! turns that state into cells and nothing more.

pub mod app;
pub mod detail;
pub mod event;
pub mod filter;
pub mod help;
pub mod job_table;
pub mod layout;
pub mod log_pane;
pub mod metadata;
pub mod render;
pub mod tabs;
pub mod terminal;
pub mod text;
pub mod theme;

pub use app::{run, App};
pub use filter::{matches, parse_query, Field, Filterable, Op, Term};
pub use job_table::{Depth, JobRow, JobTable, Row, PLACEHOLDER_KEY};
pub use theme::Theme;
