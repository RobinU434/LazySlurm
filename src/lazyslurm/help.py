"""What the help screen shows, per panel.

The help used to be one hand-written block listing every key, which drifted from
the bindings twice. It is data here instead, so `tests/test_help.py` can assert
both directions: every binding a panel declares is documented, and nothing is
documented that no longer exists.

`keys` holds the Textual key names an entry documents, which is what the test
matches against `BINDINGS`. An entry with `keys=()` documents behaviour that is
not a declared binding — Enter on a DataTable row, for instance, arrives as
`RowSelected` — and is listed in IMPLICIT below so it is an explicit decision
rather than an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Context ids. The app maps the focused panel or the current screen onto one.
JOBS = "jobs"
DETAIL = "detail"
METADATA = "metadata"
# The filter bar has no context of its own: while its Input has focus, "?" is a
# character the user is typing, so its keys live under JOBS instead.
PARTITIONS = "partitions"
NODES = "nodes"
USAGE = "usage"


@dataclass(frozen=True)
class Key:
    """One documented shortcut."""

    display: str          # how it is written for a human: "Shift+C", "[ / ]"
    text: str             # what it does
    keys: tuple[str, ...] = ()   # Textual key names, () when handled implicitly


@dataclass(frozen=True)
class Panel:
    """A panel or screen, and the keys that only mean something inside it."""

    context: str
    title: str
    subtitle: str = ""
    keys: tuple[Key, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)


# Keys documented without a Binding, because a widget or Textual handles them.
IMPLICIT: dict[str, str] = {
    "up/down in a table": "DataTable cursor movement",
    "enter on a job row": "DataTable RowSelected — expands an array group",
    "up/down in the usage table": "DataTable cursor movement",
}


# --- keys that work anywhere in the app ------------------------------------

GLOBAL: tuple[Key, ...] = (
    Key("?", "this help — it follows the panel you are in", ("question_mark",)),
    Key("/", "filter the job tables", ("slash",)),
    Key("p", "partition monitor", ("p",)),
    Key("Shift+U", "account usage and fair share", ("U",)),
    Key("r", "refresh now", ("r",)),
    Key(",", "edit the config file in your editor", ("comma",)),
    Key("Tab / Shift+Tab", "move between the panels on the right", ("tab", "shift+tab")),
    Key("Left / Right", "move between the panels on the right", ("left", "right")),
    Key("q", "quit (on a full-screen panel: back)", ("q",)),
)


# --- per-panel keys --------------------------------------------------------

PANELS: tuple[Panel, ...] = (
    Panel(
        context=JOBS,
        title="Job tables",
        subtitle="Active Jobs / Terminated Jobs",
        keys=(
            Key("Up / Down", "move through the list (wraps between the two tables)"),
            Key("Enter", "expand or collapse a job array (▸ row)"),
            Key("m", "bookmark — ★ rows pin to the top", ("m",)),
            Key("c", "cancel the selected job(s), with a confirmation", ("c",)),
            Key("Shift+C", "force cancel — SIGKILL, no confirmation", ("shift+c",)),
            Key("Ctrl+V", "multi-select mode; Up/Down extends the range", ("ctrl+v",)),
            Key("u", "edit a pending job: runtime, partition, nodes, CPUs, memory", ("u",)),
            Key("s", "resubmit a terminated job from its original script", ("s",)),
            Key("b", "view the job's sbatch script, read-only", ("b",)),
            Key("o", "SSH to the job's compute node (suspends the TUI)", ("o",)),
        ),
        notes=(
            "Multi-select applies c, Shift+C and u to every selected job.",
            "/ opens the filter bar (Escape closes and clears it). Plain words match",
            "id, name and partition; key:value terms are ANDed —",
            "  state:pend  part:gpu  name:train  id:4815  gpu:>=2",
            "Aliases: st: s: · partition: p: · n: · job: · gpus: gres:. An unknown",
            "key is searched as plain text, so nothing you type can break the filter.",
        ),
    ),
    Panel(
        context=DETAIL,
        title="Job Details",
        subtitle="stdout · stderr · cpu · gpu · stats",
        keys=(
            Key("[ / ]", "previous / next tab",
                ("left_square_bracket", "right_square_bracket")),
            Key("l", "open the active log in the pager — / searches, F follows", ("l",)),
            Key("e", "open stdout in your editor", ("e",)),
            Key("Shift+E", "open stderr in your editor", ("shift+e",)),
        ),
        notes=(
            "The log tabs show the last 500 lines; press l to read a whole large log.",
            "cpu and gpu are live from the node and refresh while the tab is open.",
            "stats opens with Efficiency: used against requested, and a sizing hint.",
        ),
    ),
    Panel(
        context=METADATA,
        title="Job Metadata",
        subtitle="Resources · Submission · Pending · Raw",
        keys=(
            Key("( / )", "previous / next tab",
                ("left_parenthesis", "right_parenthesis")),
        ),
        notes=(
            "The Pending tab appears only while a job is waiting: why it is not "
            "running, when Slurm expects to start it, and its priority breakdown.",
        ),
    ),
    Panel(
        context=PARTITIONS,
        title="Partition monitor",
        subtitle="opened with p",
        keys=(
            Key("Up / Down", "move between partitions; the job list follows"),
            Key("Enter", "show the individual nodes of this partition", ("enter",)),
            Key("Tab", "switch between the partition and job tables",
                ("tab", "shift+tab")),
            Key("r", "refresh now", ("r",)),
            Key("Escape / p / q", "back to the job view", ("escape", "p", "q")),
        ),
        notes=(
            "A/I/O/T is allocated / idle / other / total, for nodes and for CPUs.",
            "The job list is every user's jobs, not just yours.",
        ),
    ),
    Panel(
        context=NODES,
        title="Node view",
        subtitle="Enter on a partition",
        keys=(
            Key("Up / Down", "move between nodes"),
            Key("Tab", "switch panel", ("tab", "shift+tab")),
            Key("r", "refresh now", ("r",)),
            Key("Escape / q", "back to the partition monitor", ("escape", "q")),
        ),
    ),
    Panel(
        context=USAGE,
        title="Account usage",
        subtitle="opened with Shift+U",
        keys=(
            Key("w", "cycle the window: this month → last 30 days → this year", ("w",)),
            Key("r", "refetch", ("r",)),
            Key("Escape / Shift+U / q", "back to the job view", ("escape", "U", "q")),
        ),
        notes=(
            "The fair-share factor is what decides queue order: above 0.5 you are "
            "under your share and get boosted, below it you are over and get pushed back.",
        ),
    ),
)

_BY_CONTEXT = {panel.context: panel for panel in PANELS}


def panel_for(context: str) -> Panel:
    """The panel a context id refers to, defaulting to the job tables."""
    return _BY_CONTEXT.get(context, _BY_CONTEXT[JOBS])


def _escape(text: str) -> str:
    """Escape Rich markup in help data — `[ / ]` is a key, not a tag."""
    return text.replace("[", "\\[")


def render(context: str = JOBS) -> str:
    """The help text for one context: this panel first, then everything else."""
    panel = panel_for(context)
    width = 18

    lines = [f"[bold underline]LazySlurm — {panel.title}[/]"]
    if panel.subtitle:
        lines.append(f"[dim]{panel.subtitle}[/]")
    lines.append("")

    for key in panel.keys:
        lines.append(f"  [bold cyan]{_escape(key.display):<{width}}[/] {key.text}")
    for note in panel.notes:
        lines.append(f"  [dim]{_escape(note)}[/]")

    lines.append("")
    lines.append("[bold]Anywhere[/]")
    for key in GLOBAL:
        lines.append(f"  [bold cyan]{_escape(key.display):<{width}}[/] {key.text}")

    others = [p for p in PANELS if p.context != panel.context]
    if others:
        lines.append("")
        lines.append("[bold]Other panels[/] [dim](press ? inside one for its keys)[/]")
        for other in others:
            keys = _escape(" ".join(k.display for k in other.keys[:4]))
            lines.append(f"  [cyan]{other.title:<18}[/] [dim]{keys}[/]")

    lines.append("")
    lines.append("Press [bold]?[/] or [bold]Escape[/] to close.")
    return "\n".join(lines)
