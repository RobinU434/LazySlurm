//! The full-screen panels: partitions, the nodes of one partition, and account
//! usage.
//!
//! Each is a pair of stacked tables (or a table and a summary) with its own
//! focus, sitting on a stack over the main screen. Every one of them shows
//! *everyone's* jobs, not just yours — the main view is the filtered one.

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};

use crate::model::{format_hours, FairShare, NodeInfo, PartitionInfo, PartitionJob, UsageRow};
use crate::slurm::UsageWindow;

use super::simple_table::SimpleTable;
use super::text::truncate;
use super::theme::{self, Theme};

/// Which of a screen's two tables has the cursor.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Pane {
    Top,
    Bottom,
}

impl Pane {
    pub fn other(self) -> Self {
        match self {
            Self::Top => Self::Bottom,
            Self::Bottom => Self::Top,
        }
    }
}

/// The partition monitor: every partition, and the jobs on the highlighted one.
pub struct PartitionScreen {
    pub partitions: SimpleTable<PartitionInfo>,
    pub jobs: SimpleTable<PartitionJob>,
    pub focus: Pane,
    /// The partition whose jobs are currently loaded.
    pub shown: Option<String>,
}

impl Default for PartitionScreen {
    fn default() -> Self {
        Self::new()
    }
}

impl PartitionScreen {
    pub fn new() -> Self {
        Self {
            partitions: SimpleTable::new(),
            jobs: SimpleTable::new(),
            focus: Pane::Top,
            shown: None,
        }
    }

    /// The partition the job list should be showing.
    pub fn selected_partition(&self) -> Option<&str> {
        self.partitions.selected_key()
    }

    /// Whether the loaded job list is for a different partition than the cursor.
    pub fn needs_jobs(&self) -> bool {
        self.selected_partition().map(str::to_string) != self.shown
    }

    /// The one-line summary above the tables.
    pub fn summary(&self) -> Line<'static> {
        let partitions = self.partitions.items();
        let total: u32 = partitions.iter().map(|p| p.nodes.total).sum();
        let allocated: u32 = partitions.iter().map(|p| p.nodes.allocated).sum();
        let running: u32 = partitions.iter().map(|p| p.running).sum();
        let pending: u32 = partitions.iter().map(|p| p.pending).sum();

        Line::from(vec![
            Span::styled(partitions.len().to_string(), theme::bold()),
            Span::raw(" partitions   "),
            Span::styled(allocated.to_string(), theme::running()),
            Span::raw(format!("/{total} nodes allocated   ")),
            Span::styled(running.to_string(), theme::running()),
            Span::raw(" running   "),
            Span::styled(pending.to_string(), theme::pending()),
            Span::raw(" pending   "),
            Span::styled("(all users)", theme::dim()),
        ])
    }

    /// The cells of one partition row.
    pub fn row(&self, partition: &PartitionInfo, theme: &Theme) -> Vec<Line<'static>> {
        let down = !partition.is_up();
        let name = if down {
            // A partition nobody can submit to is struck through, not hidden:
            // its absence would be more confusing than its state.
            Span::styled(
                partition.name.clone(),
                Style::new().add_modifier(Modifier::DIM | Modifier::CROSSED_OUT),
            )
        } else {
            Span::styled(partition.name.clone(), theme.partition(&partition.name))
        };

        vec![
            Line::from(name),
            if down {
                Line::from(Span::styled("[down]", theme::dim()))
            } else {
                load_bar(partition.load(), 10)
            },
            Line::raw(partition.nodes.display()),
            Line::raw(partition.cpus.display()),
            Line::from(count_span(partition.running, theme::running())),
            Line::from(count_span(partition.pending, theme::pending())),
            Line::raw(partition.time_limit.clone()),
            Line::raw(truncate(&partition.gres, 24)),
        ]
    }
}

/// The nodes of one partition, and what is running on the highlighted one.
pub struct NodeScreen {
    pub partition: String,
    pub nodes: SimpleTable<NodeInfo>,
    pub jobs: SimpleTable<PartitionJob>,
    pub focus: Pane,
    pub shown: Option<String>,
}

impl NodeScreen {
    pub fn new(partition: impl Into<String>) -> Self {
        Self {
            partition: partition.into(),
            nodes: SimpleTable::new(),
            jobs: SimpleTable::new(),
            focus: Pane::Top,
            shown: None,
        }
    }

    pub fn selected_node(&self) -> Option<&str> {
        self.nodes.selected_key()
    }

    pub fn needs_jobs(&self) -> bool {
        self.selected_node().map(str::to_string) != self.shown
    }

