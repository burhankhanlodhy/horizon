//! claude-code-launcher.exe
//!
//! Claude Desktop's Code tab starts `claude.exe` from a versioned folder under
//! `%LOCALAPPDATA%\Packages\Claude_*\...\claude-code\<version>\<hash>\`. The
//! ContextShrink patcher renames the real binary to `claude.cs-real.exe` and
//! puts this launcher in its place as `claude.exe`.
//!
//! The launcher:
//! - reads `<data dir>\claude-code\codetab.json`;
//! - when it is enabled and `GET /__cs/health` on `127.0.0.1:<health_port>`
//!   answers `200` with a JSON body whose `ok` is `true`, all within a 150 ms
//!   budget, it adds `ANTHROPIC_BASE_URL` to the child's environment. The only
//!   other keys allowed are `ENABLE_TOOL_SEARCH`, set to `"true"` or `"false"`,
//!   and `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (keep-sessions-lean), set to a
//!   digits-only token count from 100000 to 1000000 with no leading zero. Any
//!   other value of either key is ignored;
//! - runs `claude.cs-real.exe` from its own folder with identical arguments,
//!   inherited stdio and the same working directory, waits, and exits with the
//!   child's exit code;
//! - puts the child in a Job Object with kill-on-close, so the child cannot
//!   outlive the launcher (for example after `taskkill /F`). When the launcher
//!   is already in a job that refuses nesting, it runs the child without one;
//! - fails open to the real binary on any config or health problem.
//!
//! The data dir is `$CONTEXTSHRINK_DATA_DIR` when set (tests use this), and
//! `%LOCALAPPDATA%\ContextShrink` otherwise.

use std::fs::File;
use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

const REAL_BINARY: &str = "claude.cs-real.exe";
const CONFIG_CAP_BYTES: u64 = 64 * 1024;
const HEALTH_BUDGET: Duration = Duration::from_millis(150);
const HEALTH_PATH: &str = "/__cs/health";
const HEALTH_CAP_BYTES: usize = 8 * 1024;
const BASE_URL_KEY: &str = "ANTHROPIC_BASE_URL";
const TOOL_SEARCH_KEY: &str = "ENABLE_TOOL_SEARCH";
const LEAN_WINDOW_KEY: &str = "CLAUDE_CODE_AUTO_COMPACT_WINDOW";
const LEAN_WINDOW_MIN: u64 = 100_000;
const LEAN_WINDOW_MAX: u64 = 1_000_000;
const DEBUG_ENV: &str = "CONTEXTSHRINK_LAUNCHER_DEBUG";

fn main() {
    let real = match exe_dir() {
        Some(dir) => dir.join(REAL_BINARY),
        None => {
            eprintln!("claude-code-launcher: cannot find its own folder to locate {REAL_BINARY}");
            std::process::exit(1);
        }
    };
    if !real.is_file() {
        eprintln!(
            "claude-code-launcher: the real Claude Code binary is missing: {}",
            real.display()
        );
        eprintln!("claude-code-launcher: reinstall or update the Claude Desktop app to restore it.");
        std::process::exit(1);
    }

    let mut command = Command::new(&real);
    // args_os keeps every argument byte-for-byte; std re-quotes them for the
    // Windows command line using the same rules the CRT parser reverses.
    command.args(std::env::args_os().skip(1));
    if let Some(overrides) = routing_environment() {
        command.envs(overrides);
    }

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(err) => {
            eprintln!("claude-code-launcher: cannot start {}: {err}", real.display());
            std::process::exit(1);
        }
    };
    // Held until the child is done; killing this launcher closes the job and
    // with it the child tree.
    let _job = guard(&child);
    let status = match child.wait() {
        Ok(status) => status,
        Err(err) => {
            eprintln!("claude-code-launcher: waiting for the child failed: {err}");
            std::process::exit(1);
        }
    };
    // Exit with the child's exact code, including large Windows codes such as
    // 0xC0000005, which do not fit in a u8 ExitCode.
    std::process::exit(status.code().unwrap_or(1));
}

fn exe_dir() -> Option<PathBuf> {
    std::env::current_exe().ok()?.parent().map(PathBuf::from)
}

#[cfg(windows)]
fn guard(child: &Child) -> Option<job::KillOnClose> {
    use std::os::windows::io::AsRawHandle;
    job::KillOnClose::attach(child.as_raw_handle())
}

#[cfg(not(windows))]
fn guard(_child: &Child) -> Option<()> {
    None
}

