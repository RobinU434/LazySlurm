//! Turning state into cells.
//!
//! Nothing here decides anything: every question of what to show has already
//! been answered by [`super::job_table`] and friends. This module owns column
//! widths, borders and where the styles from [`super::theme`] land.

use ratatui::layout::{Constraint, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{
    Block, BorderType, Borders, Cell, Clear, Paragraph, Row as TableRow, Table, TableState,
};
use ratatui::Frame;

use crate::config::Config;
use crate::model::{CompletedJob, RunningJob};

use super::job_table::{JobRow, JobTable, Row};
use super::text::truncate;
use super::theme::{self, Theme};

/// One column of a table.
pub struct ColumnSpec {
    pub name: &'static str,
    /// Whether this column absorbs the leftover width. Exactly one should.
    pub flex: bool,
}

/// A table's columns, in display order.
type Columns = &'static [ColumnSpec];

/// The widest a measured column may become, so one enormous job name cannot
/// push every other column off the panel.
const MAX_MEASURED_WIDTH: u16 = 24;

/// Everything a job table needs in order to draw itself.
pub struct TableStyle<'a> {
    pub theme: &'a Theme,
    pub config: &'a Config,
    pub focused: bool,
}

/// A job table that knows its own columns and how to fill them.
///
/// Implemented separately for the two tables rather than parameterised: they
/// genuinely differ — the active table colours the id by state and shows
/// elapsed, the terminated one has a state column and shows the longest run.
pub trait DrawableTable {
    fn columns() -> Columns;
    fn base_title() -> &'static str;

    /// The cells of one row, already styled and truncated.
    ///
    /// Returned as `Line`s rather than `Cell`s so their widths can be measured
    /// before the column layout is decided.
    fn cells(&self, row: &Row, style: &TableStyle) -> Vec<Line<'static>>;
}

/// Draw a bordered, titled job table with its cursor.
pub fn job_table<T>(frame: &mut Frame, area: Rect, table: &JobTable<T>, style: &TableStyle)
where
    T: JobRow,
    JobTable<T>: DrawableTable,
{
    let block = panel(&table.title(JobTable::<T>::base_title()), style.focused);
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let columns = JobTable::<T>::columns();
    let cells: Vec<Vec<Line>> = table
        .rows()
        .iter()
        .map(|row| table.cells(row, style))
        .collect();

    let header = TableRow::new(
        columns
            .iter()
            .map(|column| Cell::from(column.name))
            .collect::<Vec<_>>(),
    )
    .style(theme::header());

    let rows: Vec<TableRow> = cells
        .iter()
        .enumerate()
        .map(|(index, line)| {
            let mut table_row = TableRow::new(line.clone());
            // Zebra striping, so a wide row is easy to follow across.
            if index % 2 == 1 {
                table_row = table_row.style(theme::stripe());
            }
            table_row
        })
        .collect();

    let widget = Table::new(rows, column_widths(columns, &cells))
        .header(header)
        .row_highlight_style(if style.focused {
            theme::cursor()
        } else {
            theme::cursor_unfocused()
        });

    // Rendered with state so ratatui scrolls the viewport to keep the cursor
    // visible: a table with more jobs than rows would otherwise leave the
    // selection off-screen, with no way to tell where it had got to.
    let mut state = TableState::new().with_selected(Some(table.selected_index()));
    frame.render_stateful_widget(widget, inner, &mut state);
}

/// Size each column to its widest cell, leaving the flexible one the remainder.
///
/// Fixed widths would either waste space on a wide terminal or, on a narrow one,
/// add up to more than the panel has — at which point ratatui drops whole
/// columns off the right rather than shrinking them.
fn column_widths(columns: Columns, cells: &[Vec<Line>]) -> Vec<Constraint> {
    columns
        .iter()
        .enumerate()
        .map(|(index, column)| {
            if column.flex {
                return Constraint::Fill(1);
            }
            let widest = cells
                .iter()
                .filter_map(|row| row.get(index))
                .map(|line| line.width() as u16)
                .max()
                .unwrap_or(0)
                .max(column.name.len() as u16);
            Constraint::Length(widest.min(MAX_MEASURED_WIDTH))
        })
        .collect()
}

