//! The upper-right panel: logs, live node activity, and the stats report.
//!
//! The stats tab is generated text rather than a table, so it is built here as
//! styled [`Line`]s. The Python builds the same report as a Rich-markup string;
//! spans are used instead because markup means escaping, and this report is full
//! of `[`-shaped things.

use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};

use crate::model::{format_bytes, format_duration, sizing_hint, JobStats};

use super::log_pane::LogPane;
use super::tabs::TabStrip;
use super::theme;

/// Above this fraction of the request, a resource was sized about right.
const EFFICIENCY_GOOD: f64 = 0.60;
/// Below this, it was clearly over-provisioned.
const EFFICIENCY_FAIR: f64 = 0.25;

/// The width of the little `▆▆▆▁▁▁▁▁` gauges.
const GAUGE_WIDTH: usize = 8;

/// The blocks a sparkline is drawn from.
const SPARK_CHARS: [char; 8] = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];

/// Samples collected while a job runs, for the stats tab's sparklines.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ResourceHistory {
    /// Peak resident memory, in bytes.
    pub memory: Vec<f64>,
    /// Core-equivalents busy over each interval.
    ///
    /// Genuinely CPU, unlike the Python, whose "CPU" series plots `AveRSS` —
    /// memory under a second label. See issue draft 09.
    pub cpu: Vec<f64>,
}

impl ResourceHistory {
    pub fn is_empty(&self) -> bool {
        self.memory.is_empty() && self.cpu.is_empty()
    }
}

/// The upper-right panel.
pub struct DetailView {
    tabs: TabStrip,
    pub stdout: LogPane,
    pub stderr: LogPane,
    pub cpu: LogPane,
    pub gpu: LogPane,
    stats: Option<JobStats>,
    history: ResourceHistory,
    stats_scroll: usize,
}

