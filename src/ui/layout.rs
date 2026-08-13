//! Where each panel goes.
//!
//! The proportions come from the Python's stylesheet, where they are `fr` units:
//! a 1fr/2fr column split, a 1fr/1fr split of the left column, and 2fr/1fr on
//! the right above a command log sized to its content between 3 and 6 rows.

use ratatui::layout::{Constraint, Direction, Layout, Rect};

/// How tall the command log is allowed to grow.
const LOG_MIN_HEIGHT: u16 = 3;
const LOG_MAX_HEIGHT: u16 = 6;

/// The regions of the main screen.
pub struct MainLayout {
    /// One line at the top: user, job counts, partition availability.
    pub cluster_bar: Rect,
    /// The filter input, when it is showing.
    pub search: Option<Rect>,
    pub active_jobs: Rect,
    pub completed_jobs: Rect,
    pub detail: Rect,
    pub metadata: Rect,
    pub command_log: Rect,
    /// One line at the bottom: the key bar.
    pub footer: Rect,
}

/// Split the screen for the main view.
///
/// `log_lines` sizes the command log to its content, within its bounds — the
/// stylesheet's `height: auto; min-height: 3; max-height: 6`.
pub fn main_layout(area: Rect, search_visible: bool, log_lines: u16) -> MainLayout {
    let [cluster_bar, body, footer] = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),
            Constraint::Min(0),
            Constraint::Length(1),
        ])
        .areas(area);

    // The left column is 1fr against the right column's 2fr.
    let [left, right] = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Ratio(1, 3), Constraint::Ratio(2, 3)])
        .areas(body);

    let (search, tables) = if search_visible {
        let [search, tables] = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(1), Constraint::Min(0)])
            .areas(left);
        (Some(search), tables)
    } else {
        (None, left)
    };

    let [active_jobs, completed_jobs] = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Ratio(1, 2), Constraint::Ratio(1, 2)])
        .areas(tables);

    let log_height = log_lines.clamp(LOG_MIN_HEIGHT, LOG_MAX_HEIGHT);
    let [panels, command_log] = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(0), Constraint::Length(log_height)])
        .areas(right);

    let [detail, metadata] = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Ratio(2, 3), Constraint::Ratio(1, 3)])
        .areas(panels);

    MainLayout {
        cluster_bar,
        search,
        active_jobs,
        completed_jobs,
        detail,
        metadata,
        command_log,
        footer,
    }
}

/// Split a full-screen panel into a summary bar, two stacked tables and a footer.
///
/// Used by the partition monitor (2fr/3fr) and the node view (3fr/2fr).
pub fn stacked_layout(area: Rect, top_weight: u32, bottom_weight: u32) -> (Rect, Rect, Rect, Rect) {
    let [bar, body, footer] = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),
            Constraint::Min(0),
            Constraint::Length(1),
        ])
        .areas(area);

    let total = top_weight + bottom_weight;
    let [top, bottom] = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Ratio(top_weight, total),
            Constraint::Ratio(bottom_weight, total),
        ])
        .areas(body);

    (bar, top, bottom, footer)
}

/// Centre a box of the given size inside `area`, for a modal.
pub fn centered(area: Rect, width: u16, height: u16) -> Rect {
    let width = width.min(area.width);
    let height = height.min(area.height);
    Rect {
        x: area.x + (area.width - width) / 2,
        y: area.y + (area.height - height) / 2,
        width,
        height,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn screen() -> Rect {
        Rect::new(0, 0, 120, 40)
    }

    #[test]
    fn the_bars_take_one_line_each() {
        let layout = main_layout(screen(), false, 3);
        assert_eq!(layout.cluster_bar.height, 1);
        assert_eq!(layout.footer.height, 1);
        assert_eq!(layout.cluster_bar.y, 0);
        assert_eq!(layout.footer.y, 39);
    }

    #[test]
    fn the_right_column_is_twice_the_left() {
        let layout = main_layout(screen(), false, 3);
        assert_eq!(layout.active_jobs.width, 40);
        assert_eq!(layout.detail.width, 80);
    }

    #[test]
    fn the_job_tables_split_the_left_column_evenly() {
        let layout = main_layout(screen(), false, 3);
        assert_eq!(layout.active_jobs.height, layout.completed_jobs.height);
        assert!(layout.completed_jobs.y > layout.active_jobs.y);
    }

    #[test]
    fn the_search_bar_takes_a_line_from_the_tables() {
        let without = main_layout(screen(), false, 3);
        let with = main_layout(screen(), true, 3);

        assert!(without.search.is_none());
        assert_eq!(with.search.unwrap().height, 1);
        // The line comes out of the table area, not out of the screen.
        let table_height = |l: &MainLayout| l.active_jobs.height + l.completed_jobs.height;
        assert_eq!(table_height(&with), table_height(&without) - 1);
    }

    #[test]
    fn the_command_log_is_sized_to_its_content_within_bounds() {
        assert_eq!(main_layout(screen(), false, 0).command_log.height, 3);
        assert_eq!(main_layout(screen(), false, 4).command_log.height, 4);
        assert_eq!(main_layout(screen(), false, 99).command_log.height, 6);
    }

    #[test]
    fn the_detail_panel_is_twice_the_metadata_panel() {
        let layout = main_layout(screen(), false, 3);
        // 40 rows less the two bars and a 3-row log leaves 35 for the panels.
        assert_eq!(layout.detail.height + layout.metadata.height, 35);
        assert!(layout.detail.height > layout.metadata.height * 2 - 2);
    }

    #[test]
    fn a_tiny_terminal_does_not_panic() {
        // Smaller than the bars need; every region must still be valid.
        let layout = main_layout(Rect::new(0, 0, 4, 2), true, 3);
        assert!(layout.active_jobs.height <= 2);
        assert!(layout.footer.height <= 1);
    }

    #[test]
    fn stacked_panels_honour_their_weights() {
        let (bar, top, bottom, footer) = stacked_layout(screen(), 2, 3);
        assert_eq!(bar.height, 1);
        assert_eq!(footer.height, 1);
        assert_eq!(top.height + bottom.height, 38);
        assert!(bottom.height > top.height);
    }

    #[test]
    fn a_modal_is_centred_and_never_larger_than_the_screen() {
        let box_ = centered(screen(), 50, 10);
        assert_eq!(box_.width, 50);
        assert_eq!(box_.x, 35);
        assert_eq!(box_.y, 15);

        let oversized = centered(screen(), 500, 100);
        assert_eq!(oversized.width, 120);
        assert_eq!(oversized.height, 40);
    }
}
