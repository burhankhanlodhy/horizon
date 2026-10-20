//! Patching Claude Desktop's Code-tab folders. A port of the folder logic in
//! `horizon/desktop_codetab.py`, with the same states, the same safety rules
//! and the same state file, so either patcher can undo the other's work.
//!
//! Safety rules, all enforced here:
//! - a real binary is never deleted; a rename happens only after its sha256
//!   matches `.payload`, and the check is repeated just before the rename;
//! - `claude.exe` is replaced only when it is absent, holds the real binary, or
//!   is a launcher this install recorded; any other file is displaced, never deleted;
//! - a locked file skips that folder and is reported as `locked`;
//! - a failed step rolls back to a runnable real `claude.exe`;
//! - reparse points (junctions and symlinks) are never followed.

use crate::fsx::{
    failed, hex, io_fail, is_reparse, is_reparse_meta, iso_utc_now, sha256_file, unique_sibling,
    unix_now, write_json_if_changed, Fail, Lock, Res,
};
use crate::state::{read_config_object, State, STATE_NAME};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::time::UNIX_EPOCH;

pub const REAL_NAME: &str = "claude.exe";
pub const STASH_NAME: &str = "claude.cs-real.exe";
pub const PAYLOAD_NAME: &str = ".payload";
pub const CONFIG_NAME: &str = "codetab.json";
pub const CHECK_NAME: &str = "codetab-repair.json";
const DISPLACED_STEM: &str = "claude.exe.cs-displaced";
/// `repair` re-verifies everything at least this often, even when nothing it
/// looks at has changed.
const FULL_PASS_SECS: u64 = 6 * 3600;

/// One folder's outcome: `status` is the folder's state, `action` what was done.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Report {
    pub folder: PathBuf,
    pub status: String,
    pub action: String,
    pub detail: String,
}

impl Report {
    fn new(folder: &Path, status: &str, action: &str, detail: impl Into<String>) -> Report {
        Report {
            folder: folder.to_path_buf(),
            status: status.to_string(),
            action: action.to_string(),
            detail: detail.into(),
        }
    }

    /// The human line: action, status, folder and detail.
    pub fn line(&self) -> String {
        let suffix = if self.detail.is_empty() {
            String::new()
        } else {
            format!(" - {}", self.detail)
        };
        format!(
            "{:<10} {:<10} {}{suffix}",
            self.action,
            self.status,
            self.folder.display()
        )
    }
}

/// Everything one operation works on.
#[derive(Debug, Clone)]
pub struct Paths {
    pub roots: Vec<PathBuf>,
    pub code_dir: PathBuf,
    pub launcher: PathBuf,
}

impl Paths {
    pub fn config(&self) -> PathBuf {
        self.code_dir.join(CONFIG_NAME)
    }

    fn state_file(&self) -> PathBuf {
        self.code_dir.join(STATE_NAME)
    }
}

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

/// Every `<root>\<version>\<hash>` folder that holds a Claude binary, plus a
/// skipped report for each link on the way. Links are never followed.
pub fn scan(roots: &[PathBuf]) -> (Vec<PathBuf>, Vec<Report>) {
    let mut folders = Vec::new();
    let mut skipped = Vec::new();
    for root in roots {
        if is_reparse(root) {
            skipped.push(Report::new(
                root,
                "reparse",
                "skipped",
                "root is a link; not followed",
            ));
            continue;
        }
        for version in sorted_children(root) {
            if is_reparse(&version) {
                skipped.push(Report::new(
                    &version,
                    "reparse",
                    "skipped",
                    "link; not followed",
                ));
                continue;
            }
            if !version.is_dir() {
                continue;
            }
            for folder in sorted_children(&version) {
                if is_reparse(&folder) {
                    skipped.push(Report::new(
                        &folder,
                        "reparse",
                        "skipped",
                        "junction or symlink; not followed",
                    ));
                    continue;
                }
                if !folder.is_dir() {
                    continue;
                }
                let holds_binary = [REAL_NAME, STASH_NAME, PAYLOAD_NAME]
                    .iter()
                    .any(|name| folder.join(name).exists());
                if holds_binary {
                    folders.push(folder);
                }
            }
        }
    }
    (folders, skipped)
}

fn sorted_children(dir: &Path) -> Vec<PathBuf> {
    let Ok(entries) = fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut children: Vec<PathBuf> = entries
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .collect();
    children.sort();
    children
}