impl DetailView {
    /// Build the panel. `show_gpu` drops the GPU tab entirely when the user has
    /// turned GPU monitoring off, rather than showing an empty one.
    pub fn new(show_gpu: bool) -> Self {
        let mut tabs = vec!["stdout", "stderr", "cpu"];
        if show_gpu {
            tabs.push("gpu");
        }
        tabs.push("stats");

        Self {
            tabs: TabStrip::new(tabs),
            stdout: LogPane::new(),
            stderr: LogPane::new(),
            cpu: LogPane::new(),
            gpu: LogPane::new(),
            stats: None,
            history: ResourceHistory::default(),
            stats_scroll: 0,
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
    }

    pub fn select_tab(&mut self, name: &str) -> bool {
        self.tabs.select(name)
    }

    pub fn set_stats(&mut self, stats: Option<JobStats>) {
        self.stats = stats;
        self.stats_scroll = 0;
    }

    pub fn set_history(&mut self, history: ResourceHistory) {
        self.history = history;
    }

    /// Clear everything, for when no job is selected.
    pub fn clear(&mut self) {
        self.stdout.set_content("");
        self.stderr.set_content("");
        self.cpu.set_content("");
        self.gpu.set_content("");
        self.stats = None;
        self.history = ResourceHistory::default();
    }

    /// Scroll whichever pane is showing.
    pub fn scroll_by(&mut self, delta: isize) {
        match self.active_tab() {
            Some("stdout") => self.stdout.scroll_by(delta),
            Some("stderr") => self.stderr.scroll_by(delta),
            Some("cpu") => self.cpu.scroll_by(delta),
            Some("gpu") => self.gpu.scroll_by(delta),
            Some("stats") => {
                let target = self.stats_scroll as isize + delta;
                self.stats_scroll = target.max(0) as usize;
            }
            _ => {}
        }
    }

    pub fn stats_scroll(&self) -> usize {
        self.stats_scroll
    }

    /// The stats report, as styled lines.
    pub fn stats_lines(&self) -> Vec<Line<'static>> {
        let Some(stats) = &self.stats else {
            return vec![Line::from(Span::styled("No stats available", theme::dim()))];
        };

        let mut sections: Vec<Vec<Line>> = Vec::new();

        // Efficiency comes first: it is the only part that answers "was this
        // sized right?", which is the question worth asking of a finished job.
        sections.push(efficiency_lines(stats));

        let mut cpu = vec![heading("CPU")];
        field(&mut cpu, "Avg CPU Time", &stats.ave_cpu);
        field(&mut cpu, "Total CPU", &stats.total_cpu);
        field(&mut cpu, "Avg Frequency", &stats.ave_cpu_freq);
        field(&mut cpu, "Wall Time", &stats.elapsed);
        sections.push(cpu);

        let mut memory = vec![heading("Memory")];
        field(&mut memory, "Requested", &stats.req_mem);
        field(&mut memory, "Max RSS", &stats.max_rss);
        field(&mut memory, "Avg RSS", &stats.ave_rss);
        field(&mut memory, "Max VM Size", &stats.max_vm_size);
        field(&mut memory, "Avg VM Size", &stats.ave_vm_size);
        field(&mut memory, "Max RSS Node", &stats.max_rss_node);
        field(&mut memory, "Max RSS Task", &stats.max_rss_task);
        sections.push(memory);

        if !crate::model::job::is_missing(&stats.gpu_alloc) {
            let mut gpu = vec![heading("GPU")];
            field(&mut gpu, "Allocated", &stats.gpu_alloc);
            field(&mut gpu, "TRES", &stats.gpu_tres);
            sections.push(gpu);
        }

        let mut io = vec![heading("Disk I/O")];
        field(&mut io, "Avg Read", &stats.ave_disk_read);
        field(&mut io, "Max Read", &stats.max_disk_read);
        field(&mut io, "Avg Write", &stats.ave_disk_write);
        field(&mut io, "Max Write", &stats.max_disk_write);
        if io.len() > 1 {
            sections.push(io);
        }

        if !self.history.is_empty() {
            let mut lines = vec![heading("Resource History")];
            if !self.history.memory.is_empty() {
                lines.push(sparkline_line("Memory", &self.history.memory));
            }
            if !self.history.cpu.is_empty() {
                lines.push(sparkline_line("CPU", &self.history.cpu));
            }
            sections.push(lines);
        }

        let mut out: Vec<Line> = Vec::new();
        for section in sections {
            if !out.is_empty() {
                out.push(Line::raw(""));
            }
            out.extend(section);
        }
        out.push(Line::raw(""));
        out.push(Line::from(Span::styled(
            format!("Source: {}", stats.source.label()),
            theme::dim(),
        )));
        out
    }
}

/// A section heading.
fn heading(text: &str) -> Line<'static> {
    Line::from(Span::styled(
        text.to_string(),
        Style::new().add_modifier(Modifier::BOLD | Modifier::UNDERLINED),
    ))
}

/// Append `label: value`, unless Slurm did not report the value.
fn field(lines: &mut Vec<Line<'static>>, label: &str, value: &str) {
    if crate::model::job::is_missing(value) {
        return;
    }
    lines.push(Line::raw(format!("  {label:<14} {value}")));
}

