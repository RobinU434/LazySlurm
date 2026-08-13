//! The state behind a job table.
//!
//! Deliberately free of any terminal or rendering type: filtering, array
//! grouping, expansion, bookmarks, multi-select and cursor movement are all
//! decided here and asserted directly in tests. The renderer's only job is to
//! turn [`Row`]s into cells.
//!
//! The behaviour that is easy to lose, and therefore has tests of its own:
//!
//! - the cursor stays on the same job across a refresh, rather than on the same
//!   row index — otherwise the table jumps under the user every poll;
//! - an array the user expanded stays expanded across a refresh;
//! - a collapsed array resolves to a real task id, because its base id is not
//!   something Slurm can describe on its own.

use std::cmp::Reverse;
use std::collections::BTreeSet;

use crate::model::{array_index_span, array_task_count, base_job_id};

use super::filter::{matches, parse_query, Filterable, Term};

/// The row key used for the "nothing matched" line. Never a real job id.
pub const PLACEHOLDER_KEY: &str = "__no_match__";

/// A job as a table needs it: filterable, plus the columns tables share.
pub trait JobRow: Filterable {
    fn elapsed(&self) -> &str;
}

impl JobRow for crate::model::RunningJob {
    fn elapsed(&self) -> &str {
        &self.elapsed
    }
}

impl JobRow for crate::model::CompletedJob {
    fn elapsed(&self) -> &str {
        &self.elapsed
    }
}

/// Where a row sits in an expanded array.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Depth {
    /// An ordinary row, or a group header.
    Top,
    /// A member of an expanded array; `last` picks the tree glyph.
    Child { last: bool },
}

impl Depth {
    /// The tree prefix this row's name cell carries.
    pub fn indent(self) -> &'static str {
        match self {
            Self::Top => "",
            Self::Child { last: false } => "├ ",
            Self::Child { last: true } => "└ ",
        }
    }
}

/// One row as displayed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Row {
    /// A single job, by index into the table's job list.
    Job { index: usize, depth: Depth },
    /// A job array folded into one row.
    Group {
        base: String,
        members: Vec<usize>,
        expanded: bool,
    },
    /// Shown when a filter excludes everything, so an empty table does not read
    /// as a bug.
    Placeholder,
}

/// A job table's state.
pub struct JobTable<T> {
    all: Vec<T>,
    rows: Vec<Row>,
    selected: usize,
    terms: Vec<Term>,
    query: String,
    matched: usize,
    bookmarked: BTreeSet<String>,
    multiselected: BTreeSet<String>,
    /// Base ids the user has opened. Survives polls, so a refresh never folds a
    /// group back up.
    expanded: BTreeSet<String>,
    /// The key the cursor is on, tracked separately from its index.
    ///
    /// Held as state rather than read off the rows at rebuild time, because a
    /// rebuild usually follows a change to the job list — at which point the
    /// existing rows index into a list that no longer matches them.
    anchor: Option<String>,
    collapse_arrays: bool,
    focused: bool,
}

impl<T: JobRow> JobTable<T> {
    pub fn new(collapse_arrays: bool) -> Self {
        Self {
            all: Vec::new(),
            rows: Vec::new(),
            selected: 0,
            terms: Vec::new(),
            query: String::new(),
            matched: 0,
            bookmarked: BTreeSet::new(),
            multiselected: BTreeSet::new(),
            expanded: BTreeSet::new(),
            anchor: None,
            collapse_arrays,
            focused: false,
        }
    }

    // -- inputs -------------------------------------------------------------

    /// Replace the job list, as a poll does.
    pub fn set_jobs(&mut self, jobs: Vec<T>) {
        self.all = jobs;
        self.rebuild();
    }

    /// Apply a filter query.
    pub fn set_filter(&mut self, query: &str) {
        self.query = query.trim().to_string();
        self.terms = parse_query(&self.query);
        self.rebuild();
    }

    pub fn set_collapse_arrays(&mut self, collapse: bool) {
        self.collapse_arrays = collapse;
        self.rebuild();
    }

