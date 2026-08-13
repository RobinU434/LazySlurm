//! Dialogs: confirmations and the job editor.
//!
//! A modal owns its own keys and returns what the user decided; it never acts.
//! That keeps the destructive paths — cancelling jobs, editing an allocation —
//! testable by pressing keys at a struct and asserting on the request that comes
//! out, with nothing that could reach a cluster.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::text::{Line, Span};

use crate::slurm::EDITABLE_FIELDS;

use super::theme;

/// What a modal decided, once the user is finished with it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// Still open.
    Continue,
    /// Closed with nothing to do.
    Dismissed,
    /// Closed, and this should happen.
    Accepted(Request),
}

/// Something the app should ask Slurm to do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Request {
    Cancel {
        ids: Vec<String>,
        force: bool,
    },
    Update {
        ids: Vec<String>,
        updates: Vec<(String, String)>,
    },
    Resubmit {
        job_id: String,
        command: String,
        work_dir: String,
    },
}

/// An open dialog.
pub enum Modal {
    ConfirmCancel {
        ids: Vec<String>,
    },
    ConfirmResubmit {
        job_id: String,
        command: String,
        work_dir: String,
    },
    Edit(EditModal),
}

impl Modal {
    /// Handle one keystroke.
    pub fn handle_key(&mut self, key: KeyEvent) -> Outcome {
        match self {
            Self::ConfirmCancel { ids } => match key.code {
                KeyCode::Char('y') | KeyCode::Char('Y') => Outcome::Accepted(Request::Cancel {
                    ids: std::mem::take(ids),
                    force: false,
                }),
                KeyCode::Char('n') | KeyCode::Char('N') | KeyCode::Esc => Outcome::Dismissed,
                _ => Outcome::Continue,
            },
            Self::ConfirmResubmit {
                job_id,
                command,
                work_dir,
            } => match key.code {
                KeyCode::Char('y') | KeyCode::Char('Y') => Outcome::Accepted(Request::Resubmit {
                    job_id: std::mem::take(job_id),
                    command: std::mem::take(command),
                    work_dir: std::mem::take(work_dir),
                }),
                KeyCode::Char('n') | KeyCode::Char('N') | KeyCode::Esc => Outcome::Dismissed,
                _ => Outcome::Continue,
            },
            Self::Edit(edit) => edit.handle_key(key),
        }
    }

    /// The dialog's title and body.
    pub fn view(&self) -> (String, Vec<Line<'static>>) {
        match self {
            Self::ConfirmCancel { ids } => {
                let title = if ids.len() == 1 {
                    format!(" Cancel job {} ", ids[0])
                } else {
                    format!(" Cancel {} jobs ", ids.len())
                };

                let mut lines = Vec::new();
                if ids.len() > 1 {
                    let preview = ids.iter().take(5).cloned().collect::<Vec<_>>().join(", ");
                    let suffix = if ids.len() > 5 {
                        format!(", … ({} total)", ids.len())
                    } else {
                        String::new()
                    };
                    lines.push(Line::from(Span::styled(
                        format!("{preview}{suffix}"),
                        theme::dim(),
                    )));
                    lines.push(Line::raw(""));
                }
                lines.push(confirm_prompt());
                (title, lines)
            }
            Self::ConfirmResubmit {
                job_id, command, ..
            } => (
                format!(" Resubmit job {job_id} "),
                vec![
                    Line::from(vec![
                        Span::raw("Script: "),
                        Span::styled(command.clone(), theme::title_focused()),
                    ]),
                    Line::raw(""),
                    confirm_prompt(),
                ],
            ),
            Self::Edit(edit) => edit.view(),
        }
    }
}

fn confirm_prompt() -> Line<'static> {
    Line::from(vec![
        Span::raw("Press "),
        Span::styled("y", theme::bold()),
        Span::raw(" to confirm, "),
        Span::styled("n", theme::bold()),
        Span::raw(" or "),
        Span::styled("Escape", theme::bold()),
        Span::raw(" to abort."),
    ])
}

/// The job editor: one line per editable property.
///
/// Laid out like a text editor because that is what it is — line numbers down
/// the side, `^S` to write, `Esc` to quit.
pub struct EditModal {
    ids: Vec<String>,
    /// The current value of each field, indexed alongside `EDITABLE_FIELDS`.
    values: Vec<String>,
    /// What each field held when the editor opened, so "unchanged" can be told
    /// from "typed the same thing".
    original: Vec<String>,
    focused: usize,
}

impl EditModal {
    /// Open the editor for some jobs.
    ///
    /// `current` prefills the fields when exactly one job is being edited; for
    /// several it stays empty, and every non-empty field is applied to all.
    pub fn new(ids: Vec<String>, current: Option<Vec<String>>) -> Self {
        let values = current.unwrap_or_else(|| vec![String::new(); EDITABLE_FIELDS.len()]);
        Self {
            ids,
            original: values.clone(),
            values,
            focused: 0,
        }
    }