/// Returns the environment overrides to apply to the child, or `None` when the
/// child should run unchanged.
fn routing_environment() -> Option<Vec<(String, String)>> {
    let result = routing_environment_checked();
    if result.is_none() && std::env::var_os(DEBUG_ENV).is_some() {
        eprintln!("claude-code-launcher: routing disabled, running the real binary unchanged");
    }
    result
}

fn routing_environment_checked() -> Option<Vec<(String, String)>> {
    let config_path = codetab_config_path()?;
    let bytes = read_capped(&config_path, CONFIG_CAP_BYTES)?;
    let config: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
    let object = config.as_object()?;

    if object.get("v")?.as_u64()? != 1 {
        return None;
    }
    if !object.get("enabled")?.as_bool()? {
        return None;
    }
    let base_url = object.get("base_url")?.as_str()?;
    let port = u16::try_from(object.get("health_port")?.as_u64()?).ok()?;
    if port == 0 || !is_loopback_base_url(base_url) {
        return None;
    }
    if !is_healthy(port) {
        return None;
    }

    let mut overrides = Vec::new();
    if let Some(env) = object.get("env").and_then(|value| value.as_object()) {
        // Allow-list: ENABLE_TOOL_SEARCH to "true" or "false", and the lean window
        // when it is a valid token count. Nothing else.
        if let Some(text) = env.get(TOOL_SEARCH_KEY).and_then(|value| value.as_str()) {
            if text == "true" || text == "false" {
                overrides.push((TOOL_SEARCH_KEY.to_string(), text.to_string()));
            }
        }
        if let Some(text) = env.get(LEAN_WINDOW_KEY).and_then(|value| value.as_str()) {
            if lean_window_ok(text) {
                overrides.push((LEAN_WINDOW_KEY.to_string(), text.to_string()));
            }
        }
    }
    overrides.push((BASE_URL_KEY.to_string(), base_url.to_string()));
    Some(overrides)
}

/// True for a compaction window written as ASCII digits with no leading zero,
/// whose value is in `LEAN_WINDOW_MIN..=LEAN_WINDOW_MAX`. Rejects signs,
/// spaces, decimals and anything that is not a plain token count.
fn lean_window_ok(text: &str) -> bool {
    let bytes = text.as_bytes();
    if bytes.is_empty() || bytes.len() > 7 || bytes[0] == b'0' {
        return false;
    }
    if !bytes.iter().all(|b| b.is_ascii_digit()) {
        return false;
    }
    match text.parse::<u64>() {
        Ok(value) => (LEAN_WINDOW_MIN..=LEAN_WINDOW_MAX).contains(&value),
        Err(_) => false,
    }
}

fn codetab_config_path() -> Option<PathBuf> {
    let data_dir = match std::env::var_os("CONTEXTSHRINK_DATA_DIR") {
        Some(value) if !value.is_empty() => PathBuf::from(value),
        _ => {
            let local = std::env::var_os("LOCALAPPDATA")?;
            PathBuf::from(local).join("ContextShrink")
        }
    };
    Some(data_dir.join("claude-code").join("codetab.json"))
}

fn read_capped(path: &PathBuf, cap: u64) -> Option<Vec<u8>> {
    let file = File::open(path).ok()?;
    let mut bytes = Vec::new();
    file.take(cap + 1).read_to_end(&mut bytes).ok()?;
    if bytes.len() as u64 > cap {
        return None;
    }
    Some(bytes)
}

/// Time left before `deadline`, or `None` when it has passed.
fn remaining(deadline: Instant) -> Option<Duration> {
    let now = Instant::now();
    if now >= deadline {
        None
    } else {
        Some(deadline - now)
    }
}

/// `GET /__cs/health` over a minimal HTTP/1.1 exchange. The whole check, connect
/// included, has one 150 ms budget. Anything other than a 200 with a JSON body
/// whose `ok` is `true` counts as unhealthy.
fn is_healthy(port: u16) -> bool {
    let deadline = Instant::now() + HEALTH_BUDGET;
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    let Some(timeout) = remaining(deadline) else {
        return false;
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&address, timeout) else {
        return false;
    };
    let request = format!(
        "GET {HEALTH_PATH} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: application/json\r\nConnection: close\r\n\r\n"
    );
    let Some(timeout) = remaining(deadline) else {
        return false;
    };
    if stream.set_write_timeout(Some(timeout)).is_err() || stream.write_all(request.as_bytes()).is_err() {
        return false;
    }

    let mut response = Vec::new();
    let mut buffer = [0u8; 1024];
    while let Some(timeout) = remaining(deadline) {
        if stream.set_read_timeout(Some(timeout)).is_err() {
            return false;
        }
        match stream.read(&mut buffer) {
            Ok(0) => break,
            Ok(count) => {
                response.extend_from_slice(&buffer[..count]);
                if response.len() > HEALTH_CAP_BYTES {
                    return false;
                }
            }
            // Timed out or reset: judge what arrived.
            Err(_) => break,
        }
    }
    health_response_ok(&response)
}

