//! Key bindings, and the help screen that documents them.
//!
//! Both come from the same tables. The Python keeps its help in a separate
//! module and has a test cross-checking it against the real `BINDINGS`, because
//! the two drifted apart twice. Deriving one from the other removes the
//! possibility rather than testing for it: a key that is not in the table does
//! nothing, and a key that is in it is documented.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::text::{Line, Span};

use super::theme;

/// Something a keystroke asks the app to do.
///
/// Deliberately a plain enum rather than a callback: it makes the binding tables
/// `const`, and lets key handling be tested by asserting on the action rather
/// than on its effect.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Quit,
    Refresh,
    Help,
    ToggleSearch,
    Bookmark,
    ToggleExpand,
    MoveUp,
    MoveDown,
    MoveTop,
    MoveBottom,
    FocusNextPanel,
    FocusPrevPanel,
    NextDetailTab,
    PrevDetailTab,
    NextMetaTab,
    PrevMetaTab,
    ScrollUp,
    ScrollDown,
}

/// One key combination.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Key {
    pub code: KeyCode,
    pub modifiers: KeyModifiers,
}

impl Key {
    const fn plain(code: KeyCode) -> Self {
        Self {
            code,
            modifiers: KeyModifiers::NONE,
        }
    }

    const fn ctrl(code: KeyCode) -> Self {
        Self {
            code,
            modifiers: KeyModifiers::CONTROL,
        }
    }

    /// Whether an event is this key.
    ///
    /// Shift is ignored for characters, because the character already carries
    /// it: `G` arrives as `Char('G')` with SHIFT set on some terminals and not
    /// others.
    fn matches(&self, event: &KeyEvent) -> bool {
        if self.code != event.code {
            return false;
        }
        if matches!(self.code, KeyCode::Char(_)) {
            return event.modifiers.difference(KeyModifiers::SHIFT) == self.modifiers;
        }
        event.modifiers == self.modifiers
    }
}

/// A documented binding.
pub struct Binding {
    /// How the key is written for a human: `Shift+C`, `[ / ]`.
    pub display: &'static str,
    pub description: &'static str,
    pub action: Action,
    pub keys: &'static [Key],
}

/// Which panel the user is in, which decides what `?` documents.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Context {
    Jobs,
    Detail,
    Metadata,
}

impl Context {
    pub fn title(self) -> &'static str {
        match self {
            Self::Jobs => "Job tables",
            Self::Detail => "Job Details",
            Self::Metadata => "Job Metadata",
        }
    }

    pub fn subtitle(self) -> &'static str {
        match self {
            Self::Jobs => "Active Jobs / Terminated Jobs",
            Self::Detail => "stdout · stderr · cpu · gpu · stats",
            Self::Metadata => "Resources · Submission · Pending · Raw",
        }
    }

    /// The bindings that only mean something inside this panel.
    pub fn bindings(self) -> &'static [Binding] {
        match self {
            Self::Jobs => JOBS,
            Self::Detail => DETAIL,
            Self::Metadata => METADATA,
        }
    }

    /// Notes that are not bindings but are worth knowing here.
    pub fn notes(self) -> &'static [&'static str] {
        match self {
            Self::Jobs => &[
                "/ opens the filter bar. Enter accepts the filter and returns to the",
                "list; Escape abandons it. Plain words match id, name and partition;",
                "key:value terms are ANDed —",
                "  state:pend  part:gpu  name:train  id:4815  gpu:>=2",
                "Aliases: st: s: · partition: p: · n: · job: · gpus: gres:. An unknown",
                "key is searched as plain text, so nothing you type can break the filter.",
            ],
            Self::Detail => &[
                "The log tabs show the last 500 lines of a job's output.",
                "cpu and gpu are live from the node and refresh while the tab is open.",
                "stats opens with Efficiency: used against requested, and a sizing hint.",
            ],
            Self::Metadata => &[
                "The Pending tab appears only while a job is waiting: why it is not",
                "running, when Slurm expects to start it, and its priority breakdown.",
            ],
        }
    }
}

/// Keys that work anywhere.
pub const GLOBAL: &[Binding] = &[
    Binding {
        display: "?",
        description: "this help — it follows the panel you are in",
        action: Action::Help,
        keys: &[Key::plain(KeyCode::Char('?'))],
    },
    Binding {
        display: "/",
        description: "filter the job tables",
        action: Action::ToggleSearch,
        keys: &[Key::plain(KeyCode::Char('/'))],
    },
    Binding {
        display: "r",
        description: "refresh now",
        action: Action::Refresh,
        keys: &[Key::plain(KeyCode::Char('r'))],
    },
    Binding {
        display: "Tab / Shift+Tab",
        description: "move between the panels",
        action: Action::FocusNextPanel,
        keys: &[Key::plain(KeyCode::Tab)],
    },
    Binding {
        display: "Shift+Tab",
        description: "move back a panel",
        action: Action::FocusPrevPanel,
        keys: &[Key::plain(KeyCode::BackTab)],
    },
    Binding {
        display: "q",
        description: "quit",
        action: Action::Quit,
        keys: &[
            Key::plain(KeyCode::Char('q')),
            Key::ctrl(KeyCode::Char('c')),
        ],
    },
];

