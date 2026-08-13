//! The main screen: state, keys, and the loop that drives them.
//!
//! Key handling returns a [`Command`] rather than acting directly, so every
//! keystroke can be tested by feeding it to [`App::handle_key`] and asserting on
//! the state that came out — no terminal involved.

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use chrono::Local;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::Frame;

use crate::config::Config;
use crate::model::{array_task_count, CompletedJob, RunningJob};
use crate::slurm::Slurm;

use super::event::{Event, Events, JobsLoaded, Sender};
use super::job_table::JobTable;
use super::layout::main_layout;
use super::render::{self, LogEntry, TableStyle};
use super::terminal::{install_panic_hook, Session};
use super::theme::Theme;

/// How many command-log entries to keep.
const LOG_LIMIT: usize = 200;

/// Which job table has the cursor.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Focus {
    Active,
    Completed,
}

impl Focus {
    fn other(self) -> Self {
        match self {
            Self::Active => Self::Completed,
            Self::Completed => Self::Active,
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
}

/// The main screen.
pub struct App {
    pub config: Config,
    pub theme: Theme,
    pub active: JobTable<RunningJob>,
    pub completed: JobTable<CompletedJob>,
    focus: Focus,
    /// The filter text, when the bar is open.
    search: Option<String>,
    bookmarks: BTreeSet<String>,
    log: Vec<LogEntry>,
    running_tasks: u32,
    pending_tasks: u32,
    partitions: Vec<String>,
}

impl App {
    pub fn new(config: Config) -> Self {
        let theme = Theme::new(&config.partition_colors, config.abbreviate_states);
        let collapse = config.collapse_arrays;
        let mut app = Self {
            active: JobTable::new(collapse),
            completed: JobTable::new(collapse),
            config,
            theme,
            focus: Focus::Active,
            search: None,
            bookmarks: BTreeSet::new(),
            log: Vec::new(),
            running_tasks: 0,
            pending_tasks: 0,
            partitions: Vec::new(),
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

        self.active.set_jobs(loaded.running);
        self.completed.set_jobs(loaded.completed);
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

    pub fn focus(&self) -> Focus {
        self.focus
    }

    pub fn is_searching(&self) -> bool {
        self.search.is_some()
    }

    /// The job the detail panels should show.
    pub fn selected_job_id(&self) -> Option<&str> {
        match self.focus {
            Focus::Active => self.active.selected_job_id(),
            Focus::Completed => self.completed.selected_job_id(),
        }
    }

    fn set_focus(&mut self, focus: Focus) {
        self.focus = focus;
        self.active.set_focused(focus == Focus::Active);
        self.completed.set_focused(focus == Focus::Completed);
    }

    // -- keys ---------------------------------------------------------------

    /// Handle one keystroke.
    pub fn handle_key(&mut self, key: KeyEvent) -> Command {
        if self.search.is_some() {
            return self.handle_search_key(key);
        }

        match (key.code, key.modifiers) {
            (KeyCode::Char('q'), _) | (KeyCode::Char('c'), KeyModifiers::CONTROL) => Command::Quit,
            (KeyCode::Char('r'), _) => Command::Refresh,
            (KeyCode::Char('/'), _) => {
                self.open_search();
                Command::None
            }
            (KeyCode::Char('m'), _) => {
                self.toggle_bookmark();
                Command::None
            }
            (KeyCode::Enter, _) => {
                self.toggle_expand();
                Command::None
            }
            (KeyCode::Down | KeyCode::Char('j'), _) => {
                self.move_cursor(1);
                Command::None
            }
            (KeyCode::Up | KeyCode::Char('k'), _) => {
                self.move_cursor(-1);
                Command::None
            }
            (KeyCode::Home | KeyCode::Char('g'), _) => {
                self.select_edge(false);
                Command::None
            }
            (KeyCode::End | KeyCode::Char('G'), _) => {
                self.select_edge(true);
                Command::None
            }
            _ => Command::None,
        }
    }

    fn handle_search_key(&mut self, key: KeyEvent) -> Command {
        let Some(query) = self.search.as_mut() else {
            return Command::None;
        };

        match key.code {
            KeyCode::Esc => {
                // Escape abandons the filter entirely.
                self.close_search(true);
            }
            KeyCode::Enter => {
                // Enter keeps it and returns to the table, so a filtered list
                // can actually be navigated.
                self.close_search(false);
            }
            KeyCode::Backspace => {
                query.pop();
                self.apply_filter();
            }
            KeyCode::Char(character) => {
                query.push(character);
                self.apply_filter();
            }
            _ => {}
        }
        Command::None
    }

    fn open_search(&mut self) {
        self.search = Some(String::new());
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
    fn move_cursor(&mut self, delta: isize) {
        let moved = match self.focus {
            Focus::Active => self.active.move_cursor(delta),
            Focus::Completed => self.completed.move_cursor(delta),
        };
        if moved {
            return;
        }

        // At the edge: hand over to the other table, entering from the side the
        // cursor was travelling towards.
        let other = self.focus.other();
        let has_rows = match other {
            Focus::Active => self.active.row_count() > 0,
            Focus::Completed => self.completed.row_count() > 0,
        };
        if !has_rows {
            return;
        }

        self.set_focus(other);
        let enter_at_end = delta < 0;
        match other {
            Focus::Active => self.active.select_edge(enter_at_end),
            Focus::Completed => self.completed.select_edge(enter_at_end),
        }
    }

    fn select_edge(&mut self, last: bool) {
        match self.focus {
            Focus::Active => self.active.select_edge(last),
            Focus::Completed => self.completed.select_edge(last),
        }
    }

    fn toggle_expand(&mut self) {
        match self.focus {
            Focus::Active => self.active.toggle_expand(None),
            Focus::Completed => self.completed.toggle_expand(None),
        };
    }

    /// Bookmark the selected row.
    ///
    /// On a collapsed array this bookmarks the array itself, so the whole group
    /// pins to the top rather than one arbitrary task.
    fn toggle_bookmark(&mut self) {
        let target = match self.focus {
            Focus::Active => self.active.selected_key().map(str::to_string),
            Focus::Completed => self.completed.selected_key().map(str::to_string),
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

    /// The bookmarked ids, for tests and for future persistence.
    pub fn bookmarks(&self) -> &BTreeSet<String> {
        &self.bookmarks
    }

    // -- drawing ------------------------------------------------------------

    pub fn draw(&self, frame: &mut Frame) {
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

        render::job_table(
            frame,
            layout.active_jobs,
            &self.active,
            &TableStyle {
                theme: &self.theme,
                config: &self.config,
                focused: self.focus == Focus::Active && self.search.is_none(),
            },
        );
        render::job_table(
            frame,
            layout.completed_jobs,
            &self.completed,
            &TableStyle {
                theme: &self.theme,
                config: &self.config,
                focused: self.focus == Focus::Completed && self.search.is_none(),
            },
        );

        // P5 fills these in.
        render::placeholder_panel(
            frame,
            layout.detail,
            "Job Details",
            &match self.selected_job_id() {
                Some(id) => format!("  job {id} — panels arrive in P5"),
                None => "  no job selected".to_string(),
            },
            false,
        );
        render::placeholder_panel(
            frame,
            layout.metadata,
            "Job Metadata",
            "  panels arrive in P5",
            false,
        );

        render::command_log(frame, layout.command_log, &self.log);
        render::footer(
            frame,
            layout.footer,
            &[
                ("q", "quit"),
                ("/", "filter"),
                ("r", "refresh"),
                ("m", "bookmark"),
                ("↵", "expand"),
            ],
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
                    spawn_poll(slurm.clone(), events.sender());
                }
                Command::None => {}
            },
            Event::Tick => spawn_poll(slurm.clone(), events.sender()),
            Event::Jobs(loaded) => app.apply_jobs(*loaded),
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
        assert_eq!(app.focus(), Focus::Active);

        app.handle_key(key(KeyCode::Down)); // to row 1, the last active row
        assert_eq!(app.focus(), Focus::Active);

        app.handle_key(key(KeyCode::Down)); // over the edge
        assert_eq!(app.focus(), Focus::Completed);
        assert_eq!(app.completed.selected_index(), 0);
    }

    #[test]
    fn up_from_the_first_row_moves_to_the_other_tables_last() {
        let mut app = app();
        app.handle_key(key(KeyCode::Up)); // over the top edge

        assert_eq!(app.focus(), Focus::Completed);
        assert_eq!(
            app.completed.selected_index(),
            app.completed.row_count() - 1
        );
    }

    #[test]
    fn wrapping_needs_somewhere_to_wrap_to() {
        let mut app = App::new(Config::default());
        app.apply_jobs(JobsLoaded {
            running: vec![running("100", "a", "RUNNING")],
            completed: vec![],
            partitions: vec![],
        });

        app.handle_key(key(KeyCode::Down));
        // The terminated table is empty, so focus stays put.
        assert_eq!(app.focus(), Focus::Active);
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
    fn backspace_widens_the_filter_again() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('/')));
        for key in typed("train-a") {
            app.handle_key(key);
        }
        app.handle_key(key(KeyCode::Backspace));

        // "train-" matches both active jobs again.
        assert_eq!(app.active.row_keys(), vec!["100", "101"]);
    }

    #[test]
    fn escape_abandons_the_filter() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('/')));
        for key in typed("train-a") {
            app.handle_key(key);
        }
        app.handle_key(key(KeyCode::Esc));

        assert!(!app.is_searching());
        assert_eq!(app.active.row_keys(), vec!["100", "101"]);
    }

    #[test]
    fn enter_keeps_the_filter_and_returns_to_the_table() {
        // Without this there is no way to navigate a filtered list.
        let mut app = app();
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
        assert_eq!(app.handle_key(key(KeyCode::Char('q'))), Command::None);
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
    fn g_and_shift_g_jump_to_the_ends() {
        let mut app = app();
        app.handle_key(key(KeyCode::Char('G')));
        assert_eq!(app.active.selected_index(), app.active.row_count() - 1);

        app.handle_key(key(KeyCode::Char('g')));
        assert_eq!(app.active.selected_index(), 0);
    }

    #[test]
    fn the_selected_job_follows_the_focused_table() {
        let mut app = app();
        assert_eq!(app.selected_job_id(), Some("100"));

        // Wrapping upward enters the other table at its last row.
        app.handle_key(key(KeyCode::Up));
        assert_eq!(app.focus(), Focus::Completed);
        assert_eq!(app.selected_job_id(), Some("91"));
    }

    /// Render the whole screen and return what it drew.
    fn screen(app: &App, width: u16, height: u16) -> String {
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
    fn draws_every_panel_of_the_main_screen() {
        let mut app = app();
        app.log("refresh", Some("complete".into()));

        let output = screen(&app, 120, 40);

        // The cluster bar, with task counts and partition availability.
        assert!(output.contains("running"), "{output}");
        assert!(output.contains("gpu:2/2/0/4"), "{output}");
        // Both job tables, with their contents.
        assert!(output.contains("Active Jobs"), "{output}");
        assert!(output.contains("Terminated Jobs"), "{output}");
        assert!(output.contains("train-a"), "{output}");
        // The terminated table has five columns in the same width, so its name
        // column gives way — its id and state still have to be there.
        assert!(output.contains("91"), "{output}");
        assert!(output.contains("COMPLETED"), "{output}");
        // The right column and the log.
        assert!(output.contains("Job Details"), "{output}");
        assert!(output.contains("Job Metadata"), "{output}");
        assert!(output.contains("Command Log"), "{output}");
        assert!(output.contains(">>> complete"), "{output}");
        // The footer.
        assert!(output.contains("quit"), "{output}");
    }

    #[test]
    fn draws_the_filter_bar_only_while_it_is_open() {
        let mut app = app();
        assert!(!screen(&app, 120, 40).contains("state:pend"));

        app.handle_key(key(KeyCode::Char('/')));
        // The placeholder documents the filter syntax.
        assert!(screen(&app, 120, 40).contains("state:pend"));
    }

    #[test]
    fn draws_at_awkward_terminal_sizes_without_panicking() {
        let app = app();
        for (width, height) in [(120, 40), (80, 24), (40, 12), (20, 6), (5, 3), (1, 1)] {
            screen(&app, width, height);
        }
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
