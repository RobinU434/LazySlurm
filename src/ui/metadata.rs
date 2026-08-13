//! The lower-right panel: what a job asked for, how it was submitted, why it is
//! waiting, and everything Slurm said about it.
//!
//! The Pending tab appears only while the selected job is actually waiting, so
//! the tab strip changes shape as the user moves between jobs. That is handled
//! by [`super::tabs::TabStrip`], which keeps the user on the same tab by name.

use ratatui::style::Modifier;
use ratatui::text::{Line, Span};

use crate::model::{JobDetail, PriorityInfo};
use crate::slurm::reason;

use super::tabs::TabStrip;
use super::theme;

/// The tabs, in order. Pending is dropped for a job that is not waiting.
const RESOURCES: &str = "Resources";
const SUBMISSION: &str = "Submission";
const PENDING: &str = "Pending";
const RAW: &str = "Raw";

/// How wide the priority-factor bars are drawn.
const BAR_WIDTH: usize = 20;

/// The lower-right panel.
pub struct MetadataView {
    tabs: TabStrip,
    detail: Option<JobDetail>,
    priority: Option<PriorityInfo>,
    /// False once `sprio` has been found to be missing, so the panel can say
    /// *why* the breakdown is absent rather than leaving a hole.
    priority_available: bool,
    scroll: usize,
}

impl Default for MetadataView {
    fn default() -> Self {
        Self::new()
    }
}

impl MetadataView {
    pub fn new() -> Self {
        Self {
            tabs: TabStrip::new(vec![RESOURCES, SUBMISSION, RAW]),
            detail: None,
            priority: None,
            priority_available: true,
            scroll: 0,
        }
    }

    pub fn tabs(&self) -> &TabStrip {
        &self.tabs
    }

    pub fn active_tab(&self) -> Option<&'static str> {
        self.tabs.active()
    }

    pub fn cycle_tab(&mut self, delta: isize) {
        self.tabs.cycle(delta);
        self.scroll = 0;
    }

    pub fn scroll_by(&mut self, delta: isize) {
        self.scroll = (self.scroll as isize + delta).max(0) as usize;
    }

    pub fn scroll_offset(&self) -> usize {
        self.scroll
    }

    /// Show a job, adding or removing the Pending tab to match its state.
    pub fn set_detail(
        &mut self,
        detail: Option<JobDetail>,
        priority: Option<PriorityInfo>,
        priority_available: bool,
    ) {
        let pending = detail.as_ref().is_some_and(JobDetail::is_pending);
        self.tabs.set_tabs(if pending {
            vec![RESOURCES, SUBMISSION, PENDING, RAW]
        } else {
            vec![RESOURCES, SUBMISSION, RAW]
        });

        self.detail = detail;
        self.priority = priority;
        self.priority_available = priority_available;
        self.scroll = 0;
    }

    /// The contents of the active tab.
    pub fn lines(&self) -> Vec<Line<'static>> {
        let Some(detail) = &self.detail else {
            return vec![Line::from(Span::styled("No job selected", theme::dim()))];
        };

        match self.tabs.active() {
            Some(RESOURCES) => resources_lines(detail),
            Some(SUBMISSION) => submission_lines(detail),
            Some(PENDING) => pending_lines(detail, self.priority.as_ref(), self.priority_available),
            Some(RAW) => raw_lines(detail),
            _ => Vec::new(),
        }
    }
}

/// `Label: value`, with the label emphasised.
fn field(label: &str, value: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("{label:<12}"), theme::bold()),
        Span::raw(value.to_string()),
    ])
}

fn resources_lines(detail: &JobDetail) -> Vec<Line<'static>> {
    vec![
        field("State:", detail.state()),
        field("Partition:", detail.partition()),
        field("Nodes:", detail.num_nodes()),
        field("CPUs:", detail.num_cpus()),
        field("Memory:", detail.memory()),
        field("GPU/GRES:", detail.gres()),
        field("TRES:", detail.tres()),
        field("Node List:", detail.node_list()),
        field("Time Limit:", detail.time_limit()),
        field("Run Time:", detail.run_time()),
        field("Account:", detail.account()),
        field("QoS:", detail.qos()),
    ]
}

fn submission_lines(detail: &JobDetail) -> Vec<Line<'static>> {
    vec![
        field("Submit Time:", detail.submit_time()),
        field("Start Time:", detail.start_time()),
        field("End Time:", detail.end_time()),
        field("Work Dir:", &detail.work_dir),
        field("StdOut:", detail.stdout_path.as_deref().unwrap_or("N/A")),
        field("StdErr:", detail.stderr_path.as_deref().unwrap_or("N/A")),
        field("Command:", detail.submit_line()),
    ]
}

