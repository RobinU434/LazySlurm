//! Owning the terminal: entering the alternate screen, and giving it back.
//!
//! Every path out of the app goes through [`Session::restore`], including the
//! ones that shell out to an editor or a pager. A missed restore leaves the
//! user's terminal in raw mode with no cursor, which is the worst failure this
//! app has — it outlives the process.

use std::io::{stdout, Stdout};

use anyhow::{Context, Result};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;

/// The terminal, for as long as the app is drawing to it.
pub struct Session {
    terminal: Terminal<CrosstermBackend<Stdout>>,
}

impl Session {
    /// Take over the terminal.
    pub fn enter() -> Result<Self> {
        enable_raw_mode().context("could not put the terminal into raw mode")?;
        execute!(stdout(), EnterAlternateScreen).context("could not open the alternate screen")?;

        let terminal = Terminal::new(CrosstermBackend::new(stdout()))
            .context("could not initialise the terminal")?;
        Ok(Self { terminal })
    }

    pub fn terminal(&mut self) -> &mut Terminal<CrosstermBackend<Stdout>> {
        &mut self.terminal
    }

    /// Hand the terminal back to the shell.
    ///
    /// Safe to call more than once; each step is independent so one failing
    /// still lets the others run.
    pub fn restore() -> Result<()> {
        let raw = disable_raw_mode();
        let screen = execute!(stdout(), LeaveAlternateScreen);
        raw.context("could not leave raw mode")?;
        screen.context("could not leave the alternate screen")?;
        Ok(())
    }

    /// Give the terminal back, run `action`, then take it again.
    ///
    /// This is how `o`, `e`, `l` and `,` run ssh, an editor or a pager: they
    /// need the real terminal, and they need it back in a sane state.
    pub fn suspended<T>(&mut self, action: impl FnOnce() -> T) -> Result<T> {
        Self::restore()?;
        let outcome = action();

        enable_raw_mode()?;
        execute!(stdout(), EnterAlternateScreen)?;
        // Whatever ran may have written anywhere; nothing on screen is trusted.
        self.terminal.clear()?;
        Ok(outcome)
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        // A panic unwinding through here still restores the terminal, which is
        // the case that matters most: the alternative is an unusable shell.
        let _ = Self::restore();
    }
}

/// Restore the terminal before a panic message is printed.
///
/// Without this the message is drawn into the alternate screen and vanishes with
/// it, leaving a broken terminal and no explanation.
pub fn install_panic_hook() {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let _ = Session::restore();
        previous(info);
    }));
}
