//! The command line. Each command runs one operation and prints its result:
//! human lines by default, one JSON object with `--json`. `--quiet` (repair
//! only) prints nothing on success, which is what the sign-in task uses.
//!
//! Exit codes: 0 when the operation ran. Locked or conflicting folders are
//! reported, not failures, because the next run retries them. 1 when the
//! operation itself failed (for example no launcher, or an unreadable state
//! file). 2 for a usage error.

use crate::autostart;
use crate::fsx::Fail;
use crate::patch::{self, Paths, Report};
use crate::paths;
use serde_json::{json, Value};
use std::path::PathBuf;

const USAGE: &str = "usage:
  contextshrink-codetab install [--launcher <path>] [--json]
  contextshrink-codetab repair [--quiet] [--json]
  contextshrink-codetab remove [--json]
  contextshrink-codetab status [--launcher <path>] [--json]
  contextshrink-codetab autostart on|off

Patches or restores Claude Desktop's Code-tab claude.exe. The launcher is
claude-code-launcher.exe next to this helper unless --launcher is given.";

#[derive(Default)]
struct Options {
    json: bool,
    quiet: bool,
    launcher: Option<PathBuf>,
    words: Vec<String>,
}

fn parse(args: &[String], allow_quiet: bool) -> Result<Options, String> {
    let mut options = Options::default();
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--json" => options.json = true,
            "--quiet" if allow_quiet => options.quiet = true,
            "--launcher" => {
                let value = iter.next().ok_or("--launcher needs a path")?;
                options.launcher = Some(PathBuf::from(value));
            }
            other if other.starts_with("--") => return Err(format!("unknown option {other}")),
            other => options.words.push(other.to_string()),
        }
    }
    Ok(options)
}

/// Runs the command in `args` (without the program name) and returns the exit code.
pub fn run(args: &[String]) -> i32 {
    let Some((command, rest)) = args.split_first() else {
        eprintln!("{USAGE}");
        return 2;
    };
    match command.as_str() {
        "install" => install(rest),
        "repair" => repair(rest),
        "remove" => remove(rest),
        "status" => status(rest),
        "autostart" => autostart_command(rest),
        "help" | "--help" | "-h" => {
            println!("{USAGE}");
            0
        }
        other => {
            eprintln!("unknown command: {other}\n{USAGE}");
            2
        }
    }
}

fn usage_error(message: impl std::fmt::Display) -> i32 {
    eprintln!("contextshrink-codetab: {message}\n{USAGE}");
    2
}

fn resolve(launcher: Option<PathBuf>) -> Paths {
    Paths {
        roots: paths::discover_roots(),
        code_dir: paths::code_dir(),
        launcher: launcher.unwrap_or_else(paths::default_launcher),
    }
}

/// One folder in the JSON output: `path`, `state` and `detail`, the shape `status --json` uses.
fn folder_json(report: &Report, state: &str) -> Value {
    json!({
        "path": report.folder.display().to_string(),
        "state": state,
        "detail": report.detail,
    })
}

/// The state a folder is in after `install`: patched once the launcher is in
/// place, otherwise the state the pass found (locked, foreign, missing, ...).
fn state_after_install(report: &Report) -> &str {
    match report.action.as_str() {
        "patched" | "updated" | "up-to-date" => "patched",
        _ => report.status.as_str(),
    }
}

/// The state a folder is in after `remove`: real once restored, otherwise the state the pass found.
fn state_after_remove(report: &Report) -> &str {
    match report.action.as_str() {
        "restored" | "up-to-date" => "real",
        _ => report.status.as_str(),
    }
}

fn emit_error(json_output: bool, fail: &Fail) -> i32 {
    let message = match fail {
        Fail::Locked(text) | Fail::Failed(text) => text,
    };
    if json_output {
        println!("{}", json!({"ok": false, "error": message}));
    } else {
        eprintln!("contextshrink-codetab: {message}");
    }
    1
}

