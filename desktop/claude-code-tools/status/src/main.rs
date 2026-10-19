//! contextshrink-status.exe
//!
//! Claude Code runs this as the user's statusLine command. It reads the
//! statusLine JSON on stdin (possibly empty or partial), prints one line for
//! that session from `%LOCALAPPDATA%\ContextShrink\claude-code\status.json`,
//! falls back to `status.txt`, and always exits 0.
//!
//! With `--detail` it prints the session, today, the last 7 days and all time
//! as a table for the `/contextshrink-savings` command.
//!
//! Output lines:
//! - `ContextShrink ✓ trimmed 22% · $1.84 of tokens at list price · saved $0.41 verified · 18 req`
//! - `ContextShrink off` when the heartbeat is older than 30 s
//! - `ContextShrink paused` when paused

use serde_json::Value;
use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const STDIN_CAP: u64 = 256 * 1024;
const STDIN_WAIT: Duration = Duration::from_millis(10);
const STATUS_JSON_CAP: u64 = 8 * 1024 * 1024;
const STATUS_TXT_CAP: u64 = 4 * 1024;
const STALE_AFTER_SECS: f64 = 30.0;
const OFF_LINE: &str = "ContextShrink off";
const PAUSED_LINE: &str = "ContextShrink paused";

fn main() {
    // The plugin's session.end hook: pass the session on to the forwarder and go.
    // Prints nothing; a hook's output would reach the person.
    if std::env::args_os().skip(1).any(|arg| arg == "--session-end") {
        if let Some(session) = read_session_id_within(SESSION_END_STDIN_WAIT) {
            post_session_end(&session);
        }
        std::process::exit(0);
    }
    let detail = std::env::args_os().skip(1).any(|arg| arg == "--detail");
    let session = read_session_id();
    let output = if detail {
        render_detail(session.as_deref())
    } else {
        render_line(session.as_deref())
    };
    let mut stdout = std::io::stdout().lock();
    let _ = stdout.write_all(output.as_bytes());
    let _ = stdout.write_all(b"\n");
    let _ = stdout.flush();
    std::process::exit(0);
}

// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------

/// Reads stdin on a helper thread for at most 10 ms, so a terminal or a pipe
/// that never closes cannot hold the status line up.
fn read_session_id() -> Option<String> {
    read_session_id_within(STDIN_WAIT)
}

/// The same, waiting up to `wait` for the hook's JSON (a session end is not on a
/// status line's clock, but the hook still has a short budget).
fn read_session_id_within(wait: Duration) -> Option<String> {
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = std::io::stdin().take(STDIN_CAP).read_to_end(&mut buffer);
        let _ = sender.send(buffer);
    });
    let buffer = receiver.recv_timeout(wait).unwrap_or_default();
    parse_session_id(&buffer)
}

// ---------------------------------------------------------------------------
// Session end
// ---------------------------------------------------------------------------

const FORWARDER_PORT: u16 = 18788;
const SESSION_END_PATH: &str = "/__cs/session-end";
const SESSION_END_STDIN_WAIT: Duration = Duration::from_millis(1000);
const SESSION_CONNECT_BUDGET: Duration = Duration::from_millis(300);
/// Whole session-end call. The plugin's session.end hook has about 1.5 s for
/// everything, so this stays well inside it.
const SESSION_END_BUDGET: Duration = Duration::from_millis(900);

