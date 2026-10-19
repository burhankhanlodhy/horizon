//! End-to-end tests for contextshrink-status against a fake data directory.

use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static COUNTER: AtomicU64 = AtomicU64::new(0);

struct Data {
    root: PathBuf,
}

impl Data {
    fn new(name: &str) -> Data {
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let unique = COUNTER.fetch_add(1, Ordering::SeqCst);
        let root = std::env::temp_dir().join(format!(
            "cs-status-{name}-{}-{nanos}-{unique}",
            std::process::id()
        ));
        fs::create_dir_all(root.join("claude-code")).unwrap();
        Data { root }
    }

    fn code_dir(&self) -> PathBuf {
        self.root.join("claude-code")
    }

    fn write(&self, name: &str, body: &str) {
        fs::write(self.code_dir().join(name), body).unwrap();
    }

    fn run(&self, args: &[&str], stdin: &[u8]) -> (i32, String) {
        let mut child = Command::new(env!("CARGO_BIN_EXE_contextshrink-status"))
            .args(args)
            .env("CONTEXTSHRINK_DATA_DIR", &self.root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        {
            let mut input = child.stdin.take().unwrap();
            let _ = input.write_all(stdin);
        }
        let output = child.wait_with_output().unwrap();
        (
            output.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&output.stdout).into_owned(),
        )
    }
}

impl Drop for Data {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn now_secs() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64
}

/// ISO-8601 UTC timestamp for a Unix time, as the forwarder writes it.
fn iso(secs: i64) -> String {
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    // Civil-from-days (Howard Hinnant).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let mut year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    if month <= 2 {
        year += 1;
    }
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.000000+00:00",
        rem / 3600,
        (rem % 3600) / 60,
        rem % 60
    )
}

fn status_json(updated: i64, paused: bool) -> String {
    let session = |requests: u64, before: f64, after: f64, list: f64, saved: f64, verified: u64| {
        format!(
            "{{\"first_at\":\"{t}\",\"last_at\":\"{t}\",\"requests\":{requests},\"model\":\"claude\",\
             \"tokens_before\":{before},\"tokens_after\":{after},\"trimmed_list_usd\":{list},\
             \"verified_saved_usd\":{saved},\"verified_requests\":{verified},\
             \"cache\":{{\"last_write_at\":\"\",\"ttl_s\":300,\"keepalive\":false}}}}",
            t = iso(updated)
        )
    };
    let counters = "{\"requests\":18,\"tokens_before\":10000,\"tokens_after\":7800,\"trimmed_list_usd\":1.84,\"verified_saved_usd\":0.41,\"verified_requests\":18}";
    format!(
        "{{\"v\":1,\"updated_at\":\"{updated_at}\",\"forwarder\":{{\"pid\":1,\"version\":\"0.1.0\",\"port\":18788,\"healthy\":true}},\
         \"paused\":{paused},\"sessions\":{{\"sess-a\":{a},\"sess-b\":{b}}},\
         \"today\":{counters},\"last_7_days\":{counters},\"all_time\":{counters}}}",
        updated_at = iso(updated),
        a = session(18, 10000.0, 7800.0, 1.84, 0.41, 18),
        b = session(2, 500.0, 400.0, 0.05, 0.0, 0),
    )
}

const FRESH_LINE: &str = "ContextShrink \u{2713} trimmed 22% \u{b7} $1.84 of tokens at list price \u{b7} saved $0.41 verified \u{b7} 18 req";

