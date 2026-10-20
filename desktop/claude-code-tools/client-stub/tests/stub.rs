//! Tests of horizon.exe. Each one builds a layout the stub expects
//! (`horizon.exe`, `python\python.exe`, `app\horizon_main.py`) under the cargo
//! target tmp dir, with the test child as python.exe, and runs the real stub.

use std::collections::HashMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

const STUB: &str = env!("CARGO_BIN_EXE_horizon");
const CHILD: &str = env!("CARGO_BIN_EXE_stub-test-child");
const FLAGS: [&str; 4] = ["-I", "-s", "-X", "utf8"];

static COUNTER: AtomicUsize = AtomicUsize::new(0);

/// `<root>\horizon.exe`, `<root>\app\horizon_main.py` and, when asked,
/// `<root>\python\python.exe` (a copy of the test child).
fn layout(name: &str, with_python: bool) -> PathBuf {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let root = PathBuf::from(env!("CARGO_TARGET_TMPDIR"))
        .join(format!("{name}-{}-{n}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(root.join("app")).unwrap();
    fs::write(root.join("app").join("horizon_main.py"), "").unwrap();
    fs::copy(STUB, root.join("horizon.exe")).unwrap();
    if with_python {
        fs::create_dir_all(root.join("python")).unwrap();
        fs::copy(CHILD, root.join("python").join("python.exe")).unwrap();
    }
    root
}

fn unhex(text: &str) -> String {
    let bytes: Vec<u8> = (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect();
    String::from_utf8(bytes).unwrap()
}

fn lines(stdout: &[u8]) -> Vec<String> {
    String::from_utf8_lossy(stdout)
        .lines()
        .map(str::to_string)
        .collect()
}

fn args_of(stdout: &[u8]) -> Vec<String> {
    lines(stdout)
        .iter()
        .filter_map(|line| line.strip_prefix("ARG ").map(unhex))
        .collect()
}

fn env_of(stdout: &[u8]) -> HashMap<String, String> {
    lines(stdout)
        .iter()
        .filter_map(|line| line.strip_prefix("ENV "))
        .filter_map(|pair| pair.split_once('='))
        .map(|(key, value)| (key.to_string(), unhex(value)))
        .collect()
}

/// True when `pid` is in the process list. Read-only: nothing is killed by name.
fn process_alive(pid: u32) -> bool {
    let out = Command::new("tasklist")
        .args(["/FI", &format!("PID eq {pid}"), "/FO", "CSV", "/NH"])
        .output()
        .unwrap();
    String::from_utf8_lossy(&out.stdout).contains(&format!("\"{pid}\""))
}

fn wait_until(limit: Duration, mut done: impl FnMut() -> bool) -> bool {
    let deadline = Instant::now() + limit;
    while Instant::now() < deadline {
        if done() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    done()
}

#[test]
fn arguments_pass_through_unchanged() {
    let root = layout("args", true);
    let args = [
        "plain",
        "with space",
        "quo\"te",
        "back\\slash",
        "trailing\\",
        "",
        "\u{fc}n\u{ef} \u{96ea}",
        "a\"b c\\\" d",
        "*?|&^%!",
    ];
    let out = Command::new(root.join("horizon.exe"))
        .args(args)
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert_eq!(
        out.status.code(),
        Some(0),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    let mut expected: Vec<String> = FLAGS.iter().map(|s| s.to_string()).collect();
    expected.push(
        root.join("app")
            .join("horizon_main.py")
            .display()
            .to_string(),
    );
    expected.extend(args.iter().map(|s| s.to_string()));
    assert_eq!(args_of(&out.stdout), expected);
}

#[test]
fn exit_code_passes_through() {
    let root = layout("exit", true);
    // 0xC0000005 as the i32 Windows reports for an access violation.
    for code in [0, 3, 255, 256, 9009, -1_073_741_819] {
        let out = Command::new(root.join("horizon.exe"))
            .env("CS_TEST_EXIT", code.to_string())
            .stdin(Stdio::null())
            .output()
            .unwrap();
        assert_eq!(out.status.code(), Some(code), "exit code {code}");
    }
}

#[test]
fn missing_python_is_reported_with_exit_9009() {
    let root = layout("missing", false);
    let out = Command::new(root.join("horizon.exe"))
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(9009));
    let err = String::from_utf8_lossy(&out.stderr);
    assert!(
        err.contains("the embedded Python is missing"),
        "stderr: {err}"
    );
    assert!(err.contains("python.exe"), "stderr: {err}");
}

#[test]
fn stdio_environment_and_working_directory_are_inherited() {
    let root = layout("stdio", true);
    let stub = root.join("horizon.exe");
    let mut child = Command::new(&stub)
        .env("CS_TEST_ECHO_STDIN", "1")
        .env("CS_TEST_INHERITED", "yes")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let launcher_pid = child.id();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(b"hello stdin\n")
        .unwrap();
    let out = child.wait_with_output().unwrap();
    assert_eq!(out.status.code(), Some(0));

    assert!(String::from_utf8_lossy(&out.stdout).ends_with("hello stdin\n"));
    let env = env_of(&out.stdout);
    assert_eq!(
        env.get("CS_TEST_INHERITED").map(String::as_str),
        Some("yes")
    );
    assert_eq!(
        env.get("HORIZON_LAUNCHER_PID"),
        Some(&launcher_pid.to_string())
    );
    assert_eq!(
        env.get("HORIZON_CLIENT_EXE"),
        Some(&stub.display().to_string())
    );

    let cwd = lines(&out.stdout)
        .iter()
        .find_map(|line| line.strip_prefix("CWD ").map(unhex))
        .unwrap();
    assert_eq!(Path::new(&cwd), std::env::current_dir().unwrap());
}

#[test]
fn killing_the_launcher_kills_python() {
    let root = layout("kill", true);
    let pidfile = root.join("child.pid");
    let mut stub = Command::new(root.join("horizon.exe"))
        .env("CS_TEST_PIDFILE", &pidfile)
        .env("CS_TEST_SLEEP_MS", "120000")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();

    let started = wait_until(Duration::from_secs(30), || pidfile.is_file());
    assert!(started, "the child never started");
    let python_pid: u32 = fs::read_to_string(&pidfile)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert!(
        process_alive(python_pid),
        "python is not running before the kill"
    );

    stub.kill().unwrap();
    stub.wait().unwrap();
    let gone = wait_until(Duration::from_secs(20), || !process_alive(python_pid));
    assert!(
        gone,
        "python {python_pid} survived the launcher being killed"
    );
}