/// Prints the folder reports: human lines, or `{"folders": [...]}` plus `extra`
/// members with `--json`. `state_of` names each folder's state after the operation.
fn emit_reports(
    json_output: bool,
    reports: &[Report],
    state_of: fn(&Report) -> &str,
    extra: Value,
) {
    if json_output {
        let folders: Vec<Value> = reports
            .iter()
            .map(|report| folder_json(report, state_of(report)))
            .collect();
        let mut object = json!({"folders": folders});
        if let (Value::Object(target), Value::Object(more)) = (&mut object, extra) {
            target.extend(more);
        }
        println!("{object}");
    } else {
        for report in reports {
            println!("{}", report.line());
        }
    }
}

fn install(rest: &[String]) -> i32 {
    let options = match parse(rest, false) {
        Ok(options) => options,
        Err(message) => return usage_error(message),
    };
    if !options.words.is_empty() {
        return usage_error("install takes no positional arguments");
    }
    let paths = resolve(options.launcher);
    match patch::install(&paths) {
        Ok(reports) => {
            emit_reports(options.json, &reports, state_after_install, json!({}));
            0
        }
        Err(fail) => emit_error(options.json, &fail),
    }
}

fn repair(rest: &[String]) -> i32 {
    let options = match parse(rest, true) {
        Ok(options) => options,
        Err(message) => return usage_error(message),
    };
    if !options.words.is_empty() {
        return usage_error("repair takes no positional arguments");
    }
    let paths = resolve(options.launcher);
    match patch::repair(&paths) {
        Ok(None) => 0,
        Ok(Some(reports)) => {
            if !options.quiet {
                emit_reports(options.json, &reports, state_after_install, json!({}));
            }
            0
        }
        Err(fail) => {
            // Quiet still reports a failure: the caller decides whether to show it.
            emit_error(options.json, &fail)
        }
    }
}

fn remove(rest: &[String]) -> i32 {
    let options = match parse(rest, false) {
        Ok(options) => options,
        Err(message) => return usage_error(message),
    };
    if !options.words.is_empty() {
        return usage_error("remove takes no positional arguments");
    }
    let paths = resolve(None);
    match patch::remove(&paths) {
        Ok(outcome) => {
            emit_reports(
                options.json,
                &outcome.reports,
                state_after_remove,
                json!({
                    "config_removed": outcome.config_removed,
                    "config_disabled": outcome.config_disabled,
                }),
            );
            0
        }
        Err(fail) => emit_error(options.json, &fail),
    }
}

fn status(rest: &[String]) -> i32 {
    let options = match parse(rest, false) {
        Ok(options) => options,
        Err(message) => return usage_error(message),
    };
    if !options.words.is_empty() {
        return usage_error("status takes no positional arguments");
    }
    let paths = resolve(options.launcher);
    let reports = match patch::status(&paths) {
        Ok(reports) => reports,
        Err(fail) => return emit_error(options.json, &fail),
    };
    let autostart_on = match std::env::current_exe() {
        Ok(exe) => autostart::is_enabled(&exe),
        Err(_) => false,
    };
    if options.json {
        let folders: Vec<Value> = reports
            .iter()
            .map(|report| folder_json(report, &report.status))
            .collect();
        println!("{}", json!({"folders": folders, "autostart": autostart_on}));
    } else {
        for report in &reports {
            println!("{}", report.line());
        }
        println!("autostart: {}", if autostart_on { "on" } else { "off" });
    }
    0
}

fn autostart_command(rest: &[String]) -> i32 {
    let options = match parse(rest, false) {
        Ok(options) => options,
        Err(message) => return usage_error(message),
    };
    let [word] = options.words.as_slice() else {
        return usage_error("autostart takes on or off");
    };
    let exe = match std::env::current_exe() {
        Ok(exe) => exe,
        Err(err) => return emit_error(options.json, &Fail::Failed(err.to_string())),
    };
    let result = match word.as_str() {
        "on" => autostart::enable(&exe).map(|path| format!("autostart on: {}", path.display())),
        "off" => autostart::disable().map(|()| "autostart off".to_string()),
        _ => return usage_error("autostart takes on or off"),
    };
    match result {
        Ok(text) => {
            if options.json {
                println!("{}", json!({"ok": true, "autostart": word == "on"}));
            } else {
                println!("{text}");
            }
            0
        }
        Err(message) => emit_error(options.json, &Fail::Failed(message)),
    }
}
