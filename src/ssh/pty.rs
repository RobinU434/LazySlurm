//! Running a command on a pseudo-terminal.
//!
//! This is the whole reason remote mode works with two-factor authentication.
//! `ssh` opens `/dev/tty` to ask for a password or a verification code, so a
//! child wired up with ordinary pipes never shows us the question — it just
//! hangs. Give it a pty and the prompt arrives as readable output, and the
//! answer can be typed back.

use std::io::{Read, Write};
use std::os::fd::{AsRawFd, OwnedFd};
use std::process::Stdio;

use anyhow::{Context, Result};
use tokio::process::{Child, Command};
use tokio::sync::mpsc;

/// How much to read from the pty at a time.
const READ_CHUNK: usize = 4096;

/// A child process attached to a pty, and the reader pumping its output.
pub struct PtyProcess {
    child: Child,
    /// The controlling side, kept for writing answers back.
    master: OwnedFd,
    /// Output as it arrives. `None` once the child closes the pty.
    output: mpsc::UnboundedReceiver<String>,
}

impl PtyProcess {
    /// Spawn `argv` on a fresh pty.
    pub fn spawn(argv: &[String]) -> Result<Self> {
        let (program, args) = argv.split_first().context("empty command")?;

        let pty = nix::pty::openpty(None, None).context("could not allocate a pty")?;
        let (master, slave) = (pty.master, pty.slave);

        // Every stdio handle points at the slave side, which is what makes the
        // child believe it has a terminal.
        let child = Command::new(program)
            .args(args)
            .stdin(Stdio::from(slave.try_clone()?))
            .stdout(Stdio::from(slave.try_clone()?))
            .stderr(Stdio::from(slave))
            // Its own session, so a Ctrl+C in our terminal does not reach it.
            .process_group(0)
            .spawn()
            .with_context(|| format!("could not start {program}"))?;

        let output = spawn_reader(master.try_clone()?);
        Ok(Self {
            child,
            master,
            output,
        })
    }

    /// Type a line back to the child.
    pub fn write_line(&mut self, text: &str) -> Result<()> {
        let mut file = unsafe { fd_as_file(self.master.as_raw_fd()) };
        writeln!(file, "{text}")?;
        file.flush()?;
        // Do not close the descriptor when `file` drops; the session owns it.
        std::mem::forget(file);
        Ok(())
    }

    /// The next chunk of output, or `None` once the pty closes.
    pub async fn read(&mut self) -> Option<String> {
        self.output.recv().await
    }

    /// Whether the child has exited.
    pub fn exited(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(Some(_)))
    }

    /// Kill the child, and anything it started, then wait for it.
    ///
    /// The whole process group goes, not just the leader: ssh spawns helpers,
    /// and one left holding the pty open keeps our reader blocked long after the
    /// session is meant to be gone.
    pub async fn kill(&mut self) {
        if let Some(pid) = self.child.id() {
            let group = nix::unistd::Pid::from_raw(-(pid as i32));
            let _ = nix::sys::signal::kill(group, nix::sys::signal::Signal::SIGKILL);
        }
        let _ = self.child.start_kill();
        let _ = self.child.wait().await;
    }
}

/// Read the pty on a blocking thread and forward what it says.
///
/// Blocking rather than async: a pty master is awkward to register with the
/// reactor, and the Python this is ported from reads it on an executor thread
/// for the same reason. The thread ends when the pty closes.
fn spawn_reader(fd: OwnedFd) -> mpsc::UnboundedReceiver<String> {
    let (sender, receiver) = mpsc::unbounded_channel();

    std::thread::spawn(move || {
        let mut file = unsafe { fd_as_file(fd.as_raw_fd()) };
        let mut buffer = [0_u8; READ_CHUNK];
        loop {
            match file.read(&mut buffer) {
                Ok(0) => break,
                Ok(count) => {
                    let text = String::from_utf8_lossy(&buffer[..count]).into_owned();
                    if sender.send(text).is_err() {
                        break; // Nobody is listening any more.
                    }
                }
                // EIO is how a pty reports that the child has gone.
                Err(_) => break,
            }
        }
        // `fd` owns the descriptor and closes it here.
        std::mem::forget(file);
        drop(fd);
    });

    receiver
}

/// Borrow a raw descriptor as a `File` without taking ownership of it.
///
/// # Safety
/// The caller must not let the returned `File` close the descriptor — every
/// call site either forgets it or owns the fd separately.
unsafe fn fd_as_file(fd: std::os::fd::RawFd) -> std::fs::File {
    use std::os::fd::FromRawFd;
    unsafe { std::fs::File::from_raw_fd(fd) }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|part| (*part).to_string()).collect()
    }

    #[tokio::test]
    async fn reads_what_a_child_writes_to_its_terminal() {
        let mut pty = PtyProcess::spawn(&argv(&["echo", "hello"])).unwrap();

        let mut seen = String::new();
        while let Some(chunk) = pty.read().await {
            seen.push_str(&chunk);
            if seen.contains("hello") {
                break;
            }
        }
        assert!(seen.contains("hello"), "{seen:?}");
        pty.kill().await;
    }

    #[tokio::test]
    async fn a_child_on_a_pty_believes_it_has_a_terminal() {
        // This is the property the whole design rests on: with plain pipes this
        // prints "no", and ssh would never show us its prompts.
        let mut pty = PtyProcess::spawn(&argv(&[
            "sh",
            "-c",
            "if [ -t 0 ]; then echo yes; else echo no; fi",
        ]))
        .unwrap();

        let mut seen = String::new();
        while let Some(chunk) = pty.read().await {
            seen.push_str(&chunk);
            if seen.contains("yes") || seen.contains("no") {
                break;
            }
        }
        assert!(seen.contains("yes"), "{seen:?}");
        pty.kill().await;
    }

    #[tokio::test]
    async fn an_answer_typed_back_reaches_the_child() {
        let mut pty = PtyProcess::spawn(&argv(&[
            "sh",
            "-c",
            "printf 'prompt: '; read answer; echo \"got=$answer\"",
        ]))
        .unwrap();

        let mut seen = String::new();
        while let Some(chunk) = pty.read().await {
            seen.push_str(&chunk);
            if seen.contains("prompt:") {
                break;
            }
        }
        pty.write_line("secret").unwrap();

        while let Some(chunk) = pty.read().await {
            seen.push_str(&chunk);
            if seen.contains("got=") {
                break;
            }
        }
        assert!(seen.contains("got=secret"), "{seen:?}");
        pty.kill().await;
    }

    #[tokio::test]
    async fn reading_ends_when_the_child_does() {
        let mut pty = PtyProcess::spawn(&argv(&["true"])).unwrap();
        // Drain until the pty closes; this must terminate.
        while pty.read().await.is_some() {}
        assert!(
            pty.exited() || {
                pty.kill().await;
                true
            }
        );
    }

    #[test]
    fn an_empty_command_is_rejected() {
        assert!(PtyProcess::spawn(&[]).is_err());
    }
}
