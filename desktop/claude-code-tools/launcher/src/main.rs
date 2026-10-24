//! claude-code-launcher.exe
//!
//! Claude Desktop's Code tab starts `claude.exe` from a versioned folder under
//! `%LOCALAPPDATA%\Claude-Data\claude_<publisher>\claude-code\<version>\<hash>\`
//! (or the older `%LOCALAPPDATA%\Packages\Claude_*\...\claude-code\` root). The
//! ContextShrink patcher renames the real binary to `claude.cs-real.exe` and
//! puts this launcher in its place as `claude.exe`.
//!
//! The launcher:
//! - reads `<data dir>\claude-code\codetab.json`. Without a `mode` key the file
//!   is a forwarder config (written by the desktop app): when it is enabled and
//!   `GET /__cs/health` on `127.0.0.1:<health_port>` answers `200` with a JSON
//!   body whose `ok` is `true`, all within a 150 ms budget, it adds
//!   `ANTHROPIC_BASE_URL` to the child's environment;
//! - with `"mode": "direct"` (written by `npx contextshrink-claude`, no desktop
//!   app needed) it adds `ANTHROPIC_BASE_URL` and `ANTHROPIC_CUSTOM_HEADERS`
//!   when the config passes strict checks: the base URL is `https://` to
//!   `proxy.contextshrink.com` (or a host:port listed in the test-only
//!   `CONTEXTSHRINK_LAUNCHER_DIRECT_HOSTS`), or `http://127.0.0.1:<port>` for
//!   tests; its path is empty or `/k/<id>`; the only header is
//!   `X-Horizon-Proxy-Token` with a `cs_live_` token. The host must accept a TCP
//!   connection within 400 ms. Nothing is printed that contains the token;
//! - in both modes the only other keys allowed in `env` are
//!   `ENABLE_TOOL_SEARCH`, set to `"true"` or `"false"`, and
//!   `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (keep-sessions-lean), set to a
//!   digits-only token count from 100000 to 1000000 with no leading zero. Any
//!   other value of either key is ignored;
//! - runs `claude.cs-real.exe` from its own folder with identical arguments,
//!   inherited stdio and the same working directory, waits, and exits with the
//!   child's exit code;
//! - puts the child in a Job Object with kill-on-close, so the child cannot
//!   outlive the launcher (for example after `taskkill /F`). When the launcher
//!   is already in a job that refuses nesting, it runs the child without one;
//! - fails open to the real binary on any config or routing problem;
//! - for an enabled `"owner": "npx"` config whose helper
//!   `npx-bin\contextshrink-codetab.exe` exists, starts
//!   `contextshrink-codetab.exe repair --quiet` detached after the child has
//!   started, at most once an hour (stamp file `npx-repair.stamp`). It never
//!   waits for the helper and ignores its result.
//!
//! The data dir is `$CONTEXTSHRINK_DATA_DIR` when set (tests use this), and
//! `%LOCALAPPDATA%\ContextShrink` otherwise.

use serde_json::{Map, Value};
use std::fs::{self, File};
use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

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
const DIRECT_MODE: &str = "direct";
const DIRECT_HOST: &str = "proxy.contextshrink.com";
const DIRECT_PORT: u16 = 443;
const DIRECT_BUDGET: Duration = Duration::from_millis(400);
const DIRECT_URL_MAX: usize = 256;
/// Test-only: extra `host` or `host:port` entries the direct mode may reach.
const DIRECT_HOSTS_ENV: &str = "CONTEXTSHRINK_LAUNCHER_DIRECT_HOSTS";
const PROXY_HEADER: &str = "X-Horizon-Proxy-Token";
const CUSTOM_HEADERS_KEY: &str = "ANTHROPIC_CUSTOM_HEADERS";
const OWNER_NPX: &str = "npx";
const NPX_BIN_DIR: &str = "npx-bin";
const REPAIR_HELPER: &str = "contextshrink-codetab.exe";
const REPAIR_STAMP: &str = "npx-repair.stamp";
const REPAIR_INTERVAL_SECS: u64 = 3600;
#[cfg(windows)]
const CREATE_BREAKAWAY_FROM_JOB: u32 = 0x0100_0000;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
#[cfg(windows)]
const DETACHED_PROCESS: u32 = 0x0000_0008;

