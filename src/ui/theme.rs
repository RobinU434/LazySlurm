//! Every colour and style the interface uses.
//!
//! The Python keeps these in a Textual stylesheet plus Rich markup scattered
//! through the widgets. ratatui has no stylesheet, so they live here instead —
//! in one place, rather than as `Style::default().fg(...)` expressions spread
//! across the renderers.
//!
//! Colours are deliberately the terminal's own 16 where possible, so the app
//! inherits whatever palette the user has configured, rather than imposing one.

use std::collections::BTreeMap;

use ratatui::style::{Color, Modifier, Style};

/// Colours cycled through for partitions with no configured colour.
///
/// The order matters: it must match the Python's list exactly, or the same
/// partition gets a different colour in each implementation and users running
/// both side by side see them disagree.
const PARTITION_PALETTE: [Color; 9] = [
    Color::Cyan,
    Color::Magenta,
    Color::Yellow,
    Color::Green,
    Color::Blue,
    Color::Red,
    Color::LightCyan,
    Color::LightMagenta,
    Color::LightGreen,
];

/// Terminated job states, coloured by how much attention they deserve.
const TERMINATED_STATES: &[(&str, Style)] = &[
    ("COMPLETED", Style::new().fg(Color::Green)),
    (
        "FAILED",
        Style::new().fg(Color::Red).add_modifier(Modifier::BOLD),
    ),
    ("TIMEOUT", Style::new().fg(Color::Yellow)),
    ("CANCELLED", Style::new().add_modifier(Modifier::DIM)),
    ("CANCELLED+", Style::new().add_modifier(Modifier::DIM)),
    ("OUT_OF_MEMORY", Style::new().fg(Color::Red)),
    ("NODE_FAIL", Style::new().fg(Color::Red)),
    (
        "PREEMPTED",
        Style::new().fg(Color::Yellow).add_modifier(Modifier::DIM),
    ),
];

/// Active job states. Applied to the Job ID cell, not the state cell — the
/// active table has no state column, so the id carries the colour.
const ACTIVE_STATES: &[(&str, Style)] = &[
    ("RUNNING", Style::new().fg(Color::Green)),
    ("PENDING", Style::new().fg(Color::Yellow)),
    ("COMPLETING", Style::new().fg(Color::LightRed)),
    (
        "REQUEUED",
        Style::new().fg(Color::Yellow).add_modifier(Modifier::DIM),
    ),
    ("SUSPENDED", Style::new().add_modifier(Modifier::DIM)),
    (
        "PREEMPTED",
        Style::new().fg(Color::Yellow).add_modifier(Modifier::DIM),
    ),
];

/// Node states, for the node monitor.
const NODE_STATES: &[(&str, Style)] = &[
    ("idle", Style::new().fg(Color::Green)),
    ("mixed", Style::new().fg(Color::Yellow)),
    ("allocated", Style::new().fg(Color::LightRed)),
    ("completing", Style::new().fg(Color::Cyan)),
    ("reserved", Style::new().fg(Color::Blue)),
    (
        "drained",
        Style::new().fg(Color::Red).add_modifier(Modifier::DIM),
    ),
    (
        "draining",
        Style::new().fg(Color::Red).add_modifier(Modifier::DIM),
    ),
    (
        "down",
        Style::new().fg(Color::Red).add_modifier(Modifier::BOLD),
    ),
    (
        "fail",
        Style::new().fg(Color::Red).add_modifier(Modifier::BOLD),
    ),
    ("failing", Style::new().fg(Color::Red)),
    (
        "maint",
        Style::new().fg(Color::Magenta).add_modifier(Modifier::DIM),
    ),
    ("unknown", Style::new().add_modifier(Modifier::DIM)),
];

/// Short forms for state names, used when `abbreviate_states` is on.
const ABBREVIATIONS: &[(&str, &str)] = &[
    ("COMPLETED", "COMP"),
    ("FAILED", "FAIL"),
    ("TIMEOUT", "TIME"),
    ("CANCELLED", "CAN"),
    ("CANCELLED+", "CAN+"),
    ("OUT_OF_MEMORY", "OOM"),
    ("NODE_FAIL", "NFAIL"),
    ("PREEMPTED", "PREEMPT"),
    ("RUNNING", "RUN"),
    ("PENDING", "PEND"),
    ("COMPLETING", "CG"),
    ("SUSPENDED", "SUSP"),
    ("REQUEUED", "REQ"),
];

/// The interface's styles, plus whatever the user configured.
#[derive(Debug, Clone, Default)]
pub struct Theme {
    /// Explicit partition colours from the config file.
    partition_colors: BTreeMap<String, Color>,
    /// Show short state names.
    pub abbreviate_states: bool,
}