/// True when `folder` is strictly below a root and no component on the way is a link.
fn inside_roots_without_links(folder: &Path, roots: &[PathBuf]) -> bool {
    for root in roots {
        let Ok(relative) = folder.strip_prefix(root) else {
            continue;
        };
        if relative.as_os_str().is_empty() {
            return false;
        }
        let mut current = root.clone();
        for component in relative.components() {
            match component {
                Component::Normal(part) => current.push(part),
                _ => return false,
            }
            if is_reparse(&current) {
                return false;
            }
        }
        return true;
    }
    false
}

// ---------------------------------------------------------------------------
// Payloads and hashes
// ---------------------------------------------------------------------------

fn valid_sha(text: &str) -> bool {
    text.len() == 64
        && text
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// The sha256 recorded in `.payload`, or `None` when it is missing or malformed.
fn read_payload(folder: &Path) -> Option<String> {
    let text = fs::read_to_string(folder.join(PAYLOAD_NAME)).ok()?;
    let value: Value = serde_json::from_str(&text).ok()?;
    let sha = value
        .as_object()?
        .get("sha256")?
        .as_str()?
        .to_ascii_lowercase();
    valid_sha(&sha).then_some(sha)
}

/// The sha256 of `path`, or `None` when it is absent, not a regular file, or a link.
fn current_sha(path: &Path) -> Res<Option<String>> {
    let Ok(meta) = fs::symlink_metadata(path) else {
        return Ok(None);
    };
    if is_reparse_meta(&meta) || !meta.is_file() {
        return Ok(None);
    }
    sha256_file(path)
        .map(Some)
        .map_err(|err| io_fail(path.display(), err))
}

fn key_of(folder: &Path) -> String {
    folder.to_string_lossy().into_owned()
}

fn same_sha(actual: Option<&str>, expected: &str) -> bool {
    actual == Some(expected)
}

fn folder_record(real_sha: &str, launcher_sha: &str) -> Map<String, Value> {
    let mut record = Map::new();
    record.insert("real_sha256".into(), Value::from(real_sha));
    record.insert("launcher_sha256".into(), Value::from(launcher_sha));
    record.insert("patched_at".into(), Value::from(iso_utc_now()));
    record
}

// ---------------------------------------------------------------------------
// Per-folder steps
// ---------------------------------------------------------------------------

/// Installs the launcher as `claude.exe` in one folder. Never fails: a failure
/// becomes a `locked` or `error` report for this folder only.
fn install_folder(
    folder: &Path,
    paths: &Paths,
    launcher_sha: &str,
    known: &[String],
    state: &mut State,
) -> Report {
    match install_folder_checked(folder, &paths.launcher, launcher_sha, known, state) {
        Ok(report) => report,
        Err(Fail::Locked(detail)) => Report::new(
            folder,
            "locked",
            "skipped",
            format!("a file is in use; will retry ({detail})"),
        ),
        Err(Fail::Failed(detail)) => Report::new(folder, "error", "skipped", detail),
    }
}

fn install_folder_checked(
    folder: &Path,
    launcher: &Path,
    launcher_sha: &str,
    known: &[String],
    state: &mut State,
) -> Res<Report> {
    let Some(expect) = read_payload(folder) else {
        return Ok(Report::new(
            folder,
            "no-payload",
            "skipped",
            ".payload is missing or malformed",
        ));
    };
    let real = folder.join(REAL_NAME);
    let stash = folder.join(STASH_NAME);
    let key = key_of(folder);
    if is_reparse(&real) || is_reparse(&stash) {
        return Ok(Report::new(
            folder,
            "reparse",
            "skipped",
            "claude.exe or its copy is a link; not followed",
        ));
    }

    let real_sha = current_sha(&real)?;
    let stash_sha = current_sha(&stash)?;
    let stash_exists = stash.exists();
    let stash_ok = same_sha(stash_sha.as_deref(), &expect);

    match real_sha.as_deref() {
        // The real binary is in place: move it aside, then put the launcher there.
        Some(sha) if sha == expect => {
            if stash_exists {
                return Ok(Report::new(
                    folder,
                    "conflict",
                    "skipped",
                    "claude.cs-real.exe already exists; left both files alone",
                ));
            }
            fs::rename(&real, &stash).map_err(|err| io_fail(real.display(), err))?;
            if let Err(err) = place_launcher(folder, launcher, launcher_sha, None) {
                restore_real(&stash, &real, &expect)?;
                return Err(err);
            }
            state.set_folder(&key, folder_record(&expect, launcher_sha));
            Ok(Report::new(folder, "real", "patched", ""))
        }
        // claude.exe is missing but the verified real binary is stashed.
        None => {
            if !stash_ok {
                return Ok(Report::new(
                    folder,
                    "missing",
                    "skipped",
                    "no verified real binary",
                ));
            }
            place_launcher(folder, launcher, launcher_sha, None)?;
            state.set_folder(&key, folder_record(&expect, launcher_sha));
            Ok(Report::new(
                folder,
                "patched",
                "patched",
                "restored the launcher",
            ))
        }
        // Our launcher is in place and the real binary is stashed and verified.
        Some(sha) if contains(known, sha) && stash_ok => {
            if sha == launcher_sha {
                state.ensure_folder(&key, folder_record(&expect, launcher_sha));
                return Ok(Report::new(folder, "patched", "up-to-date", ""));
            }
            place_launcher(folder, launcher, launcher_sha, Some(sha))?;
            let mut record = folder_record(&expect, launcher_sha);
            let displaced = state
                .folder(&key)
                .and_then(|previous| previous.get("displaced"))
                .cloned()
                .unwrap_or_else(|| Value::Array(Vec::new()));
            record.insert("displaced".into(), displaced);
            state.set_folder(&key, record);
            Ok(Report::new(
                folder,
                "patched",
                "updated",
                "replaced an older launcher",
            ))
        }
        Some(sha) if contains(known, sha) => Ok(Report::new(
            folder,
            "conflict",
            "skipped",
            "launcher present but claude.cs-real.exe does not match .payload",
        )),
        // A foreign file sits in claude.exe. Only accepted when the real binary is
        // stashed and verified; the foreign file is renamed aside, never deleted.
        Some(_) => {
            if !stash_ok {
                return Ok(Report::new(
                    folder,
                    "foreign",
                    "skipped",
                    "claude.exe is not the real binary and claude.cs-real.exe is not verified",
                ));
            }
            let displaced = next_displaced_name(folder);
            fs::rename(&real, folder.join(&displaced))
                .map_err(|err| io_fail(real.display(), err))?;
            if let Err(err) = place_launcher(folder, launcher, launcher_sha, None) {
                restore_real(&stash, &real, &expect)?;
                return Err(err);
            }
            let mut record = folder_record(&expect, launcher_sha);
            record.insert(
                "displaced".into(),
                Value::Array(vec![Value::String(displaced.clone())]),
            );
            state.set_folder(&key, record);
            Ok(Report::new(
                folder,
                "patched",
                "patched",
                format!("moved the foreign claude.exe to {displaced}"),
            ))
        }
    }
}

fn contains(list: &[String], sha: &str) -> bool {
    list.iter().any(|known| known == sha)
}

fn next_displaced_name(folder: &Path) -> String {
    let mut index = 1;
    while folder.join(format!("{DISPLACED_STEM}-{index}")).exists() {
        index += 1;
    }
    format!("{DISPLACED_STEM}-{index}")
}

/// Copies the launcher to a unique temp name in `folder`, checks its hash, and
/// renames it over `claude.exe`. `expect` is what `claude.exe` must still hold.
fn place_launcher(
    folder: &Path,
    launcher: &Path,
    launcher_sha: &str,
    expect: Option<&str>,
) -> Res<()> {
    let tmp = unique_sibling(&folder.join(".cs-launcher"), "tmp");
    let result = (|| -> Res<()> {
        fs::copy(launcher, &tmp).map_err(|err| io_fail(launcher.display(), err))?;
        let copied = sha256_file(&tmp).map_err(|err| io_fail(tmp.display(), err))?;
        if copied != launcher_sha {
            return Err(failed(format!(
                "copied launcher does not match the source for {}",
                folder.display()
            )));
        }
        replace_checked(&tmp, &folder.join(REAL_NAME), expect)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

/// Renames `source` over `target` only while `target` still holds `expect`
/// (`None` meaning absent). Never overwrites anything else.
fn replace_checked(source: &Path, target: &Path, expect: Option<&str>) -> Res<()> {
    if current_sha(target)?.as_deref() != expect {
        return Err(failed(format!(
            "{} changed while it was being patched; not overwriting it",
            target.display()
        )));
    }
    fs::rename(source, target).map_err(|err| io_fail(target.display(), err))
}

/// Rolls back: puts the verified real binary back at `claude.exe`.
fn restore_real(stash: &Path, real: &Path, expect: &str) -> Res<()> {
    if real.exists() {
        return Ok(());
    }
    let verified = stash.is_file() && same_sha(current_sha(stash)?.as_deref(), expect);
    if !verified {
        return Err(failed(format!(
            "could not roll back: claude.exe is missing and {} is not verified; rename {} to {} by hand",
            STASH_NAME,
            stash.display(),
            real.display()
        )));
    }
    fs::rename(stash, real).map_err(|err| io_fail(real.display(), err))
}

/// The state a folder is in, as `status` reports it.
fn classify(folder: &Path, known: &[String]) -> Res<(String, String)> {
    let Some(expect) = read_payload(folder) else {
        return Ok((
            "no-payload".into(),
            ".payload is missing or malformed".into(),
        ));
    };
    let real_sha = current_sha(&folder.join(REAL_NAME))?;
    let stash = folder.join(STASH_NAME);
    let stash_sha = current_sha(&stash)?;
    let stash_ok = same_sha(stash_sha.as_deref(), &expect);
    let (status, detail) = match real_sha.as_deref() {
        None if stash_ok => (
            "missing",
            "claude.exe missing; the verified real binary is claude.cs-real.exe",
        ),
        None => ("missing", "claude.exe is missing"),
        Some(sha) if sha == expect => {
            if stash.exists() {
                (
                    "conflict",
                    "a second real copy exists as claude.cs-real.exe",
                )
            } else {
                ("real", "unpatched")
            }
        }
        Some(sha) if contains(known, sha) => {
            if stash_ok {
                ("patched", "routed through the launcher")
            } else {
                (
                    "conflict",
                    "launcher present but claude.cs-real.exe does not match .payload",
                )
            }
        }
        Some(_) => (
            "foreign",
            "claude.exe is another program's trampoline or file",
        ),
    };
    Ok((status.to_string(), detail.to_string()))
}

/// Restores one folder's real binary and removes the launcher this install placed.
fn remove_folder(folder: &Path, state: &mut State) -> Report {
    match remove_folder_checked(folder, state) {
        Ok(report) => report,
        Err(Fail::Locked(detail)) => Report::new(
            folder,
            "locked",
            "skipped",
            format!("a file is in use; will retry ({detail})"),
        ),
        Err(Fail::Failed(detail)) => Report::new(folder, "error", "skipped", detail),
    }
}

fn remove_folder_checked(folder: &Path, state: &mut State) -> Res<Report> {
    let Some(expect) = read_payload(folder) else {
        return Ok(Report::new(
            folder,
            "no-payload",
            "skipped",
            ".payload is missing or malformed",
        ));
    };
    let real = folder.join(REAL_NAME);
    let stash = folder.join(STASH_NAME);
    let key = key_of(folder);
    if is_reparse(&real) || is_reparse(&stash) {
        return Ok(Report::new(
            folder,
            "reparse",
            "skipped",
            "claude.exe or its copy is a link; not followed",
        ));
    }

    let real_sha = current_sha(&real)?;
    if !stash.is_file() {
        if same_sha(real_sha.as_deref(), &expect) {
            state.remove_folder(&key);
            return Ok(Report::new(folder, "real", "up-to-date", ""));
        }
        return Ok(Report::new(
            folder,
            "missing",
            "skipped",
            "claude.cs-real.exe is missing",
        ));
    }
    let stash_sha = current_sha(&stash)?;
    if !same_sha(stash_sha.as_deref(), &expect) {
        return Ok(Report::new(
            folder,
            "conflict",
            "skipped",
            "claude.cs-real.exe does not match .payload; not restoring",
        ));
    }
    if same_sha(real_sha.as_deref(), &expect) {
        return Ok(Report::new(
            folder,
            "conflict",
            "skipped",
            "claude.exe is already the real binary; claude.cs-real.exe left in place",
        ));
    }
    // claude.exe counts as ours only when it matches the launcher hash this
    // install recorded for this folder, and that hash is in the state file.
    let recorded = state
        .folder(&key)
        .and_then(|record| record.get("launcher_sha256"))
        .and_then(Value::as_str)
        .map(str::to_string);
    let ours = match (&real_sha, &recorded) {
        (Some(sha), Some(recorded)) => sha == recorded && state.has_launcher(sha),
        _ => false,
    };
    if real_sha.is_some() && !ours {
        return Ok(Report::new(
            folder,
            "foreign",
            "skipped",
            "claude.exe is not a launcher this install placed; left as is",
        ));
    }
    replace_checked(&stash, &real, real_sha.as_deref())?;
    if !same_sha(current_sha(&real)?.as_deref(), &expect) {
        return Err(failed(format!(
            "restored {} does not match .payload",
            real.display()
        )));
    }
    state.remove_folder(&key);
    Ok(Report::new(folder, "real", "restored", ""))
}

// ---------------------------------------------------------------------------
// Operations
// ---------------------------------------------------------------------------

/// Patches every eligible folder and records what was done. Writes no config:
/// the config is the npx installer's (or the desktop app's) to write.
pub fn install(paths: &Paths) -> Res<Vec<Report>> {
    if !paths.launcher.is_file() {
        return Err(failed(format!(
            "launcher not found: {}. Put claude-code-launcher.exe there or pass --launcher.",
            paths.launcher.display()
        )));
    }
    let launcher_sha =
        sha256_file(&paths.launcher).map_err(|err| io_fail(paths.launcher.display(), err))?;
    let _lock = Lock::take(&paths.code_dir)?;
    let mut state = State::load(&paths.code_dir)?;
    let mut known = state.launchers.clone();
    if !contains(&known, &launcher_sha) {
        known.push(launcher_sha.clone());
    }
    let (folders, skipped) = scan(&paths.roots);
    let mut reports = skipped;
    for folder in folders {
        let report = install_folder(&folder, paths, &launcher_sha, &known, &mut state);
        // Recorded only once a launcher copy has actually been placed.
        if report.action == "patched" || report.action == "updated" {
            state.add_launcher(&launcher_sha);
        }
        reports.push(report);
    }
    state.save(&paths.code_dir)?;
    Ok(reports)
}

/// What `remove` did, besides the per-folder reports.
#[derive(Debug, Default)]
pub struct RemoveOutcome {
    pub reports: Vec<Report>,
    pub config_removed: bool,
    pub config_disabled: bool,
}

/// Restores the real `claude.exe` everywhere the state records a patch, then
/// deletes the state. The config is deleted when nothing is left patched, and
/// disabled when something is still patched or locked.
pub fn remove(paths: &Paths) -> Res<RemoveOutcome> {
    let _lock = Lock::take(&paths.code_dir)?;
    let mut state = State::load(&paths.code_dir)?;
    let (scanned, skipped) = scan(&paths.roots);
    let mut outcome = RemoveOutcome {
        reports: skipped,
        ..RemoveOutcome::default()
    };
    let mut targets = scanned;
    let seen: HashSet<String> = targets.iter().map(|folder| key_of(folder)).collect();
    for key in state.folder_keys() {
        if seen.contains(&key) {
            continue;
        }
        let candidate = PathBuf::from(&key);
        if !inside_roots_without_links(&candidate, &paths.roots) {
            outcome.reports.push(Report::new(
                &candidate,
                "outside-roots",
                "skipped",
                "state entry is outside the roots; not touched",
            ));
            continue;
        }
        targets.push(candidate);
    }
    for folder in &targets {
        let report = remove_folder(folder, &mut state);
        outcome.reports.push(report);
    }
    if state.folder_count() == 0 {
        state.clear_launchers();
    }

    let config_path = paths.config();
    if state.folder_count() > 0 {
        // Something is still patched or locked: keep the config, but disabled.
        if let Some(mut config) = read_config_object(&config_path) {
            if config.get("enabled") != Some(&Value::Bool(false)) {
                config.insert("enabled".into(), Value::Bool(false));
                write_json_if_changed(&config_path, &Value::Object(config))?;
                outcome.config_disabled = true;
            }
        }
    } else if config_path.exists() {
        fs::remove_file(&config_path).map_err(|err| io_fail(config_path.display(), err))?;
        outcome.config_removed = true;
    }
    state.save(&paths.code_dir)?;
    Ok(outcome)
}

/// The state of every folder, without changing anything.
pub fn status(paths: &Paths) -> Res<Vec<Report>> {
    let state = State::load(&paths.code_dir)?;
    let mut known = state.launchers.clone();
    if paths.launcher.is_file() {
        if let Ok(sha) = sha256_file(&paths.launcher) {
            known.push(sha);
        }
    }
    let (folders, skipped) = scan(&paths.roots);
    let mut reports = skipped;
    for folder in folders {
        match classify(&folder, &known) {
            Ok((status, detail)) => reports.push(Report::new(&folder, &status, "-", detail)),
            Err(Fail::Locked(_)) => reports.push(Report::new(
                &folder,
                "locked",
                "-",
                "a file is in use; will retry",
            )),
            Err(Fail::Failed(detail)) => reports.push(Report::new(&folder, "error", "-", detail)),
        }
    }
    Ok(reports)
}

// ---------------------------------------------------------------------------
// Repair: the quiet fast path
// ---------------------------------------------------------------------------

/// A cheap snapshot of everything `install` reads: the Claude folders, and in
/// each the size and modification time of the binaries and `.payload`, plus the
/// launcher, the state, the config and the skipped links. Only listings and
/// `stat` calls, no file is opened or hashed.
pub fn fingerprint(paths: &Paths) -> String {
    let (folders, skipped) = scan(&paths.roots);
    let mut files: Vec<PathBuf> = folders
        .iter()
        .flat_map(|folder| [REAL_NAME, STASH_NAME, PAYLOAD_NAME].map(|name| folder.join(name)))
        .collect();
    files.push(paths.launcher.clone());
    files.push(paths.state_file());
    files.push(paths.config());
    let mut hasher = Sha256::new();
    for file in files {
        let line = match fs::metadata(&file) {
            Ok(meta) => {
                let modified = meta
                    .modified()
                    .ok()
                    .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
                    .map_or(0, |elapsed| elapsed.as_nanos());
                format!("{}|{}|{modified}", file.display(), meta.len())
            }
            Err(_) => format!("{}|-|-", file.display()),
        };
        hasher.update(line.as_bytes());
        hasher.update(b"\n");
    }
    for report in skipped {
        hasher.update(format!("skipped:{}\n", report.folder.display()).as_bytes());
    }
    hex(&hasher.finalize())
}

fn read_check(code_dir: &Path) -> Option<(String, u64)> {
    let text = fs::read_to_string(code_dir.join(CHECK_NAME)).ok()?;
    let value: Value = serde_json::from_str(&text).ok()?;
    let object = value.as_object()?;
    let fingerprint = object.get("fingerprint")?.as_str()?.to_string();
    let full_at = object.get("full_at")?.as_u64()?;
    Some((fingerprint, full_at))
}

fn write_check(code_dir: &Path, fingerprint: &str, full_at: u64) -> Res<()> {
    write_json_if_changed(
        &code_dir.join(CHECK_NAME),
        &json!({"v": 1, "fingerprint": fingerprint, "full_at": full_at}),
    )
}

/// `install` for the sign-in task and the launcher. When nothing it reads has
/// changed since the last clean pass, it returns `None` and writes nothing.
/// Otherwise it runs the install and records the fingerprint, unless a folder
/// was locked or failed, in which case the next run tries again.
pub fn repair(paths: &Paths) -> Res<Option<Vec<Report>>> {
    if !paths.launcher.is_file() {
        return Err(failed(format!(
            "launcher not found: {}. Put claude-code-launcher.exe there or pass --launcher.",
            paths.launcher.display()
        )));
    }
    let current = fingerprint(paths);
    let now = unix_now();
    let check = read_check(&paths.code_dir);
    let due = match &check {
        None => true,
        Some((_, full_at)) => now < *full_at || now - full_at >= FULL_PASS_SECS,
    };
    if let Some((stored, _)) = &check {
        if !due && *stored == current {
            return Ok(None);
        }
    }
    let reports = install(paths)?;
    let clean = reports
        .iter()
        .all(|report| report.status != "locked" && report.status != "error");
    if clean {
        let full_at = match (&check, due) {
            (Some((_, previous)), false) => *previous,
            _ => now,
        };
        // Taken after the pass, so the pass's own writes do not trigger another.
        let after = fingerprint(paths);
        let _ = write_check(&paths.code_dir, &after, full_at);
    }
    Ok(Some(reports))
}