/// A bordered panel, highlighted when focused.
pub fn panel(title: &str, focused: bool) -> Block<'static> {
    let (border_style, title_style) = if focused {
        (theme::border_focused(), theme::title_focused())
    } else {
        (theme::border(), theme::title())
    };
    Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(border_style)
        .title(Line::from(Span::styled(format!(" {title} "), title_style)).centered())
}

impl DrawableTable for JobTable<RunningJob> {
    fn columns() -> Columns {
        &[
            ColumnSpec {
                name: "Job ID",
                flex: false,
            },
            ColumnSpec {
                name: "Name",
                flex: true,
            },
            ColumnSpec {
                name: "Elapsed",
                flex: false,
            },
            ColumnSpec {
                name: "Partition",
                flex: false,
            },
        ]
    }

    fn base_title() -> &'static str {
        "Active Jobs"
    }

    fn cells(&self, row: &Row, style: &TableStyle) -> Vec<Line<'static>> {
        match row {
            Row::Job { index, depth } => {
                let job = self.job(*index);
                vec![
                    Line::from(Span::styled(
                        job.job_id.clone(),
                        style.theme.active_state(&job.state),
                    )),
                    Line::from(name_cell(
                        depth.indent(),
                        &self.markers(&job.job_id),
                        &job.name,
                        style.config.max_name_width,
                    )),
                    Line::from(job.elapsed.clone()),
                    Line::from(partition_cell(&job.partition, style)),
                ]
            }
            Row::Group {
                base,
                members,
                expanded,
            } => {
                let first = self.job(members[0]);
                vec![
                    Line::from(self.group_label(base, members, *expanded)),
                    Line::from(name_cell(
                        "",
                        &self.markers(base),
                        &first.name,
                        style.config.max_name_width,
                    )),
                    // The tally replaces Elapsed, which means little for a dozen
                    // tasks that started at different times.
                    tally_line(&self.state_tally(members), style.theme, true),
                    Line::from(partition_cell(&first.partition, style)),
                ]
            }
            Row::Placeholder => placeholder_cells(Self::columns().len()),
        }
    }
}

impl DrawableTable for JobTable<CompletedJob> {
    fn columns() -> Columns {
        &[
            ColumnSpec {
                name: "Job ID",
                flex: false,
            },
            ColumnSpec {
                name: "Name",
                flex: true,
            },
            ColumnSpec {
                name: "State",
                flex: false,
            },
            ColumnSpec {
                name: "Partition",
                flex: false,
            },
            ColumnSpec {
                name: "Elapsed",
                flex: false,
            },
        ]
    }

    fn base_title() -> &'static str {
        "Terminated Jobs"
    }

    fn cells(&self, row: &Row, style: &TableStyle) -> Vec<Line<'static>> {
        match row {
            Row::Job { index, depth } => {
                let job = self.job(*index);
                vec![
                    Line::from(job.job_id.clone()),
                    Line::from(name_cell(
                        depth.indent(),
                        &self.markers(&job.job_id),
                        &job.name,
                        style.config.max_name_width,
                    )),
                    Line::from(Span::styled(
                        style.theme.state_label(&job.state).to_string(),
                        style.theme.terminated_state(&job.state),
                    )),
                    Line::from(partition_cell(&job.partition, style)),
                    Line::from(job.elapsed.clone()),
                ]
            }
            Row::Group {
                base,
                members,
                expanded,
            } => {
                let first = self.job(members[0]);
                vec![
                    Line::from(self.group_label(base, members, *expanded)),
                    Line::from(name_cell(
                        "",
                        &self.markers(base),
                        &first.name,
                        style.config.max_name_width,
                    )),
                    tally_line(&self.state_tally(members), style.theme, false),
                    Line::from(partition_cell(&first.partition, style)),
                    Line::from(self.longest_elapsed(members).to_string()),
                ]
            }
            Row::Placeholder => placeholder_cells(Self::columns().len()),
        }
    }
}

/// The name cell: tree indent, then markers, then the name, all within budget.
///
/// The markers are inside the truncation budget on purpose — they must not push
/// the column wider than the user configured.
fn name_cell(indent: &str, markers: &str, name: &str, max_width: usize) -> String {
    truncate(&format!("{indent}{markers}{name}"), max_width)
}

fn partition_cell(partition: &str, style: &TableStyle) -> Span<'static> {
    Span::styled(
        truncate(partition, style.config.max_partition_width),
        style.theme.partition(partition),
    )
}