    /// The one-line summary: how the partition's nodes are doing.
    pub fn summary(&self) -> Line<'static> {
        let nodes = self.nodes.items();
        let count = |state: &str| nodes.iter().filter(|n| n.base_state() == state).count();
        let out = nodes.iter().filter(|n| n.is_out_of_service()).count();
        let gpus_used: u32 = nodes.iter().map(NodeInfo::gpus_used).sum();
        let gpus_total: u32 = nodes.iter().map(NodeInfo::gpus_total).sum();

        let mut spans = vec![
            Span::styled(self.partition.clone(), theme::bold()),
            Span::raw(format!("   {} nodes   ", nodes.len())),
            Span::styled(count("idle").to_string(), theme::running()),
            Span::raw(" idle  "),
            Span::styled(count("mixed").to_string(), theme::pending()),
            Span::raw(" mixed  "),
            Span::styled(
                count("allocated").to_string(),
                Style::new().fg(Color::LightRed),
            ),
            Span::raw(" full  "),
            Span::styled(out.to_string(), Style::new().fg(Color::Red)),
            Span::raw(" down/drained"),
        ];
        if gpus_total > 0 {
            spans.push(Span::raw("   GPUs "));
            spans.push(Span::styled(gpus_used.to_string(), theme::bold()));
            spans.push(Span::raw(format!("/{gpus_total} in use")));
        }
        spans.push(Span::styled("   (all users)", theme::dim()));
        Line::from(spans)
    }

    /// The cells of one node row.
    pub fn row(&self, node: &NodeInfo, theme: &Theme) -> Vec<Line<'static>> {
        let gpus = if node.gpus_total() > 0 {
            Span::styled(
                format!("{}/{}", node.gpus_used(), node.gpus_total()),
                if node.gpus_free() > 0 {
                    Style::new().fg(Color::Green)
                } else {
                    Style::new().fg(Color::Red)
                },
            )
        } else {
            Span::styled("—", theme::dim())
        };

        // A drained node's counters are stale; its reason is the useful part.
        let load = if node.is_out_of_service() {
            Line::from(Span::styled("—", theme::dim()))
        } else {
            load_bar(node.load(), 6)
        };

        let memory = if node.memory_mb > 0 {
            Line::from(vec![
                Span::styled(
                    format!("{:5.0}", f64::from(node.mem_used_mb()) / 1024.0),
                    if node.mem_used() < 0.9 {
                        Style::new()
                    } else {
                        Style::new().fg(Color::Red)
                    },
                ),
                Span::styled("/", theme::dim()),
                Span::raw(format!("{:.0}G", f64::from(node.memory_mb) / 1024.0)),
            ])
        } else {
            Line::from(Span::styled("—", theme::dim()))
        };

        vec![
            Line::from(Span::styled(
                node.name.clone(),
                if node.is_unresponsive() {
                    theme::bold()
                } else {
                    Style::new()
                },
            )),
            Line::from(Span::styled(
                node.state.clone(),
                theme.node_state(node.base_state()),
            )),
            Line::raw(node.cpus.display()),
            load,
            memory,
            Line::from(gpus),
            Line::from(Span::styled(
                truncate(&node.reason, 28),
                Style::new().fg(Color::Red).add_modifier(Modifier::DIM),
            )),
        ]
    }
}

/// Account usage and fair share.
pub struct UsageScreen {
    pub rows: SimpleTable<UsageRow>,
    pub shares: Vec<FairShare>,
    pub window: UsageWindow,
    pub user: String,
    /// True until the first result arrives; sreport can take seconds.
    pub loading: bool,
    /// False when the cluster turned out to have no accounting at all.
    pub accounting_available: bool,
}

impl UsageScreen {
    pub fn new(user: impl Into<String>) -> Self {
        Self {
            rows: SimpleTable::new(),
            shares: Vec::new(),
            window: UsageWindow::default(),
            user: user.into(),
            loading: true,
            accounting_available: true,
        }
    }

    /// Hours consumed by everyone in the account.
    pub fn total_hours(&self) -> f64 {
        self.rows
            .items()
            .iter()
            .filter(|row| !row.is_account_total())
            .map(|row| row.hours)
            .sum()
    }

    /// Hours consumed by this user.
    pub fn my_hours(&self) -> f64 {
        self.rows
            .items()
            .iter()
            .filter(|row| !row.is_account_total() && row.user == self.user)
            .map(|row| row.hours)
            .sum()
    }

