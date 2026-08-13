//! The main screen: state, keys, and the loop that drives them.
//!
//! Key handling resolves a keystroke to an [`Action`] through [`super::help`],
//! then applies it and returns a [`Command`] for anything the runner has to do.
//! Nothing here touches a terminal, so every keystroke can be tested by feeding
//! it to [`App::handle_key`] and asserting on the state that came out.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use chrono::Local;
use crossterm::event::{KeyCode, KeyEvent};
use ratatui::Frame;

use crate::config::Config;
use crate::model::{
    array_task_count, parse_duration, parse_mem_bytes, CompletedJob, JobStats, RunningJob,
};
use crate::slurm::{Slurm, TAIL_LINES};
use crate::startup::Settings;

use super::detail::{DetailView, ResourceHistory};
use super::event::{DetailLoaded, Event, Events, JobsLoaded, Sender, UsageLoaded};
use super::help::{self, Action, Context};
use super::job_table::JobTable;
use super::layout::main_layout;
use super::metadata::MetadataView;
use super::modal::{EditModal, Modal, Outcome, Request, SshPrompt};
use super::render::{self, LogEntry, TableStyle};
use super::screens::{NodeScreen, Pane, PartitionScreen, UsageScreen};
use super::terminal::{install_panic_hook, Session};
use super::theme::Theme;

/// The columns of a somebody-else's-jobs table.
const PARTITION_JOB_COLUMNS: &[&str] = &[
    "Job ID",
    "User",
    "Name",
    "State",
    "Time",
    "Limit",
    "N",
    "CPUs",
    "GRES",
    "Node/Reason",
];

/// The key bar shown on the partition and node screens.
const SCREEN_KEYS: &[(&str, &str)] = &[
    ("Esc", "back"),
    ("↵", "nodes"),
    ("Tab", "panel"),
    ("r", "refresh"),
    ("?", "help"),
];

/// How many command-log entries to keep.
const LOG_LIMIT: usize = 200;

/// How many samples of a running job's resource use to keep.
const HISTORY_LIMIT: usize = 60;

/// How long to wait before loading a job's details.
///
/// Arrowing through a list would otherwise fire a handful of Slurm commands per
/// row; this lets the cursor settle first.
const SELECTION_DEBOUNCE: Duration = Duration::from_millis(200);

/// Which panel has focus.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Panel {
    Active,
    Completed,
    Detail,
    Metadata,
}

impl Panel {
    /// The panels in the order Tab moves through them.
    const ORDER: [Panel; 4] = [
        Panel::Active,
        Panel::Completed,
        Panel::Detail,
        Panel::Metadata,
    ];

    fn step(self, delta: isize) -> Self {
        let index = Self::ORDER
            .iter()
            .position(|panel| *panel == self)
            .unwrap_or(0);
        let count = Self::ORDER.len() as isize;
        Self::ORDER[(((index as isize + delta) % count + count) % count) as usize]
    }

    /// Whether this panel is one of the job tables.
    fn is_table(self) -> bool {
        matches!(self, Self::Active | Self::Completed)
    }

    /// Which help context this panel is in.
    fn context(self) -> Context {
        match self {
            Self::Active | Self::Completed => Context::Jobs,
            Self::Detail => Context::Detail,
            Self::Metadata => Context::Metadata,
        }
    }
}

/// A full-screen panel stacked over the main view.
pub enum Screen {
    Partitions(PartitionScreen),
    Nodes(NodeScreen),
    Usage(UsageScreen),
}

impl Screen {
    fn context(&self) -> Context {
        match self {
            Self::Partitions(_) => Context::Partitions,
            Self::Nodes(_) => Context::Nodes,
            Self::Usage(_) => Context::Usage,
        }
    }
}

/// Something that needs the terminal to itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Shell {
    /// Open a file in the user's editor.
    Edit { path: String, label: String },
    /// Open a file read-only, for a batch script.
    View { path: String, label: String },
    /// Browse a whole log in the pager.
    Page { path: String, label: String },
    /// Open a shell on a compute node.
    Ssh { node: String },
    /// Edit the config file, then reload it.
    EditConfig,
}

/// What a keystroke asked the runner to do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    /// Nothing beyond the state change already applied.
    None,
    Quit,
    /// Poll Slurm now.
    Refresh,
    /// The selection changed; load that job's details.
    LoadDetail,
    /// A screen was opened or moved; load what it needs.
    LoadScreen,
    /// Ask Slurm to change something.
    Run(Request),
    /// Leave the interface and run something in the terminal.
    Shell(Shell),
    /// Fetch a job's batch script, then show it.
    ViewScript {
        job_id: String,
    },
}

/// The main screen.
pub struct App {
    pub config: Config,
    pub theme: Theme,
    pub active: JobTable<RunningJob>,
    pub completed: JobTable<CompletedJob>,
    pub detail: DetailView,
    pub metadata: MetadataView,
    focus: Panel,
    /// The filter text, when the bar is open.
    search: Option<String>,
    help_open: bool,
    bookmarks: BTreeSet<String>,
    log: Vec<LogEntry>,
    running_tasks: u32,
    pending_tasks: u32,
    partitions: Vec<String>,
    /// The job whose details are currently shown.
    shown_job: Option<String>,
    /// Bumped on every selection change, so stale loads can be dropped.
    generation: Arc<AtomicU64>,
    /// Sampled resource use, per running job.
    history: BTreeMap<String, ResourceHistory>,
    /// The last `(TotalCPU, Elapsed)` seen per job, for CPU rate deltas.
    cpu_samples: BTreeMap<String, (f64, f64)>,
    /// Full-screen panels stacked over the main view; empty means the main view.
    stack: Vec<Screen>,
    /// The node the shown job is on, for live monitoring.
    metadata_node: Option<String>,
    /// The shown job's log paths and submit command, kept so the editor, the
    /// pager and resubmission do not have to ask Slurm all over again.
    stdout_path: Option<String>,
    stderr_path: Option<String>,
    shown_submit: Option<String>,
    shown_work_dir: String,
    /// The open dialog, if any. It takes every key while it is up.
    modal: Option<Modal>,
    /// Where the answer to an SSH prompt has to go.
    ssh_reply: Option<tokio::sync::oneshot::Sender<Option<String>>>,
    /// Where a multi-select range started, and in which table.
    multiselect: Option<(Panel, String)>,
    selected_ids: BTreeSet<String>,
}

impl App {
    pub fn new(config: Config) -> Self {
        let theme = Theme::new(&config.partition_colors, config.abbreviate_states);
        let collapse = config.collapse_arrays;
        let show_gpu = !config.no_gpu && !config.no_live;

        let mut app = Self {
            active: JobTable::new(collapse),
            completed: JobTable::new(collapse),
            detail: DetailView::new(show_gpu),
            metadata: MetadataView::new(),
            config,
            theme,
            focus: Panel::Active,
            search: None,
            help_open: false,
            bookmarks: BTreeSet::new(),
            log: Vec::new(),
            running_tasks: 0,
            pending_tasks: 0,
            partitions: Vec::new(),
            shown_job: None,
            generation: Arc::new(AtomicU64::new(0)),
            history: BTreeMap::new(),
            cpu_samples: BTreeMap::new(),
            stack: Vec::new(),
            metadata_node: None,
            stdout_path: None,
            stderr_path: None,
            shown_submit: None,
            shown_work_dir: String::new(),
            modal: None,
            ssh_reply: None,
            multiselect: None,
            selected_ids: BTreeSet::new(),
        };
        app.active.set_focused(true);
        app
    }

    // -- state --------------------------------------------------------------

    /// Apply the result of a poll.
    pub fn apply_jobs(&mut self, loaded: JobsLoaded) {
        // Count array *tasks*, not rows: one pending `123_[3-11]` row stands for
        // nine jobs, and the cluster bar says so.
        let tasks = |state: &str, jobs: &[RunningJob]| -> u32 {
            jobs.iter()
                .filter(|job| job.state == state)
                .map(|job| array_task_count(&job.job_id))
                .sum()
        };
        self.running_tasks = tasks("RUNNING", &loaded.running);
        self.pending_tasks = tasks("PENDING", &loaded.running);
        self.partitions = loaded.partitions;

        // Forget history for jobs that are no longer running, so a long session
        // does not accumulate a series per job the user ever looked at.
        let live: BTreeSet<&str> = loaded.running.iter().map(|j| j.job_id.as_str()).collect();
        self.history
            .retain(|job_id, _| live.contains(job_id.as_str()));
        self.cpu_samples
            .retain(|job_id, _| live.contains(job_id.as_str()));

        self.active.set_jobs(loaded.running);
        self.completed.set_jobs(loaded.completed);
    }

    /// Apply a finished detail load, unless the user has moved on since.
    pub fn apply_detail(&mut self, loaded: DetailLoaded) -> bool {
        if loaded.generation != self.generation.load(Ordering::Relaxed) {
            return false;
        }

        self.shown_job = Some(loaded.job_id.clone());
        self.detail.stdout.set_content(loaded.stdout);
        self.detail.stderr.set_content(loaded.stderr);

        if let Some(stats) = &loaded.stats {
            self.sample_resources(&loaded.job_id, stats);
        }
        self.detail.set_history(
            self.history
                .get(&loaded.job_id)
                .cloned()
                .unwrap_or_default(),
        );
        self.detail.set_stats(loaded.stats);

        self.metadata_node = loaded
            .detail
            .as_ref()
            .map(|detail| detail.node_list().to_string())
            .filter(|node| crate::model::job::has_node(node));

        if let Some(detail) = &loaded.detail {
            self.stdout_path = detail.stdout_path.clone();
            self.stderr_path = detail.stderr_path.clone();
            self.shown_work_dir = detail.work_dir.clone();
            self.shown_submit = Some(detail.submit_line().to_string())
                .filter(|line| !crate::model::job::is_missing(line));
        } else {
            self.stdout_path = None;
            self.stderr_path = None;
            self.shown_submit = None;
            self.shown_work_dir.clear();
        }
        self.metadata
            .set_detail(loaded.detail, loaded.priority, loaded.sprio_available);
        true
    }

    /// Record one sample of a running job's resource use.
    ///
    /// The CPU series is a genuine rate: `TotalCPU` is cumulative, so the change
    /// between samples over the elapsed change gives core-equivalents busy. The
    /// Python plots `AveRSS` here and labels it CPU, which is memory twice.
    fn sample_resources(&mut self, job_id: &str, stats: &JobStats) {
        let history = self.history.entry(job_id.to_string()).or_default();

        if let Some(memory) = parse_mem_bytes(&stats.max_rss) {
            history.memory.push(memory);
            if history.memory.len() > HISTORY_LIMIT {
                history.memory.remove(0);
            }
        }

        let total_cpu = parse_duration(&stats.total_cpu);
        let elapsed = parse_duration(&stats.elapsed);
        if let (Some(total_cpu), Some(elapsed)) = (total_cpu, elapsed) {
            if let Some((last_cpu, last_elapsed)) = self.cpu_samples.get(job_id) {
                let interval = elapsed - last_elapsed;
                if interval > 0.0 {
                    history.cpu.push((total_cpu - last_cpu) / interval);
                    if history.cpu.len() > HISTORY_LIMIT {
                        history.cpu.remove(0);
                    }
                }
            }
            self.cpu_samples
                .insert(job_id.to_string(), (total_cpu, elapsed));
        }
    }

