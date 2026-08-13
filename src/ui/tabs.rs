//! A strip of tabs across the top of a panel.
//!
//! The visible set can change — the metadata panel's Pending tab exists only
//! while the selected job is waiting — so the strip holds only the tabs that are
//! currently shown, and cycling naturally skips the rest. The active tab is
//! remembered by name across changes, not by index, so a tab appearing or
//! disappearing elsewhere in the strip does not move the user.

use ratatui::layout::Rect;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use super::theme;

/// The tabs of one panel.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TabStrip {
    tabs: Vec<&'static str>,
    active: usize,
}

impl TabStrip {
    pub fn new(tabs: Vec<&'static str>) -> Self {
        Self { tabs, active: 0 }
    }

    /// Replace the visible tabs, keeping the user on the same one if it is still
    /// there and falling back to the first if it is not.
    pub fn set_tabs(&mut self, tabs: Vec<&'static str>) {
        let current = self.active();
        self.tabs = tabs;
        self.active = self
            .tabs
            .iter()
            .position(|tab| Some(*tab) == current)
            .unwrap_or(0);
    }

    /// The active tab, or `None` when there are no tabs at all.
    pub fn active(&self) -> Option<&'static str> {
        self.tabs.get(self.active).copied()
    }

    pub fn active_index(&self) -> usize {
        self.active
    }

    pub fn tabs(&self) -> &[&'static str] {
        &self.tabs
    }

    /// Move `delta` tabs along, wrapping at both ends.
    pub fn cycle(&mut self, delta: isize) {
        if self.tabs.is_empty() {
            return;
        }
        let count = self.tabs.len() as isize;
        self.active = (((self.active as isize + delta) % count + count) % count) as usize;
    }

    /// Jump to a tab by name. Returns whether it was there.
    pub fn select(&mut self, name: &str) -> bool {
        match self.tabs.iter().position(|tab| *tab == name) {
            Some(index) => {
                self.active = index;
                true
            }
            None => false,
        }
    }
}

/// Draw the strip: the active tab emphasised, the rest dim.
pub fn render(frame: &mut Frame, area: Rect, strip: &TabStrip) {
    let mut spans: Vec<Span> = Vec::new();
    for (index, tab) in strip.tabs().iter().enumerate() {
        if !spans.is_empty() {
            spans.push(Span::raw("  "));
        }
        let style = if index == strip.active_index() {
            theme::bold()
        } else {
            theme::dim()
        };
        spans.push(Span::styled(*tab, style));
    }
    frame.render_widget(Paragraph::new(Line::from(spans)), area);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strip() -> TabStrip {
        TabStrip::new(vec!["stdout", "stderr", "stats"])
    }

    #[test]
    fn starts_on_the_first_tab() {
        assert_eq!(strip().active(), Some("stdout"));
    }

    #[test]
    fn cycles_in_both_directions_and_wraps() {
        let mut strip = strip();

        strip.cycle(1);
        assert_eq!(strip.active(), Some("stderr"));
        strip.cycle(1);
        strip.cycle(1);
        assert_eq!(strip.active(), Some("stdout"), "should wrap past the end");

        strip.cycle(-1);
        assert_eq!(strip.active(), Some("stats"), "should wrap past the start");
    }

    #[test]
    fn keeps_the_user_on_the_same_tab_when_the_set_changes() {
        let mut strip = strip();
        strip.select("stats");

        // A tab appears before it; the user should not be moved.
        strip.set_tabs(vec!["stdout", "stderr", "cpu", "stats"]);
        assert_eq!(strip.active(), Some("stats"));
    }

    #[test]
    fn falls_back_to_the_first_tab_when_the_active_one_goes_away() {
        let mut strip = strip();
        strip.select("stats");

        strip.set_tabs(vec!["stdout", "stderr"]);
        assert_eq!(strip.active(), Some("stdout"));
    }

    #[test]
    fn cycling_skips_tabs_that_are_not_shown() {
        // The metadata panel drops Pending for a running job.
        let mut strip = TabStrip::new(vec!["Resources", "Submission", "Pending", "Raw"]);
        strip.select("Submission");
        strip.set_tabs(vec!["Resources", "Submission", "Raw"]);

        strip.cycle(1);
        assert_eq!(
            strip.active(),
            Some("Raw"),
            "Pending is gone, so Raw is next"
        );
    }

    #[test]
    fn selecting_an_absent_tab_changes_nothing() {
        let mut strip = strip();
        assert!(!strip.select("gpu"));
        assert_eq!(strip.active(), Some("stdout"));
    }

    #[test]
    fn an_empty_strip_is_harmless() {
        let mut strip = TabStrip::new(vec![]);
        strip.cycle(1);
        assert_eq!(strip.active(), None);
    }
}