/// The Efficiency block: used against requested, a gauge, and a sizing hint.
pub fn efficiency_lines(stats: &JobStats) -> Vec<Line<'static>> {
    let efficiency = stats.efficiency();
    let mut lines = vec![heading("Efficiency")];

    if !efficiency.has_any() {
        lines.push(Line::from(Span::styled(
            "  unavailable — Slurm no longer has accounting data for this job",
            theme::dim(),
        )));
        return lines;
    }

    if let Some(ratio) = efficiency.cpu {
        let used = if efficiency.cpu_used > 0.0 && efficiency.cpu_used < 0.1 {
            "<0.1".to_string()
        } else {
            format!("{:.1}", efficiency.cpu_used)
        };
        let note = (ratio < EFFICIENCY_FAIR).then(over_requested);
        lines.push(efficiency_row(
            "CPU",
            Some(ratio),
            &used,
            &format!("{} cores", efficiency.cpu_alloc),
            note,
        ));
    }

    if let Some(ratio) = efficiency.memory {
        let note = if efficiency.oom_risk() {
            Some(Span::styled(
                "  ← at the limit, risks OOM",
                Style::new().fg(ratatui::style::Color::Red),
            ))
        } else if ratio < EFFICIENCY_FAIR {
            Some(over_requested())
        } else {
            None
        };
        // A multi-node job is measured against its per-node request, so say so.
        let per_node = if efficiency.nnodes > 1 { "/node" } else { "" };
        lines.push(efficiency_row(
            "Memory",
            Some(ratio),
            &format_bytes(efficiency.mem_used),
            &format!("{}{per_node}", format_bytes(efficiency.mem_request)),
            note,
        ));
    }

    if efficiency.gpus > 0 {
        lines.push(Line::from(vec![
            Span::raw(format!(
                "  {:<9} {:>9} / {} allocated",
                "GPU", efficiency.gpus, efficiency.gpus
            )),
            Span::styled("      — utilisation is not recorded by Slurm", theme::dim()),
        ]));
    }

    if let Some(ratio) = efficiency.walltime {
        lines.push(efficiency_row(
            "Walltime",
            Some(ratio),
            &format_duration(efficiency.elapsed),
            &format_duration(efficiency.time_limit),
            None,
        ));
    }

    let hint = sizing_hint(&efficiency);
    if !hint.is_empty() {
        lines.push(Line::from(Span::styled(format!("  {hint}"), theme::dim())));
    }
    lines
}

fn over_requested() -> Span<'static> {
    Span::styled("  ← over-requested", theme::dim())
}

/// One `label used / asked pct gauge` row.
fn efficiency_row(
    label: &str,
    ratio: Option<f64>,
    used: &str,
    asked: &str,
    note: Option<Span<'static>>,
) -> Line<'static> {
    let Some(ratio) = ratio else {
        return Line::from(Span::styled(
            format!("  {label:<9} {used:>9} / {asked:<9}      —"),
            theme::dim(),
        ));
    };

    let style = efficiency_style(Some(ratio));
    // Never round a fraction of a percent up to "1%": a job that used half a
    // percent of its request should read as having used none of it.
    let percent = if ratio > 0.0 && ratio < 0.01 {
        "<1%".to_string()
    } else {
        format!("{:.0}%", ratio * 100.0)
    };

    let mut spans = vec![
        Span::raw(format!("  {label:<9} {used:>9} / {asked:<9} ")),
        Span::styled(format!("{percent:>4}"), style),
        Span::raw(" "),
        Span::styled(efficiency_gauge(Some(ratio)), style),
    ];
    if let Some(note) = note {
        spans.push(note);
    }
    Line::from(spans)
}

/// How a ratio should be coloured.
pub fn efficiency_style(ratio: Option<f64>) -> Style {
    let Some(ratio) = ratio else {
        return theme::dim();
    };
    if ratio >= 1.0 {
        // Used everything it asked for, so the next run may be capped.
        Style::new()
            .fg(ratatui::style::Color::Red)
            .add_modifier(Modifier::BOLD)
    } else if ratio >= EFFICIENCY_GOOD {
        Style::new().fg(ratatui::style::Color::Green)
    } else if ratio >= EFFICIENCY_FAIR {
        Style::new().fg(ratatui::style::Color::Yellow)
    } else {
        Style::new().fg(ratatui::style::Color::Red)
    }
}

/// `▆▆▆▁▁▁▁▁` — a compact gauge of one ratio.
pub fn efficiency_gauge(ratio: Option<f64>) -> String {
    let Some(ratio) = ratio else {
        return " ".repeat(GAUGE_WIDTH);
    };
    let filled = (ratio.min(1.0) * GAUGE_WIDTH as f64).round().max(0.0) as usize;
    let filled = filled.min(GAUGE_WIDTH);
    format!("{}{}", "▆".repeat(filled), "▁".repeat(GAUGE_WIDTH - filled))
}