    /// Apply data that belongs to whichever screen is open.
    ///
    /// Every one of these is dropped when the matching screen is no longer on
    /// top: a reply that arrives after the user has moved on has nowhere to go.
    pub fn apply_partitions(&mut self, partitions: Vec<crate::model::PartitionInfo>) {
        if let Some(Screen::Partitions(screen)) = self.stack.last_mut() {
            screen.partitions.set_items(partitions);
        }
    }

    pub fn apply_partition_jobs(&mut self, partition: &str, jobs: Vec<crate::model::PartitionJob>) {
        if let Some(Screen::Partitions(screen)) = self.stack.last_mut() {
            if screen.selected_partition() == Some(partition) {
                screen.jobs.set_items(jobs);
                screen.shown = Some(partition.to_string());
            }
        }
    }

    pub fn apply_nodes(&mut self, partition: &str, nodes: Vec<crate::model::NodeInfo>) {
        if let Some(Screen::Nodes(screen)) = self.stack.last_mut() {
            if screen.partition == partition {
                screen.nodes.set_items(nodes);
            }
        }
    }

    pub fn apply_node_jobs(&mut self, node: &str, jobs: Vec<crate::model::PartitionJob>) {
        if let Some(Screen::Nodes(screen)) = self.stack.last_mut() {
            if screen.selected_node() == Some(node) {
                screen.jobs.set_items(jobs);
                screen.shown = Some(node.to_string());
            }
        }
    }

    pub fn apply_usage(&mut self, loaded: UsageLoaded) {
        if let Some(Screen::Usage(screen)) = self.stack.last_mut() {
            screen.rows.set_items(loaded.rows);
            screen.shares = loaded.shares;
            screen.accounting_available = loaded.accounting_available;
            screen.loading = false;
        }
    }

    /// Fill a live-monitoring tab with what a compute node reported.
    pub fn apply_live(&mut self, tab: &str, content: String) {
        match tab {
            "cpu" => self.detail.cpu.set_content(content),
            "gpu" => self.detail.gpu.set_content(content),
            _ => {}
        }
    }

