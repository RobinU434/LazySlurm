//! Watching a compute node while a job runs on it.
//!
//! Unlike everything else in this module, these do not go through Slurm at all —
//! they reach the node itself. That means two different routes:
//!
//! - **Local mode**: SSH straight to the node, multiplexed through a control
//!   socket so a poll every few seconds is one handshake rather than many.
//! - **Remote mode**: the hop is made *from the login node*, inside the session
//!   that already exists. A local `ProxyJump` would open a second connection and
//!   trigger two-factor authentication again.

use std::path::PathBuf;

use super::parse::first_node;
use super::query::Slurm;
use super::transport::quote_argv;

/// How long to wait for a node to answer before giving up.
const SSH_TIMEOUT_SECONDS: u64 = 8;

/// Options for the local-mode hop to a compute node.
///
/// Multiplexing matters here: the first call opens a master connection and the
/// rest reuse it, turning many handshakes per poll into one.
fn local_ssh_options() -> Vec<String> {
    let mut options = vec![
        "-o".into(),
        "StrictHostKeyChecking=no".into(),
        "-o".into(),
        "ConnectTimeout=3".into(),
        "-o".into(),
        "BatchMode=yes".into(),
    ];

    // Created at the point of use rather than at startup: nothing should touch
    // ~/.ssh just because the program was launched.
    if let Some(dir) = control_dir() {
        options.extend([
            "-o".into(),
            "ControlMaster=auto".into(),
            "-o".into(),
            "ControlPersist=60s".into(),
            "-o".into(),
            format!("ControlPath={}/%C", dir.display()),
        ]);
    }
    options
}

/// The multiplexing socket directory, created if it does not exist.
fn control_dir() -> Option<PathBuf> {
    let home = std::env::var_os("HOME")?;
    let dir = PathBuf::from(home).join(".ssh").join("cm-lazyslurm");

    std::fs::create_dir_all(&dir).ok()?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700));
    }
    Some(dir)
}

/// Options for the login-node → compute-node hop in remote mode.
///
/// That inner ssh runs on the cluster, where nobody can answer a prompt, so
/// `BatchMode` makes it fail fast instead of hanging.
const NODE_SSH_OPTIONS: &[&str] = &[
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "ConnectTimeout=3",
    "-o",
    "BatchMode=yes",
];

/// Run a command on a compute node, whichever way round we are.
async fn on_node(slurm: &Slurm, node: &str, command: &str) -> Option<String> {
    if slurm.config().is_remote() {
        let hop = format!(
            "ssh {} {} {}",
            quote_argv(NODE_SSH_OPTIONS),
            shell_words::quote(node),
            shell_words::quote(command),
        );
        let output = slurm.runner().run(&["sh", "-c", &hop]).await;
        return (output.code == 0 && !output.stdout.trim().is_empty()).then_some(output.stdout);
    }

    let options = local_ssh_options();
    let mut args: Vec<&str> = vec!["ssh"];
    args.extend(options.iter().map(String::as_str));
    args.extend([node, command]);

    let output = tokio::time::timeout(
        std::time::Duration::from_secs(SSH_TIMEOUT_SECONDS),
        slurm.runner().run(&args),
    )
    .await
    .ok()?;

    (output.code == 0 && !output.stdout.trim().is_empty()).then_some(output.stdout)
}

/// A top-like process listing from the node a job is running on.
pub async fn node_processes(slurm: &Slurm, node_spec: &str, user: &str) -> String {
    if !crate::model::job::has_node(node_spec) {
        return "No node assigned".to_string();
    }
    let node = first_node(node_spec);
    let user = if user.is_empty() {
        slurm.config().effective_user()
    } else {
        user.to_string()
    };

    let command = format!(
        "ps -u {} -o pid,%cpu,%mem,rss:10,vsz:10,etime,comm --sort=-%cpu --no-headers \
         2>/dev/null | head -30",
        shell_words::quote(&user)
    );

    let Some(output) = on_node(slurm, &node, &command).await else {
        return format!("Could not reach {node} (SSH failed)");
    };

    let header = format!(
        "{:>7}  {:>5}  {:>5}  {:>10}  {:>10}  {:>12}  COMMAND",
        "PID", "%CPU", "%MEM", "RSS", "VSZ", "ELAPSED"
    );
    format!("Node: {node}\n\n{header}\n{}\n{output}", "-".repeat(72))
}