impl Theme {
    /// Build a theme from the configured partition colours.
    ///
    /// Unparseable colour names are dropped rather than rejected: a typo in one
    /// partition's colour should cost that colour, not the whole config.
    pub fn new(partition_colors: &BTreeMap<String, String>, abbreviate_states: bool) -> Self {
        Self {
            partition_colors: partition_colors
                .iter()
                .filter_map(|(name, color)| Some((name.clone(), parse_color(color)?)))
                .collect(),
            abbreviate_states,
        }
    }

    /// The colour for a partition name.
    ///
    /// Configured colours win; otherwise the name hashes to a stable entry in
    /// the palette, so a partition keeps its colour between sessions without
    /// anyone having to configure one.
    pub fn partition(&self, name: &str) -> Style {
        if name.is_empty() {
            return Style::new();
        }
        if let Some(color) = self.partition_colors.get(name) {
            return Style::new().fg(*color);
        }
        // Sum of the bytes, exactly as the Python sums ord() over characters.
        // The two agree for ASCII partition names, which is all Slurm allows.
        let hash: usize = name.bytes().map(usize::from).sum();
        Style::new().fg(PARTITION_PALETTE[hash % PARTITION_PALETTE.len()])
    }

    /// The style for a terminated job's state.
    pub fn terminated_state(&self, state: &str) -> Style {
        lookup(TERMINATED_STATES, base_state(state))
    }

    /// The style for an active job's state, applied to its id.
    pub fn active_state(&self, state: &str) -> Style {
        lookup(ACTIVE_STATES, base_state(state))
    }

    /// The style for a node's state.
    pub fn node_state(&self, state: &str) -> Style {
        lookup(NODE_STATES, state)
    }

    /// How a state should be written, honouring `abbreviate_states`.
    pub fn state_label<'a>(&self, state: &'a str) -> &'a str {
        if !self.abbreviate_states {
            return state;
        }
        ABBREVIATIONS
            .iter()
            .find(|(name, _)| *name == base_state(state))
            .map_or(state, |(_, short)| *short)
    }
}

/// The short form of a state name, or the state itself if it has none.
///
/// Used by array tallies regardless of `abbreviate_states`: a tally column has
/// no room for OUT_OF_MEMORY, and the count is the part that matters.
pub fn abbreviation(state: &str) -> &str {
    let base = base_state(state);
    ABBREVIATIONS
        .iter()
        .find(|(name, _)| *name == base)
        .map_or(base, |(_, short)| *short)
}

/// The state without Slurm's trailing detail: `CANCELLED by 1000` → `CANCELLED`.
fn base_state(state: &str) -> &str {
    state.split(' ').next().unwrap_or(state)
}

fn lookup(table: &[(&str, Style)], key: &str) -> Style {
    table
        .iter()
        .find(|(name, _)| *name == key)
        .map_or(Style::new(), |(_, style)| *style)
}

/// Parse a colour name from the config file.
///
/// Accepts the names the Python documents, plus `#rrggbb` and a bare ANSI index
/// for anyone who wants one of the 256.
pub fn parse_color(name: &str) -> Option<Color> {
    let name = name.trim().to_lowercase();
    Some(match name.as_str() {
        "black" => Color::Black,
        "red" => Color::Red,
        "green" => Color::Green,
        "yellow" => Color::Yellow,
        "blue" => Color::Blue,
        "magenta" => Color::Magenta,
        "cyan" => Color::Cyan,
        "white" | "grey" | "gray" => Color::Gray,
        "bright_black" | "dark_grey" | "dark_gray" => Color::DarkGray,
        "bright_red" => Color::LightRed,
        "bright_green" => Color::LightGreen,
        "bright_yellow" => Color::LightYellow,
        "bright_blue" => Color::LightBlue,
        "bright_magenta" => Color::LightMagenta,
        "bright_cyan" => Color::LightCyan,
        "bright_white" => Color::White,
        // Rich's name for what the terminal calls bright red.
        "dark_orange" => Color::LightRed,
        other => {
            if let Some(hex) = other.strip_prefix('#') {
                let value = u32::from_str_radix(hex, 16).ok()?;
                if hex.len() != 6 {
                    return None;
                }
                Color::Rgb(
                    ((value >> 16) & 0xff) as u8,
                    ((value >> 8) & 0xff) as u8,
                    (value & 0xff) as u8,
                )
            } else {
                Color::Indexed(other.parse().ok()?)
            }
        }
    })
}

// -- shared styles ----------------------------------------------------------