fn health_response_ok(response: &[u8]) -> bool {
    let Some(split) = find(response, b"\r\n\r\n") else {
        return false;
    };
    let head = &response[..split];
    let body = &response[split + 4..];
    let status_line_end = head.iter().position(|&b| b == b'\r').unwrap_or(head.len());
    let status_line = &head[..status_line_end];
    // "HTTP/1.1 200 ..." or "HTTP/1.0 200 ...": the status code sits at bytes 9..12.
    let is_200 = status_line.starts_with(b"HTTP/1.") && status_line.get(9..12) == Some(b"200");
    if !is_200 {
        return false;
    }
    match serde_json::from_slice::<serde_json::Value>(body) {
        Ok(value) => value.get("ok") == Some(&serde_json::Value::Bool(true)),
        Err(_) => false,
    }
}

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|window| window == needle)
}

/// Accepts only `http://127.0.0.1:<port>` with an optional path, such as
/// `/k/<id>`. Anything else is refused so a config cannot point the child at
/// another host.
fn is_loopback_base_url(base_url: &str) -> bool {
    let Some(rest) = base_url.strip_prefix("http://127.0.0.1:") else {
        return false;
    };
    let (port_text, path) = match rest.find('/') {
        Some(index) => (&rest[..index], &rest[index..]),
        None => (rest, ""),
    };
    let port_ok = !port_text.is_empty()
        && port_text.len() <= 5
        && port_text.bytes().all(|b| b.is_ascii_digit())
        && port_text.parse::<u32>().map(|p| (1..=65535).contains(&p)).unwrap_or(false);
    let path_ok = path.is_empty()
        || (path.starts_with('/')
            && path
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"/-_.~%".contains(&b)));
    port_ok && path_ok
}

/// Kill-on-close Job Object, through raw FFI so no crate is needed.
#[cfg(windows)]
mod job {
    use std::ffi::c_void;

    type Handle = *mut c_void;

    const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: i32 = 9;
    const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;

    #[repr(C)]
    struct BasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }

    #[repr(C)]
    struct IoCounters {
        read_operation_count: u64,
        read_transfer_count: u64,
        write_operation_count: u64,
        write_transfer_count: u64,
        other_operation_count: u64,
        other_transfer_count: u64,
    }

    #[repr(C)]
    struct ExtendedLimitInformation {
        basic: BasicLimitInformation,
        io: IoCounters,
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> Handle;
        fn SetInformationJobObject(job: Handle, class: i32, info: *const c_void, length: u32) -> i32;
        fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
        fn CloseHandle(handle: Handle) -> i32;
    }

    /// Owns a job whose only member is the child. Dropping it (or the launcher
    /// dying) closes the last handle, which kills the child and its descendants.
    pub struct KillOnClose(Handle);

    impl KillOnClose {
        /// Returns `None` when the job cannot be created or the child cannot be
        /// assigned (for example when the launcher's own job forbids nesting).
        /// The child then runs without the job, so routing still works.
        pub fn attach(process: Handle) -> Option<KillOnClose> {
            // SAFETY: plain Win32 calls on handles this function creates or was given.
            unsafe {
                let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if job.is_null() {
                    return None;
                }
                let mut info: ExtendedLimitInformation = std::mem::zeroed();
                info.basic.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let set = SetInformationJobObject(
                    job,
                    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    &info as *const ExtendedLimitInformation as *const c_void,
                    std::mem::size_of::<ExtendedLimitInformation>() as u32,
                );
                if set == 0 || AssignProcessToJobObject(job, process) == 0 {
                    CloseHandle(job);
                    return None;
                }
                Some(KillOnClose(job))
            }
        }
    }

    impl Drop for KillOnClose {
        fn drop(&mut self) {
            // SAFETY: the handle was returned by CreateJobObjectW and is closed once.
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}
