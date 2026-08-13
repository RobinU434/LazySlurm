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

use super::detail::{DetailView, ResourceHistory};
use super::event::{DetailLoaded, Event, Events, JobsLoaded, Sender, UsageLoaded};
use super::help::{self, Action, Context};
use super::job_table::JobTable;
use super::layout::main_layout;
use super::metadata::MetadataView;
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

/// What a keystroke asked the runner to do.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
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
        }
        Command::None
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
pub async fn run(slurm: Arc<Slurm>, mut app: App, notes: Vec<String>) -> Result<()> {
    install_panic_hook();
    let mut session = Session::enter()?;
    let mut events = Events::start();

    for note in notes {
        app.log(note, None);
    }
    warn_about_login_node(&mut app);

    if app.config.auto_refresh() {
        events.start_ticker(Duration::from_secs_f64(app.config.refresh));
    } else {
        app.log("auto-refresh", Some("disabled (refresh=0)".into()));
    }
    spawn_poll(slurm.clone(), events.sender());

    session.terminal().draw(|frame| app.draw(frame))?;

    while let Some(event) = events.next().await {
        match event {
            Event::Key(key) => match app.handle_key(key) {
                Command::Quit => break,
                Command::Refresh => {
                    app.log("refresh", None);
                    if app.top_screen().is_some() {
                        spawn_screen_load(&slurm, &app, events.sender());
                    } else {
                        spawn_poll(slurm.clone(), events.sender());
                        spawn_detail_load(&slurm, &mut app, events.sender());
                        spawn_live_load(&slurm, &app, events.sender());
                    }
                }
                Command::LoadDetail => spawn_detail_load(&slurm, &mut app, events.sender()),
                Command::LoadScreen => spawn_screen_load(&slurm, &app, events.sender()),
                Command::None => {}
            },
            Event::Tick => {
                if app.top_screen().is_some() {
                    // A full-screen panel is what the user is looking at, so
                    // that is what gets refreshed.
                    spawn_screen_load(&slurm, &app, events.sender());
                } else {
                    spawn_poll(slurm.clone(), events.sender());
                    spawn_detail_load(&slurm, &mut app, events.sender());
                    spawn_live_load(&slurm, &app, events.sender());
                }
            }
            Event::Jobs(loaded) => {
                let had_selection = app.shown_job.is_some();
                app.apply_jobs(*loaded);
                // The first poll is what gives us something to select.
                if !had_selection {
                    spawn_detail_load(&slurm, &mut app, events.sender());
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
fn spawn_detail_load(slurm: &Arc<Slurm>, app: &mut App, sender: Sender) {
    let Some(job_id) = app.selected_job_id().map(str::to_string) else {
        return;
    };

    let generation = app.next_generation();
    let current = app.generation_handle();
    let slurm = slurm.clone();

    tokio::spawn(async move {
        tokio::time::sleep(SELECTION_DEBOUNCE).await;
        if current.load(Ordering::Relaxed) != generation {
            return; // The user has moved on.
        }

        let detail = slurm.job_detail(&job_id).await;
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