    pub fn set_bookmarks(&mut self, ids: BTreeSet<String>) {
        self.bookmarked = ids;
        self.rebuild();
    }

    pub fn set_multiselected(&mut self, ids: BTreeSet<String>) {
        self.multiselected = ids;
        self.rebuild();
    }

    pub fn set_focused(&mut self, focused: bool) {
        self.focused = focused;
    }

    // -- reading ------------------------------------------------------------

    pub fn rows(&self) -> &[Row] {
        &self.rows
    }

    pub fn jobs(&self) -> &[T] {
        &self.all
    }

    pub fn is_focused(&self) -> bool {
        self.focused
    }

    pub fn row_count(&self) -> usize {
        self.rows.len()
    }

    pub fn selected_index(&self) -> usize {
        self.selected
    }

    pub fn has_filter(&self) -> bool {
        !self.terms.is_empty()
    }

    pub fn query(&self) -> &str {
        &self.query
    }

    /// The job behind a `Row::Job`.
    pub fn job(&self, index: usize) -> &T {
        &self.all[index]
    }

    /// The key of a row: a job id, an array's base id, or the placeholder.
    ///
    /// The key can be borrowed from either the table or the row itself, hence
    /// the shared lifetime.
    pub fn row_key<'a>(&'a self, row: &'a Row) -> &'a str {
        match row {
            Row::Job { index, .. } => self.all[*index].job_id(),
            Row::Group { base, .. } => base,
            Row::Placeholder => PLACEHOLDER_KEY,
        }
    }

    /// Every row key in display order, excluding the placeholder.
    ///
    /// This is the order multi-select extends over, so it must match what the
    /// user sees rather than the underlying job list.
    pub fn row_keys(&self) -> Vec<String> {
        self.rows
            .iter()
            .map(|row| self.row_key(row))
            .filter(|key| *key != PLACEHOLDER_KEY)
            .map(str::to_string)
            .collect()
    }

    /// The selected row's key, unless it is the placeholder.
    pub fn selected_key(&self) -> Option<&str> {
        let key = self.rows.get(self.selected).map(|row| self.row_key(row))?;
        (key != PLACEHOLDER_KEY).then_some(key)
    }

    /// The job id the detail panels should show.
    ///
    /// For a collapsed array that is its first task — the group's own key is a
    /// base id, which is not a job Slurm can describe on its own.
    pub fn selected_job_id(&self) -> Option<&str> {
        match self.rows.get(self.selected)? {
            Row::Job { index, .. } => Some(self.all[*index].job_id()),
            Row::Group { members, .. } => members.first().map(|i| self.all[*i].job_id()),
            Row::Placeholder => None,
        }
    }

    /// `(base id, member indices)` when a collapsed array row is selected.
    pub fn selected_group(&self) -> Option<(&str, &[usize])> {
        match self.rows.get(self.selected)? {
            Row::Group { base, members, .. } => Some((base, members)),
            _ => None,
        }
    }

