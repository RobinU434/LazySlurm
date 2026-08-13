//! A scrollable block of text: job logs, live node output, generated reports.
//!
//! Replaces Textual's `RichLog`. It holds its own content and scroll position
//! rather than deferring to a widget, because the interesting behaviour —
//! staying pinned to the newest line while a job writes, and letting go of that
//! the moment the user scrolls up — is state, and state is testable.

use ratatui::layout::Rect;
use ratatui::text::Line;
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use super::text::wrap;

/// A pane of scrollable text.
#[derive(Debug, Clone, Default)]
pub struct LogPane {
    content: String,
    /// The first visible wrapped line.
    scroll: usize,
    /// Whether to jump to the end when the content changes.
    ///
    /// True until the user scrolls up, which is what makes a live log follow
    /// itself without stealing the view from someone reading further back.
    following: bool,
    /// The width and height last drawn at, so scrolling knows its own bounds.
    viewport: (usize, usize),
}

impl LogPane {
    pub fn new() -> Self {
        Self {
            following: true,
            ..Self::default()
        }
    }

    /// Replace the content, following the end if the user has not scrolled away.
    pub fn set_content(&mut self, content: impl Into<String>) {
        self.content = content.into();
        if self.following {
            self.scroll = self.max_scroll();
        } else {
            self.scroll = self.scroll.min(self.max_scroll());
        }
    }

    pub fn content(&self) -> &str {
        &self.content
    }

    pub fn is_following(&self) -> bool {
        self.following
    }

    pub fn scroll_offset(&self) -> usize {
        self.scroll
    }

    /// Record the area the pane is drawn into, so scrolling can be bounded.
    pub fn set_viewport(&mut self, width: usize, height: usize) {
        self.viewport = (width, height);
        self.scroll = self.scroll.min(self.max_scroll());
    }

    /// Scroll by a number of lines; negative is towards the start.
    pub fn scroll_by(&mut self, delta: isize) {
        let target = self.scroll as isize + delta;
        self.scroll = target.clamp(0, self.max_scroll() as isize) as usize;
        // Scrolling up detaches from the end; reaching the bottom re-attaches.
        self.following = self.scroll >= self.max_scroll();
    }

    /// Jump to the start, which also stops following.
    pub fn scroll_to_start(&mut self) {
        self.scroll = 0;
        self.following = self.max_scroll() == 0;
    }

    /// Jump to the end and resume following.
    pub fn scroll_to_end(&mut self) {
        self.scroll = self.max_scroll();
        self.following = true;
    }

    /// The wrapped lines, at the last drawn width.
    fn lines(&self) -> Vec<String> {
        wrap(&self.content, self.viewport.0)
    }

    /// The furthest the pane can scroll: enough to show the last screenful.
    fn max_scroll(&self) -> usize {
        self.lines().len().saturating_sub(self.viewport.1)
    }
}

/// Draw the pane's visible slice.
///
/// Takes the area first so the pane can record it; the next scroll then knows
/// how far it may go.
pub fn render(frame: &mut Frame, area: Rect, pane: &mut LogPane) {
    pane.set_viewport(area.width as usize, area.height as usize);

    let lines = pane.lines();
    let start = pane.scroll.min(lines.len());
    let end = (start + area.height as usize).min(lines.len());

    let visible: Vec<Line> = lines[start..end].iter().map(Line::raw).collect();
    frame.render_widget(Paragraph::new(visible), area);
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A pane showing five lines at a time, filled with `count` numbered lines.
    fn pane(count: usize) -> LogPane {
        let mut pane = LogPane::new();
        pane.set_viewport(40, 5);
        pane.set_content(
            (0..count)
                .map(|index| format!("line {index}\n"))
                .collect::<String>(),
        );
        pane
    }

    /// The line numbers currently visible.
    fn visible(pane: &LogPane) -> Vec<String> {
        let lines = pane.lines();
        let start = pane.scroll.min(lines.len());
        let end = (start + pane.viewport.1).min(lines.len());
        lines[start..end].to_vec()
    }

    #[test]
    fn opens_showing_the_newest_lines() {
        // A job log is read from its end; that is where the error is.
        let pane = pane(20);
        assert_eq!(visible(&pane).first().unwrap(), "line 15");
        assert_eq!(visible(&pane).last().unwrap(), "line 19");
    }

    #[test]
    fn content_shorter_than_the_pane_does_not_scroll() {
        let pane = pane(3);
        assert_eq!(pane.scroll_offset(), 0);
        assert_eq!(visible(&pane).len(), 3);
    }

    #[test]
    fn scrolling_up_stops_it_following_new_output() {
        let mut pane = pane(20);
        pane.scroll_by(-5);
        assert!(!pane.is_following());
        assert_eq!(visible(&pane).first().unwrap(), "line 10");

        // The job writes more; the view stays where the reader put it.
        pane.set_content((0..30).map(|i| format!("line {i}\n")).collect::<String>());
        assert_eq!(visible(&pane).first().unwrap(), "line 10");
    }

    #[test]
    fn scrolling_back_to_the_bottom_resumes_following() {
        let mut pane = pane(20);
        pane.scroll_by(-5);
        pane.scroll_by(5);
        assert!(pane.is_following());

        pane.set_content((0..30).map(|i| format!("line {i}\n")).collect::<String>());
        assert_eq!(visible(&pane).last().unwrap(), "line 29");
    }

    #[test]
    fn cannot_scroll_past_either_end() {
        let mut pane = pane(20);

        pane.scroll_by(-1000);
        assert_eq!(pane.scroll_offset(), 0);
        assert_eq!(visible(&pane).first().unwrap(), "line 0");

        pane.scroll_by(1000);
        assert_eq!(visible(&pane).last().unwrap(), "line 19");
    }

    #[test]
    fn jumps_to_either_end() {
        let mut pane = pane(20);

        pane.scroll_to_start();
        assert_eq!(visible(&pane).first().unwrap(), "line 0");
        assert!(!pane.is_following());

        pane.scroll_to_end();
        assert_eq!(visible(&pane).last().unwrap(), "line 19");
        assert!(pane.is_following());
    }

    #[test]
    fn a_narrower_pane_wraps_and_lengthens_the_content() {
        let mut pane = LogPane::new();
        pane.set_viewport(10, 5);
        pane.set_content("a line that is definitely longer than ten columns\n");

        assert!(pane.lines().len() > 1);
        for line in pane.lines() {
            assert!(super::super::text::width(&line) <= 10);
        }
    }

    #[test]
    fn shrinking_the_viewport_keeps_the_scroll_in_range() {
        let mut pane = pane(20);
        pane.scroll_to_end();

        // The window is resized taller, so there is less to scroll past.
        pane.set_viewport(40, 30);
        assert_eq!(pane.scroll_offset(), 0);
    }

    #[test]
    fn empty_content_is_harmless() {
        let mut pane = LogPane::new();
        pane.set_viewport(40, 5);
        pane.set_content("");

        assert_eq!(pane.scroll_offset(), 0);
        pane.scroll_by(10);
        assert_eq!(pane.scroll_offset(), 0);
    }
}