#[test]
fn prints_session_line_for_matching_session_id() {
    let data = Data::new("session");
    data.write("status.json", &status_json(now_secs(), false));
    let (code, out) = data.run(&[], br#"{"session_id":"sess-a","model":{}}"#);
    assert_eq!(code, 0);
    assert_eq!(out.trim_end(), FRESH_LINE);
}

#[test]
fn pending_verified_figure_for_session_without_verified_requests() {
    let data = Data::new("pending");
    data.write("status.json", &status_json(now_secs(), false));
    let (_, out) = data.run(&[], br#"{"session_id":"sess-b"}"#);
    assert!(out.contains("saved pending verified"), "{out}");
    assert!(out.contains("2 req"), "{out}");
}

#[test]
fn empty_stdin_uses_status_txt_fallback() {
    let data = Data::new("txt");
    data.write("status.json", &status_json(now_secs(), false));
    data.write("status.txt", &format!("{FRESH_LINE}\n"));
    let (code, out) = data.run(&[], b"");
    assert_eq!(code, 0);
    assert_eq!(out.trim_end(), FRESH_LINE);
}

#[test]
fn unknown_session_falls_back_to_status_txt() {
    let data = Data::new("unknown");
    data.write("status.json", &status_json(now_secs(), false));
    data.write("status.txt", "latest-session line\n");
    let (_, out) = data.run(&[], br#"{"session_id":"never-seen"}"#);
    assert_eq!(out.trim_end(), "latest-session line");
}

#[test]
fn stale_heartbeat_reads_off() {
    let data = Data::new("stale");
    data.write("status.json", &status_json(now_secs() - 120, false));
    data.write("status.txt", &format!("{FRESH_LINE}\n"));
    let (code, out) = data.run(&[], br#"{"session_id":"sess-a"}"#);
    assert_eq!(code, 0);
    assert_eq!(out.trim_end(), "ContextShrink off");
}

#[test]
fn paused_reads_paused() {
    let data = Data::new("paused");
    data.write("status.json", &status_json(now_secs(), true));
    let (_, out) = data.run(&[], br#"{"session_id":"sess-a"}"#);
    assert_eq!(out.trim_end(), "ContextShrink paused");
}

#[test]
fn pause_file_alone_reads_paused() {
    let data = Data::new("pausefile");
    data.write("status.json", &status_json(now_secs(), false));
    data.write("paused", "");
    let (_, out) = data.run(&[], br#"{"session_id":"sess-a"}"#);
    assert_eq!(out.trim_end(), "ContextShrink paused");
}

#[test]
fn missing_data_reads_off_and_exits_zero() {
    let data = Data::new("empty");
    let (code, out) = data.run(&[], b"");
    assert_eq!(code, 0);
    assert_eq!(out.trim_end(), "ContextShrink off");
}

#[test]
fn partial_and_garbage_stdin_still_exits_zero() {
    let data = Data::new("partial");
    data.write("status.json", &status_json(now_secs(), false));
    data.write("status.txt", "fallback line\n");
    for stdin in [&b"{\"session_id\":\"sess-a\",\"mo"[..], &b"\xff\xfe garbage"[..]] {
        let (code, _) = data.run(&[], stdin);
        assert_eq!(code, 0);
    }
}

#[test]
fn detail_table_uses_the_three_exact_labels() {
    let data = Data::new("detail");
    data.write("status.json", &status_json(now_secs(), false));
    let (code, out) = data.run(&["--detail"], br#"{"session_id":"sess-a"}"#);
    assert_eq!(code, 0);
    assert!(out.contains("trimmed %"), "{out}");
    assert!(out.contains("$ of tokens at list price"), "{out}");
    assert!(out.contains("saved $ (verified)"), "{out}");
    assert!(out.contains("Session"), "{out}");
    assert!(out.contains("Today"), "{out}");
    assert!(out.contains("Last 7 days"), "{out}");
    assert!(out.contains("All time"), "{out}");
    assert!(out.contains("22%"), "{out}");
    assert!(out.contains("$1.84"), "{out}");
    assert!(out.contains("$0.41"), "{out}");
}

#[test]
fn detail_without_status_explains_itself() {
    let data = Data::new("detail-empty");
    let (code, out) = data.run(&["--detail"], b"");
    assert_eq!(code, 0);
    assert!(out.contains("no status yet"), "{out}");
}