    /// The one-line summary above the table.
    pub fn summary(&self) -> Line<'static> {
        if self.loading {
            return Line::from(Span::styled("loading usage...", theme::dim()));
        }

        let label = self.window.label();
        if self.rows.is_empty() && self.shares.is_empty() {
            let reason = if self.accounting_available {
                "no accounting data for you in this window"
            } else {
                "this cluster has no Slurm accounting enabled"
            };
            return Line::from(Span::styled(format!("{label} — {reason}"), theme::dim()));
        }

        let total = self.total_hours();
        let mine = self.my_hours();
        let mut spans = vec![
            Span::styled(label.to_string(), theme::bold()),
            Span::raw(format!(
                "   {} CPU-hours used by you   ",
                format_hours(mine)
            )),
            Span::styled(
                format!("account total {}", format_hours(total)),
                theme::dim(),
            ),
        ];
        if total > 0.0 {
            spans.push(Span::raw("  "));
            spans.push(Span::styled(
                format!("{:.0}%", mine / total * 100.0),
                theme::bold(),
            ));
            spans.push(Span::raw(" of it yours"));
        }
        spans.push(Span::styled("   (w cycles window)", theme::dim()));
        Line::from(spans)
    }

    /// The fair-share block: what actually drives queue priority.
    pub fn fairshare_lines(&self) -> Vec<Line<'static>> {
        // A user's own associations are the interesting ones; fall back to
        // whatever sshare gave us if it reported none.
        let mine: Vec<&FairShare> = {
            let with_user: Vec<&FairShare> =
                self.shares.iter().filter(|s| !s.user.is_empty()).collect();
            if with_user.is_empty() {
                self.shares.iter().collect()
            } else {
                with_user
            }
        };

        if mine.is_empty() {
            return vec![Line::from(Span::styled(
                "sshare reported no association for you",
                theme::dim(),
            ))];
        }

        let mut lines = Vec::new();
        for share in mine.iter().take(3) {
            let factor = match share.fairshare {
                Some(value) => format!("{value:.3}"),
                None => "n/a".to_string(),
            };
            lines.push(Line::from(vec![
                Span::styled(share.account.clone(), theme::bold()),
                Span::raw("  factor "),
                Span::styled(factor, theme::bold()),
                Span::raw(format!(
                    "  entitled {:.2}%  used {:.2}%",
                    share.norm_shares * 100.0,
                    share.effective_usage * 100.0
                )),
            ]));
            lines.push(Line::from(Span::styled(
                format!("  {}", share.reading()),
                theme::dim(),
            )));
        }
        lines
    }

    /// The cells of one usage row.
    pub fn row(&self, row: &UsageRow, total: f64) -> Vec<Line<'static>> {
        let share = if total > 0.0 { row.hours / total } else { 0.0 };
        let mine = !self.user.is_empty() && row.user == self.user;
        let highlight = if mine {
            Style::new().fg(Color::Cyan).add_modifier(Modifier::BOLD)
        } else {
            Style::new()
        };

        vec![
            Line::from(Span::styled(
                format!("{}{}", if mine { "▸ " } else { "" }, row.user),
                highlight,
            )),
            Line::from(Span::styled(
                row.name.clone(),
                if mine { Style::new() } else { theme::dim() },
            )),
            Line::raw(format_hours(row.hours)),
            Line::from(Span::styled(
                share_bar(share, 16),
                if mine {
                    Style::new().fg(Color::Cyan)
                } else {
                    theme::dim()
                },
            )),
            Line::raw(format!("{:4.1}%", share * 100.0)),
        ]
    }
}

/// The cells of one row of somebody-else's-jobs table.
pub fn partition_job_row(job: &PartitionJob, user: &str) -> Vec<Line<'static>> {
    let mine = !user.is_empty() && job.user == user;
    let id_style = if mine {
        Style::new().fg(Color::Cyan).add_modifier(Modifier::BOLD)
    } else {
        Style::new()
    };

    vec![
        Line::from(Span::styled(
            format!("{}{}", if mine { "▸ " } else { "" }, job.job_id),
            id_style,
        )),
        Line::from(Span::styled(
            job.user.clone(),
            if mine { theme::bold() } else { theme::dim() },
        )),
        Line::raw(truncate(&job.name, 18)),
        Line::from(Span::styled(
            job.state.clone(),
            if job.state == "RUNNING" {
                Style::new().fg(Color::Green)
            } else {
                Style::new().fg(Color::Yellow)
            },
        )),
        Line::raw(job.elapsed.clone()),
        Line::raw(job.time_limit.clone()),
        Line::raw(job.nodes.clone()),
        Line::raw(job.cpus.clone()),
        Line::raw(truncate(&job.gres, 16)),
        Line::raw(truncate(&job.nodelist, 22)),
    ]
}