/// The parsed `codetab.json` and the folder it lives in.
struct Config {
    dir: PathBuf,
    object: Map<String, Value>,
}

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
        eprintln!(
            "claude-code-launcher: reinstall or update the Claude Desktop app to restore it."
        );
        std::process::exit(1);
    }

    // Read once: routing uses it before the start, the repair check after it.
    let config = read_config();
    let mut command = Command::new(&real);
    // args_os keeps every argument byte-for-byte; std re-quotes them for the
    // Windows command line using the same rules the CRT parser reverses.
    command.args(std::env::args_os().skip(1));
    if let Some(overrides) = routing_environment(config.as_ref()) {
        command.envs(overrides);
    }

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(err) => {
            eprintln!(
                "claude-code-launcher: cannot start {}: {err}",
                real.display()
            );
            std::process::exit(1);
        }
    };
    // Held until the child is done; killing this launcher closes the job and
    // with it the child tree.
    let _job = guard(&child);
    if let Some(config) = config.as_ref() {
        self_heal(config);
    }
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
fn routing_environment(config: Option<&Config>) -> Option<Vec<(String, String)>> {
    let result = config.and_then(|config| routing_environment_checked(&config.object));
    if result.is_none() && std::env::var_os(DEBUG_ENV).is_some() {
        eprintln!("claude-code-launcher: routing disabled, running the real binary unchanged");
    }
    result
}

/// Dispatches on `mode`: absent is the forwarder config, `"direct"` is the npx
/// config, and any other value routes nothing.
fn routing_environment_checked(object: &Map<String, Value>) -> Option<Vec<(String, String)>> {
    if object.get("v")?.as_u64()? != 1 {
        return None;
    }
    if !object.get("enabled")?.as_bool()? {
        return None;
    }
    match object.get("mode") {
        None => forwarder_environment(object),
        Some(Value::String(mode)) if mode == DIRECT_MODE => direct_environment(object),
        Some(_) => None,
    }
}

fn forwarder_environment(object: &Map<String, Value>) -> Option<Vec<(String, String)>> {
    let base_url = object.get("base_url")?.as_str()?;
    let port = u16::try_from(object.get("health_port")?.as_u64()?).ok()?;
    if port == 0 || !is_loopback_base_url(base_url) {
        return None;
    }
    if !is_healthy(port) {
        return None;
    }

    let mut overrides = allowed_env(object);
    overrides.push((BASE_URL_KEY.to_string(), base_url.to_string()));
    Some(overrides)
}

/// Direct mode: the proxy's base URL and the one token header, after the strict
/// checks and a reachability test. The token is returned only inside the
/// overrides; it is never printed.
fn direct_environment(object: &Map<String, Value>) -> Option<Vec<(String, String)>> {
    let base_url = object.get("base_url")?.as_str()?;
    let hosts = allowed_direct_hosts();
    let (host, port) = parse_direct_base_url(base_url, &hosts)?;
    let header = proxy_header_line(object.get("headers")?)?;
    if !reachable(&host, port) {
        return None;
    }

    let mut overrides = allowed_env(object);
    overrides.push((BASE_URL_KEY.to_string(), base_url.to_string()));
    overrides.push((CUSTOM_HEADERS_KEY.to_string(), header));
    if std::env::var_os(DEBUG_ENV).is_some() {
        eprintln!("claude-code-launcher: direct routing through {host}:{port}");
    }
    Some(overrides)
}

