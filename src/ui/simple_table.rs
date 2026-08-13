//! A table with no grouping: partitions, nodes, other people's jobs, usage rows.
//!
//! The job tables need array folding, filtering and bookmarks; these do not.
//! What they *do* share is the rule that matters most in a polling UI: the
//! cursor follows the same row across a refresh rather than staying on the same
//! index, or the list moves under the user every few seconds.

/// A row that can be identified across refreshes.
pub trait Keyed {
    fn key(&self) -> &str;
}

impl Keyed for crate::model::PartitionInfo {
    fn key(&self) -> &str {
        &self.name
    }
}

impl Keyed for crate::model::NodeInfo {
    fn key(&self) -> &str {
        &self.name
    }
}

impl Keyed for crate::model::PartitionJob {
    fn key(&self) -> &str {
        &self.job_id
    }
}

impl Keyed for crate::model::UsageRow {
    fn key(&self) -> &str {
        // Account-total rows have no user, so they key on the account.
        if self.user.is_empty() {
            &self.account
        } else {
            &self.user
        }
    }
}

/// A list of rows with a cursor.
#[derive(Debug, Clone)]
pub struct SimpleTable<T> {
    items: Vec<T>,
    selected: usize,
    /// The key the cursor is on, so a refresh can find it again.
    anchor: Option<String>,
    focused: bool,
}

impl<T: Keyed> Default for SimpleTable<T> {
    fn default() -> Self {
        Self::new()
    }
}

impl<T: Keyed> SimpleTable<T> {
    pub fn new() -> Self {
        Self {
            items: Vec::new(),
            selected: 0,
            anchor: None,
            focused: false,
        }
    }

    /// Replace the rows, keeping the cursor on the same one where possible.
    pub fn set_items(&mut self, items: Vec<T>) {
        self.items = items;
        match self.anchor.clone() {
            Some(anchor) if self.select_key(&anchor) => {}
            _ => {
                self.selected = self.selected.min(self.items.len().saturating_sub(1));
                self.sync_anchor();
            }
        }
    }

    pub fn items(&self) -> &[T] {
        &self.items
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    pub fn selected_index(&self) -> usize {
        self.selected
    }

    pub fn selected(&self) -> Option<&T> {
        self.items.get(self.selected)
    }

    pub fn selected_key(&self) -> Option<&str> {
        self.selected().map(Keyed::key)
    }

    pub fn is_focused(&self) -> bool {
        self.focused
    }

    pub fn set_focused(&mut self, focused: bool) {
        self.focused = focused;
    }

    /// Move the cursor, clamping at both ends.
    ///
    /// Returns whether it moved, so a caller can fall through to something else
    /// at the edges.
    pub fn move_cursor(&mut self, delta: isize) -> bool {
        if self.items.is_empty() {
            return false;
        }
        let target = self.selected as isize + delta;
        if target < 0 || target >= self.items.len() as isize {
            return false;
        }
        self.selected = target as usize;
        self.sync_anchor();
        true
    }

    pub fn select_index(&mut self, index: usize) {
        self.selected = index.min(self.items.len().saturating_sub(1));
        self.sync_anchor();
    }

    pub fn select_edge(&mut self, last: bool) {
        self.selected = if last {
            self.items.len().saturating_sub(1)
        } else {
            0
        };
        self.sync_anchor();
    }

    /// Put the cursor on the row with this key, if it is present.
    pub fn select_key(&mut self, key: &str) -> bool {
        match self.items.iter().position(|item| item.key() == key) {
            Some(index) => {
                self.selected = index;
                self.sync_anchor();
                true
            }
            None => false,
        }
    }

    fn sync_anchor(&mut self) {
        self.anchor = self.selected_key().map(str::to_string);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::PartitionInfo;

    fn partition(name: &str) -> PartitionInfo {
        PartitionInfo {
            name: name.into(),
            ..PartitionInfo::default()
        }
    }

    fn table(names: &[&str]) -> SimpleTable<PartitionInfo> {
        let mut table = SimpleTable::new();
        table.set_items(names.iter().map(|name| partition(name)).collect());
        table
    }

    #[test]
    fn starts_on_the_first_row() {
        let table = table(&["gpu", "cpu"]);
        assert_eq!(table.selected_key(), Some("gpu"));
        assert_eq!(table.selected_index(), 0);
    }

    #[test]
    fn an_empty_table_has_no_selection() {
        let table: SimpleTable<PartitionInfo> = SimpleTable::new();
        assert!(table.is_empty());
        assert_eq!(table.selected_key(), None);
    }

    #[test]
    fn the_cursor_follows_its_row_across_a_refresh() {
        let mut table = table(&["gpu", "cpu", "fat"]);
        table.select_key("cpu");

        // A partition appears above it, shifting every row down.
        table.set_items(
            ["new", "gpu", "cpu", "fat"]
                .iter()
                .map(|name| partition(name))
                .collect(),
        );

        assert_eq!(table.selected_key(), Some("cpu"));
        assert_eq!(table.selected_index(), 2);
    }

    #[test]
    fn the_cursor_clamps_when_its_row_disappears() {
        let mut table = table(&["gpu", "cpu", "fat"]);
        table.select_edge(true);

        table.set_items(vec![partition("gpu")]);
        assert_eq!(table.selected_index(), 0);
        assert_eq!(table.selected_key(), Some("gpu"));
    }

    #[test]
    fn moving_past_an_edge_reports_that_it_could_not() {
        let mut table = table(&["gpu", "cpu"]);

        assert!(!table.move_cursor(-1));
        assert!(table.move_cursor(1));
        assert!(!table.move_cursor(1));
    }

    #[test]
    fn jumps_to_either_end() {
        let mut table = table(&["gpu", "cpu", "fat"]);

        table.select_edge(true);
        assert_eq!(table.selected_key(), Some("fat"));
        table.select_edge(false);
        assert_eq!(table.selected_key(), Some("gpu"));
    }

    #[test]
    fn selecting_an_absent_key_changes_nothing() {
        let mut table = table(&["gpu", "cpu"]);
        assert!(!table.select_key("nope"));
        assert_eq!(table.selected_key(), Some("gpu"));
    }

    #[test]
    fn a_usage_row_keys_on_its_user_or_its_account() {
        use crate::model::UsageRow;

        let user = UsageRow {
            account: "physics".into(),
            user: "rvy895".into(),
            ..UsageRow::default()
        };
        let total = UsageRow {
            account: "physics".into(),
            ..UsageRow::default()
        };
        assert_eq!(user.key(), "rvy895");
        assert_eq!(total.key(), "physics");
    }
}
