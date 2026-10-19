//! Process helpers for the bundled client: start it so it outlives the app,
//! find the process holding a forwarder port, and stop it only when it is the
//! bundled client itself. Nothing here ever stops a program that is not
//! ContextShrink's client.

use std::net::{SocketAddr, TcpStream};
use std::process::{Child, Command};
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
#[cfg(windows)]
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
#[cfg(windows)]
const CREATE_BREAKAWAY_FROM_JOB: u32 = 0x0100_0000;

/// The bundled client's image name, as the OS lists it.
#[cfg(windows)]
const CLIENT_IMAGE: &str = "horizon.exe";
#[cfg(not(windows))]
const CLIENT_IMAGE: &str = "horizon";

/// Runs a command without a console window (Windows); no-op elsewhere.
pub fn hidden(mut cmd: Command) -> Command {
    #[cfg(windows)]
    {
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd
}

/// Starts `build()` as a background process that outlives this app: no
/// window, its own process group, and outside the app's job object. Some
/// parents forbid breaking out of their job, so a failed first attempt is
/// retried without the breakaway flag. `build` is called again for the retry,
/// because a command's stdio handles are consumed by a spawn.
#[cfg(windows)]
pub fn spawn_detached(build: impl Fn() -> Command) -> Result<Child, String> {
    let mut cmd = build();
    cmd.creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB);
    match cmd.spawn() {
        Ok(child) => Ok(child),
        Err(_) => {
            let mut cmd = build();
            cmd.creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
            cmd.spawn().map_err(|e| e.to_string())
        }
    }
}

#[cfg(not(windows))]
pub fn spawn_detached(build: impl Fn() -> Command) -> Result<Child, String> {
    build().spawn().map_err(|e| e.to_string())
}

/// True when something accepts TCP connections on 127.0.0.1:`port`.
pub fn port_open(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok()
}