/// The forwarder's loopback port; ``CONTEXTSHRINK_FORWARDER_PORT`` overrides it (tests).
fn forwarder_port() -> u16 {
    std::env::var("CONTEXTSHRINK_FORWARDER_PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .filter(|port| *port != 0)
        .unwrap_or(FORWARDER_PORT)
}

/// The HTTP/1.1 request that ends one session at the forwarder.
fn session_end_request(port: u16, session_id: &str) -> String {
    let body = serde_json::json!({ "session_id": session_id }).to_string();
    format!(
        "POST {SESSION_END_PATH} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\
         Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

fn time_left(deadline: Instant) -> Option<Duration> {
    deadline.checked_duration_since(Instant::now()).filter(|left| !left.is_zero())
}

/// Posts the session end to the forwarder and waits for its first reply byte, so
/// the forwarder has read the request. The forwarder itself is not running (or
/// is paused, or refuses) on most machines most of the time: then nothing happens
/// and the proxy's idle cap ends the session instead.
fn post_session_end(session_id: &str) -> bool {
    let port = forwarder_port();
    let deadline = Instant::now() + SESSION_END_BUDGET;
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    let connect_wait = SESSION_CONNECT_BUDGET.min(time_left(deadline).unwrap_or_default());
    let Ok(mut stream) = TcpStream::connect_timeout(&address, connect_wait) else {
        return false;
    };
    let request = session_end_request(port, session_id);
    let Some(write_wait) = time_left(deadline) else {
        return false;
    };
    if stream.set_write_timeout(Some(write_wait)).is_err()
        || stream.write_all(request.as_bytes()).is_err()
    {
        return false;
    }
    let Some(read_wait) = time_left(deadline) else {
        return true;
    };
    let _ = stream.set_read_timeout(Some(read_wait));
    let mut reply = [0u8; 16];
    matches!(stream.read(&mut reply), Ok(count) if count > 0)
}

fn parse_session_id(buffer: &[u8]) -> Option<String> {
    if let Ok(value) = serde_json::from_slice::<Value>(buffer) {
        return value
            .get("session_id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
            .map(str::to_string);
    }
    // Partial or invalid JSON: scan for the field by hand.
    let needle = b"\"session_id\"";
    let start = buffer
        .windows(needle.len())
        .position(|window| window == needle)?
        + needle.len();
    let rest = &buffer[start..];
    let rest = skip_ws_and_char(rest, b':')?;
    let rest = skip_ws_and_char(rest, b'"')?;
    let end = rest.iter().position(|&b| b == b'"')?;
    let id = std::str::from_utf8(&rest[..end]).ok()?;
    if id.is_empty() || id.len() > 256 {
        return None;
    }
    Some(id.to_string())
}

fn skip_ws_and_char(bytes: &[u8], wanted: u8) -> Option<&[u8]> {
    let mut index = 0;
    while index < bytes.len() && bytes[index].is_ascii_whitespace() {
        index += 1;
    }
    if bytes.get(index) != Some(&wanted) {
        return None;
    }
    index += 1;
    while index < bytes.len() && bytes[index].is_ascii_whitespace() {
        index += 1;
    }
    Some(&bytes[index..])
}

// ---------------------------------------------------------------------------
// Files
// ---------------------------------------------------------------------------

/// `<data dir>\claude-code`, where `CONTEXTSHRINK_DATA_DIR` overrides the
/// default `%LOCALAPPDATA%\ContextShrink` (used by tests).
fn claude_code_dir() -> Option<PathBuf> {
    let base = match std::env::var_os("CONTEXTSHRINK_DATA_DIR") {
        Some(value) if !value.is_empty() => PathBuf::from(value),
        _ => PathBuf::from(std::env::var_os("LOCALAPPDATA")?).join("ContextShrink"),
    };
    Some(base.join("claude-code"))
}

fn read_capped(path: &Path, cap: u64) -> Option<Vec<u8>> {
    let file = std::fs::File::open(path).ok()?;
    let mut bytes = Vec::new();
    file.take(cap + 1).read_to_end(&mut bytes).ok()?;
    if bytes.len() as u64 > cap {
        return None;
    }
    Some(bytes)
}

fn file_age_secs(path: &Path) -> Option<f64> {
    let modified = std::fs::metadata(path).ok()?.modified().ok()?;
    let secs = modified.duration_since(UNIX_EPOCH).ok()?.as_secs_f64();
    Some(now_secs() - secs)
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Parsed `status.json` and the heartbeat age in seconds.
struct StatusFile {
    value: Value,
    age_secs: f64,
}

fn load_status_json(dir: &Path) -> Option<StatusFile> {
    let path = dir.join("status.json");
    let bytes = read_capped(&path, STATUS_JSON_CAP)?;
    let value: Value = serde_json::from_slice(&bytes).ok()?;
    let age_secs = value
        .get("updated_at")
        .and_then(Value::as_str)
        .and_then(parse_iso8601)
        .map(|updated| now_secs() - updated)
        .or_else(|| file_age_secs(&path))?;
    Some(StatusFile { value, age_secs })
}

fn pause_file_exists(dir: &Path) -> bool {
    dir.join("paused").exists()
}

/// The first line of `status.txt`, when it is fresh.
fn status_txt_line(dir: &Path) -> Option<String> {
    let path = dir.join("status.txt");
    if file_age_secs(&path)? > STALE_AFTER_SECS {
        return Some(OFF_LINE.to_string());
    }
    let bytes = read_capped(&path, STATUS_TXT_CAP)?;
    let text = String::from_utf8_lossy(&bytes);
    let line = text.lines().next()?.trim();
    if line.is_empty() {
        None
    } else {
        Some(line.to_string())
    }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

fn render_line(session: Option<&str>) -> String {
    let dir = match claude_code_dir() {
        Some(dir) => dir,
        None => return OFF_LINE.to_string(),
    };
    let status = match load_status_json(&dir) {
        Some(status) => status,
        None => return status_txt_line(&dir).unwrap_or_else(|| OFF_LINE.to_string()),
    };
    if status.age_secs > STALE_AFTER_SECS {
        return OFF_LINE.to_string();
    }
    if is_paused(&status.value, &dir) {
        return PAUSED_LINE.to_string();
    }
    if let Some(entry) = session.and_then(|id| status.value.get("sessions")?.get(id)) {
        if entry.get("requests").and_then(Value::as_u64).unwrap_or(0) > 0 {
            return render_counters_line(entry);
        }
    }
    status_txt_line(&dir).unwrap_or_else(|| OFF_LINE.to_string())
}

fn is_paused(value: &Value, dir: &Path) -> bool {
    value.get("paused").and_then(Value::as_bool).unwrap_or(false) || pause_file_exists(dir)
}

fn number(entry: &Value, key: &str) -> f64 {
    entry.get(key).and_then(Value::as_f64).unwrap_or(0.0)
}

fn trimmed_percent(entry: &Value) -> u64 {
    let before = number(entry, "tokens_before");
    let after = number(entry, "tokens_after");
    if before <= 0.0 {
        return 0;
    }
    ((1.0 - after / before) * 100.0).clamp(0.0, 100.0).round() as u64
}

/// The verified figure as the line words it: a saving, a cost when the session's
/// keep-alive pings outweighed what they saved, or pending until one is reported.
fn verified_text(entry: &Value) -> String {
    if number(entry, "verified_requests") <= 0.0 {
        return "saved pending verified".to_string();
    }
    let verified = entry.get("verified_saved_usd").and_then(Value::as_f64).unwrap_or(0.0);
    if verified < 0.0 {
        format!("cost ${:.2} verified", -verified)
    } else {
        format!("saved ${:.2} verified", verified)
    }
}

fn render_counters_line(entry: &Value) -> String {
    let saved = verified_text(entry);
    format!(
        "ContextShrink \u{2713} trimmed {}% \u{b7} ${:.2} of tokens at list price \u{b7} {} \u{b7} {} req",
        trimmed_percent(entry),
        number(entry, "trimmed_list_usd"),
        saved,
        number(entry, "requests") as u64,
    )
}

/// The compaction window "Keep sessions lean" set for this Claude Code process,
/// from the environment it was started with. `None` when lean is off or the
/// value is not one the launcher would pass through.
fn lean_window() -> Option<u64> {
    lean_window_from(std::env::var("CLAUDE_CODE_AUTO_COMPACT_WINDOW").ok().as_deref())
}

/// Digits only, no leading zero, 100000 to 1000000. Same rule as the launcher.
fn lean_window_from(text: Option<&str>) -> Option<u64> {
    let text = text?;
    if text.is_empty() || text.len() > 7 || text.starts_with('0') {
        return None;
    }
    if !text.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let value: u64 = text.parse().ok()?;
    (100_000..=1_000_000).contains(&value).then_some(value)
}

/// `200000` as `200,000`.
fn with_commas(value: u64) -> String {
    let digits = value.to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

fn lean_line(window: Option<u64>) -> String {
    match window {
        Some(window) => format!("Lean sessions: on (compact at {} tokens)", with_commas(window)),
        None => "Lean sessions: off".to_string(),
    }
}

fn render_detail(session: Option<&str>) -> String {
    let dir = match claude_code_dir() {
        Some(dir) => dir,
        None => return format!("{OFF_LINE}: no data directory found"),
    };
    let status = match load_status_json(&dir) {
        Some(status) => status,
        None => {
            return format!(
                "{OFF_LINE}: no status yet. Start the ContextShrink forwarder, then send a request."
            )
        }
    };

    let health = if status.age_secs > STALE_AFTER_SECS {
        format!(
            "off: no heartbeat for {} s",
            status.age_secs.max(0.0) as u64
        )
    } else if is_paused(&status.value, &dir) {
        "paused".to_string()
    } else {
        format!("live, heartbeat {} s ago", status.age_secs.max(0.0) as u64)
    };

    let sessions = status.value.get("sessions");
    let chosen = session
        .filter(|id| sessions.and_then(|s| s.get(*id)).is_some())
        .map(str::to_string)
        .or_else(|| latest_session(sessions));

    let columns: [Column; 4] = [
        ("Session", chosen.as_deref().and_then(|id| sessions?.get(id))),
        ("Today", status.value.get("today")),
        ("Last 7 days", status.value.get("last_7_days")),
        ("All time", status.value.get("all_time")),
    ];

    let mut out = String::new();
    out.push_str(&format!("ContextShrink: {health}\n"));
    out.push_str(&lean_line(lean_window()));
    out.push('\n');
    match &chosen {
        Some(id) => out.push_str(&format!("Session: {id}\n")),
        None => out.push_str("Session: none yet\n"),
    }
    out.push('\n');

    let label_width = 26;
    let column_width = 14;
    let mut header = format!("{:<label_width$}", "");
    for (name, _) in &columns {
        header.push_str(&format!("{:>column_width$}", name));
    }
    out.push_str(header.trim_end());
    out.push('\n');

    let rows: [Row; 4] = [
        ("trimmed %", |entry| format!("{}%", trimmed_percent(entry))),
        ("$ of tokens at list price", |entry| {
            format!("${:.2}", number(entry, "trimmed_list_usd"))
        }),
        ("saved $ (verified)", |entry| {
            if number(entry, "verified_requests") > 0.0 {
                let verified = entry.get("verified_saved_usd").and_then(Value::as_f64).unwrap_or(0.0);
                if verified < 0.0 {
                    format!("-${:.2}", -verified)
                } else {
                    format!("${:.2}", verified)
                }
            } else {
                "pending".to_string()
            }
        }),
        ("requests", |entry| format!("{}", number(entry, "requests") as u64)),
    ];

    let mut any_pending = false;
    for (label, render) in rows {
        let mut line = format!("{:<label_width$}", label);
        for (_, entry) in &columns {
            let cell = match entry {
                Some(entry) if !entry.is_null() => {
                    let text = render(entry);
                    if text == "pending" {
                        any_pending = true;
                    }
                    text
                }
                _ => "-".to_string(),
            };
            line.push_str(&format!("{:>column_width$}", cell));
        }
        out.push_str(line.trim_end());
        out.push('\n');
    }

    if any_pending {
        out.push_str(
            "\npending: the proxy has not reported a verified saving yet; \
             the verified figure starts once the proxy build that reports it is live.\n",
        );
    }
    out.trim_end().to_string()
}

/// A column of the detail table: its heading and the counters to show.
type Column<'a> = (&'static str, Option<&'a Value>);

/// A row of the detail table: its label and how to render one cell.
type Row = (&'static str, fn(&Value) -> String);

fn latest_session(sessions: Option<&Value>) -> Option<String> {
    let map = sessions?.as_object()?;
    map.iter()
        .max_by(|(_, a), (_, b)| {
            let last_a = a.get("last_at").and_then(Value::as_str).unwrap_or("");
            let last_b = b.get("last_at").and_then(Value::as_str).unwrap_or("");
            last_a.cmp(last_b)
        })
        .map(|(id, _)| id.clone())
}

// ---------------------------------------------------------------------------
// Time
// ---------------------------------------------------------------------------

/// Parses `YYYY-MM-DDTHH:MM:SS[.fraction][Z|+HH:MM|-HH:MM]` into Unix seconds.
/// A missing offset is read as UTC.
fn parse_iso8601(text: &str) -> Option<f64> {
    let bytes = text.as_bytes();
    if bytes.len() < 19 || bytes[4] != b'-' || bytes[7] != b'-' || bytes[13] != b':' || bytes[16] != b':' {
        return None;
    }
    if !matches!(bytes[10], b'T' | b't' | b' ') {
        return None;
    }
    let year: i64 = text.get(0..4)?.parse().ok()?;
    let month: i64 = text.get(5..7)?.parse().ok()?;
    let day: i64 = text.get(8..10)?.parse().ok()?;
    let hour: i64 = text.get(11..13)?.parse().ok()?;
    let minute: i64 = text.get(14..16)?.parse().ok()?;
    let second: i64 = text.get(17..19)?.parse().ok()?;
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) || hour > 23 || minute > 59 || second > 60 {
        return None;
    }

    let mut index = 19;
    let mut fraction = 0.0;
    if bytes.get(index) == Some(&b'.') {
        index += 1;
        let start = index;
        while index < bytes.len() && bytes[index].is_ascii_digit() {
            index += 1;
        }
        let digits = &text[start..index];
        if !digits.is_empty() {
            fraction = format!("0.{digits}").parse::<f64>().ok()?;
        }
    }

    let mut offset_secs: i64 = 0;
    match bytes.get(index) {
        None | Some(b'Z') | Some(b'z') => {}
        Some(&sign) if sign == b'+' || sign == b'-' => {
            let hours: i64 = text.get(index + 1..index + 3)?.parse().ok()?;
            let minutes_start = if bytes.get(index + 3) == Some(&b':') {
                index + 4
            } else {
                index + 3
            };
            let minutes: i64 = text.get(minutes_start..minutes_start + 2)?.parse().ok()?;
            let total = hours * 3600 + minutes * 60;
            offset_secs = if sign == b'+' { total } else { -total };
        }
        _ => return None,
    }

    let days = days_from_civil(year, month, day);
    let whole = days * 86_400 + hour * 3_600 + minute * 60 + second - offset_secs;
    Some(whole as f64 + fraction)
}

/// Days since 1970-01-01 for a proleptic Gregorian date (Howard Hinnant's
/// algorithm).
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let year_of_era = y - era * 400;
    let month_index = if month > 2 { month - 3 } else { month + 9 };
    let day_of_year = (153 * month_index + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_session_id_from_full_json() {
        let input = br#"{"session_id":"abc-123","model":{"id":"x"}}"#;
        assert_eq!(parse_session_id(input).as_deref(), Some("abc-123"));
    }

    #[test]
    fn parses_session_id_from_partial_json() {
        assert_eq!(
            parse_session_id(br#"{"session_id" : "partial-7", "mod"#).as_deref(),
            Some("partial-7")
        );
    }

    #[test]
    fn empty_or_garbage_input_has_no_session() {
        assert_eq!(parse_session_id(b""), None);
        assert_eq!(parse_session_id(b"not json at all"), None);
        assert_eq!(parse_session_id(br#"{"session_id":""}"#), None);
    }

    #[test]
    fn iso_timestamps_parse_with_offsets_and_fractions() {
        assert_eq!(parse_iso8601("1970-01-01T00:00:00Z"), Some(0.0));
        assert_eq!(parse_iso8601("1970-01-01T01:00:00+01:00"), Some(0.0));
        assert_eq!(parse_iso8601("2026-10-18T12:00:00.500000+00:00"), Some(1_792_324_800.5));
        assert_eq!(parse_iso8601("2026-10-18T13:00:00+01:00"), Some(1_792_324_800.0));
        assert_eq!(parse_iso8601("not a date"), None);
    }

    #[test]
    fn trimmed_percent_is_clamped() {
        let entry = serde_json::json!({"tokens_before": 1000, "tokens_after": 780});
        assert_eq!(trimmed_percent(&entry), 22);
        let grown = serde_json::json!({"tokens_before": 100, "tokens_after": 150});
        assert_eq!(trimmed_percent(&grown), 0);
        let none = serde_json::json!({});
        assert_eq!(trimmed_percent(&none), 0);
    }

    #[test]
    fn counters_line_matches_contract_format() {
        let entry = serde_json::json!({
            "tokens_before": 1000, "tokens_after": 780, "requests": 18,
            "trimmed_list_usd": 1.84, "verified_saved_usd": 0.41, "verified_requests": 18
        });
        assert_eq!(
            render_counters_line(&entry),
            "ContextShrink \u{2713} trimmed 22% \u{b7} $1.84 of tokens at list price \u{b7} saved $0.41 verified \u{b7} 18 req"
        );
    }

    #[test]
    fn verified_figure_reads_pending_until_reported() {
        let entry = serde_json::json!({
            "tokens_before": 1000, "tokens_after": 780, "requests": 2,
            "trimmed_list_usd": 0.5, "verified_saved_usd": 0.0, "verified_requests": 0
        });
        assert!(render_counters_line(&entry).contains("saved pending verified"));
    }

    #[test]
    fn a_negative_verified_figure_reads_as_a_cost() {
        let entry = serde_json::json!({
            "tokens_before": 1000, "tokens_after": 1000, "requests": 3,
            "trimmed_list_usd": 0.0, "verified_saved_usd": -0.42, "verified_requests": 3
        });
        assert!(render_counters_line(&entry).ends_with("cost $0.42 verified \u{b7} 3 req"));
        assert!(!render_counters_line(&entry).contains("saved $"));
    }

    #[test]
    fn session_end_request_names_the_route_and_carries_the_session_as_json() {
        let request = session_end_request(18788, "abc-123");
        assert!(request.starts_with("POST /__cs/session-end HTTP/1.1\r\nHost: 127.0.0.1:18788\r\n"));
        let body = request.split("\r\n\r\n").nth(1).unwrap();
        assert_eq!(body, r#"{"session_id":"abc-123"}"#);
        assert!(request.contains(&format!("Content-Length: {}\r\n", body.len())));
    }

    #[test]
    fn a_session_id_is_read_from_the_hook_json_on_stdin() {
        assert_eq!(
            parse_session_id(br#"{"session_id":"s-9","reason":"clear"}"#).as_deref(),
            Some("s-9")
        );
    }

    #[test]
    fn lean_window_accepts_only_the_launcher_form_and_range() {
        assert_eq!(lean_window_from(Some("200000")), Some(200_000));
        assert_eq!(lean_window_from(Some("100000")), Some(100_000));
        assert_eq!(lean_window_from(Some("1000000")), Some(1_000_000));
        for bad in ["", "200k", "99999", "1000001", "0200000", " 200000", "-200000", "2e5"] {
            assert_eq!(lean_window_from(Some(bad)), None, "{bad:?}");
        }
        assert_eq!(lean_window_from(None), None);
    }

    #[test]
    fn lean_line_says_on_with_the_window_or_off() {
        assert_eq!(
            lean_line(Some(200_000)),
            "Lean sessions: on (compact at 200,000 tokens)"
        );
        assert_eq!(lean_line(None), "Lean sessions: off");
        assert_eq!(with_commas(100_000), "100,000");
        assert_eq!(with_commas(999), "999");
    }
}
