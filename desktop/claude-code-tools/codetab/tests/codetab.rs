//! Integration tests for contextshrink-codetab.
//!
//! Every test builds its own fake Claude Desktop root, data folder and Startup
//! folder under the temp dir, and points the helper at them with
//! CONTEXTSHRINK_CODETAB_ROOTS, CONTEXTSHRINK_DATA_DIR and
//! CONTEXTSHRINK_CODETAB_STARTUP_DIR. The real folders are never reached.

use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const REAL: &[u8] = b"CS-REAL-CLAUDE-TEST-BYTES\n";
const LAUNCHER: &[u8] = b"CS-LAUNCHER-TEST-BYTES\n";
/// The bytes behind the Python fixtures. The state and config were written by
/// horizon/desktop_codetab.py for a folder holding PY_REAL, patched with PY_LAUNCHER.
const PY_REAL: &[u8] = b"CS-REAL-CLAUDE-FIXTURE\n";
const PY_LAUNCHER: &[u8] = b"CS-LAUNCHER-FIXTURE\n";
const PY_STATE_TEMPLATE: &str = include_str!("fixtures/python-state.json.in");
const PY_CONFIG: &str = include_str!("fixtures/python-config.json");

static COUNTER: AtomicU64 = AtomicU64::new(0);

fn sha(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

struct Env {
    root: PathBuf,
    roots: PathBuf,
    data: PathBuf,
    startup: PathBuf,
    launcher: PathBuf,
}

impl Env {
    fn new(name: &str) -> Env {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let unique = COUNTER.fetch_add(1, Ordering::SeqCst);
        let root = std::env::temp_dir().join(format!(
            "cs-codetab-{name}-{}-{nanos}-{unique}",
            std::process::id()
        ));
        let env = Env {
            roots: root.join("roots"),
            data: root.join("data"),
            startup: root.join("startup"),
            launcher: root.join("launcher.exe"),
            root,
        };
        fs::create_dir_all(&env.roots).unwrap();
        fs::create_dir_all(env.data.join("claude-code")).unwrap();
        fs::create_dir_all(&env.startup).unwrap();
        fs::write(&env.launcher, LAUNCHER).unwrap();
        env
    }

    fn code_dir(&self) -> PathBuf {
        self.data.join("claude-code")
    }

    fn state_path(&self) -> PathBuf {
        self.code_dir().join("codetab-state.json")
    }

    /// `<roots>\<version>\<hash>`, created.
    fn folder(&self, version: &str, hash: &str) -> PathBuf {
        let folder = self.roots.join(version).join(hash);
        fs::create_dir_all(&folder).unwrap();
        folder
    }

    /// A folder holding a real `claude.exe` and its `.payload`.
    fn real_folder(&self, version: &str, hash: &str) -> PathBuf {
        let folder = self.folder(version, hash);
        fs::write(folder.join("claude.exe"), REAL).unwrap();
        write_payload(&folder, REAL);
        folder
    }

    fn command(&self) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_contextshrink-codetab"));
        command.env("CONTEXTSHRINK_DATA_DIR", &self.data);
        command.env("CONTEXTSHRINK_CODETAB_ROOTS", &self.roots);
        command.env("CONTEXTSHRINK_CODETAB_STARTUP_DIR", &self.startup);
        command
    }

    fn run(&self, args: &[&str]) -> Output {
        self.command().args(args).output().unwrap()
    }

    fn run_json(&self, args: &[&str]) -> (i32, Value) {
        let output = self.run(args);
        let code = output.status.code().unwrap_or(-1);
        let text = String::from_utf8_lossy(&output.stdout).into_owned();
        let value = serde_json::from_str(text.trim()).unwrap_or_else(|_| {
            panic!(
                "not JSON: {text}\nstderr: {}",
                String::from_utf8_lossy(&output.stderr)
            )
        });
        (code, value)
    }

    fn install_args(&self) -> Vec<String> {
        vec![
            "install".into(),
            "--launcher".into(),
            self.launcher.to_string_lossy().into_owned(),
        ]
    }

    fn install(&self) -> (i32, Value) {
        let args = self.install_args();
        let refs: Vec<&str> = args.iter().map(String::as_str).collect();
        self.run_json_with(&refs)
    }

    fn run_json_with(&self, args: &[&str]) -> (i32, Value) {
        let mut all = args.to_vec();
        all.push("--json");
        self.run_json(&all)
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn write_payload(folder: &Path, real: &[u8]) {
    fs::write(
        folder.join(".payload"),
        format!(
            "{{\"sha256\": \"{}\", \"size\": {}}}",
            sha(real),
            real.len()
        ),
    )
    .unwrap();
}

/// The `state` of the folder whose path ends with `suffix`, from `folders`.
fn state_of(value: &Value, suffix: &str) -> String {
    value["folders"]
        .as_array()
        .unwrap()
        .iter()
        .find(|entry| entry["path"].as_str().unwrap().ends_with(suffix))
        .unwrap_or_else(|| panic!("no folder ends with {suffix}: {value}"))["state"]
        .as_str()
        .unwrap()
        .to_string()
}

fn read(path: &Path) -> Vec<u8> {
    fs::read(path).unwrap()
}

fn mtime(path: &Path) -> SystemTime {
    fs::metadata(path).unwrap().modified().unwrap()
}

// ---------------------------------------------------------------------------
// install, repair, remove
// ---------------------------------------------------------------------------

#[test]
fn install_patches_a_folder_and_a_second_install_changes_nothing() {
    let env = Env::new("install");
    let folder = env.real_folder("1.0.0", "aaaa");

    let (code, value) = env.install();
    assert_eq!(code, 0, "{value}");
    assert_eq!(state_of(&value, "aaaa"), "patched");
    assert_eq!(read(&folder.join("claude.exe")), LAUNCHER);
    assert_eq!(read(&folder.join("claude.cs-real.exe")), REAL);
    let state = fs::read_to_string(env.state_path()).unwrap();
    assert!(state.contains(&sha(LAUNCHER)), "{state}");

    let state_before = read(&env.state_path());
    let output = env.run(
        &env.install_args()
            .iter()
            .map(String::as_str)
            .collect::<Vec<_>>(),
    );
    assert_eq!(output.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&output.stdout).contains("up-to-date"));
    assert_eq!(read(&folder.join("claude.exe")), LAUNCHER);
    assert_eq!(
        read(&env.state_path()),
        state_before,
        "state must not change on a no-op install"
    );
}

#[test]
fn repair_quiet_takes_the_fast_path_until_something_changes() {
    let env = Env::new("repair");
    let folder = env.real_folder("1.0.0", "aaaa");
    let launcher = env.launcher.to_string_lossy().into_owned();
    assert_eq!(
        env.run(&["install", "--launcher", &launcher]).status.code(),
        Some(0)
    );

    // The first quiet repair runs a full pass and records the fingerprint.
    let first = env.run(&["repair", "--quiet", "--launcher", &launcher]);
    assert_eq!(first.status.code(), Some(0));
    assert!(
        first.stdout.is_empty(),
        "quiet repair prints nothing on success"
    );

    let check = env.code_dir().join("codetab-repair.json");
    let watched = [env.state_path(), folder.join("claude.exe"), check.clone()];
    let before: Vec<SystemTime> = watched.iter().map(|path| mtime(path)).collect();
    let state_before = read(&env.state_path());
    std::thread::sleep(Duration::from_millis(50));

    // Nothing changed: the second repair must write nothing at all.
    let second = env.run(&["repair", "--quiet", "--launcher", &launcher]);
    assert_eq!(second.status.code(), Some(0));
    let after: Vec<SystemTime> = watched.iter().map(|path| mtime(path)).collect();
    assert_eq!(before, after, "an idle repair must not write any file");
    assert_eq!(read(&env.state_path()), state_before);

    // A new Desktop version folder appears: the next repair patches it.
    let newer = env.real_folder("1.1.0", "bbbb");
    let third = env.run(&["repair", "--quiet", "--launcher", &launcher]);
    assert_eq!(third.status.code(), Some(0));
    assert_eq!(read(&newer.join("claude.exe")), LAUNCHER);
    assert_eq!(read(&newer.join("claude.cs-real.exe")), REAL);
}

#[test]
fn remove_restores_the_real_binary_byte_for_byte_and_clears_the_state() {
    let env = Env::new("remove");
    let folder = env.real_folder("1.0.0", "aaaa");
    fs::write(
        env.code_dir().join("codetab.json"),
        "{\"v\":1,\"enabled\":true}",
    )
    .unwrap();
    let (code, _) = env.install();
    assert_eq!(code, 0);

    let (code, value) = env.run_json(&["remove", "--json"]);
    assert_eq!(code, 0, "{value}");
    assert_eq!(state_of(&value, "aaaa"), "real");
    assert_eq!(value["config_removed"], Value::Bool(true));
    assert_eq!(read(&folder.join("claude.exe")), REAL);
    assert!(!folder.join("claude.cs-real.exe").exists());
    assert!(!env.state_path().exists());
    assert!(!env.code_dir().join("codetab.json").exists());
}

#[test]
fn a_payload_mismatch_is_foreign_and_left_alone() {
    let env = Env::new("foreign");
    let folder = env.folder("1.0.0", "ffff");
    fs::write(folder.join("claude.exe"), b"SOMEONE ELSE'S FILE").unwrap();
    write_payload(&folder, REAL);

    let (_, status) = env.run_json(&["status", "--json"]);
    assert_eq!(state_of(&status, "ffff"), "foreign");

    let (code, value) = env.install();
    assert_eq!(code, 0, "{value}");
    assert_eq!(state_of(&value, "ffff"), "foreign");
    assert_eq!(read(&folder.join("claude.exe")), b"SOMEONE ELSE'S FILE");
    assert!(!folder.join("claude.cs-real.exe").exists());
}

#[cfg(windows)]
#[test]
fn a_file_in_use_is_locked_then_patched_on_the_next_run() {
    use std::os::windows::fs::OpenOptionsExt;

    let env = Env::new("locked");
    let folder = env.real_folder("1.0.0", "aaaa");
    // Holding the file without sharing makes every other open fail with a sharing
    // violation. A scanner that has the file open for a moment can refuse this,
    // so retry briefly.
    let deadline = Instant::now() + Duration::from_secs(10);
    let hold = loop {
        match fs::OpenOptions::new()
            .read(true)
            .share_mode(0)
            .open(folder.join("claude.exe"))
        {
            Ok(file) => break file,
            Err(_) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(100)),
            Err(err) => panic!("could not hold claude.exe: {err}"),
        }
    };

    let (_, status) = env.run_json(&["status", "--json"]);
    assert_eq!(state_of(&status, "aaaa"), "locked");
    let (code, value) = env.install();
    assert_eq!(code, 0, "{value}");
    assert_eq!(state_of(&value, "aaaa"), "locked");
    assert!(
        !env.state_path().exists(),
        "nothing is recorded for a locked folder"
    );
    assert!(
        !folder.join("claude.cs-real.exe").exists(),
        "a locked folder is not touched"
    );

    drop(hold);
    assert_eq!(
        read(&folder.join("claude.exe")),
        REAL,
        "a locked folder is not touched"
    );
    let (code, value) = env.install();
    assert_eq!(code, 0);
    assert_eq!(state_of(&value, "aaaa"), "patched");
    assert_eq!(read(&folder.join("claude.exe")), LAUNCHER);
}

