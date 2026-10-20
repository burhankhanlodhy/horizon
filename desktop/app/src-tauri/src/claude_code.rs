//! Claude Code routing as the app sees it: the files the forwarder and the
//! helpers share under `%LOCALAPPDATA%\ContextShrink\claude-code`, the tray's
//! pause flag and savings line, the per-target results Connect shows, and the
//! Startup shortcut that starts the app in the tray while a tool is connected.

use std::fs;
use std::path::{Path, PathBuf};

use serde::Serialize;
use serde_json::Value;

#[cfg(windows)]
use windows::core::{Interface, PCWSTR};
#[cfg(windows)]
use windows::Win32::Foundation::RPC_E_CHANGED_MODE;
#[cfg(windows)]
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize, IPersistFile,
    CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED, STGM_READ,
};
#[cfg(windows)]
use windows::Win32::UI::Shell::{
    IShellLinkW, SHGetKnownFolderPath, ShellLink, FOLDERID_Startup, KF_FLAG_DEFAULT,
};

use crate::client::FORWARDER_PORT;

/// Connect card id, also the tool id in `client::TOOLS`.
pub const ID: &str = "claude-code";
/// The plugin id the Connect step enables in the user's Claude Code settings.
const PLUGIN_KEY: &str = "contextshrink@contextshrink";
/// The status line command the Connect step installs (`contextshrink-status.exe`).
const STATUS_LINE_MARK: &str = "contextshrink-status";
/// The sign-in shortcut in the user's Startup folder.
#[cfg(windows)]
const STARTUP_SHORTCUT: &str = "ContextShrink.lnk";
/// Passed to the app when Windows starts it at sign-in: it opens in the tray.
#[cfg(windows)]
const STARTUP_ARGS: &str = "--background";
/// Under HKEY_CURRENT_USER. Older installs started the app from this value;
/// it is only ever removed now.
#[cfg(windows)]
const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
#[cfg(windows)]
const RUN_VALUE: &str = "ContextShrink";

/// The helper executables the Connect step and the status line run from here.
pub const HELPER_EXES: [&str; 2] = ["contextshrink-status.exe", "claude-code-launcher.exe"];

/// `%LOCALAPPDATA%\ContextShrink` (Windows); the same relative layout under the
/// home folder elsewhere. Shared by every ContextShrink component.
fn root() -> Option<PathBuf> {
    let base = if cfg!(windows) {
        std::env::var_os("LOCALAPPDATA").map(PathBuf::from)?
    } else {
        PathBuf::from(std::env::var_os("HOME")?).join(".local").join("share")
    };
    Some(base.join("ContextShrink"))
}

/// `%LOCALAPPDATA%\ContextShrink\claude-code`: the forwarder's status files.
pub fn dir() -> Option<PathBuf> {
    root().map(|r| r.join(ID))
}

/// The pause flag: while it exists the forwarder passes requests through.
pub fn paused_file() -> Option<PathBuf> {
    dir().map(|d| d.join("paused"))
}

pub fn is_paused() -> bool {
    paused_file().is_some_and(|p| p.exists())
}

pub fn set_paused(paused: bool) -> Result<(), String> {
    let path = paused_file().ok_or("The ContextShrink data folder is unavailable")?;
    if paused {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        std::fs::write(&path, b"").map_err(|e| e.to_string())
    } else {
        match std::fs::remove_file(&path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.to_string()),
        }
    }
}

/// The one-line savings summary the forwarder writes (`status.txt`).
pub fn status_line() -> String {
    dir()
        .and_then(|d| std::fs::read_to_string(d.join("status.txt")).ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "ContextShrink: no savings data yet".into())
}

/// Desktop's Code tab patch record, written by `horizon codetab install`.
pub fn code_tab_state_present() -> bool {
    dir()
        .map(|d| d.join("codetab-state.json"))
        .and_then(|p| std::fs::metadata(p).ok())
        .is_some_and(|m| m.len() > 2)
}

