//! What the app reacts to.
//!
//! Keystrokes, the refresh timer and finished Slurm queries all arrive on one
//! channel, so the app state is only ever touched from one place and needs no
//! locking.

use std::time::Duration;

use tokio::sync::mpsc::{self, UnboundedReceiver, UnboundedSender};

use crate::model::{
    CompletedJob, FairShare, JobDetail, JobStats, NodeInfo, PartitionInfo, PartitionJob,
    PriorityInfo, RunningJob, UsageRow,
};

/// How long the input reader waits before checking whether it should stop.
const POLL_INTERVAL: Duration = Duration::from_millis(100);

/// Something the app has to respond to.
#[derive(Debug)]
pub enum Event {
    Key(crossterm::event::KeyEvent),
    Resize,
    /// The refresh timer fired.
    Tick,
    /// A poll of the job lists finished.
    Jobs(Box<JobsLoaded>),
    /// A job's details finished loading.
    Detail(Box<DetailLoaded>),
    /// The partition monitor's list arrived.
    Partitions(Vec<PartitionInfo>),
    /// The jobs on one partition arrived.
    PartitionJobs {
        partition: String,
        jobs: Vec<PartitionJob>,
    },
    /// The nodes of one partition arrived.
    Nodes {
        partition: String,
        nodes: Vec<NodeInfo>,
    },
    /// The jobs on one node arrived.
    NodeJobs {
        node: String,
        jobs: Vec<PartitionJob>,
    },
    /// Account usage and fair share arrived.
    Usage(Box<UsageLoaded>),
    /// Live output from a compute node, for one detail tab.
    Live {
        tab: &'static str,
        content: String,
    },
    /// A batch script is ready to be shown.
    OpenScript(std::path::PathBuf),
    /// The cluster is asking for a password or a verification code.
    ///
    /// The answer goes back down `reply`; the SSH session is waiting on it.
    SshPrompt {
        question: String,
        secret: bool,
        reply: tokio::sync::oneshot::Sender<Option<String>>,
    },
    /// The SSH session finished connecting.
    SshConnected(Result<String, String>),
    /// Something to write to the command log.
    Log(String, Option<String>),
}

/// The account-usage panel's two queries, which arrive together.
#[derive(Debug)]
pub struct UsageLoaded {
    pub rows: Vec<UsageRow>,
    pub shares: Vec<FairShare>,
    pub accounting_available: bool,
}

/// The result of one poll.
#[derive(Debug)]
pub struct JobsLoaded {
    pub running: Vec<RunningJob>,
    pub completed: Vec<CompletedJob>,
    pub partitions: Vec<String>,
}

/// Everything the right-hand panels need about one job.
#[derive(Debug)]
pub struct DetailLoaded {
    /// Which selection asked for this. Anything older is dropped on arrival.
    pub generation: u64,
    pub job_id: String,
    pub detail: Option<JobDetail>,
    pub stdout: String,
    pub stderr: String,
    pub stats: Option<JobStats>,
    pub priority: Option<PriorityInfo>,
    pub sprio_available: bool,
}

/// The sending half of the event channel.
pub type Sender = UnboundedSender<Event>;

/// The app's single inbox.
pub struct Events {
    receiver: UnboundedReceiver<Event>,
    sender: Sender,
}

impl Events {
    /// Open the channel and start reading the keyboard.
    pub fn start() -> Self {
        let (sender, receiver) = mpsc::unbounded_channel();
        spawn_input_reader(sender.clone());
        Self { receiver, sender }
    }

    /// A handle for tasks that need to report back.
    pub fn sender(&self) -> Sender {
        self.sender.clone()
    }

    /// Wait for the next event.
    pub async fn next(&mut self) -> Option<Event> {
        self.receiver.recv().await
    }

    /// Start the refresh timer, if auto-refresh is enabled.
    pub fn start_ticker(&self, interval: Duration) {
        let sender = self.sender.clone();
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(interval);
            // The first tick fires immediately; the initial poll is explicit, so
            // skip it rather than polling twice at startup.
            ticker.tick().await;
            loop {
                ticker.tick().await;
                if sender.send(Event::Tick).is_err() {
                    break; // The app has gone.
                }
            }
        });
    }
}

/// Read the keyboard on a blocking thread and forward what it sees.
///
/// Blocking rather than async: crossterm's event stream needs an extra feature
/// and a reactor, and a dedicated thread doing a 100 ms poll is both simpler and
/// easier to reason about at this scale.
fn spawn_input_reader(sender: Sender) {
    tokio::task::spawn_blocking(move || loop {
        match crossterm::event::poll(POLL_INTERVAL) {
            Ok(true) => match crossterm::event::read() {
                Ok(crossterm::event::Event::Key(key)) => {
                    // Windows reports both press and release; only one is a
                    // keystroke. Harmless on Unix, where only presses arrive.
                    if key.kind == crossterm::event::KeyEventKind::Press
                        && sender.send(Event::Key(key)).is_err()
                    {
                        break;
                    }
                }
                Ok(crossterm::event::Event::Resize(_, _)) => {
                    if sender.send(Event::Resize).is_err() {
                        break;
                    }
                }
                Ok(_) => {}
                Err(_) => break,
            },
            Ok(false) => {
                // Nothing typed. Check the channel is still live so this thread
                // does not outlive the app.
                if sender.is_closed() {
                    break;
                }
            }
            Err(_) => break,
        }
    });
}
