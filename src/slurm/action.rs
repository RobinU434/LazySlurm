//! Commands that change something: cancel, edit, resubmit, fetch a script.
//!
//! Every action reports back as an [`Outcome`] rather than a `Result`, because
//! none of them is fatal: a failed `scancel` is a line in the command log, not
//! an error that unwinds the app.

use std::path::Path;

use crate::model::RunningJob;

use super::fs::file_exists;
use super::transport::CommandRunner;

/// What an action did, in a form the command log can print.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Outcome {
    pub success: bool,
    pub message: String,
}

impl Outcome {
    fn ok(message: impl Into<String>) -> Self {
        Self {
            success: true,
            message: message.into(),
        }
    }

    fn failed(message: impl Into<String>) -> Self {
        Self {
            success: false,
            message: message.into(),
        }
    }
}

/// A job property the editor can change.
///
/// Only fields Slurm accepts for a *pending* job are offered; a running job's
/// allocation is fixed, so the UI refuses to open the editor for one.
pub struct EditableField {
    /// Stable identifier used by the UI and by [`build_update_args`].
    pub key: &'static str,
    /// Human label.
    pub label: &'static str,
    /// The name `scontrol update` expects.
    pub scontrol_key: &'static str,
    /// Reads the job's current value, to prefill a single-job edit.
    pub current: fn(&RunningJob) -> &str,
}

/// The editable properties, in the order the editor shows them.
///
/// [`build_update_args`] also emits in this order, so the generated command line
/// is stable regardless of how the caller collected the changes.
pub const EDITABLE_FIELDS: &[EditableField] = &[
    EditableField {
        key: "time_limit",
        label: "Runtime",
        scontrol_key: "TimeLimit",
        current: |job| &job.time_limit,
    },
    EditableField {
        key: "partition",
        label: "Partition",
        scontrol_key: "Partition",
        current: |job| &job.partition,
    },
    EditableField {
        key: "nodes",
        label: "Nodes",
        scontrol_key: "NumNodes",
        current: |job| &job.nodes,
    },
    EditableField {
        key: "cpus",
        label: "CPUs",
        scontrol_key: "NumCPUs",
        current: |job| &job.cpus,
    },
    EditableField {
        key: "memory",
        label: "Memory/node",
        scontrol_key: "MinMemoryNode",
        current: |job| &job.memory,
    },
];

/// Convert a squeue-style memory string to the MB integer `scontrol` wants.
///
/// `40G` → `40960`, `4000M` → `4000`, `512` → `512`. Trailing `n`/`c` (squeue's
/// per-node/per-cpu marker) is stripped. Anything that does not parse is passed
/// through untouched, so Slurm reports the error rather than this function
/// silently inventing a number.
pub fn normalize_memory(value: &str) -> String {
    let trimmed = value.trim();
    let stripped = trimmed.trim_end_matches(['n', 'c']);
    if stripped.is_empty() {
        return trimmed.to_string();
    }

    let (number, factor) = match stripped.chars().last().map(|c| c.to_ascii_uppercase()) {
        Some('K') => (&stripped[..stripped.len() - 1], 1.0 / 1024.0),
        Some('M') => (&stripped[..stripped.len() - 1], 1.0),
        Some('G') => (&stripped[..stripped.len() - 1], 1024.0),
        Some('T') => (&stripped[..stripped.len() - 1], 1024.0 * 1024.0),
        _ => (stripped, 1.0),
    };

    match number.parse::<f64>() {
        Ok(parsed) => ((parsed * factor) as i64).to_string(),
        Err(_) => trimmed.to_string(),
    }
}

/// Turn `{field key: value}` into `Key=Value` arguments for `scontrol update`.
///
/// Empty values are skipped, so a blank input in the editor means "leave
/// unchanged" rather than "set to nothing".
pub fn build_update_args(updates: &[(&str, &str)]) -> Vec<String> {
    EDITABLE_FIELDS
        .iter()
        .filter_map(|field| {
            let value = updates
                .iter()
                .find(|(key, _)| *key == field.key)
                .map(|(_, value)| value.trim())
                .filter(|value| !value.is_empty())?;

            let value = if field.key == "memory" {
                normalize_memory(value)
            } else {
                value.to_string()
            };
            Some(format!("{}={}", field.scontrol_key, value))
        })
        .collect()
}