    /// Which live tab, if any, is open and wants refreshing.
    ///
    /// Fetched only while its tab is showing: each one is an SSH round trip to a
    /// compute node, and paying for it behind a hidden tab would be waste.
    pub fn live_tab(&self) -> Option<&'static str> {
        if self.config.no_live || !self.stack.is_empty() {
            return None;
        }
        match self.detail.active_tab() {
            Some("cpu") => Some("cpu"),
            Some("gpu") if !self.config.no_gpu => Some("gpu"),
            _ => None,
        }
    }

    /// The node the selected job is running on, for live monitoring.
    pub fn selected_node(&self) -> Option<String> {
        self.metadata_node.clone()
    }

    /// Write a timestamped entry to the command log.
    pub fn log(&mut self, action: impl Into<String>, result: Option<String>) {
        self.log.push(LogEntry {
            time: Local::now().format("%H:%M:%S").to_string(),
            action: action.into(),
            result,
        });
        if self.log.len() > LOG_LIMIT {
            self.log.remove(0);
        }
    }

    pub fn focus(&self) -> Panel {
        self.focus
    }

    pub fn is_searching(&self) -> bool {
        self.search.is_some()
    }

    pub fn is_help_open(&self) -> bool {
        self.help_open
    }

    /// The next generation number, claimed for a fresh detail load.
    pub fn next_generation(&self) -> u64 {
        self.generation.fetch_add(1, Ordering::Relaxed) + 1
    }

    pub fn generation_handle(&self) -> Arc<AtomicU64> {
        self.generation.clone()
    }

    /// The job the detail panels should show.
    pub fn selected_job_id(&self) -> Option<&str> {
        // The right-hand panels follow whichever table was last in use, so
        // moving focus into them does not blank what they are showing.
        match self.focus {
            Panel::Completed => self.completed.selected_job_id(),
            Panel::Active => self.active.selected_job_id(),
            _ => self
                .shown_job
                .as_deref()
                .or_else(|| self.active.selected_job_id()),
        }
    }

    fn set_focus(&mut self, focus: Panel) {
        self.focus = focus;
        self.active.set_focused(focus == Panel::Active);
        self.completed.set_focused(focus == Panel::Completed);
    }

    // -- keys ---------------------------------------------------------------

    /// Handle one keystroke.
    pub fn handle_key(&mut self, key: KeyEvent) -> Command {
        // The help overlay swallows everything: any key closes it.
        if self.help_open {
            self.help_open = false;
            return Command::None;
        }
        if let Some(modal) = &mut self.modal {
            return match modal.handle_key(key) {
                Outcome::Continue => Command::None,
                Outcome::Dismissed => {
                    self.modal = None;
                    self.exit_multiselect();
                    Command::None
                }
                Outcome::Accepted(request) => {
                    self.modal = None;
                    self.exit_multiselect();
                    Command::Run(request)
                }
                Outcome::Answered(answer) => {
                    self.modal = None;
                    // Whoever asked is blocked on this; send it and move on.
                    if let Some(reply) = self.ssh_reply.take() {
                        let _ = reply.send(answer);
                    }
                    Command::None
                }
            };
        }
        if self.search.is_some() {
            return self.handle_search_key(key);
        }

        let Some(action) = help::lookup(self.context(), &key) else {
            return Command::None;
        };
        self.apply_action(action)
    }

    /// The help and key context: the top screen, or the focused panel.
    pub fn context(&self) -> Context {
        match self.stack.last() {
            Some(screen) => screen.context(),
            None => self.focus.context(),
        }
    }

    /// The screen the keys are currently driving, if any.
    pub fn top_screen(&self) -> Option<&Screen> {
        self.stack.last()
    }

    /// Open a full-screen panel.
    fn push_screen(&mut self, screen: Screen) {
        self.stack.push(screen);
    }

    /// Move the cursor on the top screen, or hand back to the main view.
    fn screen_cursor(&mut self, delta: isize) -> Command {
        match self.stack.last_mut() {
            Some(Screen::Partitions(screen)) => match screen.focus {
                Pane::Top => {
                    screen.partitions.move_cursor(delta);
                }
                Pane::Bottom => {
                    screen.jobs.move_cursor(delta);
                }
            },
            Some(Screen::Nodes(screen)) => match screen.focus {
                Pane::Top => {
                    screen.nodes.move_cursor(delta);
                }
                Pane::Bottom => {
                    screen.jobs.move_cursor(delta);
                }
            },
            Some(Screen::Usage(screen)) => {
                screen.rows.move_cursor(delta);
            }
            None => return Command::None,
        }
        Command::LoadScreen
    }

    fn apply_action(&mut self, action: Action) -> Command {
        match action {
            Action::Quit => return Command::Quit,
            Action::Refresh => return Command::Refresh,
            Action::Help => self.help_open = true,
            Action::ToggleSearch => self.search = Some(String::new()),
            Action::Bookmark => self.toggle_bookmark(),
            Action::ToggleExpand => self.toggle_expand(),
            Action::FocusNextPanel => self.set_focus(self.focus.step(1)),
            Action::FocusPrevPanel => self.set_focus(self.focus.step(-1)),
            Action::NextDetailTab => self.detail.cycle_tab(1),
            Action::PrevDetailTab => self.detail.cycle_tab(-1),
            Action::NextMetaTab => self.metadata.cycle_tab(1),
            Action::PrevMetaTab => self.metadata.cycle_tab(-1),
            Action::ScrollUp => self.scroll_panel(-1),
            Action::ScrollDown => self.scroll_panel(1),
            Action::MoveUp => {
                return if self.stack.is_empty() {
                    self.move_cursor(-1)
                } else {
                    self.screen_cursor(-1)
                }
            }
            Action::MoveDown => {
                return if self.stack.is_empty() {
                    self.move_cursor(1)
                } else {
                    self.screen_cursor(1)
                }
            }
            Action::MoveTop => return self.select_edge(false),
            Action::MoveBottom => return self.select_edge(true),
            Action::OpenPartitions => {
                self.push_screen(Screen::Partitions(PartitionScreen::new()));
                return Command::LoadScreen;
            }
            Action::OpenUsage => {
                let user = self.config.effective_user();
                self.push_screen(Screen::Usage(UsageScreen::new(user)));
                return Command::LoadScreen;
            }
            Action::ShowNodes => {
                if let Some(Screen::Partitions(screen)) = self.stack.last() {
                    if let Some(partition) = screen.selected_partition() {
                        let screen = NodeScreen::new(partition);
                        self.push_screen(Screen::Nodes(screen));
                        return Command::LoadScreen;
                    }
                }
            }
            Action::SwitchPane => match self.stack.last_mut() {
                Some(Screen::Partitions(screen)) => screen.focus = screen.focus.other(),
                Some(Screen::Nodes(screen)) => screen.focus = screen.focus.other(),
                _ => {}
            },
            Action::CycleWindow => {
                if let Some(Screen::Usage(screen)) = self.stack.last_mut() {
                    screen.window = screen.window.next();
                    screen.loading = true;
                    return Command::LoadScreen;
                }
            }
            Action::Back => {
                self.stack.pop();
            }
            Action::Cancel => {
                let ids = self.action_targets();
                if ids.is_empty() {
                    self.log("cancel", Some("no job selected".into()));
                } else {
                    self.modal = Some(Modal::ConfirmCancel { ids });
                }
            }
            Action::ForceCancel => {
                // No confirmation, deliberately: this is the key you reach for
                // when a job has to die now.
                let ids = self.action_targets();
                if ids.is_empty() {
                    self.log("force cancel", Some("no job selected".into()));
                } else {
                    self.exit_multiselect();
                    return Command::Run(Request::Cancel { ids, force: true });
                }
            }
            Action::MultiSelect => self.toggle_multiselect(),
            Action::EditJob => return self.open_editor(),
            Action::Resubmit => return self.open_resubmit(),
            Action::ViewScript => {
                if let Some(job_id) = self.selected_job_id().map(str::to_string) {
                    return Command::ViewScript { job_id };
                }
            }
            Action::EditStdout => return self.open_log(false, false),
            Action::EditStderr => return self.open_log(true, false),
            Action::PageLog => {
                // The pager opens whichever log the detail panel is showing.
                let stderr = self.detail.active_tab() == Some("stderr");
                return self.open_log(stderr, true);
            }
            Action::SshToNode => match self.selected_node() {
                Some(node) => {
                    return Command::Shell(Shell::Ssh {
                        node: crate::slurm::parse::first_node(&node),
                    })
                }
                None => self.log("ssh", Some("no node assigned to this job".into())),
            },
            Action::EditConfig => return Command::Shell(Shell::EditConfig),
        }
        Command::None
    }

    // -- actions ------------------------------------------------------------

    /// The jobs an action applies to.
    ///
    /// Multi-select wins; otherwise a collapsed array is targeted as a whole,
    /// because `scancel 123` takes every task of it; otherwise the one job.
    fn action_targets(&self) -> Vec<String> {
        if !self.selected_ids.is_empty() {
            return self.selected_ids.iter().cloned().collect();
        }
        match self.focus {
            Panel::Completed => self.completed.selected_key().map(str::to_string),
            _ => self.active.selected_key().map(str::to_string),
        }
        .into_iter()
        .collect()
    }

    /// Open the property editor for the selected pending job(s).
    fn open_editor(&mut self) -> Command {
        if self.focus == Panel::Completed {
            self.log("edit", Some("only pending jobs can be edited".into()));
            return Command::None;
        }

        // A selected row may stand for a whole array; edit its tasks.
        let targets: Vec<String> = self
            .active
            .expand_ids(self.action_targets().iter().map(String::as_str));

        let (pending, skipped): (Vec<String>, Vec<String>) = targets.into_iter().partition(|id| {
            self.active
                .jobs()
                .iter()
                .any(|job| job.job_id == *id && job.state == "PENDING")
        });

        if !skipped.is_empty() {
            self.log(
                "edit",
                Some(format!("skipping {} non-pending job(s)", skipped.len())),
            );
        }
        if pending.is_empty() {
            self.log("edit", Some("only pending jobs can be edited".into()));
            return Command::None;
        }

        // Prefill from the job's current values when editing exactly one.
        let current = (pending.len() == 1)
            .then(|| {
                self.active
                    .jobs()
                    .iter()
                    .find(|job| job.job_id == pending[0])
                    .map(|job| {
                        crate::slurm::EDITABLE_FIELDS
                            .iter()
                            .map(|field| (field.current)(job).to_string())
                            .collect::<Vec<_>>()
                    })
            })
            .flatten();

        self.modal = Some(Modal::Edit(EditModal::new(pending, current)));
        Command::None
    }

    /// Ask to resubmit the selected terminated job.
    fn open_resubmit(&mut self) -> Command {
        if self.focus != Panel::Completed {
            self.log(
                "resubmit",
                Some("only available for terminated jobs".into()),
            );
            return Command::None;
        }
        let Some(job_id) = self.completed.selected_job_id().map(str::to_string) else {
            return Command::None;
        };

        // The submit line comes from the metadata panel, which has already
        // loaded it; asking Slurm again would only repeat that work.
        let Some(command) = self.shown_submit_line() else {
            self.log(
                "resubmit",
                Some("cannot determine the submit command for this job".into()),
            );
            return Command::None;
        };

        self.modal = Some(Modal::ConfirmResubmit {
            job_id,
            command,
            work_dir: self.shown_work_dir.clone(),
        });
        Command::None
    }

    /// Open a log in the editor, or in the pager.
    fn open_log(&mut self, stderr: bool, pager: bool) -> Command {
        let (path, label) = if stderr {
            (self.stderr_path.clone(), "stderr")
        } else {
            (self.stdout_path.clone(), "stdout")
        };

        let Some(path) = path else {
            self.log(
                if pager { "page" } else { "edit" },
                Some(format!("no {label} path available")),
            );
            return Command::None;
        };

        let label = label.to_string();
        Command::Shell(if pager {
            Shell::Page { path, label }
        } else {
            Shell::Edit { path, label }
        })
    }

    // -- multi-select -------------------------------------------------------

    /// Start or stop selecting a range.
    fn toggle_multiselect(&mut self) {
        if self.multiselect.is_some() {
            self.exit_multiselect();
            self.log("multi-select", Some("disabled".into()));
            return;
        }
        if !self.focus.is_table() {
            self.log("multi-select", Some("focus a job table first".into()));
            return;
        }

        let anchor = match self.focus {
            Panel::Completed => self.completed.selected_key(),
            _ => self.active.selected_key(),
        };
        let Some(anchor) = anchor.map(str::to_string) else {
            self.log(
                "multi-select",
                Some("no job to anchor the selection".into()),
            );
            return;
        };

        self.multiselect = Some((self.focus, anchor.clone()));
        self.selected_ids = BTreeSet::from([anchor]);
        self.push_multiselect();
        self.log(
            "multi-select",
            Some("Up/Down extends · c cancels all · Ctrl+V exits".into()),
        );
    }

    fn exit_multiselect(&mut self) {
        if self.multiselect.take().is_none() && self.selected_ids.is_empty() {
            return;
        }
        self.selected_ids.clear();
        self.push_multiselect();
    }

    /// Recompute the selected range from the anchor to the cursor.
    fn extend_multiselect(&mut self) {
        let Some((panel, anchor)) = self.multiselect.clone() else {
            return;
        };
        let (order, current) = match panel {
            Panel::Completed => (
                self.completed.row_keys(),
                self.completed.selected_key().map(str::to_string),
            ),
            _ => (
                self.active.row_keys(),
                self.active.selected_key().map(str::to_string),
            ),
        };

        let (Some(from), Some(to)) = (
            order.iter().position(|key| *key == anchor),
            current.and_then(|key| order.iter().position(|row| *row == key)),
        ) else {
            return;
        };

        let (low, high) = if from <= to { (from, to) } else { (to, from) };
        self.selected_ids = order[low..=high].iter().cloned().collect();
        self.push_multiselect();
    }

    fn push_multiselect(&mut self) {
        self.active.set_multiselected(self.selected_ids.clone());
        self.completed.set_multiselected(self.selected_ids.clone());
    }

    /// Ask the user whatever the cluster is asking.
    pub fn ask_ssh(
        &mut self,
        question: String,
        secret: bool,
        reply: tokio::sync::oneshot::Sender<Option<String>>,
    ) {
        let host = self.config.remote.clone();
        self.modal = Some(Modal::SshPrompt(SshPrompt::new(host, question, secret)));
        self.ssh_reply = Some(reply);
    }

    /// Adopt a reloaded configuration, reporting what changed.
    pub fn apply_config(&mut self, config: Config) -> Vec<String> {
        let old = self.config.clone();
        let mut changes = Vec::new();

        let mut note = |name: &str, before: String, after: String| {
            if before != after {
                changes.push(format!("{name}: {before} → {after}"));
            }
        };
        note(
            "refresh",
            old.refresh.to_string(),
            config.refresh.to_string(),
        );
        note("days", old.days.to_string(), config.days.to_string());
        note("editor", old.editor.clone(), config.editor.clone());
        note("pager", old.pager.clone(), config.pager.clone());
        note(
            "max_name_width",
            old.max_name_width.to_string(),
            config.max_name_width.to_string(),
        );
        note(
            "collapse_arrays",
            old.collapse_arrays.to_string(),
            config.collapse_arrays.to_string(),
        );
        if old.partition_colors != config.partition_colors {
            changes.push("partition_colors updated".to_string());
        }

        self.theme = Theme::new(&config.partition_colors, config.abbreviate_states);
        if old.collapse_arrays != config.collapse_arrays {
            self.active.set_collapse_arrays(config.collapse_arrays);
            self.completed.set_collapse_arrays(config.collapse_arrays);
        }
        self.config = config;
        changes
    }

    /// The submit command of the job currently shown, if it has a usable one.
    fn shown_submit_line(&self) -> Option<String> {
        self.shown_submit.clone()
    }

    /// The jobs currently multi-selected.
    pub fn selected_ids(&self) -> &BTreeSet<String> {
        &self.selected_ids
    }

    pub fn is_multiselecting(&self) -> bool {
        self.multiselect.is_some()
    }

    fn scroll_panel(&mut self, delta: isize) {
        match self.focus {
            Panel::Detail => self.detail.scroll_by(delta),
            Panel::Metadata => self.metadata.scroll_by(delta),
            _ => {}
        }
    }

    fn handle_search_key(&mut self, key: KeyEvent) -> Command {
        let Some(query) = self.search.as_mut() else {
            return Command::None;
        };

        match key.code {
            // Escape abandons the filter entirely.
            KeyCode::Esc => self.close_search(true),
            // Enter keeps it and returns to the table, so a filtered list can
            // actually be navigated.
            KeyCode::Enter => self.close_search(false),
            KeyCode::Backspace => {
                query.pop();
                self.apply_filter();
            }
            KeyCode::Char(character) => {
                query.push(character);
                self.apply_filter();
            }
            _ => return Command::None,
        }
        // Filtering changes which job is selected, so the panels must follow.
        Command::LoadDetail
    }

    /// Close the filter bar, optionally discarding the query.
    fn close_search(&mut self, clear: bool) {
        self.search = None;
        if clear {
            self.active.set_filter("");
            self.completed.set_filter("");
        }
    }

    fn apply_filter(&mut self) {
        let query = self.search.clone().unwrap_or_default();
        self.active.set_filter(&query);
        self.completed.set_filter(&query);
    }

    /// Move the cursor, wrapping between the two tables at their edges.
    fn move_cursor(&mut self, delta: isize) -> Command {
        if !self.focus.is_table() {
            return Command::None;
        }

        let moved = match self.focus {
            Panel::Completed => self.completed.move_cursor(delta),
            _ => self.active.move_cursor(delta),
        };
        if moved {
            self.extend_multiselect();
            return Command::LoadDetail;
        }

        // At the edge: hand over to the other table, entering from the side the
        // cursor was travelling towards.
        let other = if self.focus == Panel::Active {
            Panel::Completed
        } else {
            Panel::Active
        };
        let has_rows = match other {
            Panel::Completed => self.completed.row_count() > 0,
            _ => self.active.row_count() > 0,
        };
        if !has_rows {
            return Command::None;
        }

        self.set_focus(other);
        let enter_at_end = delta < 0;
        match other {
            Panel::Completed => self.completed.select_edge(enter_at_end),
            _ => self.active.select_edge(enter_at_end),
        }
        Command::LoadDetail
    }

    fn select_edge(&mut self, last: bool) -> Command {
        match self.focus {
            Panel::Active => self.active.select_edge(last),
            Panel::Completed => self.completed.select_edge(last),
            _ => return Command::None,
        }
        Command::LoadDetail
    }

    fn toggle_expand(&mut self) {
        match self.focus {
            Panel::Active => self.active.toggle_expand(None),
            Panel::Completed => self.completed.toggle_expand(None),
            _ => false,
        };
    }

    /// Bookmark the selected row.
    ///
    /// On a collapsed array this bookmarks the array itself, so the whole group
    /// pins to the top rather than one arbitrary task.
    fn toggle_bookmark(&mut self) {
        let target = match self.focus {
            Panel::Active => self.active.selected_key().map(str::to_string),
            Panel::Completed => self.completed.selected_key().map(str::to_string),
            _ => None,
        };
        let Some(target) = target else {
            return;
        };

        if !self.bookmarks.remove(&target) {
            self.bookmarks.insert(target);
        }
        self.active.set_bookmarks(self.bookmarks.clone());
        self.completed.set_bookmarks(self.bookmarks.clone());
    }

    pub fn bookmarks(&self) -> &BTreeSet<String> {
        &self.bookmarks
    }

    // -- drawing ------------------------------------------------------------

    pub fn draw(&mut self, frame: &mut Frame) {
        // A full-screen panel replaces the main view entirely, as it does in
        // the Python — it is a screen, not an overlay.
        if !self.stack.is_empty() {
            self.draw_screen(frame);
            if self.help_open {
                render::help_overlay(frame, frame.area(), &help::help_lines(self.context()));
            }
            self.draw_modal(frame);
            return;
        }

        let log_lines = self
            .log
            .iter()
            .map(|entry| if entry.result.is_some() { 2 } else { 1 })
            .sum::<u16>()
            .saturating_add(2); // the panel's own borders
        let layout = main_layout(frame.area(), self.search.is_some(), log_lines);

        render::cluster_bar(
            frame,
            layout.cluster_bar,
            &self.config.effective_user(),
            self.running_tasks,
            self.pending_tasks,
            &self.partitions,
        );

        if let Some(area) = layout.search {
            let query = self.search.clone().unwrap_or_default();
            render::search_bar(frame, area, &query);
            let (x, y) = render::search_cursor(area, &query);
            frame.set_cursor_position((x, y));
        }

        let searching = self.search.is_some();
        render::job_table(
            frame,
            layout.active_jobs,
            &self.active,
            &TableStyle {
                theme: &self.theme,
                config: &self.config,
                focused: self.focus == Panel::Active && !searching,
            },
        );
        render::job_table(
            frame,
            layout.completed_jobs,
            &self.completed,
            &TableStyle {
                theme: &self.theme,
                config: &self.config,
                focused: self.focus == Panel::Completed && !searching,
            },
        );

        self.draw_detail(frame, layout.detail, searching);
        self.draw_metadata(frame, layout.metadata, searching);

        render::command_log(frame, layout.command_log, &self.log);
        render::footer(
            frame,
            layout.footer,
            &[
                ("q", "quit"),
                ("?", "help"),
                ("/", "filter"),
                ("r", "refresh"),
                ("Tab", "panel"),
            ],
        );

        if self.help_open {
            render::help_overlay(frame, frame.area(), &help::help_lines(self.focus.context()));
        }
        self.draw_modal(frame);
    }

    /// Draw the open dialog, if there is one.
    fn draw_modal(&self, frame: &mut Frame) {
        let Some(modal) = &self.modal else {
            return;
        };
        let (title, content) = modal.view();
        // Cancellation is the one that cannot be undone, so it is marked.
        let danger = matches!(modal, Modal::ConfirmCancel { .. });
        render::modal(frame, frame.area(), &title, &content, danger);
    }

    /// Draw whichever full-screen panel is on top.
    fn draw_screen(&mut self, frame: &mut Frame) {
        let theme = self.theme.clone();
        let user = self.config.effective_user();

        match self.stack.last().expect("a screen is open") {
            Screen::Partitions(screen) => {
                let (bar, top, bottom, footer) = super::layout::stacked_layout(frame.area(), 2, 3);
                render::summary_bar(frame, bar, screen.summary());

                let rows = screen
                    .partitions
                    .items()
                    .iter()
                    .map(|partition| screen.row(partition, &theme))
                    .collect();
                render::simple_table(
                    frame,
                    top,
                    "Partitions",
                    &[
                        "Partition",
                        "Load",
                        "Nodes A/I/O/T",
                        "CPUs A/I/O/T",
                        "Run",
                        "Pend",
                        "Limit",
                        "GRES",
                    ],
                    rows,
                    screen.partitions.selected_index(),
                    screen.focus == Pane::Top,
                );

                let title = match screen.selected_partition() {
                    Some(name) => format!("Jobs on {name} ({})", screen.jobs.len()),
                    None => "Jobs on partition".to_string(),
                };
                let jobs = screen
                    .jobs
                    .items()
                    .iter()
                    .map(|job| super::screens::partition_job_row(job, &user))
                    .collect();
                render::simple_table(
                    frame,
                    bottom,
                    &title,
                    PARTITION_JOB_COLUMNS,
                    jobs,
                    screen.jobs.selected_index(),
                    screen.focus == Pane::Bottom,
                );
                render::footer(frame, footer, SCREEN_KEYS);
            }
            Screen::Nodes(screen) => {
                let (bar, top, bottom, footer) = super::layout::stacked_layout(frame.area(), 3, 2);
                render::summary_bar(frame, bar, screen.summary());

                let rows = screen
                    .nodes
                    .items()
                    .iter()
                    .map(|node| screen.row(node, &theme))
                    .collect();
                render::simple_table(
                    frame,
                    top,
                    &format!("Nodes of {}", screen.partition),
                    &[
                        "Node",
                        "State",
                        "CPUs A/I/O/T",
                        "Load",
                        "Memory",
                        "GPUs",
                        "Reason",
                    ],
                    rows,
                    screen.nodes.selected_index(),
                    screen.focus == Pane::Top,
                );

                let title = match screen.selected_node() {
                    Some(node) => format!("Jobs on {node} ({})", screen.jobs.len()),
                    None => "Jobs on node".to_string(),
                };
                let jobs = screen
                    .jobs
                    .items()
                    .iter()
                    .map(|job| super::screens::partition_job_row(job, &user))
                    .collect();
                render::simple_table(
                    frame,
                    bottom,
                    &title,
                    PARTITION_JOB_COLUMNS,
                    jobs,
                    screen.jobs.selected_index(),
                    screen.focus == Pane::Bottom,
                );
                render::footer(frame, footer, SCREEN_KEYS);
            }
            Screen::Usage(screen) => {
                let (bar, top, bottom, footer) = super::layout::stacked_layout(frame.area(), 1, 3);
                render::summary_bar(frame, bar, screen.summary());

                let block = render::panel("Fair share", false);
                let inner = block.inner(top);
                frame.render_widget(block, top);
                render::lines(frame, inner, &screen.fairshare_lines(), 0);

                let total = screen.total_hours();
                let rows = screen
                    .rows
                    .items()
                    .iter()
                    .filter(|row| !row.is_account_total())
                    .map(|row| screen.row(row, total))
                    .collect();
                render::simple_table(
                    frame,
                    bottom,
                    "Account usage",
                    &["User", "Name", "CPU hours", "Share", "%"],
                    rows,
                    screen.rows.selected_index(),
                    true,
                );
                render::footer(
                    frame,
                    footer,
                    &[
                        ("Esc", "back"),
                        ("w", "window"),
                        ("r", "refresh"),
                        ("?", "help"),
                    ],
                );
            }
        }
    }

    fn draw_detail(&mut self, frame: &mut Frame, area: ratatui::layout::Rect, searching: bool) {
        let focused = self.focus == Panel::Detail && !searching;
        let content = render::tabbed_panel(frame, area, "Job Details", self.detail.tabs(), focused);

        match self.detail.active_tab() {
            Some("stdout") => super::log_pane::render(frame, content, &mut self.detail.stdout),
            Some("stderr") => super::log_pane::render(frame, content, &mut self.detail.stderr),
            Some("cpu") => super::log_pane::render(frame, content, &mut self.detail.cpu),
            Some("gpu") => super::log_pane::render(frame, content, &mut self.detail.gpu),
            Some("stats") => render::lines(
                frame,
                content,
                &self.detail.stats_lines(),
                self.detail.stats_scroll(),
            ),
            _ => {}
        }
    }

    fn draw_metadata(&mut self, frame: &mut Frame, area: ratatui::layout::Rect, searching: bool) {
        let focused = self.focus == Panel::Metadata && !searching;
        let content =
            render::tabbed_panel(frame, area, "Job Metadata", self.metadata.tabs(), focused);
        render::lines(
            frame,
            content,
            &self.metadata.lines(),
            self.metadata.scroll_offset(),
        );
    }
}