    pub fn ids(&self) -> &[String] {
        &self.ids
    }

    pub fn focused_field(&self) -> usize {
        self.focused
    }

    pub fn values(&self) -> &[String] {
        &self.values
    }

    fn handle_key(&mut self, key: KeyEvent) -> Outcome {
        match (key.code, key.modifiers) {
            (KeyCode::Char('s'), KeyModifiers::CONTROL) => self.submit(),
            (KeyCode::Enter, _) => self.submit(),
            (KeyCode::Esc, _) => Outcome::Dismissed,
            (KeyCode::Up, _) | (KeyCode::BackTab, _) => {
                self.move_focus(-1);
                Outcome::Continue
            }
            (KeyCode::Down, _) | (KeyCode::Tab, _) => {
                self.move_focus(1);
                Outcome::Continue
            }
            (KeyCode::Backspace, _) => {
                self.values[self.focused].pop();
                Outcome::Continue
            }
            (KeyCode::Char(character), _) => {
                self.values[self.focused].push(character);
                Outcome::Continue
            }
            _ => Outcome::Continue,
        }
    }

    fn move_focus(&mut self, delta: isize) {
        let count = EDITABLE_FIELDS.len() as isize;
        self.focused = (((self.focused as isize + delta) % count + count) % count) as usize;
    }

    /// Collect the fields that actually changed.
    ///
    /// A blank field means "leave unchanged", and a field retyped to its
    /// existing value is not a change either — neither should reach `scontrol`.
    fn submit(&mut self) -> Outcome {
        let updates: Vec<(String, String)> = EDITABLE_FIELDS
            .iter()
            .enumerate()
            .filter_map(|(index, field)| {
                let value = self.values[index].trim();
                if value.is_empty() || value == self.original[index].trim() {
                    return None;
                }
                Some((field.key.to_string(), value.to_string()))
            })
            .collect();

        if updates.is_empty() {
            return Outcome::Dismissed;
        }
        Outcome::Accepted(Request::Update {
            ids: std::mem::take(&mut self.ids),
            updates,
        })
    }

    fn view(&self) -> (String, Vec<Line<'static>>) {
        let title = if self.ids.len() == 1 {
            format!(" job.{} ", self.ids[0])
        } else {
            format!(" job.{}-selected ", self.ids.len())
        };

        let mut lines: Vec<Line> = EDITABLE_FIELDS
            .iter()
            .enumerate()
            .map(|(index, field)| {
                let marker = if index == self.focused { "▌" } else { " " };
                Line::from(vec![
                    Span::styled(format!("{:>2} ", index + 1), theme::dim()),
                    Span::styled(marker, theme::title_focused()),
                    Span::styled(format!("{:<14}", field.scontrol_key), theme::dim()),
                    Span::raw(self.values[index].clone()),
                    // A block cursor, so the focused line is unmistakable.
                    Span::styled(if index == self.focused { "█" } else { "" }, theme::dim()),
                ])
            })
            .collect();

        lines.push(Line::raw(""));
        lines.push(Line::from(Span::styled(
            "^S write   esc quit   ↑↓ move",
            theme::dim(),
        )));
        (title, lines)
    }
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

