//! contextshrink-codetab.exe: sets up, repairs and removes the routing of Claude
//! Desktop's Code tab through `claude-code-launcher.exe`, without the desktop app.
//!
//! Commands (see cli.rs): `install`, `repair [--quiet]`, `remove`, `status`,
//! `autostart on|off`. The state it keeps matches `horizon/desktop_codetab.py`
//! (see state.rs). It never writes `codetab.json`, which the npx installer owns;
//! `remove` only deletes or disables it when nothing is left patched.
//!
//! GUI subsystem, so a sign-in task never shows a console window.

#![cfg_attr(windows, windows_subsystem = "windows")]

mod autostart;
mod cli;
mod console;
mod fsx;
mod patch;
mod paths;
mod state;

fn main() {
    console::attach();
    let args: Vec<String> = std::env::args_os()
        .skip(1)
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    std::process::exit(cli::run(&args));
}