/// Keys for the job tables.
pub const JOBS: &[Binding] = &[
    Binding {
        display: "Up / Down",
        description: "move through the list (wraps between the two tables)",
        action: Action::MoveUp,
        keys: &[Key::plain(KeyCode::Up), Key::plain(KeyCode::Char('k'))],
    },
    Binding {
        display: "j / k",
        description: "move down / up",
        action: Action::MoveDown,
        keys: &[Key::plain(KeyCode::Down), Key::plain(KeyCode::Char('j'))],
    },
    Binding {
        display: "g / G",
        description: "jump to the first / last row",
        action: Action::MoveTop,
        keys: &[Key::plain(KeyCode::Home), Key::plain(KeyCode::Char('g'))],
    },
    Binding {
        display: "G",
        description: "jump to the last row",
        action: Action::MoveBottom,
        keys: &[Key::plain(KeyCode::End), Key::plain(KeyCode::Char('G'))],
    },
    Binding {
        display: "Enter",
        description: "expand or collapse a job array (▸ row)",
        action: Action::ToggleExpand,
        keys: &[Key::plain(KeyCode::Enter)],
    },
    Binding {
        display: "m",
        description: "bookmark — ★ rows pin to the top",
        action: Action::Bookmark,
        keys: &[Key::plain(KeyCode::Char('m'))],
    },
];

/// Keys for the detail panel.
pub const DETAIL: &[Binding] = &[
    Binding {
        display: "[ / ]",
        description: "previous / next tab",
        action: Action::PrevDetailTab,
        keys: &[Key::plain(KeyCode::Char('['))],
    },
    Binding {
        display: "]",
        description: "next tab",
        action: Action::NextDetailTab,
        keys: &[Key::plain(KeyCode::Char(']'))],
    },
    Binding {
        display: "Up / Down",
        description: "scroll the log",
        action: Action::ScrollUp,
        keys: &[Key::plain(KeyCode::Up), Key::plain(KeyCode::Char('k'))],
    },
    Binding {
        display: "j / k",
        description: "scroll down / up",
        action: Action::ScrollDown,
        keys: &[Key::plain(KeyCode::Down), Key::plain(KeyCode::Char('j'))],
    },
];

/// Keys for the metadata panel.
pub const METADATA: &[Binding] = &[
    Binding {
        display: "( / )",
        description: "previous / next tab",
        action: Action::PrevMetaTab,
        keys: &[Key::plain(KeyCode::Char('('))],
    },
    Binding {
        display: ")",
        description: "next tab",
        action: Action::NextMetaTab,
        keys: &[Key::plain(KeyCode::Char(')'))],
    },
    Binding {
        display: "Up / Down",
        description: "scroll",
        action: Action::ScrollUp,
        keys: &[Key::plain(KeyCode::Up), Key::plain(KeyCode::Char('k'))],
    },
    Binding {
        display: "j / k",
        description: "scroll down / up",
        action: Action::ScrollDown,
        keys: &[Key::plain(KeyCode::Down), Key::plain(KeyCode::Char('j'))],
    },
];

/// The action a keystroke means in this context, if any.
///
/// The panel's own bindings win over the global ones, so a panel can repurpose a
/// key without the global table shadowing it.
pub fn lookup(context: Context, event: &KeyEvent) -> Option<Action> {
    let find = |bindings: &'static [Binding]| {
        bindings
            .iter()
            .find(|binding| binding.keys.iter().any(|key| key.matches(event)))
            .map(|binding| binding.action)
    };
    find(context.bindings()).or_else(|| find(GLOBAL))
}

/// The help text for one context: this panel first, then everything else.
pub fn help_lines(context: Context) -> Vec<Line<'static>> {
    const WIDTH: usize = 18;
    let mut lines = vec![
        Line::from(Span::styled(
            format!("LazySlurm — {}", context.title()),
            theme::title_focused(),
        )),
        Line::from(Span::styled(context.subtitle(), theme::dim())),
        Line::raw(""),
    ];

    for binding in context.bindings() {
        lines.push(binding_line(binding, WIDTH));
    }
    for note in context.notes() {
        lines.push(Line::from(Span::styled(format!("  {note}"), theme::dim())));
    }

    lines.push(Line::raw(""));
    lines.push(Line::from(Span::styled("Anywhere", theme::bold())));
    for binding in GLOBAL {
        lines.push(binding_line(binding, WIDTH));
    }

    lines.push(Line::raw(""));
    lines.push(Line::from(Span::styled(
        "Press ? or Escape to close.",
        theme::dim(),
    )));
    lines
}