/// Run the interface until the user quits.
pub async fn run(
    slurm: Arc<Slurm>,
    mut app: App,
    settings: Settings,
    notes: Vec<String>,
    remote: Option<RemoteSession>,
) -> Result<()> {
    let (session_handle, prompts) = match remote {
        Some(remote) => (Some(remote.session), Some(remote.prompts)),
        None => (None, None),
    };
    install_panic_hook();
    let mut session = Session::enter()?;
    let mut events = Events::start();

    if let Some(prompts) = prompts {
        spawn_prompt_bridge(prompts, events.sender());
    }
    for note in notes {
        app.log(note, None);
    }
    warn_about_login_node(&mut app);

    if app.config.auto_refresh() {
        events.start_ticker(Duration::from_secs_f64(app.config.refresh));
    } else {
        app.log("auto-refresh", Some("disabled (refresh=0)".into()));
    }

    // Remote mode authenticates before anything else: the 2FA prompt has to be
    // answered up front, not in the middle of the first poll.
    match &session_handle {
        Some(ssh) => {
            app.log(
                format!("ssh {}", app.config.remote),
                Some("opening session...".into()),
            );
            spawn_connect(ssh.clone(), events.sender());
        }
        None => spawn_poll(slurm.clone(), events.sender()),
    }

    session.terminal().draw(|frame| app.draw(frame))?;

    while let Some(event) = events.next().await {
        match event {
            Event::Key(key) => match app.handle_key(key) {
                Command::Quit => break,
                Command::Refresh => {
                    app.log("refresh", None);
                    // In remote mode a dropped or refused session is retried
                    // here — the user's only way back after cancelling or
                    // mistyping a verification code.
                    if let Some(ssh) = &session_handle {
                        if !ssh.connected().await {
                            spawn_connect(ssh.clone(), events.sender());
                            session.terminal().draw(|frame| app.draw(frame))?;
                            continue;
                        }
                    }
                    if app.top_screen().is_some() {
                        spawn_screen_load(&slurm, &app, events.sender());
                    } else {
                        spawn_poll(slurm.clone(), events.sender());
                        spawn_detail_load(&slurm, &mut app, &settings, events.sender());
                        spawn_live_load(&slurm, &app, events.sender());
                    }
                }
                Command::LoadDetail => {
                    spawn_detail_load(&slurm, &mut app, &settings, events.sender())
                }
                Command::LoadScreen => spawn_screen_load(&slurm, &app, events.sender()),
                Command::Run(request) => {
                    describe(&mut app, &request);
                    spawn_request(slurm.clone(), request, events.sender());
                }
                Command::ViewScript { job_id } => {
                    spawn_script_fetch(slurm.clone(), &settings, job_id, events.sender());
                }
                Command::Shell(shell) => {
                    run_shell(&mut session, &mut app, &slurm, &settings, shell)?;
                    spawn_poll(slurm.clone(), events.sender());
                }
                Command::None => {}
            },
            Event::Tick => {
                if app.top_screen().is_some() {
                    // A full-screen panel is what the user is looking at, so
                    // that is what gets refreshed.
                    spawn_screen_load(&slurm, &app, events.sender());
                } else {
                    spawn_poll(slurm.clone(), events.sender());
                    spawn_detail_load(&slurm, &mut app, &settings, events.sender());
                    spawn_live_load(&slurm, &app, events.sender());
                }
            }
            Event::Jobs(loaded) => {
                let had_selection = app.shown_job.is_some();
                app.apply_jobs(*loaded);
                // The first poll is what gives us something to select.
                if !had_selection {
                    spawn_detail_load(&slurm, &mut app, &settings, events.sender());
                }
            }
            Event::Detail(loaded) => {
                app.apply_detail(*loaded);
            }
            Event::Partitions(partitions) => {
                app.apply_partitions(partitions);
                spawn_screen_load(&slurm, &app, events.sender());
            }
            Event::PartitionJobs { partition, jobs } => {
                app.apply_partition_jobs(&partition, jobs);
            }
            Event::Nodes { partition, nodes } => {
                app.apply_nodes(&partition, nodes);
                spawn_screen_load(&slurm, &app, events.sender());
            }
            Event::NodeJobs { node, jobs } => app.apply_node_jobs(&node, jobs),
            Event::Usage(loaded) => app.apply_usage(*loaded),
            Event::Live { tab, content } => app.apply_live(tab, content),
            Event::OpenScript(path) => {
                run_shell(
                    &mut session,
                    &mut app,
                    &slurm,
                    &settings,
                    Shell::View {
                        path: path.display().to_string(),
                        label: "script".into(),
                    },
                )?;
            }
            Event::SshPrompt {
                question,
                secret,
                reply,
            } => app.ask_ssh(question, secret, reply),
            Event::SshConnected(result) => match result {
                Ok(message) => {
                    app.log("ssh", Some(message));
                    spawn_poll(slurm.clone(), events.sender());
                }
                Err(error) => {
                    app.log("ssh", Some(error));
                    // `r` is the only way back after a cancelled or mistyped code.
                    app.log("ssh", Some("press r to retry".into()));
                }
            },
            Event::Log(action, result) => app.log(action, result),
            Event::Resize => {}
        }
        session.terminal().draw(|frame| app.draw(frame))?;
    }

    Session::restore()
}