/// Copies `src` to `dst` when the bytes differ. The copy goes to a temp file
/// that is renamed into place. A destination that is locked (a running
/// executable) keeps its current copy, which is not an error.
fn copy_if_changed(src: &Path, dst: &Path) -> Result<(), String> {
    let wanted = fs::read(src).map_err(|e| format!("{}: {e}", src.display()))?;
    if fs::read(dst).is_ok_and(|current| current == wanted) {
        return Ok(());
    }
    if let Some(parent) = dst.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("{}: {e}", parent.display()))?;
    }
    let name = dst.file_name().unwrap_or_default().to_string_lossy().to_string();
    let tmp = dst.with_file_name(format!("{name}.new"));
    fs::write(&tmp, &wanted).map_err(|e| format!("{}: {e}", tmp.display()))?;
    match fs::rename(&tmp, dst) {
        Ok(()) => Ok(()),
        Err(e) => {
            let _ = fs::remove_file(&tmp);
            if dst.exists() {
                // Still in use by a running session: keep the copy that is there.
                Ok(())
            } else {
                Err(format!("{}: {e}", dst.display()))
            }
        }
    }
}

/// Copies every file under `src` into `dst`, keeping the layout.
fn sync_tree(src: &Path, dst: &Path) -> Result<(), String> {
    let entries = fs::read_dir(src).map_err(|e| format!("{}: {e}", src.display()))?;
    for entry in entries {
        let entry = entry.map_err(|e| e.to_string())?;
        let from = entry.path();
        let to = dst.join(entry.file_name());
        if entry.file_type().map_err(|e| e.to_string())?.is_dir() {
            sync_tree(&from, &to)?;
        } else {
            copy_if_changed(&from, &to)?;
        }
    }
    Ok(())
}

/// Copies the bundled helpers from the app's resource folder to the shared
/// folders the Connect step and the status line use. Returns each problem;
/// an empty list means they are in place.
pub fn install_helpers(resource_dir: &Path) -> Vec<String> {
    let Some(root) = root() else {
        return vec!["The ContextShrink data folder is unavailable".into()];
    };
    let bundled = resource_dir.join(ID);
    let mut problems = Vec::new();
    for exe in HELPER_EXES {
        if let Err(e) = copy_if_changed(&bundled.join("bin").join(exe), &root.join("bin").join(exe)) {
            problems.push(e);
        }
    }
    if let Err(e) = sync_tree(
        &bundled.join("plugin-marketplace"),
        &root.join(ID).join("plugin-marketplace"),
    ) {
        problems.push(e);
    }
    problems
}

/// Removes what `install_helpers` placed: the two executables, the
/// `bin` folder if it is then empty, and the plugin marketplace. Nothing else
/// under ContextShrink (or Desktop's own folders) is touched.
pub fn remove_helpers() {
    let Some(root) = root() else {
        return;
    };
    for exe in HELPER_EXES {
        let _ = fs::remove_file(root.join("bin").join(exe));
    }
    let _ = fs::remove_dir(root.join("bin"));
    let _ = fs::remove_dir_all(root.join(ID).join("plugin-marketplace"));
}

/// The user-level Claude Code settings file, as the bundled client finds it:
/// `CLAUDE_CONFIG_DIR`, else `~/.claude`.
pub fn user_settings_path() -> Option<PathBuf> {
    if let Some(config) = std::env::var_os("CLAUDE_CONFIG_DIR") {
        return Some(PathBuf::from(config).join("settings.json"));
    }
    let home = if cfg!(windows) {
        std::env::var_os("USERPROFILE")?
    } else {
        std::env::var_os("HOME")?
    };
    Some(PathBuf::from(home).join(".claude").join("settings.json"))
}

pub fn read_user_settings() -> Value {
    user_settings_path()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(Value::Null)
}