/// Waits until nothing listens on `port`, up to `timeout`.
pub fn wait_port_free(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while port_open(port) {
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    true
}

/// The process ID listening on loopback `port`, from the OS socket table.
pub fn listener_pid_os(port: u16) -> Option<u32> {
    #[cfg(windows)]
    {
        let out = hidden(Command::new("netstat"))
            .args(["-ano", "-p", "TCP"])
            .output()
            .ok()?;
        let text = String::from_utf8_lossy(&out.stdout);
        let suffix = format!(":{port}");
        text.lines().find_map(|line| {
            let cols: Vec<&str> = line.split_whitespace().collect();
            // Proto, local, foreign, state, pid.
            if cols.len() >= 5 && cols[0] == "TCP" && cols[3] == "LISTENING" && cols[1].ends_with(&suffix) {
                cols[4].parse().ok()
            } else {
                None
            }
        })
    }
    #[cfg(not(windows))]
    {
        let out = Command::new("lsof")
            .args(["-nP", "-t", &format!("-iTCP:{port}"), "-sTCP:LISTEN"])
            .output()
            .ok()?;
        String::from_utf8_lossy(&out.stdout).lines().find_map(|l| l.trim().parse().ok())
    }
}

/// The image name of process `pid`, if it is running.
pub fn image_name(pid: u32) -> Option<String> {
    #[cfg(windows)]
    {
        let out = hidden(Command::new("tasklist"))
            .args(["/FI", &format!("PID eq {pid}"), "/FO", "CSV", "/NH"])
            .output()
            .ok()?;
        // "No tasks are running" has no leading quote; a match is "name","pid",...
        let line = String::from_utf8_lossy(&out.stdout);
        let rest = line.trim().strip_prefix('"')?;
        let name = rest.split('"').next()?;
        (!name.is_empty()).then(|| name.to_string())
    }
    #[cfg(not(windows))]
    {
        let out = Command::new("ps")
            .args(["-o", "comm=", "-p", &pid.to_string()])
            .output()
            .ok()?;
        let name = String::from_utf8_lossy(&out.stdout).trim().to_string();
        let base = name.rsplit('/').next()?.to_string();
        (!base.is_empty()).then_some(base)
    }
}

/// True when `pid` is running the bundled client.
pub fn is_client(pid: u32) -> bool {
    image_name(pid).is_some_and(|n| n.eq_ignore_ascii_case(CLIENT_IMAGE))
}

/// The full command line of process `pid`, if it is running.
pub fn command_line(pid: u32) -> Option<String> {
    #[cfg(windows)]
    {
        let script = format!("(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine");
        let out = hidden(Command::new("powershell"))
            .args(["-NoProfile", "-NonInteractive", "-Command", &script])
            .output()
            .ok()?;
        let text = String::from_utf8_lossy(&out.stdout).trim().to_string();
        (!text.is_empty()).then_some(text)
    }
    #[cfg(not(windows))]
    {
        let out = Command::new("ps")
            .args(["-o", "args=", "-p", &pid.to_string()])
            .output()
            .ok()?;
        let text = String::from_utf8_lossy(&out.stdout).trim().to_string();
        (!text.is_empty()).then_some(text)
    }
}

/// True when `cmdline` is the bundled client's `forward start ... --port <port>`.
pub fn cmdline_is_forwarder(cmdline: &str, port: u16) -> bool {
    let tokens: Vec<&str> = cmdline.split_whitespace().collect();
    let port = port.to_string();
    tokens.windows(2).any(|w| w[0] == "forward" && w[1] == "start")
        && tokens.windows(2).any(|w| w[0] == "--port" && w[1] == port)
}

/// True when `cmdline` is the bundled client's `codetab install ... --watch`.
pub fn cmdline_is_codetab_watch(cmdline: &str) -> bool {
    let tokens: Vec<&str> = cmdline.split_whitespace().collect();
    tokens.windows(2).any(|w| w[0] == "codetab" && w[1] == "install") && tokens.contains(&"--watch")
}

/// True when `pid` is OUR forwarder on `port`: the bundled client, running
/// `forward start ... --port <port>`. Anything else holding the port is foreign.
pub fn is_our_forwarder(pid: u32, port: u16) -> bool {
    is_client(pid) && command_line(pid).is_some_and(|c| cmdline_is_forwarder(&c, port))
}

/// True when `pid` is OUR Code-tab watcher (the bundled client's `codetab install --watch`).
pub fn is_our_codetab_watch(pid: u32) -> bool {
    is_client(pid) && command_line(pid).is_some_and(|c| cmdline_is_codetab_watch(&c))
}

/// Force-stops `pid`. Returns whether the OS accepted the request.
pub fn kill_pid(pid: u32) -> bool {
    #[cfg(windows)]
    {
        hidden(Command::new("taskkill"))
            .args(["/PID", &pid.to_string(), "/F"])
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }
    #[cfg(not(windows))]
    {
        Command::new("kill")
            .args(["-KILL", &pid.to_string()])
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn current_process_is_not_the_client() {
        // The test binary is never the bundled client.
        assert!(!is_client(std::process::id()));
    }

    #[test]
    fn forwarder_command_line_must_match_our_port() {
        let ours = r#""C:\Program Files\ContextShrink\horizon\horizon.exe" forward start --remote https://proxy.contextshrink.com --port 18788 --host 127.0.0.1 --pause-file C:\x\paused"#;
        assert!(cmdline_is_forwarder(ours, 18788));
        // Another port, a prefix of the port, or a different command is not ours.
        assert!(!cmdline_is_forwarder(ours, 1878));
        assert!(!cmdline_is_forwarder(ours, 18789));
        assert!(!cmdline_is_forwarder("node server.js --port 18788", 18788));
        assert!(!cmdline_is_forwarder("horizon.exe codetab install --watch", 18788));
    }

    #[test]
    fn codetab_watch_command_line_is_recognised() {
        assert!(cmdline_is_codetab_watch("horizon.exe codetab install --watch"));
        assert!(!cmdline_is_codetab_watch("horizon.exe codetab install"));
        assert!(!cmdline_is_codetab_watch("horizon.exe forward start --port 18788"));
    }

    #[test]
    fn unused_port_reports_free() {
        // Port 1 is privileged and never listened on by these tests.
        assert!(!port_open(1));
    }
}