/// A panel's border when it does not have focus.
pub fn border() -> Style {
    Style::new().fg(Color::DarkGray)
}

/// A panel's border when it does.
pub fn border_focused() -> Style {
    Style::new().fg(Color::Cyan)
}

/// A panel's title.
pub fn title() -> Style {
    Style::new().fg(Color::Gray)
}

pub fn title_focused() -> Style {
    Style::new().fg(Color::Cyan).add_modifier(Modifier::BOLD)
}

/// A table's column headers.
pub fn header() -> Style {
    Style::new().fg(Color::DarkGray)
}

/// The row the cursor is on, in a focused table.
pub fn cursor() -> Style {
    Style::new().add_modifier(Modifier::REVERSED)
}

/// The row the cursor is on, in a table that does not have focus.
///
/// Still marked, so the user can see where they will land on returning — but
/// quietly, so it does not compete with the focused table's cursor.
pub fn cursor_unfocused() -> Style {
    Style::new().add_modifier(Modifier::DIM | Modifier::REVERSED)
}

/// Alternate rows, for zebra striping.
pub fn stripe() -> Style {
    Style::new().bg(Color::Indexed(235))
}

/// De-emphasised text.
pub fn dim() -> Style {
    Style::new().add_modifier(Modifier::DIM)
}

/// Emphasised text.
pub fn bold() -> Style {
    Style::new().add_modifier(Modifier::BOLD)
}

/// A count of running things.
pub fn running() -> Style {
    Style::new().fg(Color::Green)
}

/// A count of pending things.
pub fn pending() -> Style {
    Style::new().fg(Color::Yellow)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn default_theme() -> Theme {
        Theme::default()
    }

    #[test]
    fn a_partition_keeps_the_same_colour_across_calls() {
        let theme = default_theme();
        assert_eq!(theme.partition("gpu"), theme.partition("gpu"));
        // "gpu" is 103+112+117 = 332; 332 % 9 = 8.
        assert_eq!(
            theme.partition("gpu"),
            Style::new().fg(PARTITION_PALETTE[332 % 9])
        );
    }

    #[test]
    fn a_configured_colour_wins_over_the_hash() {
        let colors = BTreeMap::from([("gpu".to_string(), "magenta".to_string())]);
        let theme = Theme::new(&colors, false);
        assert_eq!(theme.partition("gpu"), Style::new().fg(Color::Magenta));
    }

    #[test]
    fn an_unparseable_colour_falls_back_to_the_hash() {
        // One bad colour must not cost the others.
        let colors = BTreeMap::from([
            ("gpu".to_string(), "not-a-colour".to_string()),
            ("cpu".to_string(), "green".to_string()),
        ]);
        let theme = Theme::new(&colors, false);
        assert_eq!(theme.partition("gpu"), default_theme().partition("gpu"));
        assert_eq!(theme.partition("cpu"), Style::new().fg(Color::Green));
    }

    #[test]
    fn an_empty_partition_has_no_style() {
        assert_eq!(default_theme().partition(""), Style::new());
    }

    #[test]
    fn states_carry_their_detail_but_style_on_the_base() {
        let theme = default_theme();
        // "CANCELLED by 1000" is styled as CANCELLED.
        assert_eq!(
            theme.terminated_state("CANCELLED by 1000"),
            theme.terminated_state("CANCELLED")
        );
    }

    #[test]
    fn an_unknown_state_has_no_style_rather_than_a_wrong_one() {
        assert_eq!(
            default_theme().terminated_state("SOME_FUTURE_STATE"),
            Style::new()
        );
    }

    #[test]
    fn abbreviations_apply_only_when_asked_for() {
        let plain = Theme::default();
        let short = Theme::new(&BTreeMap::new(), true);

        assert_eq!(plain.state_label("OUT_OF_MEMORY"), "OUT_OF_MEMORY");
        assert_eq!(short.state_label("OUT_OF_MEMORY"), "OOM");
        // An unknown state keeps its full name.
        assert_eq!(short.state_label("SOME_FUTURE_STATE"), "SOME_FUTURE_STATE");
    }

    #[test]
    fn parses_the_documented_colour_names() {
        assert_eq!(parse_color("green"), Some(Color::Green));
        assert_eq!(parse_color("  Bright_Cyan "), Some(Color::LightCyan));
        assert_eq!(parse_color("dark_orange"), Some(Color::LightRed));
        assert_eq!(parse_color("#ff8800"), Some(Color::Rgb(255, 136, 0)));
        assert_eq!(parse_color("42"), Some(Color::Indexed(42)));
        assert_eq!(parse_color("nonsense"), None);
        assert_eq!(parse_color("#ff88"), None);
    }
}