/// Index of the script path in an sbatch argument list.
///
/// The script is the last bare (non-flag) token, skipping any token that is the
/// value of a preceding separate-form option such as `--array 1-4` or `-J name`.
pub fn script_token_index(tokens: &[String]) -> Option<usize> {
    let mut skip_next = false;
    let mut found = None;

    for (index, token) in tokens.iter().enumerate() {
        if skip_next {
            skip_next = false;
            continue;
        }
        if token.starts_with('-') {
            // A separate-form option ("--array 1-4") consumes the next token;
            // the "=" form ("--array=1-4") carries its own value.
            skip_next = !token.contains('=');
            continue;
        }
        found = Some(index);
    }

    found
}

/// Cancel a job. `force` sends SIGKILL immediately.
pub async fn cancel_job(runner: &dyn CommandRunner, job_id: &str, force: bool) -> Outcome {
    let args: Vec<&str> = if force {
        vec!["scancel", "--signal=KILL", job_id]
    } else {
        vec!["scancel", job_id]
    };

    let output = runner.run(&args).await;
    if output.code == 0 {
        let kind = if force {
            "force-cancelled"
        } else {
            "cancelled"
        };
        Outcome::ok(format!("Job {job_id} {kind}."))
    } else {
        Outcome::failed(format!(
            "Failed to cancel job {job_id}: {}",
            output.stderr.trim()
        ))
    }
}

/// Apply property changes to a pending job via `scontrol update`.
pub async fn update_job(
    runner: &dyn CommandRunner,
    job_id: &str,
    updates: &[(&str, &str)],
) -> Outcome {
    let args = build_update_args(updates);
    if args.is_empty() {
        return Outcome::failed(format!("Job {job_id}: nothing to update."));
    }

    let job_arg = format!("jobid={job_id}");
    let mut argv: Vec<&str> = vec!["scontrol", "update", &job_arg];
    argv.extend(args.iter().map(String::as_str));

    let output = runner.run(&argv).await;
    if output.code == 0 {
        Outcome::ok(format!("Job {job_id} updated: {}", args.join(" ")))
    } else {
        Outcome::failed(format!(
            "Failed to update job {job_id}: {}",
            output.stderr.trim()
        ))
    }
}

/// What to do when a job's original batch script no longer exists.
#[derive(Debug, Clone, Copy)]
pub enum ScriptFallback<'a> {
    /// Do not check whether the script still exists — submit the command as-is.
    Unchecked,
    /// Check, and substitute this archived copy when the original is gone.
    /// `None` means no archived copy is available.
    Archived(Option<&'a Path>),
}

/// Resubmit a job using its original sbatch command.
///
/// `command` is either a script path (from scontrol `Command=`) or a full sbatch
/// command line (from `SubmitLine=`, e.g. `sbatch --array=1-4 job.sh`). The
/// leading `sbatch` is stripped so it is not passed as a script, and `--chdir`
/// is injected so the job runs where it originally did.
pub async fn resubmit_job(
    runner: &dyn CommandRunner,
    command: &str,
    work_dir: &str,
    fallback: ScriptFallback<'_>,
) -> Outcome {
    let Ok(mut tokens) = shell_words::split(command) else {
        return Outcome::failed("Resubmit failed: could not parse the submit command");
    };
    if tokens.is_empty() {
        return Outcome::failed("Resubmit failed: empty submit command");
    }
    // Drop a leading "sbatch" from a full SubmitLine.
    if tokens[0] == "sbatch" {
        tokens.remove(0);
    }

    let mut note = String::new();
    if let ScriptFallback::Archived(archived) = fallback {
        if let Some(index) = script_token_index(&tokens) {
            if !file_exists(runner, &tokens[index]).await {
                let Some(archived) = archived else {
                    return Outcome::failed(format!(
                        "Resubmit failed: script '{}' no longer exists and no archived \
                         copy is available",
                        tokens[index]
                    ));
                };
                if runner.is_remote() {
                    // The archive is local, but sbatch runs on the login node.
                    return Outcome::failed(format!(
                        "Resubmit failed: script '{}' no longer exists; the \
                         archived-script fallback is not supported in remote mode",
                        tokens[index]
                    ));
                }
                note = format!(
                    " (original script missing — submitted archived copy {})",
                    archived.display()
                );
                tokens[index] = archived.display().to_string();
            }
        }
    }

    let mut argv: Vec<&str> = vec!["sbatch"];
    if !work_dir.is_empty() {
        argv.extend(["--chdir", work_dir]);
    }
    argv.extend(tokens.iter().map(String::as_str));

    let output = runner.run(&argv).await;
    if output.code == 0 {
        Outcome::ok(format!("{}{note}", output.stdout.trim()))
    } else {
        Outcome::failed(format!("Resubmit failed: {}", output.stderr.trim()))
    }
}

