//! Test-only stand-in for claude.cs-real.exe.
//!
//! It reports what it received so the launcher tests can check it exactly:
//! - `ARG <hex>` per argument, hex of the UTF-8 bytes (argv[1..]);
//! - `ENV <name>=<hex>` for environment variables starting with `ANTHROPIC_`
//!   or `ENABLE_TOOL_`;
//! - `CWD <hex>` for the working directory.
//!
//! Behaviour switches (environment): `CS_TEST_EXIT=<i32>` sets the exit code,
//! and `CS_TEST_ECHO_STDIN=1` copies stdin to stdout.

use std::io::{self, Read, Write};

fn hex(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

fn main() {
    let stdout = io::stdout();
    let mut out = stdout.lock();

    for arg in std::env::args_os().skip(1) {
        let text = arg.to_string_lossy();
        writeln!(out, "ARG {}", hex(text.as_bytes())).unwrap();
    }

    let mut env: Vec<(String, String)> = std::env::vars()
        .filter(|(key, _)| key.starts_with("ANTHROPIC_") || key.starts_with("ENABLE_TOOL_"))
        .collect();
    env.sort();
    for (key, value) in env {
        writeln!(out, "ENV {key}={}", hex(value.as_bytes())).unwrap();
    }

    if let Ok(cwd) = std::env::current_dir() {
        let text = cwd.to_string_lossy();
        writeln!(out, "CWD {}", hex(text.as_bytes())).unwrap();
    }

    if std::env::var("CS_TEST_ECHO_STDIN").as_deref() == Ok("1") {
        let mut input = Vec::new();
        io::stdin().read_to_end(&mut input).unwrap();
        out.write_all(&input).unwrap();
    }

    out.flush().unwrap();

    // Kill test support: publish our pid, then stay alive for a while.
    if let Some(path) = std::env::var_os("CS_TEST_PIDFILE") {
        std::fs::write(path, std::process::id().to_string()).unwrap();
    }
    if let Some(millis) = std::env::var("CS_TEST_SLEEP_MS").ok().and_then(|v| v.parse::<u64>().ok()) {
        std::thread::sleep(std::time::Duration::from_millis(millis));
    }

    let code: i32 = std::env::var("CS_TEST_EXIT")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(0);
    drop(out);
    std::process::exit(code);
}