/// `2run 10pend` — an array's state counts, coloured per state.
fn tally_line(counts: &[(String, u32)], theme: &Theme, active: bool) -> Line<'static> {
    let mut spans: Vec<Span> = Vec::new();
    for (state, count) in counts {
        if !spans.is_empty() {
            spans.push(Span::raw(" "));
        }
        let style = if active {
            theme.active_state(state)
        } else {
            theme.terminated_state(state)
        };
        spans.push(Span::styled(count.to_string(), style));
        // Always the short form, however `abbreviate_states` is set: a tally has
        // no room for OUT_OF_MEMORY, and the count is the part that matters.
        let short: String = theme::abbreviation(state).chars().take(4).collect();
        spans.push(Span::styled(short.to_lowercase(), theme::dim()));
    }
    Line::from(spans)
}

/// The "nothing matched" row: a message where the id goes, and empty cells after.
fn placeholder_cells(columns: usize) -> Vec<Line<'static>> {
    let mut cells = vec![Line::from(Span::styled("no jobs match", theme::dim()))];
    cells.extend((1..columns).map(|_| Line::from("")));
    cells
}

/// The one-line cluster summary above the tables.
pub fn cluster_bar(
    frame: &mut Frame,
    area: Rect,
    user: &str,
    running: u32,
    pending: u32,
    partitions: &[String],
) {
    let mut spans = vec![
        Span::styled(user.to_string(), theme::bold()),
        Span::raw("  "),
        Span::styled(running.to_string(), theme::running()),
        Span::raw(" running  "),
        Span::styled(pending.to_string(), theme::pending()),
        Span::raw(" pending"),
    ];
    if !partitions.is_empty() {
        spans.push(Span::raw("   "));
        spans.push(Span::styled(partitions.join("  "), theme::dim()));
    }
    frame.render_widget(Paragraph::new(Line::from(spans)), area);
}

/// One entry in the command log.
#[derive(Debug, Clone)]
pub struct LogEntry {
    pub time: String,
    pub action: String,
    pub result: Option<String>,
}

impl LogEntry {
    /// The lines this entry occupies: the action, then its result if it has one.
    fn lines(&self) -> Vec<Line<'static>> {
        let mut lines = vec![Line::from(vec![
            Span::styled(self.time.clone(), theme::dim()),
            Span::raw(" "),
            Span::raw(self.action.clone()),
        ])];
        if let Some(result) = &self.result {
            lines.push(Line::from(Span::styled(
                format!("  >>> {result}"),
                theme::dim(),
            )));
        }
        lines
    }
}

/// The command log panel, showing its most recent entries.
pub fn command_log(frame: &mut Frame, area: Rect, entries: &[LogEntry]) {
    let block = panel("Command Log", false);
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let lines: Vec<Line> = entries.iter().flat_map(LogEntry::lines).collect();
    // Show the tail: the newest entry is the one worth seeing.
    let start = lines.len().saturating_sub(inner.height as usize);
    frame.render_widget(Paragraph::new(lines[start..].to_vec()), inner);
}

/// The key bar along the bottom.
pub fn footer(frame: &mut Frame, area: Rect, keys: &[(&str, &str)]) {
    let mut spans: Vec<Span> = Vec::new();
    for (key, description) in keys {
        if !spans.is_empty() {
            spans.push(Span::raw("  "));
        }
        spans.push(Span::styled((*key).to_string(), theme::bold()));
        spans.push(Span::raw(" "));
        spans.push(Span::styled((*description).to_string(), theme::dim()));
    }
    frame.render_widget(Paragraph::new(Line::from(spans)), area);
}

/// Draw a bordered panel with a tab strip, and return the area below it.
pub fn tabbed_panel(
    frame: &mut Frame,
    area: Rect,
    title: &str,
    strip: &super::tabs::TabStrip,
    focused: bool,
) -> Rect {
    let block = panel(title, focused);
    let inner = block.inner(area);
    frame.render_widget(block, area);

    if inner.height == 0 {
        return inner;
    }
    let tab_row = Rect { height: 1, ..inner };
    super::tabs::render(frame, tab_row, strip);

    Rect {
        y: inner.y + 1,
        height: inner.height - 1,
        ..inner
    }
}

