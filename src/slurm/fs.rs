//! Reading files, locally or on the cluster.
//!
//! Job logs live wherever the job wrote them, which in remote mode is the far
//! side of the SSH session. Every read here therefore goes through the
//! [`CommandRunner`] when remote and straight to the filesystem when not.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

use super::transport::CommandRunner;

/// How many lines the inline log panels show.
pub const TAIL_LINES: usize = 500;

/// Never pull more than this off disk for one tail, however long the lines are.
///
/// A training log with a single 200 MB progress-bar "line" must not be read
/// whole just to show its end.
const TAIL_MAX_BYTES: u64 = 4 * 1024 * 1024;

/// How much to read per backwards step.
const TAIL_BLOCK: u64 = 64 * 1024;

/// Return the last `tail_lines` lines of a file, reading only its end.
///
/// Job logs routinely reach hundreds of megabytes on a shared filesystem, so
/// this seeks backwards in blocks instead of iterating the whole file — the cost
/// is the size of the tail, not the size of the log.
pub fn tail_file(path: &Path, tail_lines: usize) -> std::io::Result<String> {
    tail_file_capped(path, tail_lines, TAIL_MAX_BYTES)
}

/// [`tail_file`], with the byte cap injectable so it can be tested cheaply.
fn tail_file_capped(path: &Path, tail_lines: usize, max_bytes: u64) -> std::io::Result<String> {
    let mut file = File::open(path)?;
    let mut position = file.seek(SeekFrom::End(0))?;
    let mut data: Vec<u8> = Vec::new();

    while position > 0
        && count_newlines(&data) <= tail_lines
        && (data.len() as u64) < max_bytes
    {
        let step = TAIL_BLOCK.min(position);
        position -= step;
        file.seek(SeekFrom::Start(position))?;

        let mut block = vec![0_u8; step as usize];
        file.read_exact(&mut block)?;
        block.extend_from_slice(&data);
        data = block;
    }

    // We stopped on the byte cap rather than on a line boundary: the file has a
    // single line longer than the cap, and the user needs to know why the panel
    // looks truncated.
    let truncated = position > 0 && count_newlines(&data) <= tail_lines;

    let text = String::from_utf8_lossy(&data);
    let mut lines: Vec<&str> = split_lines_keepends(&text);
    // The first line is partial unless we reached the start of the file.
    if position > 0 && !lines.is_empty() {
        lines.remove(0);
    }

    let start = lines.len().saturating_sub(tail_lines);
    let mut out: String = lines[start..].concat();

    if truncated {
        let human = human_size(max_bytes);
        out.insert_str(
            0,
            &format!(
                "... (truncated: no line break in the last {human} — \
                 press 'l' to open the whole file in the pager)\n"
            ),
        );
    }

    Ok(out)
}

fn count_newlines(data: &[u8]) -> usize {
    data.iter().filter(|byte| **byte == b'\n').count()
}

/// A round size for the truncation banner.
fn human_size(bytes: u64) -> String {
    const MIB: u64 = 1024 * 1024;
    if bytes >= MIB {
        format!("{} MB", bytes / MIB)
    } else {
        format!("{} KB", bytes / 1024)
    }
}