/// Why a job is waiting, when it might start, and what its priority is made of.
fn pending_lines(
    detail: &JobDetail,
    priority: Option<&PriorityInfo>,
    priority_available: bool,
) -> Vec<Line<'static>> {
    let code = detail.reason();
    let why = reason::explain(code, Some(&detail.raw), priority);
    let starts = reason::format_start_estimate(
        detail
            .raw
            .get("StartTime")
            .map(String::as_str)
            .unwrap_or(""),
        reason::now(),
    );

    let mut lines = vec![
        field("Why:", &why),
        // The raw code stays visible: the explanation is a translation, and a
        // user who knows Slurm may want the original.
        Line::from(Span::styled(
            format!("             reason code: {code}"),
            theme::dim(),
        )),
        field("Starts:", &starts),
        Line::raw(""),
    ];

    let Some(priority) = priority else {
        lines.push(Line::from(Span::styled(
            if priority_available {
                "Priority breakdown unavailable — sprio reported nothing for this job."
            } else {
                "Priority breakdown unavailable — this cluster does not run sprio \
                 (priority accounting is off)."
            },
            theme::dim(),
        )));
        return lines;
    };

    lines.push(field(
        "Queue:",
        &format!(
            "#{} of {} pending in {}",
            priority.rank,
            priority.queued,
            detail.partition()
        ),
    ));
    lines.push(field("Priority:", &priority.total.to_string()));

    let factors = priority.factors();
    if factors.is_empty() {
        lines.push(Line::from(Span::styled(
            "  all priority factors are zero",
            theme::dim(),
        )));
        return lines;
    }

    let width = factors
        .iter()
        .map(|(name, _)| name.len())
        .max()
        .unwrap_or(0);
    for (name, value) in factors {
        let share = if priority.total > 0 {
            value as f64 / priority.total as f64 * 100.0
        } else {
            0.0
        };
        lines.push(Line::from(vec![
            Span::raw(format!("  {name:<width$}  ")),
            Span::styled(
                priority_bar(value, priority.total),
                ratatui::style::Style::new().fg(ratatui::style::Color::Cyan),
            ),
            Span::raw(format!(" {value:>8}  {share:4.0}%")),
        ]));
    }
    lines
}

/// `████████░░░░` — one factor's share of the total priority.
///
/// A non-zero factor always shows at least one block: rounding it away would
/// read as "contributes nothing", which is a different claim.
pub fn priority_bar(value: i64, total: i64) -> String {
    if total <= 0 || value <= 0 {
        return String::new();
    }
    let filled = ((BAR_WIDTH as f64 * value as f64 / total as f64).round() as usize).max(1);
    let filled = filled.min(BAR_WIDTH);
    format!("{}{}", "█".repeat(filled), "░".repeat(BAR_WIDTH - filled))
}