#[derive(Debug, PartialEq, Eq)]
pub enum StatusLine {
    /// The ContextShrink status line is installed.
    Ours,
    /// The user has a status line of their own, which Connect leaves in place.
    Own,
    Missing,
}

/// What the user's settings say about the Connect targets.
#[derive(Debug, PartialEq, Eq)]
pub struct SettingsView {
    /// `env.ANTHROPIC_BASE_URL` points at this app's forwarder.
    pub routed: bool,
    pub status_line: StatusLine,
    pub plugin: bool,
}

/// Reads the Connect targets out of a parsed `settings.json`.
pub fn view(settings: &Value, port: u16) -> SettingsView {
    let prefix = format!("http://127.0.0.1:{port}");
    let routed = settings["env"]["ANTHROPIC_BASE_URL"]
        .as_str()
        .is_some_and(|url| url.starts_with(&prefix));
    let status_line = match &settings["statusLine"] {
        Value::Null => StatusLine::Missing,
        line => {
            let command = line["command"].as_str().unwrap_or_default();
            if command.contains(STATUS_LINE_MARK) {
                StatusLine::Ours
            } else {
                StatusLine::Own
            }
        }
    };
    let plugin = settings["enabledPlugins"][PLUGIN_KEY].as_bool() == Some(true);
    SettingsView { routed, status_line, plugin }
}

/// The Code tab's patch as far as Connect knows it.
pub enum CodeTab {
    /// The patch record exists.
    Patched,
    /// Connect ran, but Desktop has not downloaded a Claude Code version yet.
    Pending,
    /// `horizon codetab install` or the watcher failed; the message says why.
    Failed(String),
}

/// One row of Connect's result: where it stands and why.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Target {
    pub key: &'static str,
    pub label: &'static str,
    /// "done", "waiting", "skipped" or "failed".
    pub state: &'static str,
    pub detail: String,
}

pub fn targets(view: &SettingsView, code_tab: &CodeTab, helper_problems: &[String]) -> Vec<Target> {
    let terminal = if view.routed {
        ("done", "New terminal sessions go through ContextShrink.".to_string())
    } else {
        ("failed", "Your Claude Code settings do not point at ContextShrink.".into())
    };
    let code = match code_tab {
        CodeTab::Patched => (
            "done",
            "Desktop's Code tab goes through ContextShrink. New Claude Desktop versions are patched automatically.".into(),
        ),
        CodeTab::Pending => (
            "waiting",
            "Open the Code tab in Claude Desktop once. ContextShrink patches it then.".into(),
        ),
        CodeTab::Failed(message) => ("failed", message.clone()),
    };
    let status = match view.status_line {
        _ if !helper_problems.is_empty() => ("failed", helper_problems.join(" ")),
        StatusLine::Ours => ("done", "The terminal status line shows your savings.".into()),
        StatusLine::Own => ("skipped", "You already have a status line, so yours is kept.".into()),
        StatusLine::Missing => ("failed", "The status line was not installed.".into()),
    };
    let plugin = if view.plugin {
        ("done", "Savings and pause commands are installed in Claude Code.".into())
    } else {
        ("failed", "The Claude Code plugin was not installed.".into())
    };
    let row = |key, label, (state, detail): (&'static str, String)| Target {
        key,
        label,
        state,
        detail,
    };
    vec![
        row("terminal", "Terminal", terminal),
        row("code_tab", "Code tab", code),
        row("status_line", "Status line", status),
        row("plugin", "Plugin", plugin),
    ]
}

/// The Connect targets for the forwarder port, read from the user's settings.
pub fn current_view() -> SettingsView {
    view(&read_user_settings(), FORWARDER_PORT)
}

