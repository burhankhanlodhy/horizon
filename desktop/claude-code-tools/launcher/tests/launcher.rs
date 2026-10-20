//! Integration tests for claude-code-launcher.
//!
//! Each test builds a fake Claude folder in a temp directory: the launcher is
//! copied in as `claude.exe`, the test child as `claude.cs-real.exe`, and the
//! data dir is pointed at the temp folder with CONTEXTSHRINK_DATA_DIR.

use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

static COUNTER: AtomicU64 = AtomicU64::new(0);

struct Fixture {
    root: PathBuf,
}

impl Fixture {
    fn new(name: &str) -> Fixture {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let unique = COUNTER.fetch_add(1, Ordering::SeqCst);
        let root = std::env::temp_dir().join(format!(
            "cs-launcher-{name}-{}-{nanos}-{unique}",
            std::process::id()
        ));
        fs::create_dir_all(root.join("data").join("claude-code")).unwrap();
        fs::create_dir_all(root.join("work")).unwrap();
        fs::copy(
            env!("CARGO_BIN_EXE_claude-code-launcher"),
            root.join("claude.exe"),
        )
        .unwrap();
        fs::copy(
            env!("CARGO_BIN_EXE_cs-test-child"),
            root.join("claude.cs-real.exe"),
        )
        .unwrap();
        Fixture { root }
    }

    fn launcher(&self) -> PathBuf {
        self.root.join("claude.exe")
    }

    fn write_config(&self, body: &str) {
        fs::write(
            self.root
                .join("data")
                .join("claude-code")
                .join("codetab.json"),
            body,
        )
        .unwrap();
    }

    fn command(&self) -> Command {
        let mut command = Command::new(self.launcher());
        command.env("CONTEXTSHRINK_DATA_DIR", self.root.join("data"));
        command.env_remove("ANTHROPIC_BASE_URL");
        command.env_remove("ENABLE_TOOL_SEARCH");
        command.env_remove("CLAUDE_CODE_AUTO_COMPACT_WINDOW");
        command.env_remove("CS_TEST_EXIT");
        command.env_remove("CS_TEST_ECHO_STDIN");
        command.env_remove("CS_TEST_PIDFILE");
        command.env_remove("CS_TEST_SLEEP_MS");
        command.env_remove("CS_TEST_HELPER_LOG");
        command.env_remove("ANTHROPIC_CUSTOM_HEADERS");
        command.env_remove("CONTEXTSHRINK_LAUNCHER_DEBUG");
        command.env_remove("CONTEXTSHRINK_LAUNCHER_DIRECT_HOSTS");
        command.current_dir(self.root.join("work"));
        command
    }