/// Fetch the job lists, and report them when they arrive.
fn spawn_poll(slurm: Arc<Slurm>, sender: Sender) {
    tokio::spawn(async move {
        let (running, completed, partitions) = tokio::join!(
            slurm.running_jobs(),
            slurm.completed_jobs(),
            slurm.partition_availability(),
        );
        let _ = sender.send(Event::Jobs(Box::new(JobsLoaded {
            running,
            completed,
            partitions,
        })));
    });
}

/// Load the selected job's details, after a pause.
///
/// The pause lets the cursor settle: arrowing through a list would otherwise
/// fire several Slurm commands per row. A load that is superseded while waiting
/// does no work at all — it checks the generation before asking Slurm anything.
fn spawn_detail_load(slurm: &Arc<Slurm>, app: &mut App, settings: &Settings, sender: Sender) {
    let Some(job_id) = app.selected_job_id().map(str::to_string) else {
        return;
    };

    let generation = app.next_generation();
    let current = app.generation_handle();
    let slurm = slurm.clone();
    let scripts = settings.script_cache();

    tokio::spawn(async move {
        tokio::time::sleep(SELECTION_DEBOUNCE).await;
        if current.load(Ordering::Relaxed) != generation {
            return; // The user has moved on.
        }

        let detail = slurm.job_detail(&job_id).await;

        // scontrol answering means the job is still in slurmctld, so its batch
        // script is still retrievable — archive it now, because after MinJobAge
        // it is gone for good. Cheap after the first time, and a failure here
        // must never interrupt loading the job.
        if detail
            .as_ref()
            .is_some_and(|detail| detail.source == crate::model::DetailSource::Scontrol)
        {
            crate::slurm::archive_batch_script(slurm.runner(), &scripts, &job_id).await;
        }

        let (stdout_path, stderr_path, partition, pending) = match &detail {
            Some(detail) => (
                detail.stdout_path.clone(),
                detail.stderr_path.clone(),
                detail.partition().to_string(),
                detail.is_pending(),
            ),
            None => (None, None, String::new(), false),
        };

        let (stdout, stderr, stats, priority) = tokio::join!(
            crate::slurm::read_log_file(slurm.runner(), stdout_path.as_deref(), TAIL_LINES),
            crate::slurm::read_log_file(slurm.runner(), stderr_path.as_deref(), TAIL_LINES),
            slurm.job_stats(&job_id),
            async {
                // Only a pending job has a queue position worth asking for.
                if pending {
                    slurm.job_priority(&job_id, &partition).await
                } else {
                    None
                }
            },
        );

        let _ = sender.send(Event::Detail(Box::new(DetailLoaded {
            generation,
            job_id,
            detail,
            stdout,
            stderr,
            stats,
            priority,
            sprio_available: slurm.sprio_available(),
        })));
    });
}

/// Questions the SSH session needs answered, and where to send the answers.
pub type PromptRequest = (String, bool, tokio::sync::oneshot::Sender<Option<String>>);

/// The remote connection, and the channel its prompts arrive on.
pub struct RemoteSession {
    pub session: Arc<crate::ssh::SshSession>,
    pub prompts: tokio::sync::mpsc::UnboundedReceiver<PromptRequest>,
}

/// Forward the session's questions onto the app's own event channel.
///
/// The SSH session runs on its own task while the modal lives on the main loop,
/// so the question crosses over here and the answer goes back down a oneshot.
fn spawn_prompt_bridge(
    mut prompts: tokio::sync::mpsc::UnboundedReceiver<PromptRequest>,
    sender: Sender,
) {
    tokio::spawn(async move {
        while let Some((question, secret, reply)) = prompts.recv().await {
            if sender
                .send(Event::SshPrompt {
                    question,
                    secret,
                    reply,
                })
                .is_err()
            {
                break;
            }
        }
    });
}

/// Open the SSH session, reporting the outcome when it finishes.
fn spawn_connect(session: Arc<crate::ssh::SshSession>, sender: Sender) {
    tokio::spawn(async move {
        let result = session.connect().await;
        let _ = sender.send(Event::SshConnected(result));
    });
}

/// Read-only flags per editor.
///
/// vim gets `-R` rather than `-M` on purpose: `-R` still allows `:w elsewhere`,
/// which is the "save a copy of this script" workflow.
const READONLY_FLAGS: &[(&str, &[&str])] = &[
    ("vim", &["-R"]),
    ("nvim", &["-R"]),
    ("vi", &["-R"]),
    ("view", &["-R"]),
    ("gvim", &["-R"]),
    ("nano", &["-v"]),
    ("less", &[]),
    ("more", &[]),
    ("bat", &[]),
    ("most", &[]),
];

/// Pagers that can open a file at its end, so a live log shows its newest lines.
const PAGER_FLAGS: &[(&str, &[&str])] = &[
    ("less", &["-R", "+G"]),
    ("most", &["+"]),
    ("more", &["+G"]),
    ("bat", &["--paging=always", "--style=plain"]),
];

fn flags_for(
    table: &[(&str, &'static [&'static str])],
    program: &str,
) -> Option<&'static [&'static str]> {
    let name = std::path::Path::new(program)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or(program);
    table
        .iter()
        .find(|(known, _)| *known == name)
        .map(|(_, flags)| *flags)
}

/// Give the terminal to another program, then take it back.
fn run_shell(
    session: &mut Session,
    app: &mut App,
    slurm: &Arc<Slurm>,
    settings: &Settings,
    shell: Shell,
) -> Result<()> {
    let config = app.config.clone();
    let remote = config.is_remote();

    // Anything we shell out to in remote mode must ride the connection that is
    // already authenticated; a fresh one would ask for the 2FA code again.
    let control = slurm
        .session_control_path()
        .map(|path| vec!["-o".to_string(), format!("ControlPath={path}")])
        .unwrap_or_default();

    let (argv, note) = match &shell {
        Shell::Edit { path, label } | Shell::View { path, label } => {
            let readonly = matches!(shell, Shell::View { .. });
            if which(&config.editor).is_none() {
                app.log(
                    format!("edit {label}"),
                    Some(format!(
                        "editor '{}' not found — set 'editor' in config.toml",
                        config.editor
                    )),
                );
                return Ok(());
            }

            let mut argv = vec![config.editor.clone()];
            if readonly {
                match flags_for(READONLY_FLAGS, &config.editor) {
                    Some(flags) => argv.extend(flags.iter().map(|f| (*f).to_string())),
                    None => app.log(
                        label.clone(),
                        Some(format!(
                            "'{}' has no known read-only flag — opening writable",
                            config.editor
                        )),
                    ),
                }
            }
            argv.push(path.clone());
            (argv, format!("{} {path}", config.editor))
        }
        Shell::Page { path, label } => {
            if !remote && which(&config.pager).is_none() {
                app.log(
                    format!("page {label}"),
                    Some(format!(
                        "pager '{}' not found — set 'pager' in config.toml",
                        config.pager
                    )),
                );
                return Ok(());
            }
            let mut argv = vec![config.pager.clone()];
            argv.extend(
                flags_for(PAGER_FLAGS, &config.pager)
                    .unwrap_or(&[])
                    .iter()
                    .map(|f| (*f).to_string()),
            );
            argv.push(path.clone());

            if remote {
                // Run the pager *on the cluster*: no copying a multi-gigabyte
                // log down, and no second authentication. Every argument is
                // quoted for the remote shell, and the whole command quoted
                // again for the local one.
                let remote_command = argv
                    .iter()
                    .map(|arg| shell_words::quote(arg).into_owned())
                    .collect::<Vec<_>>()
                    .join(" ");
                let mut ssh = vec!["ssh".to_string(), "-t".to_string()];
                ssh.extend(control.clone());
                ssh.push(config.remote.clone());
                ssh.push(remote_command);
                (ssh, format!("{} {path} (on the cluster)", config.pager))
            } else {
                (argv, format!("{} {path}", config.pager))
            }
        }
        Shell::Ssh { node } => {
            let mut argv = vec!["ssh".to_string()];
            if remote {
                // Hop through the live session rather than `-J`, which would
                // open a second connection to the login node.
                let proxy = format!(
                    "ssh {} -W %h:%p {}",
                    control.join(" "),
                    shell_words::quote(&config.remote)
                );
                argv.extend(["-o".to_string(), format!("ProxyCommand={proxy}")]);
            }
            argv.push(node.clone());
            (argv, format!("ssh {node}"))
        }
        Shell::EditConfig => {
            if let Err(error) = crate::config::file::write_template_if_missing(&settings.paths) {
                app.log("edit config", Some(format!("{error}")));
                return Ok(());
            }
            if which(&config.editor).is_none() {
                app.log(
                    "edit config",
                    Some(format!("editor '{}' not found", config.editor)),
                );
                return Ok(());
            }
            (
                vec![
                    config.editor.clone(),
                    settings.paths.config_file().display().to_string(),
                ],
                settings.paths.config_file().display().to_string(),
            )
        }
    };

    app.log(shell_label(&shell), Some(note));

    let status = session.suspended(|| {
        std::process::Command::new(&argv[0])
            .args(&argv[1..])
            .status()
    })?;
    if let Err(error) = status {
        app.log(shell_label(&shell), Some(format!("failed: {error}")));
    }

    if matches!(shell, Shell::EditConfig) {
        reload_config(app, settings);
    } else {
        app.log(shell_label(&shell), Some("closed".into()));
    }
    Ok(())
}

fn shell_label(shell: &Shell) -> String {
    match shell {
        Shell::Edit { label, .. } => format!("edit {label}"),
        Shell::View { label, .. } => label.clone(),
        Shell::Page { label, .. } => format!("page {label}"),
        Shell::Ssh { node } => format!("ssh {node}"),
        Shell::EditConfig => "edit config".to_string(),
    }
}

/// Whether a program is on `PATH`.
fn which(program: &str) -> Option<std::path::PathBuf> {
    if program.contains('/') {
        let path = std::path::PathBuf::from(program);
        return path.is_file().then_some(path);
    }
    std::env::var_os("PATH").and_then(|paths| {
        std::env::split_paths(&paths)
            .map(|dir| dir.join(program))
            .find(|candidate| candidate.is_file())
    })
}

/// Re-read the config file and apply what changed.
fn reload_config(app: &mut App, settings: &Settings) {
    let mut config = crate::config::Config::default();
    match crate::config::FileConfig::load(&settings.paths) {
        Ok(file) => file.apply_to(&mut config),
        Err(error) => {
            app.log("config reloaded", Some(error.to_string()));
            return;
        }
    }

    // Settings that only ever come from the command line survive the reload.
    config.remote = app.config.remote.clone();
    if config.user.is_empty() {
        config.user = app.config.user.clone();
    }

    let changes = app.apply_config(config);
    if changes.is_empty() {
        app.log("config reloaded", Some("no changes".into()));
    } else {
        for change in changes {
            app.log("config reloaded", Some(change));
        }
    }
}

/// Say in the command log what is about to be asked of Slurm.
fn describe(app: &mut App, request: &Request) {
    match request {
        Request::Cancel { ids, force } => {
            let flag = if *force { " --signal=KILL" } else { "" };
            app.log(
                format!("scancel{flag} {} job(s)", ids.len()),
                Some(preview(ids)),
            );
        }
        Request::Update { ids, updates } => {
            let pairs: Vec<(&str, &str)> = updates
                .iter()
                .map(|(key, value)| (key.as_str(), value.as_str()))
                .collect();
            app.log(
                format!("scontrol update {} job(s)", ids.len()),
                Some(crate::slurm::build_update_args(&pairs).join(" ")),
            );
        }
        Request::Resubmit { command, .. } => app.log(format!("sbatch {command}"), None),
    }
}