/// Split text into lines, keeping their terminators.
///
/// Breaks after `\n`, and after a `\r` that does not begin a `\r\n` pair. The
/// carriage-return case matters: progress bars redraw with bare `\r`, and
/// treating a whole run of them as one line would make the log panel show a
/// single enormous row instead of the last few updates.
fn split_lines_keepends(text: &str) -> Vec<&str> {
    let bytes = text.as_bytes();
    let mut lines = Vec::new();
    let mut start = 0;

    for (index, byte) in bytes.iter().enumerate() {
        let is_break = match byte {
            b'\n' => true,
            b'\r' => bytes.get(index + 1) != Some(&b'\n'),
            _ => false,
        };
        if is_break {
            lines.push(&text[start..=index]);
            start = index + 1;
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }

    lines
}

/// Whether a file exists, locally or on the remote host.
pub async fn file_exists(runner: &dyn CommandRunner, path: &str) -> bool {
    if runner.is_remote() {
        runner.run(&["test", "-f", path]).await.code == 0
    } else {
        Path::new(path).is_file()
    }
}

/// Read the tail of a log file for the inline panels.
///
/// Never fails: every error becomes text in the panel, because a missing log is
/// the normal state of a job that has not started writing yet.
pub async fn read_log_file(
    runner: &dyn CommandRunner,
    path: Option<&str>,
    tail_lines: usize,
) -> String {
    let Some(path) = path.filter(|p| !p.is_empty()) else {
        return "(no log file path available)".to_string();
    };

    if runner.is_remote() {
        let lines = tail_lines.to_string();
        let output = runner.run(&["tail", "-n", &lines, path]).await;
        return if output.stdout.trim().is_empty() {
            format!("(file not found: {path})")
        } else {
            output.stdout
        };
    }

    let local = Path::new(path);
    if !local.is_file() {
        return format!("(file not found: {path})");
    }

    // Reading a large file is blocking work; keep it off the async runtime.
    let owned = local.to_path_buf();
    match tokio::task::spawn_blocking(move || tail_file(&owned, tail_lines)).await {
        Ok(Ok(text)) => text,
        Ok(Err(error)) => format!("(could not read {path}: {error})"),
        Err(error) => format!("(could not read {path}: {error})"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    /// Write `content` to a uniquely named file under the test temp directory.
    fn temp_file(name: &str, content: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("lazyslurm-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(name);
        let mut file = File::create(&path).unwrap();
        file.write_all(content.as_bytes()).unwrap();
        path
    }

    #[test]
    fn returns_the_last_lines() {
        let content: String = (0..1000).map(|i| format!("line {i}\n")).collect();
        let path = temp_file("last-lines.out", &content);
        let out = tail_file(&path, 3).unwrap();
        assert_eq!(out, "line 997\nline 998\nline 999\n");
    }

    #[test]
    fn handles_a_file_shorter_than_requested() {
        let path = temp_file("short.out", "a\nb\n");
        assert_eq!(tail_file(&path, 500).unwrap(), "a\nb\n");
    }

    #[test]
    fn handles_an_empty_file() {
        let path = temp_file("empty.out", "");
        assert_eq!(tail_file(&path, 500).unwrap(), "");
    }

    #[test]
    fn keeps_a_final_line_without_a_newline() {
        let path = temp_file("no-newline.out", "first\nlast line, no newline");
        assert_eq!(
            tail_file(&path, 2).unwrap(),
            "first\nlast line, no newline"
        );
    }

    #[test]
    fn never_splits_a_line_mid_way() {
        // Lines much longer than one 64 KiB read block.
        let content: String = (0..5).map(|i| format!("{i}:{}\n", "x".repeat(100_000))).collect();
        let path = temp_file("long-lines.out", &content);

        let out = tail_file(&path, 2).unwrap();
        let lines: Vec<&str> = out.lines().collect();
        assert_eq!(lines.len(), 2);
        assert!(lines[0].starts_with("3:"));
        assert!(lines[1].starts_with("4:"));
        assert!(lines.iter().all(|line| line.len() == 100_002));
    }

    #[test]
    fn caps_a_single_enormous_line() {
        // A log that is one giant line (progress bars) must still read fast.
        let path = temp_file("giant-line.out", &"x".repeat(2 * 1024 * 1024));
        let out = tail_file_capped(&path, 500, 128 * 1024).unwrap();
        assert!(out.contains("truncated"), "{}", &out[..80.min(out.len())]);
        assert!(out.len() < 200 * 1024);
    }

    #[test]
    fn treats_carriage_returns_as_line_breaks() {
        // Progress-bar output: three redraws on one physical line.
        let path = temp_file("progress.out", "start\r10%\r50%\r100%\n");
        let out = tail_file(&path, 2).unwrap();
        assert_eq!(out, "50%\r100%\n");
    }

    #[test]
    fn keeps_crlf_pairs_together() {
        let lines = split_lines_keepends("a\r\nb\r\n");
        assert_eq!(lines, vec!["a\r\n", "b\r\n"]);
    }

    #[tokio::test]
    async fn reports_a_missing_path() {
        let runner = crate::slurm::transport::LocalRunner;
        assert_eq!(
            read_log_file(&runner, None, 10).await,
            "(no log file path available)"
        );
    }

    #[tokio::test]
    async fn reports_a_missing_file() {
        let runner = crate::slurm::transport::LocalRunner;
        let out = read_log_file(&runner, Some("/nonexistent/lazyslurm.log"), 10).await;
        assert!(out.contains("file not found"));
    }
}