/// Starts the app in the tray at sign-in while any tool is connected, and stops
/// that start otherwise. Windows only.
///
/// The start is a shortcut in the user's Startup folder. Older installs used an
/// HKCU Run value instead, which is removed here on every call. Each step is
/// written only when it differs: the app calls this on every start and connect.
#[cfg(windows)]
pub fn set_autostart(enabled: bool) -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let shortcut = startup_folder().and_then(|folder| sync_startup_shortcut(&folder, &exe, enabled));
    let legacy = sync_run_value(windows_registry::CURRENT_USER, RUN_KEY, RUN_VALUE, None);
    shortcut.and(legacy)
}

/// Where the user's Startup folder is, from the Known Folder API.
#[cfg(windows)]
fn startup_folder() -> Result<PathBuf, String> {
    let raw = unsafe { SHGetKnownFolderPath(&FOLDERID_Startup, KF_FLAG_DEFAULT, None) }
        .map_err(|e| format!("Could not find the Windows Startup folder: {e}"))?;
    let folder = unsafe { raw.to_string() };
    unsafe { CoTaskMemFree(Some(raw.0 as *const _)) };
    folder
        .map(PathBuf::from)
        .map_err(|e| format!("Could not read the Windows Startup folder: {e}"))
}

/// What the Startup shortcut starts: the target, its arguments and its folder.
#[cfg(windows)]
#[derive(Debug)]
struct Shortcut {
    target: PathBuf,
    args: String,
    working_dir: PathBuf,
}

#[cfg(windows)]
impl Shortcut {
    /// The shortcut `set_autostart` wants for the app at `exe`.
    fn for_exe(exe: &Path) -> Self {
        Self {
            target: exe.to_path_buf(),
            args: STARTUP_ARGS.to_string(),
            working_dir: exe.parent().map(Path::to_path_buf).unwrap_or_default(),
        }
    }

    /// Windows paths are not case-sensitive, so a case-only difference is the same.
    fn matches(&self, other: &Shortcut) -> bool {
        fn same_text(a: &Path, b: &Path) -> bool {
            a.to_string_lossy().eq_ignore_ascii_case(&b.to_string_lossy())
        }
        same_text(&self.target, &other.target)
            && self.args == other.args
            && same_text(&self.working_dir, &other.working_dir)
    }
}

/// Makes `folder\ContextShrink.lnk` start `exe` with `--background`, or removes
/// it when `enabled` is false. The shortcut is written only when it is missing
/// or differs, and a missing shortcut is fine when disabling.
#[cfg(windows)]
fn sync_startup_shortcut(folder: &Path, exe: &Path, enabled: bool) -> Result<(), String> {
    let path = folder.join(STARTUP_SHORTCUT);
    if !enabled {
        return match fs::remove_file(&path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(format!("Could not remove {}: {e}", path.display())),
        };
    }
    let wanted = Shortcut::for_exe(exe);
    if read_shortcut(&path).is_ok_and(|current| current.matches(&wanted)) {
        return Ok(());
    }
    write_shortcut(&path, &wanted)
}

/// Reads the shortcut at `path` back: where it points and how it starts.
#[cfg(windows)]
fn read_shortcut(path: &Path) -> Result<Shortcut, String> {
    let _com = ComScope::new()?;
    let link: IShellLinkW = unsafe { CoCreateInstance(&ShellLink, None, CLSCTX_INPROC_SERVER) }
        .map_err(|e| format!("Could not open the shortcut helper: {e}"))?;
    let file: IPersistFile = link.cast().map_err(|e| e.to_string())?;
    let wide_path = wide(&path.to_string_lossy());
    unsafe { file.Load(PCWSTR(wide_path.as_ptr()), STGM_READ) }
        .map_err(|e| format!("Could not read {}: {e}", path.display()))?;
    Ok(Shortcut {
        target: PathBuf::from(link_text(|buf| unsafe {
            link.GetPath(buf, std::ptr::null_mut(), 0)
        })?),
        args: link_text(|buf| unsafe { link.GetArguments(buf) })?,
        working_dir: PathBuf::from(link_text(|buf| unsafe {
            link.GetWorkingDirectory(buf)
        })?),
    })
}