/// The first few ids of a list, for a log line.
fn preview(ids: &[String]) -> String {
    let head = ids.iter().take(5).cloned().collect::<Vec<_>>().join(", ");
    if ids.len() > 5 {
        format!("{head}, …")
    } else {
        head
    }
}

/// Carry out a change, then report each outcome to the command log.
fn spawn_request(slurm: Arc<Slurm>, request: Request, sender: Sender) {
    tokio::spawn(async move {
        let outcomes = match request {
            Request::Cancel { ids, force } => {
                let mut outcomes = Vec::new();
                for id in ids {
                    outcomes.push(crate::slurm::cancel_job(slurm.runner(), &id, force).await);
                }
                outcomes
            }
            Request::Update { ids, updates } => {
                let pairs: Vec<(&str, &str)> = updates
                    .iter()
                    .map(|(key, value)| (key.as_str(), value.as_str()))
                    .collect();
                let mut outcomes = Vec::new();
                for id in ids {
                    outcomes
                        .push(crate::slurm::action::update_job(slurm.runner(), &id, &pairs).await);
                }
                outcomes
            }
            Request::Resubmit {
                command, work_dir, ..
            } => vec![
                crate::slurm::resubmit_job(
                    slurm.runner(),
                    &command,
                    &work_dir,
                    crate::slurm::ScriptFallback::Unchecked,
                )
                .await,
            ],
        };

        let failures = outcomes.iter().filter(|outcome| !outcome.success).count();
        for outcome in &outcomes {
            let _ = sender.send(Event::Log(
                if outcome.success { "ok" } else { "failed" }.to_string(),
                Some(outcome.message.clone()),
            ));
        }
        if failures > 0 {
            let _ = sender.send(Event::Log(
                "result".into(),
                Some(format!("{failures}/{} failed", outcomes.len())),
            ));
        }

        // Whatever changed, the job lists are now out of date.
        let (running, completed, partitions) = tokio::join!(
            slurm.running_jobs(),
            slurm.completed_jobs(),
            slurm.partition_availability(),
        );
        let _ = sender.send(Event::Jobs(Box::new(JobsLoaded {
            running,
            completed,
            partitions,
        })));
    });
}

/// Fetch and archive a job's batch script, then ask to open it.
fn spawn_script_fetch(slurm: Arc<Slurm>, settings: &Settings, job_id: String, sender: Sender) {
    let store = settings.script_cache();
    tokio::spawn(async move {
        let cached = store.get(&job_id).is_some();
        if !cached {
            let _ = sender.send(Event::Log(
                "batch script".into(),
                Some(format!("fetching script for {job_id}...")),
            ));
        }

        match crate::slurm::archive_batch_script(slurm.runner(), &store, &job_id).await {
            Some(path) => {
                let _ = sender.send(Event::Log(
                    "batch script".into(),
                    Some(format!(
                        "{} {}",
                        if cached { "cached" } else { "archived" },
                        path.display()
                    )),
                ));
                let _ = sender.send(Event::OpenScript(path));
            }
            None => {
                let _ = sender.send(Event::Log(
                    "batch script".into(),
                    Some(
                        "unavailable — Slurm no longer holds this job (MinJobAge) and no \
                         copy was archived while it was live"
                            .into(),
                    ),
                ));
            }
        }
    });
}

/// Load whatever the open full-screen panel needs.
///
/// Called both when a screen opens and when its cursor moves; each branch only
/// fetches the part that is actually out of date.
fn spawn_screen_load(slurm: &Arc<Slurm>, app: &App, sender: Sender) {
    let Some(screen) = app.top_screen() else {
        return;
    };
    let slurm = slurm.clone();

    match screen {
        Screen::Partitions(screen) => {
            if screen.partitions.is_empty() {
                let sender = sender.clone();
                let slurm = slurm.clone();
                tokio::spawn(async move {
                    let _ = sender.send(Event::Partitions(slurm.partitions().await));
                });
            }
            if screen.needs_jobs() {
                if let Some(partition) = screen.selected_partition().map(str::to_string) {
                    tokio::spawn(async move {
                        let jobs = slurm.partition_jobs(&partition, "RUNNING,PENDING").await;
                        let _ = sender.send(Event::PartitionJobs { partition, jobs });
                    });
                }
            }
        }
        Screen::Nodes(screen) => {
            if screen.nodes.is_empty() {
                let sender = sender.clone();
                let slurm = slurm.clone();
                let partition = screen.partition.clone();
                tokio::spawn(async move {
                    let nodes = slurm.partition_nodes(&partition).await;
                    let _ = sender.send(Event::Nodes { partition, nodes });
                });
            }
            if screen.needs_jobs() {
                if let Some(node) = screen.selected_node().map(str::to_string) {
                    tokio::spawn(async move {
                        let jobs = slurm.node_jobs(&node).await;
                        let _ = sender.send(Event::NodeJobs { node, jobs });
                    });
                }
            }
        }
        Screen::Usage(screen) => {
            let window = screen.window;
            let user = screen.user.clone();
            tokio::spawn(async move {
                let (rows, shares) =
                    tokio::join!(slurm.account_usage(window, ""), slurm.fairshare(&user));
                let _ = sender.send(Event::Usage(Box::new(UsageLoaded {
                    rows,
                    shares,
                    accounting_available: slurm.accounting_available(),
                })));
            });
        }
    }
}

/// Refresh the live cpu/gpu tab, if one is open.
fn spawn_live_load(slurm: &Arc<Slurm>, app: &App, sender: Sender) {
    let Some(tab) = app.live_tab() else {
        return;
    };
    let Some(node) = app.selected_node() else {
        return;
    };

    let slurm = slurm.clone();
    let user = app.config.user.clone();
    let job_id = app.selected_job_id().unwrap_or_default().to_string();

    tokio::spawn(async move {
        let content = match tab {
            "gpu" => crate::slurm::gpu_status(&slurm, &node, &job_id).await,
            _ => crate::slurm::node_processes(&slurm, &node, &user).await,
        };
        let _ = sender.send(Event::Live { tab, content });
    });
}

/// Say so when the app is watching a cluster from one of its login nodes.
fn warn_about_login_node(app: &mut App) {
    let local = hostname();
    let remote = app
        .config
        .remote
        .rsplit('@')
        .next()
        .unwrap_or_default()
        .to_string();

    for name in [local, remote] {
        if !name.is_empty() && name.to_lowercase().contains("login") {
            app.log(
                "warning",
                Some(format!(
                    "running on login node '{name}' — be mindful of resource usage"
                )),
            );
        }
    }
}

