//! Test-only stand-in for the bundled python.exe.
//!
//! The stub's tests copy this binary to `<layout>\python\python.exe`, so the
//! stub runs it exactly as it runs the real interpreter. It reports what it got:
//! - `ARG <hex>` for each argument (argv[1..], hex of the UTF-8 bytes; the stub
//!   puts `-I -s -X utf8 <script>` first);
//! - `ENV <name>=<hex>` for variables starting with `HORIZON_` or `CS_TEST_`;
//! - `CWD <hex>` for the working directory.
//!
//! Behaviour switches (environment): `CS_TEST_EXIT=<i32>` sets the exit code,
//! `CS_TEST_ECHO_STDIN=1` copies stdin to stdout, `CS_TEST_SLEEP_MS=<ms>` sleeps,
//! and `CS_TEST_PIDFILE=<path>` writes this process's PID (after the report).

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
        .filter(|(key, _)| key.starts_with("HORIZON_") || key.starts_with("CS_TEST_"))
        .collect();
    env.sort();
    for (key, value) in env {
        writeln!(out, "ENV {key}={}", hex(value.as_bytes())).unwrap();
    }

    if let Ok(cwd) = std::env::current_dir() {
        let text = cwd.to_string_lossy();
        writeln!(out, "CWD {}", hex(text.as_bytes())).unwrap();
    }
    out.flush().unwrap();

    if std::env::var("CS_TEST_ECHO_STDIN").as_deref() == Ok("1") {
        let mut input = Vec::new();
        io::stdin().read_to_end(&mut input).unwrap();
        let mut out = io::stdout().lock();
        out.write_all(&input).unwrap();
        out.flush().unwrap();
    }

    if let Some(path) = std::env::var_os("CS_TEST_PIDFILE") {
        std::fs::write(path, std::process::id().to_string()).unwrap();
    }
    if let Some(millis) = std::env::var("CS_TEST_SLEEP_MS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
    {
        std::thread::sleep(std::time::Duration::from_millis(millis));
    }

    let code: i32 = std::env::var("CS_TEST_EXIT")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(0);
    std::process::exit(code);
}