    fn ctrl(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::CONTROL)
    }

    /// The index of a field in `EDITABLE_FIELDS`.
    fn field_index(key: &str) -> usize {
        EDITABLE_FIELDS
            .iter()
            .position(|field| field.key == key)
            .expect("field exists")
    }

    #[test]
    fn confirming_a_cancel_returns_the_ids() {
        let mut modal = Modal::ConfirmCancel {
            ids: vec!["100".into(), "101".into()],
        };
        assert_eq!(
            modal.handle_key(key(KeyCode::Char('y'))),
            Outcome::Accepted(Request::Cancel {
                ids: vec!["100".into(), "101".into()],
                force: false,
            })
        );
    }

    #[test]
    fn a_cancel_can_be_refused_two_ways() {
        for code in [KeyCode::Char('n'), KeyCode::Esc] {
            let mut modal = Modal::ConfirmCancel {
                ids: vec!["100".into()],
            };
            assert_eq!(modal.handle_key(key(code)), Outcome::Dismissed);
        }
    }

    #[test]
    fn an_unrelated_key_leaves_a_confirmation_open() {
        let mut modal = Modal::ConfirmCancel {
            ids: vec!["100".into()],
        };
        assert_eq!(modal.handle_key(key(KeyCode::Char('x'))), Outcome::Continue);
    }

    #[test]
    fn a_multi_job_cancel_previews_what_it_will_take() {
        let modal = Modal::ConfirmCancel {
            ids: (0..8).map(|index| index.to_string()).collect(),
        };
        let (title, lines) = modal.view();
        let body: String = lines
            .iter()
            .flat_map(|line| line.spans.iter())
            .map(|span| span.content.to_string())
            .collect();

        assert!(title.contains("Cancel 8 jobs"), "{title}");
        assert!(body.contains("… (8 total)"), "{body}");
    }

    #[test]
    fn confirming_a_resubmit_returns_its_command() {
        let mut modal = Modal::ConfirmResubmit {
            job_id: "123".into(),
            command: "sbatch --array=1-4 job.sh".into(),
            work_dir: "/work".into(),
        };
        assert_eq!(
            modal.handle_key(key(KeyCode::Char('y'))),
            Outcome::Accepted(Request::Resubmit {
                job_id: "123".into(),
                command: "sbatch --array=1-4 job.sh".into(),
                work_dir: "/work".into(),
            })
        );
    }

    #[test]
    fn the_editor_returns_only_what_changed() {
        let mut current = vec![String::new(); EDITABLE_FIELDS.len()];
        current[field_index("time_limit")] = "1:00:00".into();
        current[field_index("partition")] = "gpu".into();

        let mut modal = Modal::Edit(EditModal::new(vec!["123".into()], Some(current)));

        // Retype the runtime, and leave the partition alone.
        for _ in 0..7 {
            modal.handle_key(key(KeyCode::Backspace));
        }
        for stroke in typed("4:00:00") {
            modal.handle_key(stroke);
        }

        assert_eq!(
            modal.handle_key(ctrl(KeyCode::Char('s'))),
            Outcome::Accepted(Request::Update {
                ids: vec!["123".into()],
                updates: vec![("time_limit".into(), "4:00:00".into())],
            })
        );
    }

    #[test]
    fn a_field_retyped_to_its_existing_value_is_not_a_change() {
        let mut current = vec![String::new(); EDITABLE_FIELDS.len()];
        current[field_index("time_limit")] = "1:00:00".into();

        let mut modal = Modal::Edit(EditModal::new(vec!["123".into()], Some(current)));
        // Delete and retype the same thing.
        for _ in 0..7 {
            modal.handle_key(key(KeyCode::Backspace));
        }
        for stroke in typed("1:00:00") {
            modal.handle_key(stroke);
        }

        assert_eq!(
            modal.handle_key(ctrl(KeyCode::Char('s'))),
            Outcome::Dismissed
        );
    }

    #[test]
    fn submitting_an_untouched_editor_does_nothing() {
        let mut modal = Modal::Edit(EditModal::new(vec!["123".into()], None));
        assert_eq!(
            modal.handle_key(ctrl(KeyCode::Char('s'))),
            Outcome::Dismissed
        );
    }

    #[test]
    fn escape_abandons_the_editor() {
        let mut modal = Modal::Edit(EditModal::new(vec!["123".into()], None));
        for stroke in typed("4:00:00") {
            modal.handle_key(stroke);
        }
        assert_eq!(modal.handle_key(key(KeyCode::Esc)), Outcome::Dismissed);
    }

    #[test]
    fn the_editor_moves_between_fields_and_wraps() {
        let mut edit = EditModal::new(vec!["123".into()], None);
        assert_eq!(edit.focused_field(), 0);

        edit.handle_key(key(KeyCode::Down));
        assert_eq!(edit.focused_field(), 1);

        edit.handle_key(key(KeyCode::Up));
        edit.handle_key(key(KeyCode::Up));
        assert_eq!(
            edit.focused_field(),
            EDITABLE_FIELDS.len() - 1,
            "should wrap past the top"
        );
    }

    #[test]
    fn typing_goes_to_the_focused_field_only() {
        let mut edit = EditModal::new(vec!["123".into()], None);
        edit.handle_key(key(KeyCode::Down));
        for stroke in typed("gpu") {
            edit.handle_key(stroke);
        }

        assert_eq!(edit.values()[0], "");
        assert_eq!(edit.values()[1], "gpu");
    }

    #[test]
    fn a_multi_job_edit_starts_blank_and_says_how_many() {
        let edit = EditModal::new(vec!["1".into(), "2".into(), "3".into()], None);
        let (title, _) = edit.view();

        assert!(title.contains("3-selected"), "{title}");
        assert!(edit.values().iter().all(String::is_empty));
    }

    #[test]
    fn enter_submits_as_well_as_ctrl_s() {
        let mut modal = Modal::Edit(EditModal::new(vec!["123".into()], None));
        for stroke in typed("8:00:00") {
            modal.handle_key(stroke);
        }
        assert!(matches!(
            modal.handle_key(key(KeyCode::Enter)),
            Outcome::Accepted(_)
        ));
    }
}