/// Everything Slurm reported, in key order.
fn raw_lines(detail: &JobDetail) -> Vec<Line<'static>> {
    if detail.raw.is_empty() {
        return vec![Line::from(Span::styled("No raw data", theme::dim()))];
    }
    detail
        .raw
        .iter()
        .map(|(key, value)| {
            Line::from(vec![
                Span::styled(
                    format!("{key}: "),
                    ratatui::style::Style::new().add_modifier(Modifier::BOLD),
                ),
                Span::raw(value.clone()),
            ])
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::job::DetailSource;
    use std::collections::BTreeMap;

    fn detail(pairs: &[(&str, &str)]) -> JobDetail {
        JobDetail {
            job_id: "123".into(),
            raw: pairs
                .iter()
                .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
                .collect::<BTreeMap<_, _>>(),
            stdout_path: Some("/work/slurm-123.out".into()),
            stderr_path: None,
            work_dir: "/work".into(),
            source: DetailSource::Scontrol,
        }
    }

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

    #[test]
    fn shows_nothing_until_a_job_is_selected() {
        assert!(text(&MetadataView::new().lines()).contains("No job selected"));
    }

    #[test]
    fn the_pending_tab_appears_only_for_a_waiting_job() {
        let mut view = MetadataView::new();

        view.set_detail(Some(detail(&[("JobState", "RUNNING")])), None, true);
        assert_eq!(view.tabs().tabs(), &[RESOURCES, SUBMISSION, RAW]);

        view.set_detail(Some(detail(&[("JobState", "PENDING")])), None, true);
        assert_eq!(view.tabs().tabs(), &[RESOURCES, SUBMISSION, PENDING, RAW]);
    }

    #[test]
    fn leaving_the_pending_tab_behind_does_not_strand_the_user() {
        let mut view = MetadataView::new();
        view.set_detail(Some(detail(&[("JobState", "PENDING")])), None, true);
        view.tabs.select(PENDING);

        // The job starts running; its Pending tab goes away.
        view.set_detail(Some(detail(&[("JobState", "RUNNING")])), None, true);
        assert_eq!(view.active_tab(), Some(RESOURCES));
    }

    #[test]
    fn cycling_skips_the_pending_tab_when_it_is_absent() {
        let mut view = MetadataView::new();
        view.set_detail(Some(detail(&[("JobState", "RUNNING")])), None, true);

        view.cycle_tab(1);
        view.cycle_tab(1);
        assert_eq!(view.active_tab(), Some(RAW));
    }

    #[test]
    fn resources_reports_what_the_job_asked_for() {
        let mut view = MetadataView::new();
        view.set_detail(
            Some(detail(&[
                ("JobState", "RUNNING"),
                ("Partition", "gpu"),
                ("NumCPUs", "8"),
                ("NumNodes", "1"),
                ("MinMemoryNode", "32G"),
                ("TRES", "cpu=8,mem=32G,gres/gpu=2"),
                ("NodeList", "gpu-node01"),
                ("Account", "physics"),
                ("QOS", "normal"),
            ])),
            None,
            true,
        );

        let report = text(&view.lines());
        assert!(report.contains("gpu-node01"), "{report}");
        assert!(report.contains("gres/gpu=2"), "{report}");
        assert!(report.contains("physics"), "{report}");
    }

    #[test]
    fn submission_reports_the_paths_and_the_command() {
        let mut view = MetadataView::new();
        view.set_detail(
            Some(detail(&[
                ("JobState", "RUNNING"),
                ("SubmitLine", "sbatch --array=1-4 job.sh"),
            ])),
            None,
            true,
        );
        view.cycle_tab(1);

        let report = text(&view.lines());
        assert!(report.contains("/work/slurm-123.out"), "{report}");
        // No stderr path was reported; the panel says so rather than showing a gap.
        assert!(report.contains("StdErr:     N/A"), "{report}");
        assert!(report.contains("sbatch --array=1-4 job.sh"), "{report}");
    }

    #[test]
    fn pending_explains_the_reason_and_keeps_the_code() {
        let mut view = MetadataView::new();
        view.set_detail(
            Some(detail(&[
                ("JobState", "PENDING"),
                ("Reason", "Resources"),
                ("Partition", "gpu"),
                ("StartTime", "Unknown"),
            ])),
            None,
            true,
        );
        view.tabs.select(PENDING);

        let report = text(&view.lines());
        assert!(report.contains("free nodes"), "{report}");
        assert!(report.contains("reason code: Resources"), "{report}");
        assert!(report.contains("not estimated"), "{report}");
    }

    #[test]
    fn pending_shows_the_priority_breakdown() {
        let mut view = MetadataView::new();
        view.set_detail(
            Some(detail(&[
                ("JobState", "PENDING"),
                ("Reason", "Priority"),
                ("Partition", "gpu"),
            ])),
            Some(PriorityInfo {
                job_id: "123".into(),
                total: 1000,
                age: 100,
                fairshare: 900,
                rank: 3,
                queued: 40,
                ..PriorityInfo::default()
            }),
            true,
        );
        view.tabs.select(PENDING);

        let report = text(&view.lines());
        assert!(report.contains("#3 of 40 pending in gpu"), "{report}");
        assert!(report.contains("Fair-share"), "{report}");
        assert!(report.contains("90%"), "{report}");
        // The explanation uses the queue position.
        assert!(report.contains("2 jobs ahead"), "{report}");
    }

    #[test]
    fn pending_distinguishes_no_data_from_no_sprio() {
        let waiting = detail(&[("JobState", "PENDING"), ("Reason", "Priority")]);

        let mut view = MetadataView::new();
        view.set_detail(Some(waiting.clone()), None, true);
        view.tabs.select(PENDING);
        assert!(text(&view.lines()).contains("sprio reported nothing"));

        view.set_detail(Some(waiting), None, false);
        view.tabs.select(PENDING);
        assert!(text(&view.lines()).contains("does not run sprio"));
    }

    #[test]
    fn the_raw_tab_lists_everything_in_key_order() {
        let mut view = MetadataView::new();
        view.set_detail(
            Some(detail(&[("JobState", "RUNNING"), ("Account", "physics")])),
            None,
            true,
        );
        view.tabs.select(RAW);

        let report = text(&view.lines());
        // BTreeMap ordering: Account before JobState.
        assert!(report.find("Account").unwrap() < report.find("JobState").unwrap());
    }

    #[test]
    fn a_priority_bar_never_rounds_a_contribution_away() {
        assert_eq!(priority_bar(50, 100), "██████████░░░░░░░░░░");
        assert_eq!(priority_bar(100, 100), "████████████████████");
        // One part in a thousand still shows.
        assert_eq!(priority_bar(1, 1000).chars().next(), Some('█'));
        assert_eq!(priority_bar(0, 100), "");
        assert_eq!(priority_bar(5, 0), "");
    }
}