/// Render a series as a sparkline, scaled to its own maximum.
pub fn sparkline(values: &[f64]) -> String {
    if values.is_empty() {
        return String::new();
    }
    let max = values.iter().copied().fold(f64::MIN, f64::max);
    if max <= 0.0 {
        return SPARK_CHARS[0].to_string().repeat(values.len());
    }
    values
        .iter()
        .map(|value| {
            let index = ((value / max) * 7.0) as usize;
            SPARK_CHARS[index.min(7)]
        })
        .collect()
}

fn sparkline_line(label: &str, values: &[f64]) -> Line<'static> {
    Line::from(vec![
        Span::raw(format!("  {label:<7} {}", sparkline(values))),
        Span::styled(format!("  ({} samples)", values.len()), theme::dim()),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::job::StatsSource;

    fn stats(build: impl FnOnce(&mut JobStats)) -> JobStats {
        let mut stats = JobStats::empty("123", StatsSource::Combined);
        build(&mut stats);
        stats
    }

    /// The plain text of a set of lines.
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
    fn the_gpu_tab_is_absent_when_gpu_monitoring_is_off() {
        assert_eq!(
            DetailView::new(false).tabs().tabs(),
            &["stdout", "stderr", "cpu", "stats"]
        );
        assert!(DetailView::new(true).tabs().tabs().contains(&"gpu"));
    }

    #[test]
    fn reports_when_there_is_nothing_to_report() {
        let view = DetailView::new(true);
        assert!(text(&view.stats_lines()).contains("No stats available"));
    }

    #[test]
    fn says_why_efficiency_is_missing_rather_than_showing_nothing() {
        let lines = efficiency_lines(&stats(|_| {}));
        assert!(text(&lines).contains("no longer has accounting data"));
    }

    #[test]
    fn reports_cpu_memory_and_walltime_efficiency() {
        let stats = stats(|s| {
            s.elapsed = "01:00:00".into();
            s.total_cpu = "02:00:00".into();
            s.alloc_cpus = 4;
            s.max_rss = "8G".into();
            s.req_mem = "16G".into();
            s.nnodes = 1;
            s.time_limit = "04:00:00".into();
        });

        let report = text(&efficiency_lines(&stats));
        assert!(report.contains("CPU"), "{report}");
        assert!(report.contains(" 50%"), "{report}"); // 2 of 4 core-hours
        assert!(report.contains("Memory"), "{report}");
        assert!(report.contains("8.0G / 16G"), "{report}");
        assert!(report.contains("Walltime"), "{report}");
        assert!(report.contains(" 25%"), "{report}"); // 1 hour of 4
    }

    #[test]
    fn a_tiny_fraction_never_rounds_up_to_one_percent() {
        let stats = stats(|s| {
            s.elapsed = "10:00:00".into();
            s.total_cpu = "00:01:00".into(); // 0.16% of one core-hour
            s.alloc_cpus = 1;
        });
        let report = text(&efficiency_lines(&stats));
        assert!(report.contains("<1%"), "{report}");
    }

    #[test]
    fn flags_a_job_that_used_everything_it_asked_for() {
        let stats = stats(|s| {
            s.max_rss = "16G".into();
            s.req_mem = "16G".into();
            s.nnodes = 1;
        });
        assert!(text(&efficiency_lines(&stats)).contains("risks OOM"));
    }

    #[test]
    fn marks_an_over_provisioned_resource_and_suggests_a_size() {
        let stats = stats(|s| {
            s.elapsed = "01:00:00".into();
            s.total_cpu = "00:06:00".into(); // 10% of one core
            s.alloc_cpus = 1;
            s.max_rss = "1G".into();
            s.req_mem = "64G".into();
            s.nnodes = 1;
        });

        let report = text(&efficiency_lines(&stats));
        assert!(report.contains("← over-requested"), "{report}");
        assert!(report.contains("next time try"), "{report}");
        assert!(report.contains("--mem=2G"), "{report}");
    }

    #[test]
    fn says_the_memory_request_is_per_node_for_a_multi_node_job() {
        let stats = stats(|s| {
            s.max_rss = "8G".into();
            s.req_mem = "32Gn".into();
            s.nnodes = 2;
            s.alloc_cpus = 8;
        });
        assert!(text(&efficiency_lines(&stats)).contains("/node"));
    }

    #[test]
    fn notes_that_slurm_does_not_record_gpu_utilisation() {
        let stats = stats(|s| {
            s.gpu_tres = "cpu=8,gres/gpu=2".into();
            s.elapsed = "01:00:00".into();
            s.total_cpu = "01:00:00".into();
            s.alloc_cpus = 1;
        });
        let report = text(&efficiency_lines(&stats));
        assert!(report.contains("GPU"), "{report}");
        assert!(report.contains("not recorded by Slurm"), "{report}");
    }

    #[test]
    fn the_gauge_fills_in_proportion() {
        assert_eq!(efficiency_gauge(Some(0.0)), "▁▁▁▁▁▁▁▁");
        assert_eq!(efficiency_gauge(Some(0.5)), "▆▆▆▆▁▁▁▁");
        assert_eq!(efficiency_gauge(Some(1.0)), "▆▆▆▆▆▆▆▆");
        // Over the request still fills, rather than overflowing.
        assert_eq!(efficiency_gauge(Some(2.0)), "▆▆▆▆▆▆▆▆");
        assert_eq!(efficiency_gauge(None).trim(), "");
    }

    #[test]
    fn sparklines_scale_to_their_own_maximum() {
        assert_eq!(sparkline(&[0.0, 50.0, 100.0]), "▁▄█");
        assert_eq!(sparkline(&[]), "");
        // An all-zero series is flat, not a division by zero.
        assert_eq!(sparkline(&[0.0, 0.0]), "▁▁");
    }

    #[test]
    fn omits_sections_slurm_reported_nothing_for() {
        let mut view = DetailView::new(true);
        view.set_stats(Some(stats(|s| {
            s.elapsed = "01:00:00".into();
            s.total_cpu = "01:00:00".into();
            s.alloc_cpus = 1;
        })));

        let report = text(&view.stats_lines());
        // No disk counters were reported, so there is no Disk I/O section.
        assert!(!report.contains("Disk I/O"), "{report}");
        assert!(report.contains("Source: combined"), "{report}");
    }

    #[test]
    fn shows_sparklines_once_there_is_history() {
        let mut view = DetailView::new(true);
        view.set_stats(Some(stats(|s| {
            s.elapsed = "01:00:00".into();
            s.total_cpu = "01:00:00".into();
            s.alloc_cpus = 1;
        })));
        view.set_history(ResourceHistory {
            memory: vec![1.0, 2.0, 3.0],
            cpu: vec![0.5, 0.9],
        });

        let report = text(&view.stats_lines());
        assert!(report.contains("Resource History"), "{report}");
        assert!(report.contains("(3 samples)"), "{report}");
        assert!(report.contains("(2 samples)"), "{report}");
    }

    #[test]
    fn scrolling_applies_to_whichever_tab_is_showing() {
        let mut view = DetailView::new(true);
        view.stdout.set_viewport(40, 5);
        view.stdout
            .set_content((0..20).map(|i| format!("line {i}\n")).collect::<String>());

        view.scroll_by(-3);
        assert!(!view.stdout.is_following());

        view.select_tab("stats");
        view.scroll_by(2);
        assert_eq!(view.stats_scroll(), 2);
        // The stdout pane was not touched by scrolling the stats tab.
        assert_eq!(view.stdout.scroll_offset(), 12);
    }
}
