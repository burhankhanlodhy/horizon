//! Linux and macOS pieces of the launcher.
//!
//! Two things differ from Windows. Apps started from the desktop menu do not
//! get the PATH a user's shell builds (~/.local/bin, nvm, ~/.npm-global,
//! ~/.bun, ...), so tools installed there would look missing. And there is
//! no single console to open, so the user's terminal emulator is found and
//! handed a small launch script.

use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// Puts the login shell's PATH entries in front of the app's own.
///
/// Runs `$SHELL -ilc` once at startup (interactive, so ~/.bashrc additions
/// such as nvm count too) and gives up after a few seconds on a slow setup.
pub fn import_login_path() {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".into());
    // `env` prints PATH colon-separated in every shell, fish included.
    let child = Command::new(&shell)
        .args(["-ilc", "echo __CS_ENV__; /usr/bin/env"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn();
    let Ok(mut child) = child else { return };
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(50)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return;
            }
        }
    }
    let Ok(out) = child.wait_with_output() else { return };
    let text = String::from_utf8_lossy(&out.stdout);
    let Some(env) = text.split("__CS_ENV__").nth(1) else { return };
    let Some(login) = env.lines().find_map(|l| l.strip_prefix("PATH=")) else { return };

    let current = std::env::var("PATH").unwrap_or_default();
    let mut merged: Vec<&str> = Vec::new();
    for dir in login.split(':').chain(current.split(':')) {
        if !dir.is_empty() && !merged.contains(&dir) {
            merged.push(dir);
        }
    }
    std::env::set_var("PATH", merged.join(":"));
}

/// Display settings that must be in place before GTK starts.
///
/// WebKitGTK's DMA-BUF renderer draws a blank window when it cannot use the
/// GPU (WSL, some NVIDIA drivers); this UI does not need it. Under WSL, the
/// Wayland bridge can also place windows off-screen or drop input, so use X11
/// there. Settings the user already made are left alone.
#[cfg(target_os = "linux")]
pub fn display_workarounds() {
    if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }
    if std::env::var_os("WSL_DISTRO_NAME").is_some() && std::env::var_os("GDK_BACKEND").is_none() {
        std::env::set_var("GDK_BACKEND", "x11");
    }
}

/// Single-quotes `s` for a POSIX shell.
pub fn sh_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', r"'\''"))
}

/// Terminal emulators, in the order tried, with the arguments that make each
/// run one program. `$TERMINAL` and Debian's `x-terminal-emulator` come first
/// so the user's own choice wins.
#[cfg(target_os = "linux")]
const TERMINALS: &[(&str, &[&str])] = &[
    ("x-terminal-emulator", &["-e"]),
    ("gnome-terminal", &["--"]),
    ("ptyxis", &["--"]),
    ("kgx", &["-e"]),
    ("konsole", &["-e"]),
    ("xfce4-terminal", &["-x"]),
    ("mate-terminal", &["-x"]),
    ("tilix", &["-e"]),
    ("terminator", &["-x"]),
    ("kitty", &[]),
    ("alacritty", &["-e"]),
    ("wezterm", &["start", "--"]),
    ("foot", &[]),
    ("lxterminal", &["-e"]),
    ("xterm", &["-e"]),
];

/// The terminal to use and its run-a-program arguments.
#[cfg(target_os = "linux")]
fn find_terminal() -> Option<(String, Vec<String>)> {
    // An explicit choice: CONTEXTSHRINK_TERMINAL, then the common $TERMINAL.
    for var in ["CONTEXTSHRINK_TERMINAL", "TERMINAL"] {
        if let Ok(value) = std::env::var(var) {
            let value = value.trim();
            if value.is_empty() || which::which(value).is_err() {
                continue;
            }
            let name = Path::new(value).file_name().and_then(|n| n.to_str()).unwrap_or(value);
            let args = TERMINALS
                .iter()
                .find(|(t, _)| *t == name)
                .map(|(_, a)| a.to_vec())
                .unwrap_or(vec!["-e"]);
            return Some((value.to_string(), args.iter().map(|a| a.to_string()).collect()));
        }
    }
    TERMINALS.iter().find_map(|(name, args)| {
        which::which(name)
            .ok()
            .map(|p| (p.to_string_lossy().into_owned(), args.iter().map(|a| a.to_string()).collect()))
    })
}

/// macOS: `open -a Terminal <script>` runs the script in a new Terminal window.
/// CONTEXTSHRINK_TERMINAL names another app instead (for example `iTerm`).
#[cfg(target_os = "macos")]
fn find_terminal() -> Option<(String, Vec<String>)> {
    let app = std::env::var("CONTEXTSHRINK_TERMINAL")
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "Terminal".into());
    Some(("/usr/bin/open".into(), vec!["-a".into(), app]))
}

/// File extension for launch scripts: macOS Terminal runs `.command` files.
pub const SCRIPT_EXT: &str = if cfg!(target_os = "macos") { "command" } else { "sh" };

/// Writes `script` to `path` (owner-only) and runs it in a new terminal window.
///
/// Values the script needs are written into it rather than passed through the
/// environment: some terminals (gnome-terminal) start the program from a
/// separate server process that does not inherit this app's environment.
pub fn open_in_terminal(script: &str, path: &Path, folder: &Path, title: &str) -> Result<(), String> {
    std::fs::write(path, script).map_err(|e| e.to_string())?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;
    let (terminal, args) = find_terminal().ok_or(
        "No terminal app found. Install one (for example GNOME Terminal or Konsole), \
         or set CONTEXTSHRINK_TERMINAL to the terminal to use.",
    )?;
    let mut child = Command::new(&terminal)
        .args(&args)
        .arg(path)
        .current_dir(folder)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("Could not open a terminal for {title}: {e}"))?;
    // Reap it whenever it exits; the window lives on its own.
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(())
}

/// This computer's name, for the device key label.
pub fn host_name() -> Option<String> {
    #[cfg(target_os = "macos")]
    let name = Command::new("/usr/sbin/scutil")
        .args(["--get", "ComputerName"])
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned());
    #[cfg(not(target_os = "macos"))]
    let name = std::fs::read_to_string("/proc/sys/kernel/hostname").ok();
    name.map(|h| h.trim().to_string()).filter(|h| !h.is_empty())
}