/// Draw pre-styled lines, scrolled by `offset`.
pub fn lines(frame: &mut Frame, area: Rect, content: &[Line<'static>], offset: usize) {
    let start = offset.min(content.len());
    let end = (start + area.height as usize).min(content.len());
    frame.render_widget(Paragraph::new(content[start..end].to_vec()), area);
}

/// Draw the help overlay, centred over whatever is behind it.
pub fn help_overlay(frame: &mut Frame, area: Rect, content: &[Line<'static>]) {
    let widest = content.iter().map(Line::width).max().unwrap_or(0) as u16;
    let box_area = super::layout::centered(
        area,
        (widest + 4).min(area.width),
        (content.len() as u16 + 2).min(area.height),
    );

    frame.render_widget(Clear, box_area);
    let block = panel("Help", true);
    let inner = block.inner(box_area);
    frame.render_widget(block, box_area);
    lines(frame, inner, content, 0);
}

/// Draw a bordered table of pre-built rows, scrolling to keep the cursor shown.
pub fn simple_table(
    frame: &mut Frame,
    area: Rect,
    title: &str,
    columns: &[&str],
    rows: Vec<Vec<Line<'static>>>,
    selected: usize,
    focused: bool,
) {
    let block = panel(title, focused);
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let header = TableRow::new(
        columns
            .iter()
            .map(|name| Cell::from(*name))
            .collect::<Vec<_>>(),
    )
    .style(theme::header());

    let widths: Vec<Constraint> = (0..columns.len())
        .map(|index| {
            let widest = rows
                .iter()
                .filter_map(|row| row.get(index))
                .map(|line| line.width() as u16)
                .max()
                .unwrap_or(0)
                .max(columns[index].len() as u16);
            Constraint::Length(widest.min(MAX_MEASURED_WIDTH))
        })
        .collect();

    let table_rows: Vec<TableRow> = rows
        .into_iter()
        .enumerate()
        .map(|(index, cells)| {
            let mut row = TableRow::new(cells);
            if index % 2 == 1 {
                row = row.style(theme::stripe());
            }
            row
        })
        .collect();

    let widget = Table::new(table_rows, widths)
        .header(header)
        .row_highlight_style(if focused {
            theme::cursor()
        } else {
            theme::cursor_unfocused()
        });

    let mut state = TableState::new().with_selected(Some(selected));
    frame.render_stateful_widget(widget, inner, &mut state);
}

/// Draw a one-line summary bar.
pub fn summary_bar(frame: &mut Frame, area: Rect, line: Line<'static>) {
    frame.render_widget(Paragraph::new(line), area);
}

/// A panel with a single message in it, for areas not yet implemented.
pub fn placeholder_panel(frame: &mut Frame, area: Rect, title: &str, message: &str, focused: bool) {
    let block = panel(title, focused);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    frame.render_widget(
        Paragraph::new(Span::styled(message.to_string(), theme::dim())),
        inner,
    );
}

/// The style used for a text input's contents.
pub fn search_bar(frame: &mut Frame, area: Rect, query: &str) {
    let spans = if query.is_empty() {
        vec![
            Span::styled("/", theme::bold()),
            Span::styled(
                " Filter: text · state:pend · part:gpu · name:train · gpu:>0",
                theme::dim(),
            ),
        ]
    } else {
        vec![
            Span::styled("/", theme::bold()),
            Span::raw(query.to_string()),
        ]
    };
    frame.render_widget(Paragraph::new(Line::from(spans)), area);
}

/// Where the terminal cursor should sit while the filter bar is open.
pub fn search_cursor(area: Rect, query: &str) -> (u16, u16) {
    let offset = 1 + super::text::width(query) as u16;
    (area.x + offset.min(area.width.saturating_sub(1)), area.y)
}

/// A style that draws nothing, for cells with no emphasis.
pub fn plain() -> Style {
    Style::new()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::job_table::JobTable;
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    fn running(id: &str, name: &str, state: &str, partition: &str) -> RunningJob {
        RunningJob {
            job_id: id.into(),
            name: name.into(),
            elapsed: "1:00".into(),
            partition: partition.into(),
            state: state.into(),
            ..RunningJob::default()
        }
    }

    /// Render a table into a test terminal and return what it drew.
    fn draw(table: &JobTable<RunningJob>, width: u16, height: u16) -> String {
        let config = Config::default();
        let theme = Theme::default();
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();

        terminal
            .draw(|frame| {
                job_table(
                    frame,
                    frame.area(),
                    table,
                    &TableStyle {
                        theme: &theme,
                        config: &config,
                        focused: true,
                    },
                );
            })
            .unwrap();

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
    fn draws_a_bordered_table_with_headers_and_rows() {
        let mut table = JobTable::new(true);
        table.set_jobs(vec![
            running("100", "train-a", "RUNNING", "gpu"),
            running("101", "train-b", "PENDING", "cpu"),
        ]);

        let output = draw(&table, 60, 8);
        assert!(output.contains("Active Jobs"), "{output}");
        assert!(output.contains("Job ID"), "{output}");
        assert!(output.contains("100"), "{output}");
        assert!(output.contains("train-b"), "{output}");
        // Rounded borders, as the stylesheet asks for.
        assert!(output.contains('╭'), "{output}");
    }

    #[test]
    fn shows_the_match_count_in_the_title_while_filtering() {
        let mut table = JobTable::new(true);
        table.set_jobs(vec![
            running("100", "train-a", "RUNNING", "gpu"),
            running("101", "other", "PENDING", "cpu"),
        ]);
        table.set_filter("train");

        assert!(draw(&table, 60, 8).contains("1/2 match"));
    }

    #[test]
    fn draws_the_placeholder_when_nothing_matches() {
        let mut table = JobTable::new(true);
        table.set_jobs(vec![running("100", "train-a", "RUNNING", "gpu")]);
        table.set_filter("zzz");

        assert!(draw(&table, 60, 8).contains("no jobs match"));
    }

    #[test]
    fn draws_a_collapsed_array_as_one_row() {
        let mut table = JobTable::new(true);
        table.set_jobs(vec![
            running("200_0", "sweep", "RUNNING", "gpu"),
            running("200_[1-9]", "sweep", "PENDING", "gpu"),
        ]);

        let output = draw(&table, 70, 8);
        assert!(output.contains("▸ 200_[0-9]"), "{output}");
        assert!(output.contains("×10"), "{output}");
        // The tally replaces the elapsed column.
        assert!(output.contains("pend"), "{output}");
    }

    #[test]
    fn scrolls_to_keep_the_cursor_visible() {
        // More jobs than the panel has rows: the selection must still be drawn.
        let mut table = JobTable::new(true);
        table.set_jobs(
            (0..100)
                .map(|index| running(&format!("{}", 1000 + index), "job", "RUNNING", "gpu"))
                .collect(),
        );
        table.select_edge(true); // the last row

        let output = draw(&table, 60, 10);
        assert!(
            output.contains("1099"),
            "the cursor scrolled out of view:\n{output}"
        );
        // And the top of the list has scrolled away.
        assert!(!output.contains("1000"), "{output}");
    }

    #[test]
    fn shows_the_top_of_the_list_before_the_cursor_moves() {
        let mut table = JobTable::new(true);
        table.set_jobs(
            (0..100)
                .map(|index| running(&format!("{}", 1000 + index), "job", "RUNNING", "gpu"))
                .collect(),
        );

        let output = draw(&table, 60, 10);
        assert!(output.contains("1000"), "{output}");
    }

    #[test]
    fn a_narrow_terminal_does_not_panic() {
        let mut table = JobTable::new(true);
        table.set_jobs(vec![running("100", "train-a", "RUNNING", "gpu")]);
        draw(&table, 20, 5);
        draw(&table, 4, 3);
    }

    #[test]
    fn truncates_a_long_name_to_the_configured_width() {
        assert_eq!(name_cell("", "", "a-very-long-job-name", 10), "a-very-lo…");
        // Markers count against the budget, so the column cannot grow.
        assert_eq!(name_cell("", "★ ", "abcdefghij", 10), "★ abcdefg…");
    }

    #[test]
    fn a_tally_names_states_in_short_form() {
        let theme = Theme::default();
        let counts = vec![("PENDING".to_string(), 10), ("RUNNING".to_string(), 2)];
        let text: String = tally_line(&counts, &theme, true)
            .spans
            .iter()
            .map(|span| span.content.to_string())
            .collect();
        assert_eq!(text, "10pend 2run");
    }
}