#[cfg(windows)]
#[test]
fn junctions_are_reported_and_their_targets_are_never_touched() {
    let env = Env::new("junction");
    // The target lies outside the roots, as a real link to elsewhere would.
    let target = env.root.join("elsewhere").join("target");
    fs::create_dir_all(&target).unwrap();
    fs::write(target.join("claude.exe"), REAL).unwrap();
    write_payload(&target, REAL);
    let link = env.roots.join("1.0.0").join("link");
    fs::create_dir_all(env.roots.join("1.0.0")).unwrap();
    let made = Command::new("cmd")
        .args(["/C", "mklink", "/J"])
        .arg(&link)
        .arg(&target)
        .output()
        .unwrap();
    assert!(
        made.status.success(),
        "mklink failed: {}",
        String::from_utf8_lossy(&made.stderr)
    );

    let (code, value) = env.install();
    assert_eq!(code, 0, "{value}");
    assert_eq!(state_of(&value, "link"), "reparse");
    assert_eq!(read(&target.join("claude.exe")), REAL);
    assert!(!target.join("claude.cs-real.exe").exists());
}

#[test]
fn status_names_every_state_and_the_autostart_flag() {
    let env = Env::new("status");
    env.real_folder("1.0.0", "real");
    let missing = env.folder("1.0.0", "missing");
    fs::write(missing.join("claude.cs-real.exe"), REAL).unwrap();
    write_payload(&missing, REAL);
    let foreign = env.folder("1.0.0", "foreign");
    fs::write(foreign.join("claude.exe"), b"other").unwrap();
    write_payload(&foreign, REAL);

    let (code, value) = env.run_json(&["status", "--json"]);
    assert_eq!(code, 0, "{value}");
    assert_eq!(state_of(&value, "real"), "real");
    assert_eq!(state_of(&value, "missing"), "missing");
    assert_eq!(state_of(&value, "foreign"), "foreign");
    assert_eq!(value["autostart"], Value::Bool(false));
}