/// `nvidia-smi` for the GPUs a job actually holds.
///
/// Two strategies, in order:
///
/// 1. `srun --overlap --jobid=<id>` runs inside the job's cgroup, so Slurm's own
///    `CUDA_VISIBLE_DEVICES` restricts what nvidia-smi can see — exactly the
///    job's GPUs and no others.
/// 2. Failing that, SSH to the node and run it directly. That shows *every* GPU
///    on the node, so the output says so rather than implying they are all the
///    job's.
pub async fn gpu_status(slurm: &Slurm, node_spec: &str, job_id: &str) -> String {
    if !crate::model::job::has_node(node_spec) {
        return "No node assigned".to_string();
    }
    let node = first_node(node_spec);

    if !job_id.is_empty() {
        let jobid = format!("--jobid={job_id}");
        let output = slurm
            .runner()
            .run(&[
                "srun",
                "--overlap",
                &jobid,
                "bash",
                "-c",
                "echo CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES; \
                 nvidia-smi 2>/dev/null || echo 'nvidia-smi not available'",
            ])
            .await;

        if output.is_useful() {
            let text = output.stdout.trim_end().to_string();
            let (devices, body) = match text.strip_prefix("CUDA_VISIBLE_DEVICES=") {
                Some(rest) => {
                    let (line, body) = rest.split_once('\n').unwrap_or((rest, ""));
                    (Some(line.trim().to_string()), body.to_string())
                }
                None => (None, text),
            };

            let mut header = format!("Node: {node}");
            if let Some(devices) = devices.filter(|d| !d.is_empty()) {
                header.push_str(&format!("  (CUDA_VISIBLE_DEVICES={devices})"));
            }
            return format!("{header}\n\n{body}");
        }
    }

    let command = "nvidia-smi 2>/dev/null || echo 'nvidia-smi not available on this node'";
    let Some(output) = on_node(slurm, &node, command).await else {
        return format!("Could not reach {node}");
    };

    let mut header = format!("Node: {node}");
    if !job_id.is_empty() {
        header.push_str("  (showing all GPUs — srun --overlap failed, falling back to SSH)");
    }
    format!("{header}\n\n{output}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use crate::slurm::transport::testing::StubRunner;
    use crate::slurm::transport::Output;
    use std::sync::Arc;

    fn local(runner: StubRunner) -> Slurm {
        Slurm::new(Box::new(runner), Config::default())
    }

    fn remote(runner: Arc<StubRunner>) -> Slurm {
        Slurm::new(
            Box::new(runner),
            Config {
                remote: "me@login.hpc.edu".into(),
                user: "rvy895".into(),
                ..Config::default()
            },
        )
    }

    #[tokio::test]
    async fn reports_when_a_job_has_no_node_yet() {
        let slurm = local(StubRunner::with_stdout(""));
        assert_eq!(node_processes(&slurm, "", "me").await, "No node assigned");
        assert_eq!(gpu_status(&slurm, "(null)", "1").await, "No node assigned");
    }

    #[tokio::test]
    async fn asks_only_the_first_node_of_a_multi_node_job() {
        let runner = Arc::new(StubRunner::with_stdout("1234 10.0 2.0 ...\n"));
        let slurm = Slurm::new(Box::new(runner.clone()), Config::default());

        let output = node_processes(&slurm, "gpu[01-04]", "rvy895").await;
        assert!(output.starts_with("Node: gpu01"), "{output}");

        let call = runner.only_call();
        assert_eq!(call[0], "ssh");
        assert!(call.contains(&"gpu01".to_string()));
        assert!(call.last().unwrap().contains("ps -u rvy895"));
    }

    #[tokio::test]
    async fn a_process_listing_gets_a_header() {
        let slurm = local(StubRunner::with_stdout("1234 10.0 2.0\n"));
        let output = node_processes(&slurm, "gpu01", "rvy895").await;

        assert!(output.contains("PID"), "{output}");
        assert!(output.contains("COMMAND"), "{output}");
        assert!(output.contains("1234"), "{output}");
    }

    #[tokio::test]
    async fn reports_an_unreachable_node() {
        let slurm = local(StubRunner::new(|_| Output::failure("connection refused")));
        let output = node_processes(&slurm, "gpu01", "rvy895").await;
        assert!(output.contains("Could not reach gpu01"), "{output}");
    }

    #[tokio::test]
    async fn remote_mode_hops_from_the_login_node() {
        // A local ProxyJump would open a second connection and ask for the
        // verification code again, so the hop happens on the cluster.
        let runner = Arc::new(StubRunner::with_stdout("1234 10.0 2.0\n").remote());
        let slurm = remote(runner.clone());

        node_processes(&slurm, "gpu01", "rvy895").await;

        let call = runner.only_call();
        assert_eq!(call[0], "sh");
        let command = call.last().unwrap();
        assert!(command.starts_with("ssh "), "{command}");
        assert!(command.contains("BatchMode=yes"), "{command}");
        assert!(command.contains("gpu01"), "{command}");
    }

    #[tokio::test]
    async fn gpu_status_prefers_the_jobs_own_cgroup() {
        let runner = Arc::new(StubRunner::new(|args| {
            if args[0] == "srun" {
                return Output {
                    stdout: "CUDA_VISIBLE_DEVICES=0,1\nGPU 0: A100\nGPU 1: A100\n".into(),
                    stderr: String::new(),
                    code: 0,
                };
            }
            Output::failure("should not have been reached")
        }));
        let slurm = Slurm::new(Box::new(runner.clone()), Config::default());

        let output = gpu_status(&slurm, "gpu01", "4815").await;

        assert!(output.contains("CUDA_VISIBLE_DEVICES=0,1"), "{output}");
        assert!(output.contains("GPU 0: A100"), "{output}");
        // srun answered, so no SSH was needed.
        assert_eq!(runner.calls().len(), 1);
        assert_eq!(runner.only_call()[0], "srun");
    }

    #[tokio::test]
    async fn gpu_status_falls_back_to_ssh_and_says_so() {
        let runner = Arc::new(StubRunner::new(|args| {
            if args[0] == "srun" {
                return Output::failure("srun: unable to attach");
            }
            Output {
                stdout: "GPU 0: A100\nGPU 1: A100\n".into(),
                stderr: String::new(),
                code: 0,
            }
        }));
        let slurm = Slurm::new(Box::new(runner.clone()), Config::default());

        let output = gpu_status(&slurm, "gpu01", "4815").await;

        // The output shows every GPU on the node, so it must not imply
        // otherwise.
        assert!(output.contains("showing all GPUs"), "{output}");
        assert!(output.contains("GPU 0: A100"), "{output}");
        assert_eq!(runner.calls().len(), 2);
    }

    #[tokio::test]
    async fn gpu_status_without_a_job_id_goes_straight_to_ssh() {
        let runner = Arc::new(StubRunner::with_stdout("GPU 0: A100\n"));
        let slurm = Slurm::new(Box::new(runner.clone()), Config::default());

        let output = gpu_status(&slurm, "gpu01", "").await;

        assert_eq!(runner.only_call()[0], "ssh");
        // With no job to attribute them to, there is nothing to warn about.
        assert!(!output.contains("showing all GPUs"), "{output}");
    }
}