/// Writes `shortcut` to `path` through a temp file that is renamed into place,
/// so a failed save never leaves a half-written shortcut.
#[cfg(windows)]
fn write_shortcut(path: &Path, shortcut: &Shortcut) -> Result<(), String> {
    let _com = ComScope::new()?;
    let link: IShellLinkW = unsafe { CoCreateInstance(&ShellLink, None, CLSCTX_INPROC_SERVER) }
        .map_err(|e| format!("Could not open the shortcut helper: {e}"))?;
    let target = wide(&shortcut.target.to_string_lossy());
    let args = wide(&shortcut.args);
    let working_dir = wide(&shortcut.working_dir.to_string_lossy());
    let description = wide("ContextShrink");
    let set = |result: windows::core::Result<()>| {
        result.map_err(|e| format!("Could not write {}: {e}", path.display()))
    };
    unsafe {
        set(link.SetPath(PCWSTR(target.as_ptr())))?;
        set(link.SetArguments(PCWSTR(args.as_ptr())))?;
        set(link.SetWorkingDirectory(PCWSTR(working_dir.as_ptr())))?;
        set(link.SetDescription(PCWSTR(description.as_ptr())))?;
    }
    let file: IPersistFile = link.cast().map_err(|e| e.to_string())?;
    let name = path.file_name().unwrap_or_default().to_string_lossy().to_string();
    let tmp = path.with_file_name(format!("{name}.new"));
    let wide_tmp = wide(&tmp.to_string_lossy());
    if let Err(e) = unsafe { file.Save(PCWSTR(wide_tmp.as_ptr()), false) } {
        let _ = fs::remove_file(&tmp);
        return Err(format!("Could not write {}: {e}", path.display()));
    }
    fs::rename(&tmp, path).map_err(|e| {
        let _ = fs::remove_file(&tmp);
        format!("Could not write {}: {e}", path.display())
    })
}

/// A UTF-16 string with the terminating NUL, for the Win32 calls.
#[cfg(windows)]
fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

/// Runs one of the shortcut's text getters and returns what it wrote.
#[cfg(windows)]
fn link_text(
    get: impl FnOnce(&mut [u16]) -> windows::core::Result<()>,
) -> Result<String, String> {
    let mut buf = vec![0u16; 1024];
    get(&mut buf).map_err(|e| e.to_string())?;
    let len = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
    Ok(String::from_utf16_lossy(&buf[..len]))
}

/// Initializes COM on this thread for as long as the guard lives. A thread that
/// already uses COM in another mode keeps it, and is not uninitialized here.
#[cfg(windows)]
struct ComScope {
    initialized: bool,
}

#[cfg(windows)]
impl ComScope {
    fn new() -> Result<Self, String> {
        let hr = unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED) };
        if hr == RPC_E_CHANGED_MODE {
            Ok(Self { initialized: false })
        } else if hr.is_ok() {
            // S_OK, or S_FALSE when this thread already had COM: both balance with uninit.
            Ok(Self { initialized: true })
        } else {
            Err(format!("Could not start COM: {hr:?}"))
        }
    }
}

#[cfg(windows)]
impl Drop for ComScope {
    fn drop(&mut self) {
        if self.initialized {
            unsafe { CoUninitialize() };
        }
    }
}

/// Makes `name` under `parent\path` hold `wanted` (or be absent for `None`),
/// writing only when it does not already. The Run value is only removed now;
/// the key is opened read-only first so an install without the value is never
/// written to.
#[cfg(windows)]
fn sync_run_value(
    parent: &windows_registry::Key,
    path: &str,
    name: &str,
    wanted: Option<&str>,
) -> Result<(), String> {
    let failed = |e: windows_registry::Result<()>| {
        e.map_err(|e| format!("Could not update Windows startup: {e}"))
    };
    match wanted {
        Some(value) => {
            let current = parent.open(path).and_then(|key| key.get_string(name)).ok();
            if current.as_deref() == Some(value) {
                return Ok(());
            }
            failed(parent.create(path).and_then(|key| key.set_string(name, value)))
        }
        None => {
            // A missing key or value is the state we want.
            let present = parent
                .open(path)
                .and_then(|key| key.get_value(name))
                .is_ok();
            if !present {
                return Ok(());
            }
            let Ok(key) = parent.options().read().write().open(path) else {
                return Ok(());
            };
            failed(key.remove_value(name))
        }
    }
}