    fn run(&self, args: &[&str]) -> Output {
        self.command().args(args).output().unwrap()
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn hex_decode(text: &str) -> String {
    let bytes: Vec<u8> = (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect();
    String::from_utf8(bytes).unwrap()
}

/// Lines of the form `ARG <hex>` decoded back to text.
fn args_seen(stdout: &str) -> Vec<String> {
    stdout
        .lines()
        .filter_map(|line| line.strip_prefix("ARG "))
        .map(hex_decode)
        .collect()
}

/// `ENV <key>=<hex>` lines as (key, decoded value) pairs.
fn env_seen(stdout: &str) -> Vec<(String, String)> {
    stdout
        .lines()
        .filter_map(|line| line.strip_prefix("ENV "))
        .map(|pair| {
            let (key, value) = pair.split_once('=').unwrap();
            (key.to_string(), hex_decode(value))
        })
        .collect()
}

fn env_value(stdout: &str, key: &str) -> Option<String> {
    env_seen(stdout)
        .into_iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
}

fn stdout_of(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

fn free_port() -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.local_addr().unwrap().port()
}

fn config(port: u16, base_url: &str, enabled: bool, extra_env: &str) -> String {
    format!(
        "{{\"v\":1,\"enabled\":{enabled},\"base_url\":\"{base_url}\",\"env\":{{{extra_env}}},\"health_port\":{port}}}"
    )
}

#[test]
fn arguments_pass_through_unchanged_without_config() {
    let fixture = Fixture::new("args");
    let args = [
        "plain",
        "with space",
        "quote\"inside",
        "back\\slash",
        "",
        "trailing\\",
        "unicode \u{fc}n\u{ef} \u{1F642}",
        "--flag=a b",
        "tab\there",
    ];
    let output = fixture.run(&args);
    assert_eq!(
        output.status.code(),
        Some(0),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let seen = args_seen(&stdout_of(&output));
    assert_eq!(seen, args.to_vec());
    assert_eq!(env_value(&stdout_of(&output), "ANTHROPIC_BASE_URL"), None);
}

#[test]
fn exit_code_propagates_from_child() {
    let fixture = Fixture::new("exit");
    let output = fixture
        .command()
        .env("CS_TEST_EXIT", "42")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(42));
}

#[cfg(windows)]
#[test]
fn large_windows_exit_code_propagates_exactly() {
    // 0xC0000005 (access violation) as a signed i32.
    let fixture = Fixture::new("exit-big");
    let output = fixture
        .command()
        .env("CS_TEST_EXIT", "-1073741819")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(-1073741819));
}

#[test]
fn routes_child_through_forwarder_when_healthy() {
    let fixture = Fixture::new("routed");
    let port = serve_health(ok_response(), Duration::ZERO);
    // The config tries to override ANTHROPIC_BASE_URL through env, which must
    // be ignored in favour of base_url.
    fixture.write_config(&config(
        port,
        "http://127.0.0.1:18788/k/machine-id",
        true,
        "\"ENABLE_TOOL_SEARCH\":\"true\",\"ANTHROPIC_BASE_URL\":\"https://evil.example\",\"bad key\":\"x\"",
    ));
    let output = fixture.run(&["--version"]);
    assert_eq!(output.status.code(), Some(0));
    let stdout = stdout_of(&output);
    assert_eq!(
        env_value(&stdout, "ANTHROPIC_BASE_URL").as_deref(),
        Some("http://127.0.0.1:18788/k/machine-id")
    );
    assert_eq!(
        env_value(&stdout, "ENABLE_TOOL_SEARCH").as_deref(),
        Some("true")
    );
    assert_eq!(args_seen(&stdout), vec!["--version".to_string()]);
}

#[test]
fn fails_open_when_health_check_fails() {
    let fixture = Fixture::new("down");
    fixture.write_config(&config(
        free_port(),
        "http://127.0.0.1:18788/k/machine-id",
        true,
        "",
    ));
    let output = fixture.run(&["x"]);
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(env_value(&stdout_of(&output), "ANTHROPIC_BASE_URL"), None);
    assert_eq!(args_seen(&stdout_of(&output)), vec!["x".to_string()]);
}

#[test]
fn fails_open_on_malformed_config() {
    let fixture = Fixture::new("malformed");
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    fixture.write_config("{not json");
    let output = fixture.run(&["x"]);
    drop(listener);
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(env_value(&stdout_of(&output), "ANTHROPIC_BASE_URL"), None);
}

#[test]
fn fails_open_when_disabled_or_wrong_version() {
    let fixture = Fixture::new("disabled");
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    fixture.write_config(&config(port, "http://127.0.0.1:18788", false, ""));
    let output = fixture.run(&["x"]);
    assert_eq!(env_value(&stdout_of(&output), "ANTHROPIC_BASE_URL"), None);

    fixture.write_config(
        &config(port, "http://127.0.0.1:18788", true, "").replace("\"v\":1", "\"v\":2"),
    );
    let output = fixture.run(&["x"]);
    drop(listener);
    assert_eq!(env_value(&stdout_of(&output), "ANTHROPIC_BASE_URL"), None);
}

#[test]
fn refuses_base_urls_that_are_not_loopback() {
    let fixture = Fixture::new("nonloopback");
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    for bad in [
        "http://10.0.0.5:18788",
        "https://127.0.0.1:18788",
        "http://127.0.0.1.evil.example:18788",
        "http://127.0.0.1:18788/k/x y",
        "http://127.0.0.1:99999",
    ] {
        fixture.write_config(&config(port, bad, true, ""));
        let output = fixture.run(&["x"]);
        assert_eq!(
            env_value(&stdout_of(&output), "ANTHROPIC_BASE_URL"),
            None,
            "{bad}"
        );
    }
    drop(listener);
}

#[test]
fn child_inherits_stdin_and_working_directory() {
    let fixture = Fixture::new("stdio");
    let mut child = fixture
        .command()
        .env("CS_TEST_ECHO_STDIN", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(b"hello-stdin")
        .unwrap();
    let output = child.wait_with_output().unwrap();
    let stdout = stdout_of(&output);
    assert!(stdout.contains("hello-stdin"), "stdout was: {stdout}");
    let cwd = stdout
        .lines()
        .find_map(|line| line.strip_prefix("CWD "))
        .map(hex_decode)
        .unwrap();
    assert!(Path::new(&cwd).ends_with("work"), "cwd was {cwd}");
}

#[test]
fn missing_real_binary_fails_with_clear_message() {
    let fixture = Fixture::new("missing");
    fs::remove_file(fixture.root.join("claude.cs-real.exe")).unwrap();
    let output = fixture.run(&["x"]);
    assert_eq!(output.status.code(), Some(1));
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("real Claude Code binary is missing"),
        "stderr: {stderr}"
    );
}

// ---------------------------------------------------------------------------
// Health check (M3): GET /__cs/health must answer 200 with {"ok": true}
// ---------------------------------------------------------------------------

fn http_response(status: &str, body: &str) -> Vec<u8> {
    format!(
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
    .into_bytes()
}

fn ok_response() -> Vec<u8> {
    http_response("200 OK", r#"{"ok":true}"#)
}

/// A one-shot HTTP responder on 127.0.0.1. It reads the request, waits `delay`,
/// then writes `response`. Returns its port.
fn serve_health(response: Vec<u8>, delay: Duration) -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    std::thread::spawn(move || {
        if let Ok((mut stream, _)) = listener.accept() {
            let mut request = [0u8; 1024];
            let _ = stream.read(&mut request);
            std::thread::sleep(delay);
            let _ = stream.write_all(&response);
        }
    });
    port
}

fn routed_env(port: u16) -> String {
    config(port, "http://127.0.0.1:18788/k/machine-id", true, "")
}

fn assert_not_routed(fixture: &Fixture, port: u16) -> Output {
    fixture.write_config(&routed_env(port));
    let output = fixture.run(&["x"]);
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(env_value(&stdout_of(&output), "ANTHROPIC_BASE_URL"), None);
    assert_eq!(args_seen(&stdout_of(&output)), vec!["x".to_string()]);
    output
}

#[test]
fn health_non_200_fails_open() {
    let fixture = Fixture::new("health-503");
    let port = serve_health(
        http_response("503 Service Unavailable", r#"{"ok":true}"#),
        Duration::ZERO,
    );
    assert_not_routed(&fixture, port);
}

#[test]
fn health_ok_false_fails_open() {
    let fixture = Fixture::new("health-false");
    let port = serve_health(http_response("200 OK", r#"{"ok":false}"#), Duration::ZERO);
    assert_not_routed(&fixture, port);
}

#[test]
fn health_non_json_body_fails_open() {
    let fixture = Fixture::new("health-text");
    let port = serve_health(http_response("200 OK", "ok"), Duration::ZERO);
    assert_not_routed(&fixture, port);
}

#[test]
fn health_slower_than_the_budget_fails_open_quickly() {
    let fixture = Fixture::new("health-slow");
    let port = serve_health(ok_response(), Duration::from_millis(600));
    fixture.write_config(&routed_env(port));
    let started = Instant::now();
    let output = fixture.run(&["x"]);
    let elapsed = started.elapsed();
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(env_value(&stdout_of(&output), "ANTHROPIC_BASE_URL"), None);
    // The check gives up after 150 ms; the rest is process start-up. Generous bound for slow CI.
    assert!(elapsed < Duration::from_millis(2500), "took {elapsed:?}");
}

// ---------------------------------------------------------------------------
// L1: the config may set only ENABLE_TOOL_SEARCH, to "true" or "false"
// ---------------------------------------------------------------------------

#[test]
fn config_env_allows_only_enable_tool_search_as_a_string() {
    let fixture = Fixture::new("env-allowlist");
    let port = serve_health(ok_response(), Duration::ZERO);
    fixture.write_config(&config(
        port,
        "http://127.0.0.1:18788/k/machine-id",
        true,
        "\"ENABLE_TOOL_SEARCH\":\"false\",\"ANTHROPIC_LOG\":\"debug\",\"ENABLE_TOOL_SEARCH_X\":\"yes\",\"PATH_EXTRA\":\"x\"",
    ));
    let output = fixture.run(&["x"]);
    let stdout = stdout_of(&output);
    assert_eq!(
        env_value(&stdout, "ENABLE_TOOL_SEARCH").as_deref(),
        Some("false")
    );
    assert_eq!(env_value(&stdout, "ANTHROPIC_LOG"), None);
    assert_eq!(env_value(&stdout, "ENABLE_TOOL_SEARCH_X"), None);
    assert!(env_value(&stdout, "ANTHROPIC_BASE_URL").is_some());
}

#[test]
fn config_env_ignores_non_string_or_other_values() {
    let fixture = Fixture::new("env-values");
    let port = serve_health(ok_response(), Duration::ZERO);
    fixture.write_config(&config(
        port,
        "http://127.0.0.1:18788/k/machine-id",
        true,
        "\"ENABLE_TOOL_SEARCH\":1,\"ANTHROPIC_LOG\":\"debug\"",
    ));
    let output = fixture.run(&["x"]);
    let stdout = stdout_of(&output);
    assert_eq!(env_value(&stdout, "ENABLE_TOOL_SEARCH"), None);
    assert_eq!(env_value(&stdout, "ANTHROPIC_LOG"), None);
    assert!(env_value(&stdout, "ANTHROPIC_BASE_URL").is_some());
}

// ---------------------------------------------------------------------------
// M1: killing the launcher kills the child (Job Object, kill on close)
// ---------------------------------------------------------------------------

#[cfg(windows)]
fn process_alive(pid: u32) -> bool {
    let output = Command::new("tasklist")
        .args(["/FI", &format!("PID eq {pid}"), "/NH", "/FO", "CSV"])
        .output()
        .unwrap();
    String::from_utf8_lossy(&output.stdout).contains(&format!("\"{pid}\""))
}

#[cfg(windows)]
fn wait_for_pidfile(path: &Path) -> u32 {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Ok(text) = fs::read_to_string(path) {
            if let Ok(pid) = text.trim().parse() {
                return pid;
            }
        }
        assert!(Instant::now() < deadline, "the child never wrote its pid");
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[cfg(windows)]
#[test]
fn killing_the_launcher_kills_the_child() {
    let fixture = Fixture::new("job");
    let pidfile = fixture.root.join("child.pid");
    let mut launcher = fixture
        .command()
        .env("CS_TEST_PIDFILE", &pidfile)
        .env("CS_TEST_SLEEP_MS", "60000")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let child_pid = wait_for_pidfile(&pidfile);
    assert!(
        process_alive(child_pid),
        "the child should be running before the kill"
    );

    let killed = Command::new("taskkill")
        .args(["/F", "/PID", &launcher.id().to_string()])
        .output()
        .unwrap();
    assert!(
        killed.status.success(),
        "taskkill failed: {}",
        String::from_utf8_lossy(&killed.stderr)
    );
    let _ = launcher.wait();

    let deadline = Instant::now() + Duration::from_secs(10);
    while process_alive(child_pid) && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(100));
    }
    assert!(
        !process_alive(child_pid),
        "child {child_pid} outlived the killed launcher"
    );
}

#[cfg(windows)]
#[test]
fn child_runs_normally_when_the_job_is_not_used() {
    // Normal exit path: the job is closed after a clean exit and the child's code still comes back.
    let fixture = Fixture::new("job-normal");
    let output = fixture.command().env("CS_TEST_EXIT", "7").output().unwrap();
    assert_eq!(output.status.code(), Some(7));
}

// ---------------------------------------------------------------------------
// Keep sessions lean: CLAUDE_CODE_AUTO_COMPACT_WINDOW passes through only as a
// digits-only token count from 100000 to 1000000, with no leading zero.
// ---------------------------------------------------------------------------

fn lean_env(value: &str) -> String {
    format!("\"CLAUDE_CODE_AUTO_COMPACT_WINDOW\":\"{value}\"")
}

#[test]
fn lean_window_passes_through_inside_the_range() {
    let fixture = Fixture::new("lean-valid");
    for window in ["100000", "200000", "999999", "1000000"] {
        let port = serve_health(ok_response(), Duration::ZERO);
        fixture.write_config(&config(
            port,
            "http://127.0.0.1:18788/k/machine-id",
            true,
            &lean_env(window),
        ));
        let output = fixture.run(&["x"]);
        assert_eq!(output.status.code(), Some(0));
        let stdout = stdout_of(&output);
        assert_eq!(
            env_value(&stdout, "CLAUDE_CODE_AUTO_COMPACT_WINDOW").as_deref(),
            Some(window),
            "window {window}"
        );
        assert!(env_value(&stdout, "ANTHROPIC_BASE_URL").is_some());
    }
}

#[test]
fn lean_window_outside_the_allowed_form_is_ignored() {
    let fixture = Fixture::new("lean-bad");
    for bad in [
        "200k",
        "99999",
        "1000001",
        "10000000",
        "0",
        "0200000",
        "",
        " 200000",
        "200000 ",
        "+200000",
        "-200000",
        "2e5",
        "200.0",
        "\u{0661}\u{0660}\u{0660}\u{0660}\u{0660}\u{0660}",
    ] {
        let port = serve_health(ok_response(), Duration::ZERO);
        fixture.write_config(&config(
            port,
            "http://127.0.0.1:18788/k/machine-id",
            true,
            &lean_env(bad),
        ));
        let output = fixture.run(&["x"]);
        assert_eq!(output.status.code(), Some(0), "{bad:?}");
        let stdout = stdout_of(&output);
        assert_eq!(
            env_value(&stdout, "CLAUDE_CODE_AUTO_COMPACT_WINDOW"),
            None,
            "{bad:?}"
        );
        // Only the lean key is dropped; routing still applies.
        assert!(
            env_value(&stdout, "ANTHROPIC_BASE_URL").is_some(),
            "{bad:?}"
        );
    }
}

#[test]
fn lean_window_non_string_is_ignored() {
    let fixture = Fixture::new("lean-type");
    let port = serve_health(ok_response(), Duration::ZERO);
    fixture.write_config(&config(
        port,
        "http://127.0.0.1:18788/k/machine-id",
        true,
        "\"CLAUDE_CODE_AUTO_COMPACT_WINDOW\":200000",
    ));
    let output = fixture.run(&["x"]);
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        env_value(&stdout_of(&output), "CLAUDE_CODE_AUTO_COMPACT_WINDOW"),
        None
    );
}

// ---------------------------------------------------------------------------
// Direct mode (npx config): strict base URL and header checks, reachability,
// fail-open, and the token never printed.
// ---------------------------------------------------------------------------

/// A valid-looking proxy token. Test data only.
const GOOD_TOKEN: &str = "cs_live_0123456789abcdefABCDEF_-xy";

fn token_header(token: &str) -> String {
    format!("\"X-Horizon-Proxy-Token\":\"{token}\"")
}

/// A direct-mode config. `headers` and `env` are the bodies of their objects;
/// `extra` is appended as more top-level members (each starting with a comma).
fn direct_config(base_url: &str, headers: &str, env: &str, extra: &str) -> String {
    format!(
        "{{\"v\":1,\"enabled\":true,\"mode\":\"direct\",\"base_url\":\"{base_url}\",\"headers\":{{{headers}}},\"env\":{{{env}}}{extra}}}"
    )
}

/// A listener that accepts TCP connections (the kernel completes them even
/// without an accept call). The caller keeps it alive.
fn listening() -> (TcpListener, u16) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    (listener, port)
}

/// Asserts that the child ran unrouted with its arguments unchanged.
fn assert_plain_run(fixture: &Fixture, body: &str, args: &[&str]) {
    fixture.write_config(body);
    let output = fixture.run(args);
    let stdout = stdout_of(&output);
    assert_eq!(output.status.code(), Some(0), "{body}");
    assert_eq!(env_value(&stdout, "ANTHROPIC_BASE_URL"), None, "{body}");
    assert_eq!(
        env_value(&stdout, "ANTHROPIC_CUSTOM_HEADERS"),
        None,
        "{body}"
    );
    assert_eq!(
        args_seen(&stdout),
        args.iter().map(|a| a.to_string()).collect::<Vec<_>>(),
        "{body}"
    );
}

#[test]
fn direct_mode_sets_base_url_and_proxy_header_for_a_valid_config() {
    let fixture = Fixture::new("direct-valid");
    let (_listener, port) = listening();
    let base = format!("https://127.0.0.1:{port}/k/0123456789abcdef");
    fixture.write_config(&direct_config(
        &base,
        &token_header(GOOD_TOKEN),
        "\"ENABLE_TOOL_SEARCH\":\"true\"",
        "",
    ));
    let output = fixture
        .command()
        .env(
            "CONTEXTSHRINK_LAUNCHER_DIRECT_HOSTS",
            format!("127.0.0.1:{port}"),
        )
        .args(["--version"])
        .output()
        .unwrap();
    assert_eq!(
        output.status.code(),
        Some(0),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = stdout_of(&output);
    assert_eq!(
        env_value(&stdout, "ANTHROPIC_BASE_URL").as_deref(),
        Some(base.as_str())
    );
    assert_eq!(
        env_value(&stdout, "ANTHROPIC_CUSTOM_HEADERS").as_deref(),
        Some(format!("X-Horizon-Proxy-Token: {GOOD_TOKEN}").as_str())
    );
    assert_eq!(
        env_value(&stdout, "ENABLE_TOOL_SEARCH").as_deref(),
        Some("true")
    );
    assert_eq!(args_seen(&stdout), vec!["--version".to_string()]);
}

#[test]
fn direct_mode_accepts_loopback_http_with_and_without_a_path() {
    let fixture = Fixture::new("direct-http");
    let (_listener, port) = listening();
    for base in [
        format!("http://127.0.0.1:{port}/k/machine-id"),
        format!("http://127.0.0.1:{port}"),
    ] {
        fixture.write_config(&direct_config(&base, &token_header(GOOD_TOKEN), "", ""));
        let output = fixture.run(&["x"]);
        let stdout = stdout_of(&output);
        assert_eq!(
            env_value(&stdout, "ANTHROPIC_BASE_URL").as_deref(),
            Some(base.as_str())
        );
        assert!(env_value(&stdout, "ANTHROPIC_CUSTOM_HEADERS").is_some());
    }
}

#[test]
fn direct_mode_refuses_hosts_schemes_and_paths_it_does_not_know() {
    let fixture = Fixture::new("direct-bad-url");
    let (_listener, port) = listening();
    let long_id = "a".repeat(65);
    let bad_urls = [
        "https://evil.example/k/machine-id".to_string(),
        "https://proxy.contextshrink.com.evil.example/k/machine-id".to_string(),
        "https://user@proxy.contextshrink.com/k/machine-id".to_string(),
        "https://proxy.contextshrink.com:8443/k/machine-id".to_string(),
        "http://proxy.contextshrink.com/k/machine-id".to_string(),
        "http://10.0.0.5:18788/k/machine-id".to_string(),
        "http://127.0.0.1/k/machine-id".to_string(),
        "ftp://127.0.0.1:18788/k/x".to_string(),
        // Loopback over https is only for hosts a test lists; none is listed here.
        format!("https://127.0.0.1:{port}/k/machine-id"),
        "https://proxy.contextshrink.com/k/".to_string(),
        "https://proxy.contextshrink.com/k/a/b".to_string(),
        "https://proxy.contextshrink.com/k/x?y=1".to_string(),
        "https://proxy.contextshrink.com/k/x#frag".to_string(),
        format!("https://proxy.contextshrink.com/k/{long_id}"),
        "https://proxy.contextshrink.com/other".to_string(),
        "https://proxy.contextshrink.com/k/a%20b".to_string(),
        "https://proxy.contextshrink.com/k/x/".to_string(),
    ];
    for base in bad_urls {
        // The header is valid and the port is reachable, so only the URL decides.
        assert_plain_run(
            &fixture,
            &direct_config(&base, &token_header(GOOD_TOKEN), "", ""),
            &["x"],
        );
    }
}

#[test]
fn direct_mode_refuses_bad_headers_and_tokens() {
    let fixture = Fixture::new("direct-bad-header");
    let (_listener, port) = listening();
    let base = format!("http://127.0.0.1:{port}/k/machine-id");
    let bad_headers = [
        String::new(),
        "\"Authorization\":\"Bearer x\"".to_string(),
        format!("\"x-horizon-proxy-token\":\"{GOOD_TOKEN}\""),
        "\"X-Horizon-Proxy-Token\":\"cs_live_short\"".to_string(),
        "\"X-Horizon-Proxy-Token\":\"sk_live_0123456789abcdefABCDEF\"".to_string(),
        "\"X-Horizon-Proxy-Token\":\"cs_live_0123456789abcdef\\r\\nX-Evil: 1\"".to_string(),
        "\"X-Horizon-Proxy-Token\":\"cs_live_0123456789abcdef 0123\"".to_string(),
        "\"X-Horizon-Proxy-Token\":123".to_string(),
        format!("{},\"X-Other\":\"1\"", token_header(GOOD_TOKEN)),
    ];
    for headers in bad_headers {
        assert_plain_run(&fixture, &direct_config(&base, &headers, "", ""), &["x"]);
    }
}

#[test]
fn direct_mode_missing_disabled_or_unknown_mode_routes_nothing() {
    let fixture = Fixture::new("direct-off");
    let (_listener, port) = listening();
    let base = format!("http://127.0.0.1:{port}/k/machine-id");
    let header = token_header(GOOD_TOKEN);
    // No headers at all.
    assert_plain_run(
        &fixture,
        &format!("{{\"v\":1,\"enabled\":true,\"mode\":\"direct\",\"base_url\":\"{base}\"}}"),
        &["x"],
    );
    assert_plain_run(
        &fixture,
        &direct_config(&base, &header, "", "").replace("\"enabled\":true", "\"enabled\":false"),
        &["x"],
    );
    assert_plain_run(
        &fixture,
        &direct_config(&base, &header, "", "").replace("\"v\":1", "\"v\":2"),
        &["x"],
    );
    assert_plain_run(
        &fixture,
        &direct_config(&base, &header, "", "").replace("\"mode\":\"direct\"", "\"mode\":\"proxy\""),
        &["x"],
    );
}

#[test]
fn direct_mode_fails_open_when_the_host_is_not_reachable() {
    let fixture = Fixture::new("direct-closed");
    let port = free_port();
    fixture.write_config(&direct_config(
        &format!("https://127.0.0.1:{port}/k/machine-id"),
        &token_header(GOOD_TOKEN),
        "\"ENABLE_TOOL_SEARCH\":\"true\"",
        "",
    ));
    let started = Instant::now();
    let output = fixture
        .command()
        .env(
            "CONTEXTSHRINK_LAUNCHER_DIRECT_HOSTS",
            format!("127.0.0.1:{port}"),
        )
        .args(["x"])
        .output()
        .unwrap();
    let elapsed = started.elapsed();
    let stdout = stdout_of(&output);
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(env_value(&stdout, "ANTHROPIC_BASE_URL"), None);
    assert_eq!(env_value(&stdout, "ANTHROPIC_CUSTOM_HEADERS"), None);
    assert_eq!(env_value(&stdout, "ENABLE_TOOL_SEARCH"), None);
    assert_eq!(args_seen(&stdout), vec!["x".to_string()]);
    // The 400 ms budget plus process start-up. Generous bound for slow CI.
    assert!(elapsed < Duration::from_millis(3000), "took {elapsed:?}");
}

#[test]
fn direct_mode_env_allow_list_is_the_same_as_forwarder_mode() {
    let fixture = Fixture::new("direct-env");
    let (_listener, port) = listening();
    fixture.write_config(&direct_config(
        &format!("http://127.0.0.1:{port}/k/machine-id"),
        &token_header(GOOD_TOKEN),
        "\"ENABLE_TOOL_SEARCH\":\"false\",\"ANTHROPIC_LOG\":\"debug\",\"ENABLE_TOOL_SEARCH_X\":\"yes\",\"CLAUDE_CODE_AUTO_COMPACT_WINDOW\":\"200000\"",
        "",
    ));
    let output = fixture.run(&["x"]);
    let stdout = stdout_of(&output);
    assert_eq!(
        env_value(&stdout, "ENABLE_TOOL_SEARCH").as_deref(),
        Some("false")
    );
    assert_eq!(
        env_value(&stdout, "CLAUDE_CODE_AUTO_COMPACT_WINDOW").as_deref(),
        Some("200000")
    );
    assert_eq!(env_value(&stdout, "ANTHROPIC_LOG"), None);
    assert_eq!(env_value(&stdout, "ENABLE_TOOL_SEARCH_X"), None);
}

#[test]
fn debug_output_never_contains_the_token() {
    let fixture = Fixture::new("direct-debug");
    let (_listener, port) = listening();
    // Routed case, with debug on.
    fixture.write_config(&direct_config(
        &format!("http://127.0.0.1:{port}/k/machine-id"),
        &token_header(GOOD_TOKEN),
        "",
        "",
    ));
    let routed = fixture
        .command()
        .env("CONTEXTSHRINK_LAUNCHER_DEBUG", "1")
        .args(["x"])
        .output()
        .unwrap();
    assert_eq!(routed.status.code(), Some(0));
    assert!(!String::from_utf8_lossy(&routed.stderr).contains(GOOD_TOKEN));
    // Unreachable case, with debug on.
    let closed = free_port();
    fixture.write_config(&direct_config(
        &format!("https://127.0.0.1:{closed}/k/machine-id"),
        &token_header(GOOD_TOKEN),
        "",
        "",
    ));
    let unreachable = fixture
        .command()
        .env("CONTEXTSHRINK_LAUNCHER_DEBUG", "1")
        .env(
            "CONTEXTSHRINK_LAUNCHER_DIRECT_HOSTS",
            format!("127.0.0.1:{closed}"),
        )
        .args(["x"])
        .output()
        .unwrap();
    assert_eq!(unreachable.status.code(), Some(0));
    assert!(!String::from_utf8_lossy(&unreachable.stderr).contains(GOOD_TOKEN));
}

#[test]
fn unknown_mode_value_in_a_forwarder_shaped_config_routes_nothing() {
    let fixture = Fixture::new("mode-unknown");
    let port = serve_health(ok_response(), Duration::ZERO);
    let body = config(port, "http://127.0.0.1:18788/k/machine-id", true, "")
        .replace("\"health_port\"", "\"mode\":\"forwarder\",\"health_port\"");
    fixture.write_config(&body);
    let output = fixture.run(&["x"]);
    assert_eq!(env_value(&stdout_of(&output), "ANTHROPIC_BASE_URL"), None);
}

// ---------------------------------------------------------------------------
// Self-heal: an npx config starts `contextshrink-codetab.exe repair --quiet`
// detached, at most once an hour. The tests use a stub that only logs.
// ---------------------------------------------------------------------------

/// Polls `path` until it has `count` lines or ten seconds pass.
fn wait_for_lines(path: &Path, count: usize) -> Vec<String> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let lines: Vec<String> = fs::read_to_string(path)
            .unwrap_or_default()
            .lines()
            .map(str::to_string)
            .collect();
        if lines.len() >= count || Instant::now() >= deadline {
            return lines;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// Puts the logging stub where the launcher looks for the helper.
fn install_helper_stub(fixture: &Fixture) -> PathBuf {
    let bin = fixture
        .root
        .join("data")
        .join("claude-code")
        .join("npx-bin");
    fs::create_dir_all(&bin).unwrap();
    fs::copy(
        env!("CARGO_BIN_EXE_cs-test-helper"),
        bin.join("contextshrink-codetab.exe"),
    )
    .unwrap();
    bin
}

#[test]
fn npx_owner_repairs_detached_at_most_once_an_hour() {
    let fixture = Fixture::new("heal-once");
    install_helper_stub(&fixture);
    let log = fixture.root.join("helper.log");
    let stamp = fixture
        .root
        .join("data")
        .join("claude-code")
        .join("npx-repair.stamp");
    // Routing fails (closed port), but the repair runs anyway: it does not depend on routing.
    let closed = free_port();
    fixture.write_config(&direct_config(
        &format!("https://127.0.0.1:{closed}/k/machine-id"),
        &token_header(GOOD_TOKEN),
        "",
        ",\"owner\":\"npx\"",
    ));
    let run = || {
        fixture
            .command()
            .env("CS_TEST_HELPER_LOG", &log)
            .args(["x"])
            .output()
            .unwrap()
    };
    let output = run();
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(wait_for_lines(&log, 1), vec!["repair --quiet".to_string()]);
    assert!(stamp.is_file(), "the stamp should be written");

    let output = run();
    assert_eq!(output.status.code(), Some(0));
    std::thread::sleep(Duration::from_millis(800));
    assert_eq!(
        fs::read_to_string(&log).unwrap().lines().count(),
        1,
        "a second launch within the hour must not repair again"
    );
}

#[test]
fn a_stale_stamp_allows_a_new_repair() {
    let fixture = Fixture::new("heal-stale");
    install_helper_stub(&fixture);
    let log = fixture.root.join("helper.log");
    let stamp = fixture
        .root
        .join("data")
        .join("claude-code")
        .join("npx-repair.stamp");
    fs::write(&stamp, "1").unwrap();
    fixture.write_config(&direct_config(
        "http://127.0.0.1:9/k/machine-id",
        &token_header(GOOD_TOKEN),
        "",
        ",\"owner\":\"npx\"",
    ));
    let output = fixture
        .command()
        .env("CS_TEST_HELPER_LOG", &log)
        .args(["x"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(wait_for_lines(&log, 1), vec!["repair --quiet".to_string()]);
    assert_ne!(fs::read_to_string(&stamp).unwrap().trim(), "1");
}

#[test]
fn no_repair_without_an_enabled_npx_owner_or_a_helper() {
    let fixture = Fixture::new("heal-none");
    install_helper_stub(&fixture);
    let log = fixture.root.join("helper.log");
    let stamp = fixture
        .root
        .join("data")
        .join("claude-code")
        .join("npx-repair.stamp");
    // No owner: the desktop app's config.
    fixture.write_config(&direct_config(
        "http://127.0.0.1:9/k/machine-id",
        &token_header(GOOD_TOKEN),
        "",
        "",
    ));
    fixture
        .command()
        .env("CS_TEST_HELPER_LOG", &log)
        .args(["x"])
        .output()
        .unwrap();
    // Owner npx but disabled: a remove must not be undone by a repair.
    fixture.write_config(
        &direct_config(
            "http://127.0.0.1:9/k/machine-id",
            &token_header(GOOD_TOKEN),
            "",
            ",\"owner\":\"npx\"",
        )
        .replace("\"enabled\":true", "\"enabled\":false"),
    );
    fixture
        .command()
        .env("CS_TEST_HELPER_LOG", &log)
        .args(["x"])
        .output()
        .unwrap();
    std::thread::sleep(Duration::from_millis(500));
    assert!(!log.exists(), "no repair should have started");
    assert!(!stamp.exists());

    // Owner npx and enabled, but no helper beside the config.
    fs::remove_dir_all(
        fixture
            .root
            .join("data")
            .join("claude-code")
            .join("npx-bin"),
    )
    .unwrap();
    fixture.write_config(&direct_config(
        "http://127.0.0.1:9/k/machine-id",
        &token_header(GOOD_TOKEN),
        "",
        ",\"owner\":\"npx\"",
    ));
    fixture
        .command()
        .env("CS_TEST_HELPER_LOG", &log)
        .args(["x"])
        .output()
        .unwrap();
    std::thread::sleep(Duration::from_millis(500));
    assert!(!log.exists());
    assert!(!stamp.exists());
}