fn hostname() -> String {
    std::fs::read_to_string("/proc/sys/kernel/hostname")
        .map(|name| name.trim().to_string())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::job::{DetailSource, StatsSource};
    use crate::model::JobDetail;
    use crossterm::event::KeyModifiers;

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    fn typed(text: &str) -> Vec<KeyEvent> {
        text.chars().map(|c| key(KeyCode::Char(c))).collect()
    }

    fn running(id: &str, name: &str, state: &str) -> RunningJob {
        RunningJob {
            job_id: id.into(),
            name: name.into(),
            elapsed: "1:00".into(),
            partition: "gpu".into(),
            state: state.into(),
            ..RunningJob::default()
        }
    }

    fn completed(id: &str, name: &str) -> CompletedJob {
        CompletedJob {
            job_id: id.into(),
            name: name.into(),
            state: "COMPLETED".into(),
            partition: "gpu".into(),
            elapsed: "1:00".into(),
            ..CompletedJob::default()
        }
    }

    fn app() -> App {
        let mut app = App::new(Config::default());
        app.apply_jobs(JobsLoaded {
            running: vec![
                running("100", "train-a", "RUNNING"),
                running("101", "train-b", "PENDING"),
            ],
            completed: vec![completed("90", "old-a"), completed("91", "old-b")],
            partitions: vec!["gpu:2/2/0/4".into()],
        });
        app
    }

    fn partition(name: &str) -> crate::model::PartitionInfo {
        crate::model::PartitionInfo {
            name: name.into(),
            avail: "up".into(),
            ..crate::model::PartitionInfo::default()
        }
    }

    fn partition_job(job_id: &str) -> crate::model::PartitionJob {
        crate::model::PartitionJob {
            job_id: job_id.into(),
            user: "someone".into(),
            name: "their-job".into(),
            state: "RUNNING".into(),
            ..crate::model::PartitionJob::default()
        }
    }

    /// Render whatever is on screen and return it as text.
    fn screen_text(app: &mut App, width: u16, height: u16) -> String {
        screen(app, width, height)
    }

    fn detail_for(job_id: &str, state: &str) -> JobDetail {
        JobDetail {
            job_id: job_id.into(),
            raw: BTreeMap::from([
                ("JobState".to_string(), state.to_string()),
                ("Partition".to_string(), "gpu".to_string()),
            ]),
            stdout_path: None,
            stderr_path: None,
            work_dir: "/work".into(),
            source: DetailSource::Scontrol,
        }
    }

    fn loaded(app: &App, job_id: &str, state: &str) -> DetailLoaded {
        DetailLoaded {
            generation: app.generation.load(Ordering::Relaxed),
            job_id: job_id.into(),
            detail: Some(detail_for(job_id, state)),
            stdout: "output\n".into(),
            stderr: String::new(),
            stats: None,
            priority: None,
            sprio_available: true,
        }
    }

    /// Render the whole screen and return what it drew.
    fn screen(app: &mut App, width: u16, height: u16) -> String {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;

        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();

        let buffer = terminal.backend().buffer().clone();
        (0..buffer.area.height)
            .map(|y| {
                (0..buffer.area.width)
                    .map(|x| buffer[(x, y)].symbol().to_string())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn q_quits_and_r_refreshes() {
        let mut app = app();
        assert_eq!(app.handle_key(key(KeyCode::Char('r'))), Command::Refresh);
        assert_eq!(app.handle_key(key(KeyCode::Char('q'))), Command::Quit);
    }

    #[test]
    fn ctrl_c_quits_too() {
        let mut app = app();
        let ctrl_c = KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL);
        assert_eq!(app.handle_key(ctrl_c), Command::Quit);
    }

    #[test]
    fn the_cluster_bar_counts_array_tasks_not_rows() {
        let mut app = App::new(Config::default());
        app.apply_jobs(JobsLoaded {
            running: vec![
                running("100", "a", "RUNNING"),
                running("200_0", "b", "RUNNING"),
                running("200_[1-9]", "b", "PENDING"),
            ],
            completed: vec![],
            partitions: vec![],
        });

        assert_eq!(app.running_tasks, 2); // 100 and 200_0
        assert_eq!(app.pending_tasks, 9); // the whole range
    }

    #[test]
    fn down_past_the_last_row_moves_to_the_other_table() {
        let mut app = app();
        assert_eq!(app.focus(), Panel::Active);

        app.handle_key(key(KeyCode::Down)); // to row 1, the last active row
        assert_eq!(app.focus(), Panel::Active);

        app.handle_key(key(KeyCode::Down)); // over the edge
        assert_eq!(app.focus(), Panel::Completed);
        assert_eq!(app.completed.selected_index(), 0);
    }

    #[test]
    fn up_from_the_first_row_moves_to_the_other_tables_last() {
        let mut app = app();
        app.handle_key(key(KeyCode::Up));

        assert_eq!(app.focus(), Panel::Completed);
        assert_eq!(
            app.completed.selected_index(),
            app.completed.row_count() - 1
        );
    }

    #[test]
    fn moving_the_cursor_asks_for_a_detail_load() {
        let mut app = app();
        assert_eq!(app.handle_key(key(KeyCode::Down)), Command::LoadDetail);
    }

    #[test]
    fn tab_cycles_through_all_four_panels() {
        let mut app = app();
        let order = [
            Panel::Completed,
            Panel::Detail,
            Panel::Metadata,
            Panel::Active,
        ];
        for expected in order {
            app.handle_key(key(KeyCode::Tab));
            assert_eq!(app.focus(), expected);
        }

        app.handle_key(key(KeyCode::BackTab));
        assert_eq!(app.focus(), Panel::Metadata);
    }

    #[test]
    fn the_same_key_means_different_things_in_different_panels() {
        let mut app = app();
        app.detail.stdout.set_viewport(40, 5);
        app.detail
            .stdout
            .set_content((0..20).map(|i| format!("line {i}\n")).collect::<String>());

        // In a job table, Up moves the cursor.
        app.handle_key(key(KeyCode::Up));
        assert_eq!(app.focus(), Panel::Completed);

        // In the detail panel, it scrolls.
        app.set_focus(Panel::Detail);
        app.handle_key(key(KeyCode::Up));
        assert!(!app.detail.stdout.is_following());
    }

    #[test]
    fn bracket_keys_cycle_the_detail_tabs() {
        let mut app = app();
        app.set_focus(Panel::Detail);

        assert_eq!(app.detail.active_tab(), Some("stdout"));
        app.handle_key(key(KeyCode::Char(']')));
        assert_eq!(app.detail.active_tab(), Some("stderr"));
        app.handle_key(key(KeyCode::Char('[')));
        assert_eq!(app.detail.active_tab(), Some("stdout"));
    }

    #[test]
    fn parenthesis_keys_cycle_the_metadata_tabs() {
        let mut app = app();
        app.set_focus(Panel::Metadata);
        app.metadata
            .set_detail(Some(detail_for("100", "RUNNING")), None, true);

        assert_eq!(app.metadata.active_tab(), Some("Resources"));
        app.handle_key(key(KeyCode::Char(')')));
        assert_eq!(app.metadata.active_tab(), Some("Submission"));
        app.handle_key(key(KeyCode::Char('(')));
        assert_eq!(app.metadata.active_tab(), Some("Resources"));
    }

    #[test]
    fn help_opens_and_any_key_closes_it() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('?')));
        assert!(app.is_help_open());

        // While it is open the app is not quitting or moving.
        assert_eq!(app.handle_key(key(KeyCode::Char('q'))), Command::None);
        assert!(!app.is_help_open());
    }

    #[test]
    fn the_help_follows_the_panel_the_user_is_in() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('?')));
        assert!(screen(&mut app, 100, 40).contains("Job tables"));

        app.handle_key(key(KeyCode::Esc)); // closes
        app.set_focus(Panel::Detail);
        app.handle_key(key(KeyCode::Char('?')));
        assert!(screen(&mut app, 100, 40).contains("Job Details"));
    }

    #[test]
    fn typing_a_filter_narrows_both_tables_live() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('/')));
        assert!(app.is_searching());

        for key in typed("train-a") {
            app.handle_key(key);
        }
        assert_eq!(app.active.row_keys(), vec!["100"]);
        assert!(app.completed.row_keys().is_empty());
    }

    #[test]
    fn escape_abandons_the_filter_and_enter_keeps_it() {
        let mut app = app();

        app.handle_key(key(KeyCode::Char('/')));
        for key in typed("train-a") {
            app.handle_key(key);
        }
        app.handle_key(key(KeyCode::Esc));
        assert!(!app.is_searching());
        assert_eq!(app.active.row_keys(), vec!["100", "101"]);

        app.handle_key(key(KeyCode::Char('/')));
        for key in typed("train-a") {
            app.handle_key(key);
        }
        app.handle_key(key(KeyCode::Enter));
        assert!(!app.is_searching());
        assert_eq!(app.active.row_keys(), vec!["100"]);
    }

    #[test]
    fn keys_go_to_the_filter_rather_than_the_table_while_it_is_open() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('/')));
        // 'q' is a character in the query, not a quit.
        assert_ne!(app.handle_key(key(KeyCode::Char('q'))), Command::Quit);
        assert!(app.is_searching());
    }

    #[test]
    fn m_bookmarks_the_selected_row_and_pins_it() {
        let mut app = app();
        app.handle_key(key(KeyCode::Down)); // job 101
        app.handle_key(key(KeyCode::Char('m')));

        assert!(app.bookmarks().contains("101"));
        assert_eq!(app.active.row_keys()[0], "101");

        app.handle_key(key(KeyCode::Char('m')));
        assert!(app.bookmarks().is_empty());
    }

    #[test]
    fn enter_expands_a_collapsed_array() {
        let mut app = App::new(Config::default());
        app.apply_jobs(JobsLoaded {
            running: vec![
                running("200_0", "sweep", "RUNNING"),
                running("200_1", "sweep", "PENDING"),
            ],
            completed: vec![],
            partitions: vec![],
        });

        assert_eq!(app.active.row_count(), 1);
        app.handle_key(key(KeyCode::Enter));
        assert_eq!(app.active.row_count(), 3);
    }

    #[test]
    fn a_stale_detail_load_is_dropped() {
        let mut app = app();
        let stale = loaded(&app, "100", "RUNNING");

        // The user moves on before the load returns.
        app.next_generation();

        assert!(!app.apply_detail(stale));
        assert_eq!(app.metadata.active_tab(), Some("Resources"));
        assert_eq!(app.detail.stdout.content(), "");
    }

    #[test]
    fn a_current_detail_load_fills_the_panels() {
        let mut app = app();
        let fresh = loaded(&app, "100", "RUNNING");

        assert!(app.apply_detail(fresh));
        assert_eq!(app.detail.stdout.content(), "output\n");
        assert!(!app.metadata.tabs().tabs().contains(&"Pending"));
    }

    #[test]
    fn a_pending_job_gains_a_pending_tab() {
        let mut app = app();
        let fresh = loaded(&app, "101", "PENDING");

        app.apply_detail(fresh);
        assert!(app.metadata.tabs().tabs().contains(&"Pending"));
    }

    #[test]
    fn the_cpu_series_is_a_rate_not_a_memory_reading() {
        let mut app = app();
        let mut stats = JobStats::empty("100", StatsSource::Combined);

        // Two samples ten minutes apart, five core-minutes of CPU between them.
        stats.total_cpu = "00:00:00".into();
        stats.elapsed = "00:00:00".into();
        app.sample_resources("100", &stats);

        stats.total_cpu = "00:05:00".into();
        stats.elapsed = "00:10:00".into();
        app.sample_resources("100", &stats);

        let history = &app.history["100"];
        assert_eq!(history.cpu, vec![0.5], "half a core busy over the interval");
    }

    #[test]
    fn history_is_forgotten_once_a_job_stops_running() {
        let mut app = app();
        let mut stats = JobStats::empty("100", StatsSource::Combined);
        stats.max_rss = "1G".into();
        app.sample_resources("100", &stats);
        assert!(app.history.contains_key("100"));

        // Job 100 leaves the queue.
        app.apply_jobs(JobsLoaded {
            running: vec![running("101", "train-b", "PENDING")],
            completed: vec![],
            partitions: vec![],
        });
        assert!(!app.history.contains_key("100"));
    }

    #[test]
    fn draws_every_panel_of_the_main_screen() {
        let mut app = app();
        app.log("refresh", Some("complete".into()));
        let fresh = loaded(&app, "100", "RUNNING");
        app.apply_detail(fresh);

        let output = screen(&mut app, 120, 40);

        assert!(output.contains("running"), "{output}");
        assert!(output.contains("gpu:2/2/0/4"), "{output}");
        assert!(output.contains("Active Jobs"), "{output}");
        assert!(output.contains("Terminated Jobs"), "{output}");
        assert!(output.contains("train-a"), "{output}");
        assert!(output.contains("Job Details"), "{output}");
        assert!(output.contains("stdout"), "{output}");
        assert!(output.contains("output"), "{output}");
        assert!(output.contains("Job Metadata"), "{output}");
        assert!(output.contains("Resources"), "{output}");
        assert!(output.contains("Command Log"), "{output}");
        assert!(output.contains(">>> complete"), "{output}");
        assert!(output.contains("quit"), "{output}");
    }

    #[test]
    fn draws_the_filter_bar_only_while_it_is_open() {
        let mut app = app();
        assert!(!screen(&mut app, 120, 40).contains("state:pend"));

        app.handle_key(key(KeyCode::Char('/')));
        assert!(screen(&mut app, 120, 40).contains("state:pend"));
    }

    #[test]
    fn draws_at_awkward_terminal_sizes_without_panicking() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('?'))); // with the overlay up, too
        for (width, height) in [(120, 40), (80, 24), (40, 12), (20, 6), (5, 3), (1, 1)] {
            screen(&mut app, width, height);
        }
    }

    #[test]
    fn p_opens_the_partition_monitor_and_escape_closes_it() {
        let mut app = app();

        assert_eq!(app.handle_key(key(KeyCode::Char('p'))), Command::LoadScreen);
        assert_eq!(app.context(), Context::Partitions);

        app.handle_key(key(KeyCode::Esc));
        assert!(app.top_screen().is_none());
        assert_eq!(app.context(), Context::Jobs);
    }

    #[test]
    fn shift_u_opens_the_usage_panel() {
        let mut app = app();
        assert_eq!(app.handle_key(key(KeyCode::Char('U'))), Command::LoadScreen);
        assert_eq!(app.context(), Context::Usage);
    }

    #[test]
    fn enter_on_a_partition_drills_into_its_nodes() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('p')));
        app.apply_partitions(vec![partition("gpu"), partition("cpu")]);

        assert_eq!(app.handle_key(key(KeyCode::Enter)), Command::LoadScreen);
        assert_eq!(app.context(), Context::Nodes);

        // Escape goes back to the partition monitor, not all the way out.
        app.handle_key(key(KeyCode::Esc));
        assert_eq!(app.context(), Context::Partitions);
    }

    #[test]
    fn a_screen_takes_the_keys_from_the_main_view() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('p')));
        app.apply_partitions(vec![partition("gpu"), partition("cpu")]);

        // Down moves the partition cursor, not the job table's.
        app.handle_key(key(KeyCode::Down));
        assert_eq!(app.active.selected_index(), 0);
        let Some(Screen::Partitions(screen)) = app.top_screen() else {
            panic!("the partition monitor should be open");
        };
        assert_eq!(screen.selected_partition(), Some("cpu"));
    }

    #[test]
    fn tab_switches_panes_within_a_screen() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('p')));

        app.handle_key(key(KeyCode::Tab));
        let Some(Screen::Partitions(screen)) = app.top_screen() else {
            panic!("expected the partition monitor");
        };
        assert_eq!(screen.focus, Pane::Bottom);
    }

    #[test]
    fn the_job_list_is_dropped_when_it_arrives_for_another_partition() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('p')));
        app.apply_partitions(vec![partition("gpu"), partition("cpu")]);

        // A reply for a partition the cursor has left has nowhere to go.
        app.apply_partition_jobs("cpu", vec![partition_job("500")]);
        let Some(Screen::Partitions(screen)) = app.top_screen() else {
            panic!("expected the partition monitor");
        };
        assert!(screen.jobs.is_empty());

        app.apply_partition_jobs("gpu", vec![partition_job("500")]);
        let Some(Screen::Partitions(screen)) = app.top_screen() else {
            panic!("expected the partition monitor");
        };
        assert_eq!(screen.jobs.len(), 1);
    }

    #[test]
    fn w_cycles_the_usage_window_and_reloads() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('U')));

        assert_eq!(app.handle_key(key(KeyCode::Char('w'))), Command::LoadScreen);
        let Some(Screen::Usage(screen)) = app.top_screen() else {
            panic!("expected the usage panel");
        };
        assert_eq!(screen.window, crate::slurm::UsageWindow::Last30Days);
        assert!(screen.loading, "a new window means new data");
    }

    #[test]
    fn the_help_follows_the_open_screen() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('p')));
        app.handle_key(key(KeyCode::Char('?')));

        let output = screen_text(&mut app, 100, 40);
        assert!(output.contains("Partition monitor"), "{output}");
        assert!(output.contains("A/I/O/T"), "{output}");
    }

    #[test]
    fn draws_the_partition_monitor() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('p')));
        app.apply_partitions(vec![partition("gpu"), partition("cpu")]);
        app.apply_partition_jobs("gpu", vec![partition_job("500")]);

        let output = screen_text(&mut app, 120, 40);
        assert!(output.contains("2 partitions"), "{output}");
        assert!(output.contains("Partitions"), "{output}");
        assert!(output.contains("gpu"), "{output}");
        assert!(output.contains("Jobs on gpu"), "{output}");
        assert!(output.contains("all users"), "{output}");
    }

    #[test]
    fn draws_the_usage_panel() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('U')));
        app.apply_usage(UsageLoaded {
            rows: vec![crate::model::UsageRow {
                account: "physics".into(),
                user: "robin".into(),
                hours: 2500.0,
                ..crate::model::UsageRow::default()
            }],
            shares: vec![],
            accounting_available: true,
        });

        let output = screen_text(&mut app, 120, 40);
        assert!(output.contains("Account usage"), "{output}");
        assert!(output.contains("Fair share"), "{output}");
        assert!(output.contains("2 500"), "{output}");
    }

    #[test]
    fn screens_draw_at_awkward_sizes_without_panicking() {
        for opener in ['p', 'U'] {
            let mut app = app();
            app.handle_key(key(KeyCode::Char(opener)));
            app.apply_partitions(vec![partition("gpu")]);
            for (width, height) in [(120, 40), (40, 12), (10, 4), (1, 1)] {
                screen_text(&mut app, width, height);
            }
        }
    }

    #[test]
    fn a_live_tab_is_only_fetched_while_it_is_showing() {
        let mut app = app();
        assert_eq!(app.live_tab(), None, "stdout is showing, not cpu");

        app.detail.select_tab("cpu");
        assert_eq!(app.live_tab(), Some("cpu"));

        // Not while a full-screen panel is covering it.
        app.handle_key(key(KeyCode::Char('p')));
        assert_eq!(app.live_tab(), None);
    }

    #[test]
    fn live_monitoring_can_be_turned_off_entirely() {
        let mut app = App::new(Config {
            no_live: true,
            ..Config::default()
        });
        app.detail.select_tab("cpu");
        assert_eq!(app.live_tab(), None);
    }

    #[test]
    fn c_asks_before_cancelling() {
        let mut app = app();

        assert_eq!(app.handle_key(key(KeyCode::Char('c'))), Command::None);
        // Nothing happens until the user says so.
        let output = screen_text(&mut app, 100, 40);
        assert!(output.contains("Cancel job 100"), "{output}");

        assert_eq!(
            app.handle_key(key(KeyCode::Char('y'))),
            Command::Run(Request::Cancel {
                ids: vec!["100".into()],
                force: false,
            })
        );
    }

    #[test]
    fn refusing_a_cancel_does_nothing_at_all() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('c')));
        assert_eq!(app.handle_key(key(KeyCode::Char('n'))), Command::None);
        assert!(!screen_text(&mut app, 100, 40).contains("Cancel job"));
    }

    #[test]
    fn shift_c_force_cancels_without_asking() {
        // Deliberate: this is the key you reach for when a job has to die now.
        let mut app = app();
        assert_eq!(
            app.handle_key(key(KeyCode::Char('C'))),
            Command::Run(Request::Cancel {
                ids: vec!["100".into()],
                force: true,
            })
        );
    }

    #[test]
    fn a_collapsed_array_cancels_as_a_whole() {
        // `scancel 200` takes every task, so the base id is the right target.
        let mut app = App::new(Config::default());
        app.apply_jobs(JobsLoaded {
            running: vec![
                running("200_0", "sweep", "RUNNING"),
                running("200_1", "sweep", "PENDING"),
            ],
            completed: vec![],
            partitions: vec![],
        });

        app.handle_key(key(KeyCode::Char('c')));
        assert_eq!(
            app.handle_key(key(KeyCode::Char('y'))),
            Command::Run(Request::Cancel {
                ids: vec!["200".into()],
                force: false,
            })
        );
    }

    #[test]
    fn multi_select_extends_over_the_displayed_rows() {
        let mut app = app();
        let ctrl_v = KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL);

        app.handle_key(ctrl_v);
        assert!(app.is_multiselecting());
        assert_eq!(app.selected_ids().len(), 1);

        app.handle_key(key(KeyCode::Down));
        assert_eq!(
            app.selected_ids().iter().cloned().collect::<Vec<_>>(),
            vec!["100", "101"]
        );

        app.handle_key(ctrl_v);
        assert!(!app.is_multiselecting());
        assert!(app.selected_ids().is_empty());
    }

    #[test]
    fn cancelling_applies_to_every_selected_job() {
        let mut app = app();
        app.handle_key(KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL));
        app.handle_key(key(KeyCode::Down));

        app.handle_key(key(KeyCode::Char('c')));
        assert_eq!(
            app.handle_key(key(KeyCode::Char('y'))),
            Command::Run(Request::Cancel {
                ids: vec!["100".into(), "101".into()],
                force: false,
            })
        );
        // Acting on the selection also ends it.
        assert!(!app.is_multiselecting());
    }

    #[test]
    fn only_pending_jobs_can_be_edited() {
        let mut app = app();
        // Job 100 is RUNNING; the editor refuses and says so.
        assert_eq!(app.handle_key(key(KeyCode::Char('u'))), Command::None);
        assert!(!screen_text(&mut app, 100, 40).contains("job.100"));

        app.handle_key(key(KeyCode::Down)); // job 101, PENDING
        app.handle_key(key(KeyCode::Char('u')));
        assert!(screen_text(&mut app, 100, 40).contains("job.101"));
    }

    #[test]
    fn the_editor_returns_an_update_request() {
        let mut app = app();
        app.handle_key(key(KeyCode::Down)); // the pending job
        app.handle_key(key(KeyCode::Char('u')));

        for stroke in typed("4:00:00") {
            app.handle_key(stroke);
        }
        assert_eq!(
            app.handle_key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::CONTROL)),
            Command::Run(Request::Update {
                ids: vec!["101".into()],
                updates: vec![("time_limit".into(), "4:00:00".into())],
            })
        );
    }

    #[test]
    fn resubmit_is_only_offered_for_terminated_jobs() {
        let mut app = app();
        assert_eq!(app.handle_key(key(KeyCode::Char('s'))), Command::None);

        // Move to the terminated table and load a job that has a submit line.
        app.handle_key(key(KeyCode::Up));
        let mut loaded = loaded(&app, "91", "COMPLETED");
        loaded.detail.as_mut().unwrap().raw.insert(
            "SubmitLine".to_string(),
            "sbatch --array=1-4 job.sh".to_string(),
        );
        app.apply_detail(loaded);

        app.handle_key(key(KeyCode::Char('s')));
        assert!(screen_text(&mut app, 100, 40).contains("Resubmit job 91"));

        assert_eq!(
            app.handle_key(key(KeyCode::Char('y'))),
            Command::Run(Request::Resubmit {
                job_id: "91".into(),
                command: "sbatch --array=1-4 job.sh".into(),
                work_dir: "/work".into(),
            })
        );
    }

    #[test]
    fn a_modal_takes_every_key_while_it_is_open() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('c')));

        // q does not quit while a confirmation is up.
        assert_eq!(app.handle_key(key(KeyCode::Char('q'))), Command::None);
        assert_eq!(app.handle_key(key(KeyCode::Down)), Command::None);
        assert_eq!(app.active.selected_index(), 0);
    }

    #[test]
    fn opening_a_log_needs_a_path() {
        let mut app = app();
        // No detail loaded, so there is no stdout path yet.
        assert_eq!(app.handle_key(key(KeyCode::Char('e'))), Command::None);

        app.set_focus(Panel::Detail);
        let mut loaded = loaded(&app, "100", "RUNNING");
        loaded.detail.as_mut().unwrap().stdout_path = Some("/work/slurm-100.out".into());
        app.apply_detail(loaded);

        assert_eq!(
            app.handle_key(key(KeyCode::Char('e'))),
            Command::Shell(Shell::Edit {
                path: "/work/slurm-100.out".into(),
                label: "stdout".into(),
            })
        );
    }

    #[test]
    fn the_pager_opens_whichever_log_is_showing() {
        let mut app = app();
        app.set_focus(Panel::Detail);
        let mut loaded = loaded(&app, "100", "RUNNING");
        {
            let detail = loaded.detail.as_mut().unwrap();
            detail.stdout_path = Some("/work/out".into());
            detail.stderr_path = Some("/work/err".into());
        }
        app.apply_detail(loaded);

        assert_eq!(
            app.handle_key(key(KeyCode::Char('l'))),
            Command::Shell(Shell::Page {
                path: "/work/out".into(),
                label: "stdout".into(),
            })
        );

        app.detail.select_tab("stderr");
        assert_eq!(
            app.handle_key(key(KeyCode::Char('l'))),
            Command::Shell(Shell::Page {
                path: "/work/err".into(),
                label: "stderr".into(),
            })
        );
    }

    #[test]
    fn o_needs_a_node_to_connect_to() {
        let mut app = app();
        assert_eq!(app.handle_key(key(KeyCode::Char('o'))), Command::None);

        let mut loaded = loaded(&app, "100", "RUNNING");
        loaded
            .detail
            .as_mut()
            .unwrap()
            .raw
            .insert("NodeList".to_string(), "gpu[01-04]".to_string());
        app.apply_detail(loaded);

        // A multi-node job connects to the first of them.
        assert_eq!(
            app.handle_key(key(KeyCode::Char('o'))),
            Command::Shell(Shell::Ssh {
                node: "gpu01".into()
            })
        );
    }

    #[test]
    fn b_asks_for_the_batch_script() {
        let mut app = app();
        assert_eq!(
            app.handle_key(key(KeyCode::Char('b'))),
            Command::ViewScript {
                job_id: "100".into()
            }
        );
    }

    #[test]
    fn reloading_the_config_reports_what_changed() {
        let mut app = app();
        let changes = app.apply_config(Config {
            days: 21,
            editor: "nvim".into(),
            ..Config::default()
        });

        assert!(
            changes.iter().any(|c| c.contains("days: 7 → 21")),
            "{changes:?}"
        );
        assert!(changes.iter().any(|c| c.contains("editor")), "{changes:?}");
        assert_eq!(app.config.days, 21);
    }

    #[test]
    fn reloading_with_no_changes_says_so() {
        let mut app = app();
        assert!(app.apply_config(Config::default()).is_empty());
    }

    #[test]
    fn the_command_log_keeps_only_its_most_recent_entries() {
        let mut app = app();
        for index in 0..(LOG_LIMIT + 20) {
            app.log(format!("entry {index}"), None);
        }
        assert_eq!(app.log.len(), LOG_LIMIT);
        assert_eq!(
            app.log.last().unwrap().action,
            format!("entry {}", LOG_LIMIT + 19)
        );
    }
}