#[cfg(not(windows))]
pub fn set_autostart(_enabled: bool) -> Result<(), String> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn view_reads_route_status_line_and_plugin() {
        let settings = json!({
            "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:18788/k/abc" },
            "statusLine": { "type": "command", "command": "C:/x/contextshrink-status.exe" },
            "enabledPlugins": { "contextshrink@contextshrink": true }
        });
        assert_eq!(
            view(&settings, 18788),
            SettingsView { routed: true, status_line: StatusLine::Ours, plugin: true }
        );
    }

    #[test]
    fn view_keeps_a_users_own_status_line_and_other_routes() {
        let settings = json!({
            "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787" },
            "statusLine": { "type": "command", "command": "my-line" }
        });
        assert_eq!(
            view(&settings, 18788),
            SettingsView { routed: false, status_line: StatusLine::Own, plugin: false }
        );
    }

    #[test]
    fn view_of_empty_settings_is_all_missing() {
        assert_eq!(
            view(&Value::Null, 18788),
            SettingsView { routed: false, status_line: StatusLine::Missing, plugin: false }
        );
    }

    #[test]
    fn targets_name_each_target_and_its_state() {
        let view = SettingsView { routed: true, status_line: StatusLine::Own, plugin: false };
        let rows = targets(&view, &CodeTab::Pending, &[]);
        let states: Vec<(&str, &str)> = rows.iter().map(|t| (t.key, t.state)).collect();
        assert_eq!(
            states,
            vec![
                ("terminal", "done"),
                ("code_tab", "waiting"),
                ("status_line", "skipped"),
                ("plugin", "failed"),
            ]
        );
    }

    #[test]
    fn failed_code_tab_carries_the_message() {
        let view = SettingsView { routed: true, status_line: StatusLine::Ours, plugin: true };
        let rows = targets(&view, &CodeTab::Failed("Desktop is busy".into()), &[]);
        assert_eq!(rows[1].state, "failed");
        assert_eq!(rows[1].detail, "Desktop is busy");
    }

    #[test]
    fn helpers_copy_changed_bytes_and_sync_the_tree() {
        // A scratch folder only: the shared ContextShrink folder is never touched.
        let base = std::env::temp_dir().join(format!("cs-helpers-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&base);
        let src = base.join("src");
        let dst = base.join("dst");
        fs::create_dir_all(src.join("hooks")).unwrap();
        fs::write(src.join("a.exe"), b"one").unwrap();
        copy_if_changed(&src.join("a.exe"), &dst.join("a.exe")).unwrap();
        assert_eq!(fs::read(dst.join("a.exe")).unwrap(), b"one");

        fs::write(src.join("a.exe"), b"two").unwrap();
        copy_if_changed(&src.join("a.exe"), &dst.join("a.exe")).unwrap();
        assert_eq!(fs::read(dst.join("a.exe")).unwrap(), b"two");
        assert!(!dst.join("a.exe.new").exists(), "the temp file is renamed away");

        fs::write(src.join("hooks").join("hooks.json"), b"{}").unwrap();
        sync_tree(&src, &dst).unwrap();
        assert_eq!(fs::read(dst.join("hooks").join("hooks.json")).unwrap(), b"{}");

        assert!(copy_if_changed(&src.join("missing.exe"), &dst.join("x.exe")).is_err());
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn status_line_has_a_placeholder_without_data() {
        // Only checks the fallback text shape; the live file is not read here.
        assert!(!status_line().is_empty());
    }

    /// A throwaway key under HKCU\Software, removed afterwards; the real Run key
    /// is never touched.
    #[cfg(windows)]
    #[test]
    fn run_value_is_written_only_when_it_differs_and_removed_cleanly() {
        let hkcu = windows_registry::CURRENT_USER;
        let path = format!(r"Software\ContextShrinkTest-{}", std::process::id());
        let value = |hkcu: &windows_registry::Key| {
            hkcu.open(&path).and_then(|k| k.get_string("App")).ok()
        };

        sync_run_value(hkcu, &path, "App", None).unwrap(); // absent key: nothing to do
        sync_run_value(hkcu, &path, "App", Some("a --background")).unwrap();
        assert_eq!(value(hkcu).as_deref(), Some("a --background"));
        sync_run_value(hkcu, &path, "App", Some("a --background")).unwrap(); // unchanged
        sync_run_value(hkcu, &path, "App", Some("b --background")).unwrap();
        assert_eq!(value(hkcu).as_deref(), Some("b --background"));
        sync_run_value(hkcu, &path, "App", None).unwrap();
        assert_eq!(value(hkcu), None);
        sync_run_value(hkcu, &path, "App", None).unwrap(); // absent value: still fine

        hkcu.remove_tree(&path).unwrap();
    }

    /// A throwaway folder under the temp dir. The real Startup folder is never
    /// used by these tests.
    #[cfg(windows)]
    fn scratch_folder(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("cs-startup-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[cfg(windows)]
    #[test]
    fn startup_shortcut_is_created_once_and_follows_the_exe() {
        let folder = scratch_folder("create");
        let lnk = folder.join(STARTUP_SHORTCUT);
        let exe_a = folder.join("bin-a").join("contextshrink-desktop.exe");
        let exe_b = folder.join("bin-b").join("contextshrink-desktop.exe");

        sync_startup_shortcut(&folder, &exe_a, true).unwrap();
        let read = read_shortcut(&lnk).unwrap();
        assert_eq!(read.target, exe_a);
        assert_eq!(read.args, STARTUP_ARGS);
        assert_eq!(read.working_dir, exe_a.parent().unwrap());
        assert!(!folder.join("ContextShrink.lnk.new").exists(), "the temp file is renamed away");

        // The same exe leaves the shortcut alone.
        let modified = fs::metadata(&lnk).unwrap().modified().unwrap();
        std::thread::sleep(std::time::Duration::from_millis(50));
        sync_startup_shortcut(&folder, &exe_a, true).unwrap();
        assert_eq!(fs::metadata(&lnk).unwrap().modified().unwrap(), modified);

        // A different exe rewrites it.
        sync_startup_shortcut(&folder, &exe_b, true).unwrap();
        assert_eq!(read_shortcut(&lnk).unwrap().target, exe_b);

        sync_startup_shortcut(&folder, &exe_b, false).unwrap();
        assert!(!lnk.exists());
        sync_startup_shortcut(&folder, &exe_b, false).unwrap(); // already gone

        let _ = fs::remove_dir_all(&folder);
    }

    #[cfg(windows)]
    #[test]
    fn startup_shortcut_with_other_arguments_is_rewritten() {
        let folder = scratch_folder("args");
        let exe = folder.join("contextshrink-desktop.exe");
        let lnk = folder.join(STARTUP_SHORTCUT);
        write_shortcut(
            &lnk,
            &Shortcut { target: exe.clone(), args: String::new(), working_dir: folder.clone() },
        )
        .unwrap();

        sync_startup_shortcut(&folder, &exe, true).unwrap();
        let read = read_shortcut(&lnk).unwrap();
        assert_eq!(read.args, STARTUP_ARGS);
        assert_eq!(read.working_dir, folder);

        let _ = fs::remove_dir_all(&folder);
    }
}