#[test]
fn python_written_state_round_trips_through_status_and_remove() {
    let env = Env::new("python");
    let folder = env.folder("1.0.0", "fixture");
    fs::write(folder.join("claude.exe"), PY_LAUNCHER).unwrap();
    fs::write(folder.join("claude.cs-real.exe"), PY_REAL).unwrap();
    write_payload(&folder, PY_REAL);
    // Same escaping the Python patcher uses inside the state file.
    let escaped = serde_json::to_string(&folder.to_string_lossy().into_owned()).unwrap();
    let escaped = &escaped[1..escaped.len() - 1];
    fs::write(
        env.state_path(),
        PY_STATE_TEMPLATE.replace("__FOLDER__", escaped),
    )
    .unwrap();
    fs::write(env.code_dir().join("codetab.json"), PY_CONFIG).unwrap();

    let (_, status) = env.run_json(&["status", "--json"]);
    assert_eq!(state_of(&status, "fixture"), "patched", "{status}");

    // The Python launcher is the same bytes, so install recognises it as up to date.
    fs::write(&env.launcher, PY_LAUNCHER).unwrap();
    let (code, value) = env.install();
    assert_eq!(code, 0, "{value}");
    assert_eq!(state_of(&value, "fixture"), "patched");
    assert_eq!(read(&folder.join("claude.exe")), PY_LAUNCHER);

    let (code, value) = env.run_json(&["remove", "--json"]);
    assert_eq!(code, 0, "{value}");
    assert_eq!(state_of(&value, "fixture"), "real");
    assert_eq!(read(&folder.join("claude.exe")), PY_REAL);
    assert!(!folder.join("claude.cs-real.exe").exists());
    assert!(!env.state_path().exists());
    assert!(!env.code_dir().join("codetab.json").exists());
}