    /// Replace any array row keys with the ids of their members.
    pub fn expand_ids<'a>(&self, ids: impl IntoIterator<Item = &'a str>) -> Vec<String> {
        let mut out = Vec::new();
        for id in ids {
            match self.group_members(id) {
                Some(members) => {
                    out.extend(members.iter().map(|i| self.all[*i].job_id().to_string()))
                }
                None => out.push(id.to_string()),
            }
        }
        out
    }

    /// The member indices of an array row, if `key` names one.
    fn group_members(&self, key: &str) -> Option<&[usize]> {
        self.rows.iter().find_map(|row| match row {
            Row::Group { base, members, .. } if base == key => Some(members.as_slice()),
            _ => None,
        })
    }

    /// The border title, carrying a match count while a filter is active.
    pub fn title(&self, base: &str) -> String {
        if self.terms.is_empty() {
            base.to_string()
        } else {
            format!("{base} — {}/{} match", self.matched, self.all.len())
        }
    }

    /// Whether a row carries a bookmark or multi-select marker.
    pub fn markers(&self, key: &str) -> String {
        let mut markers = String::new();
        if self.multiselected.contains(key) {
            markers.push_str("◉ ");
        }
        if self.bookmarked.contains(key) {
            markers.push_str("★ ");
        }
        markers
    }

    /// `▸ 123_[0-11] ×12` — the collapsed row's Job ID cell.
    pub fn group_label(&self, base: &str, members: &[usize], expanded: bool) -> String {
        let tasks: u32 = members
            .iter()
            .map(|i| array_task_count(self.all[*i].job_id()))
            .sum();
        let span = array_index_span(members.iter().map(|i| self.all[*i].job_id()))
            .map(|(low, high)| format!("[{low}-{high}]"))
            .unwrap_or_else(|| "[]".to_string());
        let arrow = if expanded { "▾" } else { "▸" };
        format!("{arrow} {base}_{span} ×{tasks}")
    }

    /// State counts across an array's members, largest first.
    ///
    /// Counts array *tasks*, not rows: one pending `123_[3-11]` row stands for
    /// nine jobs, and the tally says so.
    pub fn state_tally(&self, members: &[usize]) -> Vec<(String, u32)> {
        let mut counts: Vec<(String, u32)> = Vec::new();
        for index in members {
            let job = &self.all[*index];
            // "CANCELLED by 1000" tallies as CANCELLED.
            let state = job
                .state()
                .split(' ')
                .next()
                .unwrap_or_default()
                .to_string();
            let tasks = array_task_count(job.job_id());
            match counts.iter_mut().find(|(name, _)| *name == state) {
                Some((_, count)) => *count += tasks,
                None => counts.push((state, tasks)),
            }
        }
        counts.sort_by_key(|(_, count)| Reverse(*count));
        counts
    }

    /// The longest elapsed time among an array's members.
    pub fn longest_elapsed(&self, members: &[usize]) -> &str {
        members
            .iter()
            .map(|i| self.all[*i].elapsed())
            .max_by_key(|text| crate::model::elapsed_seconds(text))
            .unwrap_or_default()
    }

    // -- cursor -------------------------------------------------------------

    /// Move the cursor by `delta` rows, clamped to the table.
    ///
    /// Returns false when the cursor was already at that end, which is the
    /// signal the app uses to hand focus to the other table.
    pub fn move_cursor(&mut self, delta: isize) -> bool {
        if self.rows.is_empty() {
            return false;
        }
        let target = self.selected as isize + delta;
        if target < 0 || target >= self.rows.len() as isize {
            return false;
        }
        self.selected = target as usize;
        self.sync_anchor();
        true
    }

    /// Put the cursor on a row index, clamped.
    pub fn select_index(&mut self, index: usize) {
        self.selected = index.min(self.rows.len().saturating_sub(1));
        self.sync_anchor();
    }

    /// Put the cursor on the first or last row.
    pub fn select_edge(&mut self, last: bool) {
        self.selected = if last {
            self.rows.len().saturating_sub(1)
        } else {
            0
        };
        self.sync_anchor();
    }

    /// Put the cursor on the row with this key, if it is present.
    pub fn select_key(&mut self, key: &str) -> bool {
        match self.rows.iter().position(|row| self.row_key(row) == key) {
            Some(index) => {
                self.selected = index;
                self.sync_anchor();
                true
            }
            None => false,
        }
    }

    /// Record which key the cursor now sits on, so a rebuild can find it again.
    fn sync_anchor(&mut self) {
        self.anchor = self.selected_key().map(str::to_string);
    }

    // -- expansion ----------------------------------------------------------

    /// Expand or collapse an array row. `None` targets the selected row.
    ///
    /// Returns whether anything changed, so the caller can fall through to
    /// another handler when the cursor is not on an array.
    pub fn toggle_expand(&mut self, base: Option<&str>) -> bool {
        let key = match base {
            Some(key) => key.to_string(),
            None => match self.rows.get(self.selected) {
                Some(Row::Group { base, .. }) => base.clone(),
                _ => return false,
            },
        };
        if self.group_members(&key).is_none() {
            return false;
        }

        if !self.expanded.remove(&key) {
            self.expanded.insert(key);
        }
        self.rebuild();
        true
    }

    // -- rebuild ------------------------------------------------------------

    /// Recompute the displayed rows from the job list.
    fn rebuild(&mut self) {
        // The cursor follows the job, not the row index: without this the table
        // jumps under the user whenever a poll changes the row count.
        let anchor = self.anchor.clone();

        let filtered: Vec<usize> = (0..self.all.len())
            .filter(|i| self.terms.is_empty() || matches(&self.all[*i], &self.terms))
            .collect();
        self.matched = filtered.len();

        let groups = self.group(&filtered);
        // Forget the expansion state of arrays that have finished.
        let live: BTreeSet<String> = groups
            .iter()
            .filter(|(_, members)| members.len() > 1)
            .map(|(base, _)| base.clone())
            .collect();
        self.expanded.retain(|base| live.contains(base));

        let ordered = self.pin_bookmarked(groups);

        self.rows = Vec::new();
        for (base, members) in ordered {
            // A single row is an ordinary job — including a pending array that
            // squeue already reports as one `123_[12-40]` row.
            if members.len() == 1 {
                self.rows.push(Row::Job {
                    index: members[0],
                    depth: Depth::Top,
                });
                continue;
            }

            let expanded = self.expanded.contains(&base);
            let count = members.len();
            self.rows.push(Row::Group {
                base,
                members: members.clone(),
                expanded,
            });
            if expanded {
                for (position, index) in members.into_iter().enumerate() {
                    self.rows.push(Row::Job {
                        index,
                        depth: Depth::Child {
                            last: position == count - 1,
                        },
                    });
                }
            }
        }

        if self.rows.is_empty() && !self.terms.is_empty() {
            self.rows.push(Row::Placeholder);
        }

        self.restore_cursor(anchor);
    }

    /// Group array tasks by base id, keeping the incoming order.
    fn group(&self, filtered: &[usize]) -> Vec<(String, Vec<usize>)> {
        if !self.collapse_arrays {
            return filtered
                .iter()
                .map(|i| (self.all[*i].job_id().to_string(), vec![*i]))
                .collect();
        }

        let mut groups: Vec<(String, Vec<usize>)> = Vec::new();
        for index in filtered {
            let job_id = self.all[*index].job_id();
            let base = base_job_id(job_id).unwrap_or(job_id).to_string();
            match groups.iter_mut().find(|(name, _)| *name == base) {
                Some((_, members)) => members.push(*index),
                None => groups.push((base, vec![*index])),
            }
        }
        groups
    }

    /// Move bookmarked groups to the top, stably.
    fn pin_bookmarked(&self, groups: Vec<(String, Vec<usize>)>) -> Vec<(String, Vec<usize>)> {
        if self.bookmarked.is_empty() {
            return groups;
        }
        let pinned = |base: &str, members: &[usize]| {
            self.bookmarked.contains(base)
                || members
                    .iter()
                    .any(|i| self.bookmarked.contains(self.all[*i].job_id()))
        };
        let (top, rest): (Vec<_>, Vec<_>) = groups
            .into_iter()
            .partition(|(base, members)| pinned(base, members));
        top.into_iter().chain(rest).collect()
    }

    /// Put the cursor back on the row it was on, or as close as possible.
    fn restore_cursor(&mut self, anchor: Option<String>) {
        if let Some(anchor) = anchor {
            if self.select_key(&anchor) {
                return;
            }
            // The job is gone — but if it was an array task whose group has
            // since collapsed, following it to the group keeps the cursor in
            // the same place on screen.
            if let Some(base) = base_job_id(&anchor) {
                if self.select_key(base) {
                    return;
                }
            }
        }
        self.selected = self.selected.min(self.rows.len().saturating_sub(1));
        self.sync_anchor();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{CompletedJob, RunningJob};

    fn running(id: &str, name: &str, elapsed: &str, partition: &str, state: &str) -> RunningJob {
        RunningJob {
            job_id: id.into(),
            name: name.into(),
            elapsed: elapsed.into(),
            partition: partition.into(),
            state: state.into(),
            ..RunningJob::default()
        }
    }

    /// The fixture from the Python array-collapse tests.
    fn fixture() -> Vec<RunningJob> {
        vec![
            running("4815300", "solo-job", "0:42", "cpu", "RUNNING"),
            running("4815201_0", "sweep-lr", "1:12:04", "gpu", "RUNNING"),
            running("4815201_1", "sweep-lr", "1:11:58", "gpu", "RUNNING"),
            running("4815201_2", "sweep-lr", "0:00", "gpu", "PENDING"),
            running("4815201_[3-11]", "sweep-lr", "0:00", "gpu", "PENDING"),
            running("4815100", "other", "3:00", "cpu", "RUNNING"),
        ]
    }

    fn table() -> JobTable<RunningJob> {
        let mut table = JobTable::new(true);
        table.set_jobs(fixture());
        table
    }

    /// The Job ID cell of the row with this key.
    fn label(table: &JobTable<RunningJob>, key: &str) -> String {
        let row = table
            .rows()
            .iter()
            .find(|row| table.row_key(row) == key)
            .expect("row is present");
        match row {
            Row::Group {
                base,
                members,
                expanded,
            } => table.group_label(base, members, *expanded),
            Row::Job { index, .. } => table.job(*index).job_id.clone(),
            Row::Placeholder => "no jobs match".into(),
        }
    }

    #[test]
    fn an_array_occupies_one_row_until_expanded() {
        let mut table = table();
        assert_eq!(table.row_count(), 3); // solo + array + other

        let text = label(&table, "4815201");
        assert!(text.contains("4815201_[0-11]"), "{text}");
        assert!(text.contains("×12"), "{text}"); // 2 running + 1 + 9 from the range
        assert!(text.starts_with('▸'), "{text}");

        assert!(table.toggle_expand(Some("4815201")));
        assert_eq!(table.row_count(), 7); // 3 + its 4 members
        assert!(label(&table, "4815201").starts_with('▾'));

        table.toggle_expand(Some("4815201"));
        assert_eq!(table.row_count(), 3);
    }

    #[test]
    fn the_tally_counts_tasks_not_rows() {
        let table = table();
        let (_, members) = match &table.rows()[1] {
            Row::Group { base, members, .. } => (base, members),
            other => panic!("expected a group, got {other:?}"),
        };
        let tally = table.state_tally(members);
        // 2 running; pending is 1 task plus the 9 behind "[3-11]".
        assert_eq!(tally, vec![("PENDING".into(), 10), ("RUNNING".into(), 2)]);
    }

    #[test]
    fn expansion_survives_a_poll_and_a_filter() {
        let mut table = table();
        table.toggle_expand(Some("4815201"));
        assert_eq!(table.row_count(), 7);

        table.set_jobs(fixture()); // a poll
        assert_eq!(table.row_count(), 7);

        table.set_filter("sweep"); // only the array matches
        assert_eq!(table.row_count(), 5); // group row + 4 members
        table.set_filter("");
        assert_eq!(table.row_count(), 7);
    }

    #[test]
    fn non_array_jobs_are_untouched() {
        let mut table = JobTable::new(true);
        table.set_jobs(vec![
            running("1", "a", "0:10", "gpu", "RUNNING"),
            running("2", "b", "0:20", "cpu", "PENDING"),
        ]);
        assert_eq!(table.row_count(), 2);
        assert!(table.selected_group().is_none());
        assert_eq!(label(&table, "1"), "1");
    }

    #[test]
    fn collapsing_can_be_switched_off() {
        let mut table = JobTable::new(false);
        table.set_jobs(fixture());
        assert_eq!(table.row_count(), fixture().len());
        assert!(table.selected_group().is_none());
    }

    #[test]
    fn selection_resolves_to_a_real_task_id() {
        let mut table = table();
        table.select_index(1); // the collapsed array

        let (base, members) = table.selected_group().expect("a group is selected");
        assert_eq!(base, "4815201");
        assert_eq!(members.len(), 4);
        // The base id is not a job Slurm can describe; the first task is.
        assert_eq!(table.selected_job_id(), Some("4815201_0"));
    }

    #[test]
    fn expand_ids_maps_group_keys_to_members() {
        let table = table();
        let mut out = table.expand_ids(["4815201", "4815300"]);
        out.sort();
        assert_eq!(
            out,
            vec![
                "4815201_0",
                "4815201_1",
                "4815201_2",
                "4815201_[3-11]",
                "4815300",
            ]
        );
    }

    #[test]
    fn bookmarking_an_array_pins_the_group() {
        let mut table = table();
        table.set_bookmarks(BTreeSet::from(["4815201".to_string()]));
        assert_eq!(table.row_keys()[0], "4815201");
    }

    #[test]
    fn bookmarking_a_member_pins_its_group() {
        let mut table = table();
        table.set_bookmarks(BTreeSet::from(["4815201_2".to_string()]));
        assert_eq!(table.row_keys()[0], "4815201");
    }

    #[test]
    fn the_completed_table_groups_and_shows_the_longest_run() {
        let mut table = JobTable::new(true);
        table.set_jobs(vec![
            CompletedJob {
                job_id: "900_0".into(),
                name: "arr".into(),
                state: "COMPLETED".into(),
                elapsed: "0:30".into(),
                partition: "gpu".into(),
                ..CompletedJob::default()
            },
            CompletedJob {
                job_id: "900_1".into(),
                name: "arr".into(),
                state: "FAILED".into(),
                elapsed: "2:00:00".into(),
                partition: "gpu".into(),
                ..CompletedJob::default()
            },
            CompletedJob {
                job_id: "900_2".into(),
                name: "arr".into(),
                state: "COMPLETED".into(),
                elapsed: "1:00".into(),
                partition: "gpu".into(),
                ..CompletedJob::default()
            },
        ]);

        assert_eq!(table.row_count(), 1);
        let Row::Group { members, .. } = &table.rows()[0] else {
            panic!("expected a group");
        };
        assert_eq!(table.longest_elapsed(members), "2:00:00");
        assert_eq!(
            table.state_tally(members),
            vec![("COMPLETED".into(), 2), ("FAILED".into(), 1)]
        );
    }

    #[test]
    fn a_throttled_array_label_shows_the_real_index_range() {
        // `[1-4%10]` must render as [1-4] ×4, never [1-10].
        let mut table = JobTable::new(true);
        table.set_jobs(vec![
            running("700_0", "throttled", "1:00", "gpu", "RUNNING"),
            running("700_[1-4%10]", "throttled", "0:00", "gpu", "PENDING"),
        ]);

        let text = label(&table, "700");
        assert!(text.contains("700_[0-4]"), "{text}");
        assert!(text.contains("×5"), "{text}"); // task 0 plus tasks 1-4
        assert!(
            !text.contains("10"),
            "the throttle reached the label: {text}"
        );
    }

    #[test]
    fn a_strided_array_label_counts_only_real_tasks() {
        // `[1-9:2]` is five tasks spanning 1-9 — the stride is not an index.
        let mut table = JobTable::new(true);
        table.set_jobs(vec![
            running("800_0", "strided", "1:00", "gpu", "RUNNING"),
            running("800_[1-9:2]", "strided", "0:00", "gpu", "PENDING"),
        ]);

        let text = label(&table, "800");
        assert!(text.contains("800_[0-9]"), "{text}");
        assert!(text.contains("×6"), "{text}"); // task 0, plus 1, 3, 5, 7, 9
        assert!(!text.contains("×10"), "the span became the count: {text}");
    }

    #[test]
    fn filtering_an_array_can_dissolve_the_group() {
        let mut table = JobTable::new(true);
        table.set_jobs(vec![
            running("200_0", "sweep", "1:00", "gpu", "RUNNING"),
            running("200_1", "sweep", "0:00", "gpu", "PENDING"),
            running("300", "other", "1:00", "cpu", "RUNNING"),
        ]);

        table.set_filter("state:run");
        // Only the running task survives, so the array is no longer a group.
        assert_eq!(table.row_keys(), vec!["200_0", "300"]);

        table.set_filter("name:sweep");
        assert_eq!(table.row_keys(), vec!["200"]); // collapsed group row
    }

    #[test]
    fn no_match_shows_a_placeholder_that_cannot_be_acted_on() {
        let mut table = table();
        table.set_filter("zzz");

        assert_eq!(table.row_count(), 1); // the placeholder
        assert!(table.row_keys().is_empty()); // but it is not a job
        assert!(table.selected_key().is_none());
        assert!(table.selected_job_id().is_none());
        assert!(table.selected_group().is_none());
        assert_eq!(table.rows()[0], Row::Placeholder);
    }

    #[test]
    fn the_title_reports_the_match_count() {
        let mut table = JobTable::new(false);
        table.set_jobs(fixture());

        table.set_filter("state:pend");
        assert_eq!(table.title("Active Jobs"), "Active Jobs — 2/6 match");
        table.set_filter("");
        assert_eq!(table.title("Active Jobs"), "Active Jobs");
    }

    #[test]
    fn the_cursor_follows_the_job_across_a_poll() {
        let mut table = JobTable::new(false);
        table.set_jobs(fixture());
        table.select_index(3);
        let anchor = table.selected_key().unwrap().to_string();

        // A newer job arrives at the top, shifting every row down one.
        let mut jobs = fixture();
        jobs.insert(0, running("9999999", "newest", "0:01", "gpu", "RUNNING"));
        table.set_jobs(jobs);

        assert_eq!(table.selected_key(), Some(anchor.as_str()));
        assert_eq!(table.selected_index(), 4);
    }

    #[test]
    fn the_cursor_follows_a_task_into_its_collapsed_group() {
        let mut table = JobTable::new(false);
        table.set_jobs(fixture());
        table.select_key("4815201_2");

        // Collapsing removes that row; the group takes its place.
        table.set_collapse_arrays(true);
        assert_eq!(table.selected_key(), Some("4815201"));
    }

    #[test]
    fn the_cursor_clamps_when_its_job_disappears() {
        let mut table = JobTable::new(false);
        table.set_jobs(fixture());
        table.select_edge(true);

        table.set_jobs(vec![running("1", "a", "0:10", "gpu", "RUNNING")]);
        assert_eq!(table.selected_index(), 0);
        assert_eq!(table.selected_key(), Some("1"));
    }

    #[test]
    fn moving_past_the_end_reports_that_it_could_not() {
        let mut table = JobTable::new(false);
        table.set_jobs(fixture());

        assert!(!table.move_cursor(-1)); // already at the top
        table.select_edge(true);
        assert!(!table.move_cursor(1)); // already at the bottom
        assert!(table.move_cursor(-1));
    }

    #[test]
    fn markers_show_bookmarks_and_multi_selection() {
        let mut table = table();
        table.set_bookmarks(BTreeSet::from(["4815300".to_string()]));
        table.set_multiselected(BTreeSet::from(["4815300".to_string()]));

        assert_eq!(table.markers("4815300"), "◉ ★ ");
        assert_eq!(table.markers("4815100"), "");
    }

    #[test]
    fn toggling_expand_off_an_array_does_nothing() {
        let mut table = table();
        table.select_index(0); // the solo job
        assert!(!table.toggle_expand(None));
        assert_eq!(table.row_count(), 3);
    }

    #[test]
    fn an_array_that_finishes_forgets_it_was_expanded() {
        let mut table = table();
        table.toggle_expand(Some("4815201"));
        assert_eq!(table.row_count(), 7);

        // The array leaves the queue entirely.
        table.set_jobs(vec![running(
            "4815300", "solo-job", "0:42", "cpu", "RUNNING",
        )]);
        // It comes back later — collapsed, not still expanded.
        table.set_jobs(fixture());
        assert_eq!(table.row_count(), 3);
    }
}
