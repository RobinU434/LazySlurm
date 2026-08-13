//! Accounting records: usage, fair share, and job priority.
//!
//! These come from `sreport`, `sshare` and `sprio`, all of which need Slurm
//! accounting to be enabled. Clusters without slurmdbd simply have none of this,
//! which the UI reports as a message rather than an empty table.

/// One line of `sreport cluster AccountUtilizationByUser`.
///
/// sreport emits a row per user plus, where the caller is allowed to see it, a
/// row for the account itself with an empty login.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct UsageRow {
    pub account: String,
    pub user: String,
    pub name: String,
    pub hours: f64,
}

impl UsageRow {
    /// The account-level total row, as opposed to one user's usage.
    pub fn is_account_total(&self) -> bool {
        self.user.is_empty()
    }
}

/// One line of `sshare` — what actually drives queue priority.
///
/// `norm_shares` is the slice of the cluster you are entitled to;
/// `effective_usage` is the slice you have actually consumed. The `fairshare`
/// factor Slurm derives from the two is what enters the priority calculation:
/// 0.5 means you are using exactly your share, above that you are
/// under-consuming and get boosted, below that you are over-consuming.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct FairShare {
    pub account: String,
    pub user: String,
    /// A number, or the literal `parent`.
    pub raw_shares: String,
    pub norm_shares: f64,
    pub raw_usage: f64,
    pub effective_usage: f64,
    /// `None` on account rows, which leave the column empty.
    pub fairshare: Option<f64>,
}

impl FairShare {
    /// How many times your entitlement you have used. 1.0 is exactly fair.
    pub fn share_ratio(&self) -> Option<f64> {
        (self.norm_shares > 0.0).then(|| self.effective_usage / self.norm_shares)
    }

    /// The fairshare factor, in a sentence.
    pub fn reading(&self) -> String {
        let Some(factor) = self.fairshare else {
            return "no fairshare factor reported for this association".to_string();
        };

        let share = match self.share_ratio() {
            Some(ratio) if ratio >= 1.05 => format!(" (using {ratio:.1}x your share)"),
            Some(ratio) if ratio > 0.0 && ratio <= 0.95 => {
                format!(" (using {ratio:.2} of your share)")
            }
            _ => String::new(),
        };

        let verdict = if factor >= 0.75 {
            "well under your share — your jobs get boosted priority"
        } else if factor > 0.55 {
            "a little under your share — slight priority boost"
        } else if factor >= 0.45 {
            "using about exactly your share"
        } else if factor >= 0.25 {
            "over your share — your jobs get reduced priority"
        } else {
            "far over your share — your jobs are heavily deprioritised"
        };

        format!("{verdict}{share}")
    }
}

/// One pending job's priority, broken into the factors `sprio` reports.
///
/// `rank`/`queued` place the job among the other pending jobs of its partition:
/// rank 1 is next in line. Both are 0 when the queue could not be read.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PriorityInfo {
    pub job_id: String,
    pub total: i64,
    pub age: i64,
    pub fairshare: i64,
    pub job_size: i64,
    pub partition: i64,
    pub qos: i64,
    pub rank: u32,
    pub queued: u32,
}

impl PriorityInfo {
    /// The non-zero contributions, largest first.
    pub fn factors(&self) -> Vec<(&'static str, i64)> {
        let mut named = vec![
            ("Age", self.age),
            ("Fair-share", self.fairshare),
            ("Job size", self.job_size),
            ("Partition", self.partition),
            ("QOS", self.qos),
        ];
        named.retain(|(_, value)| *value != 0);
        named.sort_by_key(|(_, value)| -*value);
        named
    }

    /// How many pending jobs of the partition outrank this one.
    pub fn ahead(&self) -> u32 {
        self.rank.saturating_sub(1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifies_the_account_total_row() {
        let total = UsageRow {
            account: "physics".into(),
            ..UsageRow::default()
        };
        let user = UsageRow {
            account: "physics".into(),
            user: "rturich".into(),
            ..UsageRow::default()
        };
        assert!(total.is_account_total());
        assert!(!user.is_account_total());
    }

    #[test]
    fn reads_an_under_consuming_association_as_boosted() {
        let share = FairShare {
            fairshare: Some(0.9),
            norm_shares: 0.1,
            effective_usage: 0.05,
            ..FairShare::default()
        };
        let reading = share.reading();
        assert!(reading.starts_with("well under your share"), "{reading}");
        assert!(reading.contains("0.50 of your share"), "{reading}");
    }

    #[test]
    fn reads_an_over_consuming_association_as_deprioritised() {
        let share = FairShare {
            fairshare: Some(0.1),
            norm_shares: 0.1,
            effective_usage: 0.3,
            ..FairShare::default()
        };
        let reading = share.reading();
        assert!(reading.starts_with("far over your share"), "{reading}");
        assert!(reading.contains("3.0x your share"), "{reading}");
    }

    #[test]
    fn account_rows_have_no_fairshare_factor() {
        let share = FairShare::default();
        assert_eq!(
            share.reading(),
            "no fairshare factor reported for this association"
        );
        assert_eq!(share.share_ratio(), None);
    }

    #[test]
    fn ranks_priority_factors_largest_first() {
        let priority = PriorityInfo {
            total: 1000,
            age: 100,
            fairshare: 500,
            job_size: 0,
            partition: 300,
            qos: 0,
            ..PriorityInfo::default()
        };
        assert_eq!(
            priority.factors(),
            vec![("Fair-share", 500), ("Partition", 300), ("Age", 100)]
        );
    }

    #[test]
    fn rank_one_has_nothing_ahead_of_it() {
        let priority = PriorityInfo {
            rank: 1,
            ..PriorityInfo::default()
        };
        assert_eq!(priority.ahead(), 0);
    }
}