/// The allow-listed `env` entries of a config: ENABLE_TOOL_SEARCH to "true" or
/// "false", and the lean window when it is a valid token count. Nothing else.
fn allowed_env(object: &Map<String, Value>) -> Vec<(String, String)> {
    let mut overrides = Vec::new();
    if let Some(env) = object.get("env").and_then(|value| value.as_object()) {
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
    overrides
}

/// The hosts direct mode may reach, as (host, port): the proxy on 443, plus
/// whatever the test-only env lists.
fn allowed_direct_hosts() -> Vec<(String, u16)> {
    let mut hosts = vec![(DIRECT_HOST.to_string(), DIRECT_PORT)];
    if let Some(list) = std::env::var_os(DIRECT_HOSTS_ENV) {
        for entry in list.to_string_lossy().split(',') {
            let entry = entry.trim();
            let (host, port) = match entry.rsplit_once(':') {
                Some((host, port)) => (host, parse_port(port)),
                None => (entry, Some(DIRECT_PORT)),
            };
            if let Some(port) = port {
                if valid_host(host) {
                    hosts.push((host.to_string(), port));
                }
            }
        }
    }
    hosts
}

/// Accepts `https://<allowed host>[:port][/k/<id>]`, or `http://127.0.0.1:<port>`
/// with the same path rule. Returns the host and port to check.
fn parse_direct_base_url(url: &str, hosts: &[(String, u16)]) -> Option<(String, u16)> {
    if url.len() > DIRECT_URL_MAX || !url.bytes().all(|b| (0x21..=0x7e).contains(&b)) {
        return None;
    }
    let (https, rest) = match (url.strip_prefix("https://"), url.strip_prefix("http://")) {
        (Some(rest), _) => (true, rest),
        (None, Some(rest)) => (false, rest),
        (None, None) => return None,
    };
    let (authority, path) = match rest.find('/') {
        Some(index) => rest.split_at(index),
        None => (rest, ""),
    };
    let (host, port_text) = match authority.rsplit_once(':') {
        Some((host, port)) => (host, Some(port)),
        None => (authority, None),
    };
    if !valid_host(host) {
        return None;
    }
    let port = match port_text {
        Some(text) => parse_port(text)?,
        None if https => DIRECT_PORT,
        None => return None,
    };
    let allowed = if https {
        hosts
            .iter()
            .any(|(allowed_host, allowed_port)| allowed_host == host && *allowed_port == port)
    } else {
        host == "127.0.0.1"
    };
    if !allowed || !direct_path_ok(path) {
        return None;
    }
    Some((host.to_string(), port))
}

fn valid_host(host: &str) -> bool {
    !host.is_empty()
        && host.len() <= 253
        && host
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-')
}

fn parse_port(text: &str) -> Option<u16> {
    if text.is_empty() || text.len() > 5 || !text.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    match text.parse::<u32>() {
        Ok(port) if (1..=65535).contains(&port) => u16::try_from(port).ok(),
        _ => None,
    }
}

/// Empty, or `/k/<id>` with a 1 to 64 character id of letters, digits, `_` and `-`.
fn direct_path_ok(path: &str) -> bool {
    if path.is_empty() {
        return true;
    }
    match path.strip_prefix("/k/") {
        Some(id) => {
            (1..=64).contains(&id.len())
                && id
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        }
        None => false,
    }
}

/// `headers` must hold exactly one entry, `X-Horizon-Proxy-Token` with a
/// `cs_live_` token. Returns the `Name: value` line for `ANTHROPIC_CUSTOM_HEADERS`.
fn proxy_header_line(headers: &Value) -> Option<String> {
    let map = headers.as_object()?;
    if map.len() != 1 {
        return None;
    }
    let token = map.get(PROXY_HEADER)?.as_str()?;
    if !proxy_token_ok(token) {
        return None;
    }
    Some(format!("{PROXY_HEADER}: {token}"))
}

fn proxy_token_ok(token: &str) -> bool {
    match token.strip_prefix("cs_live_") {
        Some(rest) => {
            (20..=128).contains(&rest.len())
                && rest
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        }
        None => false,
    }
}

/// True when a TCP connection to `host:port` succeeds within the direct budget.
/// Name resolution runs on a helper thread, because the standard resolver cannot
/// be time-limited; a lookup still running when the budget ends is left behind
/// and ends with the process.
fn reachable(host: &str, port: u16) -> bool {
    let deadline = Instant::now() + DIRECT_BUDGET;
    let target = (host.to_string(), port);
    let (sender, receiver) = mpsc::channel();
    let spawned = std::thread::Builder::new().spawn(move || {
        let _ = sender.send(connect_before(&target.0, target.1, deadline));
    });
    if spawned.is_err() {
        return false;
    }
    match remaining(deadline) {
        Some(timeout) => receiver.recv_timeout(timeout).unwrap_or(false),
        None => false,
    }
}

fn connect_before(host: &str, port: u16, deadline: Instant) -> bool {
    let Ok(addresses) = (host, port).to_socket_addrs() else {
        return false;
    };
    for address in addresses {
        let Some(timeout) = remaining(deadline) else {
            return false;
        };
        if TcpStream::connect_timeout(&address, timeout).is_ok() {
            return true;
        }
    }
    false
}

/// Starts `contextshrink-codetab.exe repair --quiet` detached, at most once an
/// hour, when this is an npx config and the helper is installed beside it. Only
/// the stamp is written before the start; the child's result is never read.
fn self_heal(config: &Config) {
    let object = &config.object;
    if object.get("owner").and_then(Value::as_str) != Some(OWNER_NPX) {
        return;
    }
    if object.get("enabled").and_then(Value::as_bool) != Some(true) {
        return;
    }
    let helper = config.dir.join(NPX_BIN_DIR).join(REPAIR_HELPER);
    if !helper.is_file() {
        return;
    }
    let Some(now) = unix_now() else {
        return;
    };
    let stamp = config.dir.join(REPAIR_STAMP);
    if let Some(last) = read_stamp(&stamp) {
        if now >= last && now - last < REPAIR_INTERVAL_SECS {
            return;
        }
    }
    // Written first, so a launch that starts while this one runs does not start a second repair.
    let _ = fs::write(&stamp, now.to_string());
    spawn_detached(&helper);
}

/// Starts the helper without waiting for it. Breakaway lets it outlive a job
/// that kills its members; when the job forbids breakaway, it is started
/// detached without it. Every error is ignored.
fn spawn_detached(helper: &Path) {
    #[cfg(windows)]
    {
        if spawn_with(
            helper,
            CREATE_BREAKAWAY_FROM_JOB | CREATE_NO_WINDOW | DETACHED_PROCESS,
        ) {
            return;
        }
        let _ = spawn_with(helper, CREATE_NO_WINDOW | DETACHED_PROCESS);
    }
    #[cfg(not(windows))]
    {
        let _ = spawn_with(helper);
    }
}

#[cfg(windows)]
fn spawn_with(helper: &Path, flags: u32) -> bool {
    let mut command = Command::new(helper);
    command
        .args(["repair", "--quiet"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(flags);
    command.spawn().is_ok()
}

#[cfg(not(windows))]
fn spawn_with(helper: &Path) -> bool {
    Command::new(helper)
        .args(["repair", "--quiet"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .is_ok()
}

fn unix_now() -> Option<u64> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .map(|elapsed| elapsed.as_secs())
}

fn read_stamp(path: &Path) -> Option<u64> {
    let bytes = read_capped(path, 64)?;
    String::from_utf8(bytes).ok()?.trim().parse().ok()
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

/// The config file, parsed as an object, or `None` when it is absent, too large,
/// not JSON or not an object.
fn read_config() -> Option<Config> {
    let path = codetab_config_path()?;
    let bytes = read_capped(&path, CONFIG_CAP_BYTES)?;
    let Value::Object(object) = serde_json::from_slice::<Value>(&bytes).ok()? else {
        return None;
    };
    Some(Config {
        dir: path.parent()?.to_path_buf(),
        object,
    })
}

fn read_capped(path: &Path, cap: u64) -> Option<Vec<u8>> {
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
    if stream.set_write_timeout(Some(timeout)).is_err()
        || stream.write_all(request.as_bytes()).is_err()
    {
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
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
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
        && port_text
            .parse::<u32>()
            .map(|p| (1..=65535).contains(&p))
            .unwrap_or(false);
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
        fn SetInformationJobObject(
            job: Handle,
            class: i32,
            info: *const c_void,
            length: u32,
        ) -> i32;
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
