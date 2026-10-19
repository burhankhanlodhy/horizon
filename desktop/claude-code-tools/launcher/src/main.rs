//! claude-code-launcher.exe
//!
//! Claude Desktop's Code tab starts `claude.exe` from a versioned folder under
//! `%LOCALAPPDATA%\Packages\Claude_*\...\claude-code\<version>\<hash>\`. The
//! ContextShrink patcher renames the real binary to `claude.cs-real.exe` and
//! puts this launcher in its place as `claude.exe`.
//!
//! The launcher:
//! - reads `<data dir>\claude-code\codetab.json`;
//! - when it is enabled and a TCP connect to `127.0.0.1:<health_port>` succeeds
//!   within 150 ms, it adds `ANTHROPIC_BASE_URL` and the configured `env`
//!   entries to the environment of the child only;
//! - runs `claude.cs-real.exe` from its own folder with identical arguments,
//!   inherited stdio and the same working directory, waits, and exits with the
//!   child's exit code;
//! - fails open to the real binary on any config or health problem.
//!
//! The data dir is `$CONTEXTSHRINK_DATA_DIR` when set (tests use this), and
//! `%LOCALAPPDATA%\ContextShrink` otherwise.

use std::fs::File;
use std::io::Read;
use std::net::{Ipv4Addr, SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

const REAL_BINARY: &str = "claude.cs-real.exe";
const CONFIG_CAP_BYTES: u64 = 64 * 1024;
const HEALTH_TIMEOUT: Duration = Duration::from_millis(150);
const BASE_URL_KEY: &str = "ANTHROPIC_BASE_URL";
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

    match command.status() {
        // Exit with the child's exact code, including large Windows codes such
        // as 0xC0000005, which do not fit in a u8 ExitCode.
        Ok(status) => std::process::exit(status.code().unwrap_or(1)),
        Err(err) => {
            eprintln!("claude-code-launcher: cannot start {}: {err}", real.display());
            std::process::exit(1);
        }
    }
}

fn exe_dir() -> Option<PathBuf> {
    std::env::current_exe().ok()?.parent().map(PathBuf::from)
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
        for (key, value) in env {
            if !is_valid_env_key(key) || key == BASE_URL_KEY {
                continue;
            }
            let text = match value {
                serde_json::Value::String(text) => text.clone(),
                serde_json::Value::Bool(flag) => flag.to_string(),
                serde_json::Value::Number(number) => number.to_string(),
                _ => continue,
            };
            overrides.push((key.clone(), text));
        }
    }
    overrides.push((BASE_URL_KEY.to_string(), base_url.to_string()));
    Some(overrides)
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

fn is_healthy(port: u16) -> bool {
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    TcpStream::connect_timeout(&address, HEALTH_TIMEOUT).is_ok()
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

fn is_valid_env_key(key: &str) -> bool {
    let mut chars = key.chars();
    match chars.next() {
        Some(first) if first.is_ascii_alphabetic() || first == '_' => {}
        _ => return false,
    }
    key.len() <= 128 && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}