fn binding_line(binding: &Binding, width: usize) -> Line<'static> {
    Line::from(vec![
        Span::styled(
            format!("  {:<width$}", binding.display),
            theme::title_focused(),
        ),
        Span::raw(binding.description),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    const CONTEXTS: [Context; 3] = [Context::Jobs, Context::Detail, Context::Metadata];

    fn press(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    #[test]
    fn every_binding_is_documented_and_reachable() {
        // The point of one table: a key that exists is described, and a key that
        // is described can be pressed.
        for bindings in CONTEXTS
            .iter()
            .map(|context| context.bindings())
            .chain(std::iter::once(GLOBAL))
        {
            for binding in bindings {
                assert!(!binding.display.is_empty(), "a binding has no display name");
                assert!(
                    !binding.description.is_empty(),
                    "{} has no description",
                    binding.display
                );
                assert!(
                    !binding.keys.is_empty(),
                    "{} is documented but bound to nothing",
                    binding.display
                );
            }
        }
    }

    #[test]
    fn no_key_is_bound_twice_in_the_same_context() {
        for context in CONTEXTS {
            let mut seen: Vec<Key> = Vec::new();
            for binding in context.bindings() {
                for key in binding.keys {
                    assert!(
                        !seen.contains(key),
                        "{:?} is bound twice in {context:?}",
                        key.code
                    );
                    seen.push(*key);
                }
            }
        }
    }

    #[test]
    fn every_declared_key_resolves_to_its_action() {
        for context in CONTEXTS {
            for binding in context.bindings() {
                for key in binding.keys {
                    let event = KeyEvent::new(key.code, key.modifiers);
                    assert_eq!(
                        lookup(context, &event),
                        Some(binding.action),
                        "{:?} in {context:?}",
                        key.code
                    );
                }
            }
        }
    }

    #[test]
    fn global_keys_work_in_every_context() {
        for context in CONTEXTS {
            assert_eq!(
                lookup(context, &press(KeyCode::Char('q'))),
                Some(Action::Quit)
            );
            assert_eq!(
                lookup(context, &press(KeyCode::Char('r'))),
                Some(Action::Refresh)
            );
            assert_eq!(
                lookup(context, &press(KeyCode::Char('?'))),
                Some(Action::Help)
            );
        }
    }

    #[test]
    fn a_panel_binding_beats_the_global_one() {
        // Up scrolls in the detail panel but moves the cursor in the job tables.
        assert_eq!(
            lookup(Context::Jobs, &press(KeyCode::Up)),
            Some(Action::MoveUp)
        );
        assert_eq!(
            lookup(Context::Detail, &press(KeyCode::Up)),
            Some(Action::ScrollUp)
        );
    }

    #[test]
    fn an_unbound_key_means_nothing() {
        assert_eq!(lookup(Context::Jobs, &press(KeyCode::Char('~'))), None);
    }

    #[test]
    fn ctrl_c_quits_but_a_plain_c_does_not() {
        let ctrl_c = KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL);
        assert_eq!(lookup(Context::Jobs, &ctrl_c), Some(Action::Quit));
        assert_eq!(lookup(Context::Jobs, &press(KeyCode::Char('c'))), None);
    }

    #[test]
    fn shift_on_a_character_is_ignored() {
        // Terminals disagree about whether `G` carries SHIFT.
        let bare = press(KeyCode::Char('G'));
        let shifted = KeyEvent::new(KeyCode::Char('G'), KeyModifiers::SHIFT);
        assert_eq!(lookup(Context::Jobs, &bare), Some(Action::MoveBottom));
        assert_eq!(lookup(Context::Jobs, &shifted), Some(Action::MoveBottom));
    }

    #[test]
    fn the_help_screen_documents_the_panel_and_the_global_keys() {
        let text: String = help_lines(Context::Jobs)
            .iter()
            .map(|line| {
                line.spans
                    .iter()
                    .map(|span| span.content.to_string())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n");

        assert!(text.contains("Job tables"), "{text}");
        assert!(text.contains("bookmark"), "{text}");
        assert!(text.contains("Anywhere"), "{text}");
        assert!(text.contains("quit"), "{text}");
        // The filter syntax is documented where the filter is used.
        assert!(text.contains("state:pend"), "{text}");
        // `[ / ]` is a key, not markup — it survives verbatim.
        assert!(help_text(Context::Detail).contains("[ / ]"));
    }

    fn help_text(context: Context) -> String {
        help_lines(context)
            .iter()
            .map(|line| {
                line.spans
                    .iter()
                    .map(|span| span.content.to_string())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }
}