/// `███████░░░ 70%` — a load bar, coloured by how full it is.
pub fn load_bar(fraction: f64, width: usize) -> Line<'static> {
    let fraction = fraction.clamp(0.0, 1.0);
    let filled = (fraction * width as f64).round() as usize;
    let style = if fraction >= 0.9 {
        Style::new().fg(Color::Red)
    } else if fraction >= 0.6 {
        Style::new().fg(Color::Yellow)
    } else {
        Style::new().fg(Color::Green)
    };

    Line::from(vec![
        Span::styled(
            format!("{}{}", "█".repeat(filled), "░".repeat(width - filled)),
            style,
        ),
        Span::raw(format!(" {:3.0}%", fraction * 100.0)),
    ])
}

/// `████████░░░░░░░░` — one consumer's slice of the account's hours.
///
/// Any non-zero share shows at least one block, so a small user does not read as
/// having consumed nothing at all.
pub fn share_bar(fraction: f64, width: usize) -> String {
    let fraction = fraction.clamp(0.0, 1.0);
    let mut filled = (fraction * width as f64).round() as usize;
    if fraction > 0.0 {
        filled = filled.max(1);
    }
    format!("{}{}", "█".repeat(filled), "░".repeat(width - filled))
}

fn count_span(count: u32, style: Style) -> Span<'static> {
    Span::styled(
        count.to_string(),
        if count > 0 { style } else { theme::dim() },
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Aiot;

    fn text(lines: &[Line]) -> String {
        lines
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

    fn line_text(line: &Line) -> String {
        line.spans
            .iter()
            .map(|span| span.content.to_string())
            .collect()
    }

    fn partition(name: &str, up: bool) -> PartitionInfo {
        PartitionInfo {
            name: name.into(),
            avail: if up { "up".into() } else { "down".into() },
            nodes: Aiot {
                allocated: 10,
                idle: 5,
                other: 0,
                total: 15,
            },
            cpus: Aiot {
                allocated: 100,
                idle: 100,
                other: 0,
                total: 200,
            },
            running: 3,
            pending: 2,
            ..PartitionInfo::default()
        }
    }

    #[test]
    fn the_partition_summary_totals_every_partition() {
        let mut screen = PartitionScreen::new();
        screen
            .partitions
            .set_items(vec![partition("gpu", true), partition("cpu", true)]);

        let summary = line_text(&screen.summary());
        assert!(summary.contains("2 partitions"), "{summary}");
        assert!(summary.contains("20/30 nodes allocated"), "{summary}");
        assert!(summary.contains("6 running"), "{summary}");
        assert!(summary.contains("(all users)"), "{summary}");
    }

    #[test]
    fn a_down_partition_is_marked_rather_than_hidden() {
        let screen = PartitionScreen::new();
        let theme = Theme::default();

        let row = screen.row(&partition("maint", false), &theme);
        assert!(line_text(&row[1]).contains("[down]"));
        // Its name is struck through.
        assert!(row[0].spans[0]
            .style
            .add_modifier
            .contains(Modifier::CROSSED_OUT));
    }

    #[test]
    fn the_job_list_reloads_when_the_partition_changes() {
        let mut screen = PartitionScreen::new();
        screen
            .partitions
            .set_items(vec![partition("gpu", true), partition("cpu", true)]);

        assert!(screen.needs_jobs());
        screen.shown = Some("gpu".into());
        assert!(!screen.needs_jobs());

        screen.partitions.move_cursor(1);
        assert!(screen.needs_jobs());
    }

    fn node(name: &str, state: &str) -> NodeInfo {
        NodeInfo {
            name: name.into(),
            state: state.into(),
            cpus: Aiot {
                allocated: 32,
                idle: 32,
                other: 0,
                total: 64,
            },
            memory_mb: 128_000,
            free_mem_mb: 64_000,
            cpu_load: 16.0,
            gres: "gpu:a100:8".into(),
            gres_used: "gpu:a100:3".into(),
            ..NodeInfo::default()
        }
    }

    #[test]
    fn the_node_summary_counts_states_and_gpus() {
        let mut screen = NodeScreen::new("gpu");
        screen.nodes.set_items(vec![
            node("gpu01", "idle"),
            node("gpu02", "mixed"),
            node("gpu03", "drained*"),
        ]);

        let summary = line_text(&screen.summary());
        assert!(summary.contains("gpu"), "{summary}");
        assert!(summary.contains("3 nodes"), "{summary}");
        assert!(summary.contains("1 idle"), "{summary}");
        assert!(summary.contains("1 down/drained"), "{summary}");
        assert!(summary.contains("GPUs 9/24 in use"), "{summary}");
    }

    #[test]
    fn an_out_of_service_node_shows_a_dash_rather_than_a_stale_bar() {
        let screen = NodeScreen::new("gpu");
        let theme = Theme::default();

        let healthy = screen.row(&node("gpu01", "mixed"), &theme);
        let drained = screen.row(&node("gpu03", "drained*"), &theme);

        assert!(line_text(&healthy[3]).contains('%'));
        assert_eq!(line_text(&drained[3]), "—");
    }

    #[test]
    fn a_node_with_no_gpus_shows_a_dash() {
        let screen = NodeScreen::new("cpu");
        let theme = Theme::default();
        let mut plain = node("cpu01", "idle");
        plain.gres = String::new();
        plain.gres_used = String::new();

        assert_eq!(line_text(&screen.row(&plain, &theme)[5]), "—");
    }

    #[test]
    fn usage_reports_the_users_share_of_the_account() {
        let mut screen = UsageScreen::new("rvy895");
        screen.loading = false;
        screen.rows.set_items(vec![
            UsageRow {
                account: "physics".into(),
                user: "rvy895".into(),
                hours: 2500.0,
                ..UsageRow::default()
            },
            UsageRow {
                account: "physics".into(),
                user: "pba175".into(),
                hours: 7500.0,
                ..UsageRow::default()
            },
        ]);

        let summary = line_text(&screen.summary());
        assert!(summary.contains("2 500 CPU-hours used by you"), "{summary}");
        assert!(summary.contains("account total 10 000"), "{summary}");
        assert!(summary.contains("25% of it yours"), "{summary}");
    }

    #[test]
    fn usage_says_which_kind_of_nothing_it_found() {
        let mut screen = UsageScreen::new("rvy895");
        screen.loading = false;

        assert!(line_text(&screen.summary()).contains("no accounting data for you"));

        screen.accounting_available = false;
        assert!(line_text(&screen.summary()).contains("no Slurm accounting enabled"));
    }

    #[test]
    fn usage_says_it_is_loading_until_the_first_result() {
        let screen = UsageScreen::new("rvy895");
        assert!(line_text(&screen.summary()).contains("loading"));
    }

    #[test]
    fn fairshare_prefers_the_users_own_associations() {
        let mut screen = UsageScreen::new("rvy895");
        screen.shares = vec![
            FairShare {
                account: "physics".into(),
                fairshare: None,
                ..FairShare::default()
            },
            FairShare {
                account: "physics".into(),
                user: "rvy895".into(),
                norm_shares: 0.05,
                effective_usage: 0.03,
                fairshare: Some(0.7125),
                ..FairShare::default()
            },
        ];

        let report = text(&screen.fairshare_lines());
        assert!(report.contains("0.713"), "{report}");
        assert!(report.contains("under your share"), "{report}");
        // The account row, which has no factor, is not what it shows.
        assert!(!report.contains("n/a"), "{report}");
    }

    #[test]
    fn a_load_bar_is_coloured_by_how_full_it_is() {
        assert_eq!(load_bar(0.0, 10).spans[0].style.fg, Some(Color::Green));
        assert_eq!(load_bar(0.7, 10).spans[0].style.fg, Some(Color::Yellow));
        assert_eq!(load_bar(0.95, 10).spans[0].style.fg, Some(Color::Red));
        // Oversubscribed nodes report a load above 1.0; the bar clamps.
        assert_eq!(line_text(&load_bar(1.5, 10)), "██████████ 100%");
    }

    #[test]
    fn a_share_bar_never_rounds_a_real_share_away() {
        assert_eq!(share_bar(0.5, 10), "█████░░░░░");
        assert_eq!(share_bar(0.0, 10), "░░░░░░░░░░");
        // One percent of the account is still a block.
        assert_eq!(share_bar(0.01, 10).chars().next(), Some('█'));
    }

    #[test]
    fn your_own_jobs_are_marked_in_a_shared_list() {
        let job = PartitionJob {
            job_id: "100".into(),
            user: "rvy895".into(),
            name: "train".into(),
            state: "RUNNING".into(),
            ..PartitionJob::default()
        };

        assert!(line_text(&partition_job_row(&job, "rvy895")[0]).starts_with("▸ "));
        assert!(!line_text(&partition_job_row(&job, "someone")[0]).starts_with("▸ "));
    }
}