#[test]
fn a_malformed_state_file_stops_the_run_and_is_kept() {
    let env = Env::new("bad-state");
    let folder = env.real_folder("1.0.0", "aaaa");
    fs::write(env.state_path(), "{not json").unwrap();

    let output = env.run(&["install", "--launcher", &env.launcher.to_string_lossy()]);
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(read(&env.state_path()), b"{not json");
    assert_eq!(read(&folder.join("claude.exe")), REAL);
}

#[test]
fn a_missing_launcher_fails_before_anything_changes() {
    let env = Env::new("no-launcher");
    let folder = env.real_folder("1.0.0", "aaaa");
    let missing = env.root.join("nope.exe");
    let (code, value) = env.run_json_with(&["install", "--launcher", &missing.to_string_lossy()]);
    assert_eq!(code, 1);
    assert_eq!(value["ok"], Value::Bool(false));
    assert!(value["error"]
        .as_str()
        .unwrap()
        .contains("launcher not found"));
    assert_eq!(read(&folder.join("claude.exe")), REAL);
    assert!(!env.state_path().exists());
}

#[test]
fn a_lock_held_by_another_operation_is_reported_after_the_wait() {
    let env = Env::new("lock");
    env.real_folder("1.0.0", "aaaa");
    fs::write(
        env.code_dir().join("codetab.lock"),
        "1 2026-01-01T00:00:00+00:00\n",
    )
    .unwrap();
    let started = Instant::now();
    let output = env.run(&["install", "--launcher", &env.launcher.to_string_lossy()]);
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("another codetab operation"));
    assert!(started.elapsed() >= Duration::from_secs(4));
}

#[test]
fn usage_errors_exit_with_two() {
    let env = Env::new("usage");
    assert_eq!(env.run(&[]).status.code(), Some(2));
    assert_eq!(env.run(&["frobnicate"]).status.code(), Some(2));
    assert_eq!(env.run(&["install", "--bogus"]).status.code(), Some(2));
    assert_eq!(env.run(&["autostart", "maybe"]).status.code(), Some(2));
    assert_eq!(
        env.run(&["repair", "--quiet", "extra"]).status.code(),
        Some(2)
    );
}

// ---------------------------------------------------------------------------
// autostart: the sign-in shortcut, in the temp Startup folder only
// ---------------------------------------------------------------------------

#[test]
fn autostart_writes_once_and_removes_cleanly() {
    let env = Env::new("autostart");
    let shortcut = env.startup.join("ContextShrink Code tab.lnk");

    let output = env.run(&["autostart", "on"]);
    assert_eq!(
        output.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(shortcut.is_file(), "the shortcut should exist");
    let (_, status) = env.run_json(&["status", "--json"]);
    assert_eq!(status["autostart"], Value::Bool(true));

    let written = mtime(&shortcut);
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(env.run(&["autostart", "on"]).status.code(), Some(0));
    assert_eq!(
        mtime(&shortcut),
        written,
        "an unchanged shortcut is not rewritten"
    );

    assert_eq!(env.run(&["autostart", "off"]).status.code(), Some(0));
    assert!(!shortcut.exists());
    assert_eq!(
        env.run(&["autostart", "off"]).status.code(),
        Some(0),
        "off twice is fine"
    );
    let (_, status) = env.run_json(&["status", "--json"]);
    assert_eq!(status["autostart"], Value::Bool(false));
}

#[test]
fn autostart_rewrites_a_shortcut_that_points_elsewhere() {
    let env = Env::new("autostart-other");
    let shortcut = env.startup.join("ContextShrink Code tab.lnk");
    fs::write(&shortcut, b"not a real shortcut").unwrap();
    let output = env.run(&["autostart", "on"]);
    assert_eq!(
        output.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let (_, status) = env.run_json(&["status", "--json"]);
    assert_eq!(status["autostart"], Value::Bool(true));
}