/// Fetch a job's sbatch script text from Slurm.
///
/// Only works while the job is still in slurmctld — the same window as
/// `scontrol show job`, i.e. until `MinJobAge` seconds after the job ends.
///
/// Two things to know about this command:
///
/// - The trailing `-` makes it write to stdout. Without it, scontrol drops a
///   `slurm-<id>.sh` file into the current directory, which would litter the
///   user's cwd and land on the wrong host in remote mode.
/// - **The exit code is 0 even when retrieval fails** ("job script retrieval
///   failed" goes to stderr, stdout stays empty). So the exit code is
///   deliberately ignored; non-empty stdout is the only success test.
pub async fn get_batch_script(runner: &dyn CommandRunner, job_id: &str) -> Option<String> {
    let output = runner
        .run(&["scontrol", "write", "batch_script", job_id, "-"])
        .await;
    (!output.stdout.trim().is_empty()).then_some(output.stdout)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::slurm::transport::testing::StubRunner;
    use crate::slurm::transport::Output;
    use rstest::rstest;

    fn tokens(values: &[&str]) -> Vec<String> {
        values.iter().map(|v| (*v).to_string()).collect()
    }

    #[rstest]
    #[case("40G", "40960")]
    #[case("4000M", "4000")]
    #[case("512", "512")]
    // squeue's per-node marker
    #[case("40Gn", "40960")]
    // squeue's per-cpu marker
    #[case("2000Mc", "2000")]
    #[case("1T", "1048576")]
    #[case("", "")]
    // Passed through for Slurm to reject.
    #[case("garbage", "garbage")]
    fn normalizes_memory(#[case] value: &str, #[case] expected: &str) {
        assert_eq!(normalize_memory(value), expected);
    }

    #[test]
    fn builds_update_args_and_skips_blanks() {
        let args = build_update_args(&[
            ("time_limit", "2-00:00:00"),
            ("partition", "gpu"),
            ("nodes", ""), // blank -> unchanged
            ("cpus", "8"),
            ("memory", "40G"),
        ]);
        assert_eq!(
            args,
            vec![
                "TimeLimit=2-00:00:00",
                "Partition=gpu",
                "NumCPUs=8",
                "MinMemoryNode=40960",
            ]
        );
    }

    #[rstest]
    #[case(vec!["job.sh"], Some(0))]
    #[case(vec!["--array=1-4", "job.sh"], Some(1))]
    // A separate-form option's value is skipped.
    #[case(vec!["--array", "1-4", "job.sh"], Some(2))]
    #[case(vec!["-J", "myname", "job.sh"], Some(2))]
    #[case(vec!["--array=1-4", "-J", "n", "job.sh"], Some(3))]
    // Flags only, no script.
    #[case(vec!["--hold"], None)]
    #[case(vec![], None)]
    fn finds_the_script_token(#[case] argv: Vec<&str>, #[case] expected: Option<usize>) {
        assert_eq!(script_token_index(&tokens(&argv)), expected);
    }

    #[tokio::test]
    async fn resubmits_a_full_submit_line() {
        let runner = StubRunner::with_stdout("Submitted batch job 999");
        let outcome = resubmit_job(
            &runner,
            "sbatch --array=1-4 job.sh",
            "/work",
            ScriptFallback::Unchecked,
        )
        .await;

        assert!(outcome.success);
        // sbatch stripped, --chdir injected, original flags preserved.
        assert_eq!(
            runner.only_call(),
            vec!["sbatch", "--chdir", "/work", "--array=1-4", "job.sh"]
        );
    }

    #[tokio::test]
    async fn resubmits_a_bare_script() {
        let runner = StubRunner::with_stdout("Submitted batch job 999");
        resubmit_job(&runner, "/work/run.sh", "/work", ScriptFallback::Unchecked).await;
        assert_eq!(
            runner.only_call(),
            vec!["sbatch", "--chdir", "/work", "/work/run.sh"]
        );
    }

    #[tokio::test]
    async fn resubmits_without_a_work_dir() {
        let runner = StubRunner::with_stdout("Submitted batch job 999");
        resubmit_job(&runner, "sbatch job.sh", "", ScriptFallback::Unchecked).await;
        assert_eq!(runner.only_call(), vec!["sbatch", "job.sh"]);
    }

    #[tokio::test]
    async fn refuses_an_empty_submit_command() {
        let runner = StubRunner::with_stdout("");
        let outcome = resubmit_job(&runner, "", "/work", ScriptFallback::Unchecked).await;
        assert!(!outcome.success);
        assert!(outcome.message.to_lowercase().contains("empty"));
    }

    #[tokio::test]
    async fn falls_back_to_the_archived_script() {
        let runner = StubRunner::with_stdout("Submitted batch job 999");
        let archived = Path::new("/cache/123.sh");
        let outcome = resubmit_job(
            &runner,
            "sbatch --array=1-4 /gone/job.sh",
            "/work",
            ScriptFallback::Archived(Some(archived)),
        )
        .await;

        assert!(outcome.success);
        // The missing script token is replaced; flags and --chdir are untouched.
        assert_eq!(
            runner.only_call(),
            vec!["sbatch", "--chdir", "/work", "--array=1-4", "/cache/123.sh"]
        );
        assert!(outcome.message.contains("archived copy"));
    }

    #[tokio::test]
    async fn refuses_the_archive_fallback_in_remote_mode() {
        // The archive is local, but sbatch runs on the login node.
        let runner = StubRunner::new(|args| {
            // `test -f` on the login node: the script is gone.
            if args[0] == "test" {
                Output::failure("")
            } else {
                Output::default()
            }
        })
        .remote();
        let outcome = resubmit_job(
            &runner,
            "sbatch /gone/job.sh",
            "/work",
            ScriptFallback::Archived(Some(Path::new("/cache/123.sh"))),
        )
        .await;

        assert!(!outcome.success);
        assert!(outcome.message.contains("not supported in remote mode"));
    }

    #[tokio::test]
    async fn reports_when_no_archived_copy_exists() {
        let runner = StubRunner::with_stdout("");
        let outcome = resubmit_job(
            &runner,
            "sbatch /gone/job.sh",
            "/work",
            ScriptFallback::Archived(None),
        )
        .await;
        assert!(!outcome.success);
        assert!(outcome.message.contains("no archived copy"));
    }

    #[tokio::test]
    async fn unchecked_resubmit_never_looks_at_the_filesystem() {
        let runner = StubRunner::with_stdout("Submitted batch job 999");
        let outcome = resubmit_job(
            &runner,
            "sbatch /gone/job.sh",
            "/work",
            ScriptFallback::Unchecked,
        )
        .await;
        assert!(outcome.success);
        assert_eq!(
            runner.only_call(),
            vec!["sbatch", "--chdir", "/work", "/gone/job.sh"]
        );
    }

    #[tokio::test]
    async fn fetches_a_batch_script_to_stdout() {
        let runner = StubRunner::with_stdout("#!/bin/bash\necho hi\n");
        let text = get_batch_script(&runner, "123").await;
        assert_eq!(text.as_deref(), Some("#!/bin/bash\necho hi\n"));
        // The trailing "-" is what sends the script to stdout instead of a file.
        assert_eq!(
            runner.only_call(),
            vec!["scontrol", "write", "batch_script", "123", "-"]
        );
    }

    #[tokio::test]
    async fn treats_empty_script_output_as_failure_despite_exit_zero() {
        // scontrol exits 0 even when retrieval fails, so the code must not be trusted.
        let runner = StubRunner::new(|_| Output {
            stdout: String::new(),
            stderr: "job script retrieval failed: Invalid job id specified".into(),
            code: 0,
        });
        assert!(get_batch_script(&runner, "123").await.is_none());
    }

    #[tokio::test]
    async fn builds_the_scontrol_update_command() {
        let runner = StubRunner::with_stdout("");
        let outcome = update_job(&runner, "1234", &[("time_limit", "4:00:00")]).await;
        assert!(outcome.success);
        assert_eq!(
            runner.only_call(),
            vec!["scontrol", "update", "jobid=1234", "TimeLimit=4:00:00"]
        );
        assert!(outcome.message.contains("1234"));
    }

    #[tokio::test]
    async fn an_update_with_no_changes_never_calls_scontrol() {
        let runner = StubRunner::with_stdout("");
        let outcome = update_job(&runner, "1234", &[("partition", "   ")]).await;
        assert!(!outcome.success);
        assert!(runner.calls().is_empty());
        assert!(outcome.message.contains("nothing to update"));
    }

    #[tokio::test]
    async fn reports_an_update_failure() {
        let runner = StubRunner::new(|_| Output {
            stdout: String::new(),
            stderr: "Invalid partition name specified".into(),
            code: 1,
        });
        let outcome = update_job(&runner, "1234", &[("partition", "nope")]).await;
        assert!(!outcome.success);
        assert!(outcome.message.contains("Invalid partition name"));
    }

    #[tokio::test]
    async fn cancels_and_force_cancels() {
        let runner = StubRunner::with_stdout("");
        cancel_job(&runner, "123", false).await;
        cancel_job(&runner, "123", true).await;
        assert_eq!(
            runner.calls(),
            vec![
                vec!["scancel", "123"],
                vec!["scancel", "--signal=KILL", "123"],
            ]
        );
    }
}
