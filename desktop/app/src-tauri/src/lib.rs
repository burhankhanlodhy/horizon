//! ContextShrink desktop launcher.
//!
//! Sign in, get a per-device proxy key (created through the API and kept in
//! the OS credential store), keep a loopback forwarder running, and launch
//! the user's installed coding tools through the ContextShrink proxy.
//!
//! The forwarder is a background process of its own. It is not stopped when
//! the app exits; it is stopped on sign-out, on update, on uninstall, and when
//! the last tool that needs it is disconnected. The tray app starts at sign-in
//! (`--background`) while any tool is connected, so connected tools keep
//! working without a window.

mod api;
mod claude_code;
mod client;
mod process;
mod secrets;
#[cfg(unix)]
mod unix;

use std::collections::HashMap;
use std::path::PathBuf;
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use api::{Api, ApiError, User};
use client::Client;
use serde::Serialize;
use serde_json::Value;
use tauri::menu::{CheckMenuItem, IsMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, State, WindowEvent, Wry};

/// How often the watchdog checks the forwarder and the Code-tab watcher.
const WATCHDOG_EVERY: Duration = Duration::from_secs(30);
/// How often the tray menu re-reads the savings line.
const TRAY_EVERY: Duration = Duration::from_secs(5);
const CLIENT_MISSING: &str = "The ContextShrink client is missing. Reinstall the app.";

/// Bring the main window back from the tray (or from behind other windows).
fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// Tray menu items the status refresh and the pause handlers change.
#[derive(Clone)]
struct TrayItems {
    status: MenuItem<Wry>,
    pause: MenuItem<Wry>,
    resume: MenuItem<Wry>,
    lean: CheckMenuItem<Wry>,
}

/// The tray's Keep-sessions-lean check item.
const LEAN_MENU_LABEL: &str = "Keep sessions lean (compact at 200k)";

/// Shows the savings line and enables Pause or Resume to match the pause flag.
fn refresh_tray(app: &AppHandle) {
    let Some(items) = app.try_state::<TrayItems>() else {
        return;
    };
    let _ = items.status.set_text(claude_code::status_line());
    let paused = claude_code::is_paused();
    let _ = items.pause.set_enabled(!paused);
    let _ = items.resume.set_enabled(paused);
}

/// Reads the lean state through `horizon lean status --json`.
fn read_lean(client: &Client) -> Result<client::LeanStatus, String> {
    let raw = client.run_horizon(&client::lean_status_args())?;
    client::parse_lean_status(&raw)
}

/// Turns Keep sessions lean on or off through `horizon lean on|off --json`,
/// then reads back what is now set.
fn apply_lean(client: &Client, enabled: bool) -> Result<client::LeanStatus, String> {
    let args = client::lean_args(enabled);
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    let raw = client.run_horizon(&refs)?;
    if let Some(message) = client::lean_change_error(&raw) {
        return Err(message);
    }
    read_lean(client)
}

/// Sets the tray lean item to match a status. A failed read leaves it off and disabled.
fn show_lean(items: &TrayItems, status: Result<client::LeanStatus, String>) {
    let (checked, enabled) = match status {
        Ok(status) => (status.enabled, status.terminal_connected),
        Err(_) => (false, false),
    };
    let _ = items.lean.set_checked(checked);
    let _ = items.lean.set_enabled(enabled);
}

/// Re-reads the lean state on a worker thread, so the tray menu never waits on the client.
fn refresh_lean_tray(app: &AppHandle) {
    let Some(items) = app.try_state::<TrayItems>() else {
        return;
    };
    let items = items.inner().clone();
    let handle = app.clone();
    std::thread::spawn(move || {
        let state = handle.state::<AppState>();
        show_lean(&items, read_lean(&state.client));
    });
}

/// Tray click on the lean item. The click has already flipped the check, so its
/// new state is the target. The switch runs on a worker thread.
fn toggle_lean_from_tray(app: &AppHandle) {
    let Some(items) = app.try_state::<TrayItems>() else {
        return;
    };
    let enabled = items.lean.is_checked().unwrap_or(false);
    let items = items.inner().clone();
    let handle = app.clone();
    std::thread::spawn(move || {
        let state = handle.state::<AppState>();
        let _ops = state.ops.lock().unwrap();
        let status = match apply_lean(&state.client, enabled) {
            Ok(status) => Ok(status),
            Err(e) => {
                eprintln!("keep sessions lean: {e}");
                read_lean(&state.client)
            }
        };
        show_lean(&items, status);
    });
}

/// Tray icon: left-click restores the window. The menu shows the savings line,
/// Pause and Resume, the Keep-sessions-lean switch, and can open or quit the app.
/// Quitting leaves the forwarder and connected tools running, as closing the window does.
fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let status = MenuItem::with_id(app, "status", claude_code::status_line(), false, None::<&str>)?;
    let pause = MenuItem::with_id(app, "pause", "Pause ContextShrink", true, None::<&str>)?;
    let resume = MenuItem::with_id(app, "resume", "Resume ContextShrink", true, None::<&str>)?;
    let lean = CheckMenuItem::with_id(app, "lean", LEAN_MENU_LABEL, false, false, None::<&str>)?;
    let open = MenuItem::with_id(app, "open", "Open ContextShrink", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let sep_a = PredefinedMenuItem::separator(app)?;
    let sep_b = PredefinedMenuItem::separator(app)?;
    let sep_c = PredefinedMenuItem::separator(app)?;
    let items: [&dyn IsMenuItem<Wry>; 9] = [
        &status, &sep_a, &pause, &resume, &sep_b, &lean, &sep_c, &open, &quit,
    ];
    let menu = Menu::with_items(app, &items)?;
    app.manage(TrayItems {
        status: status.clone(),
        pause: pause.clone(),
        resume: resume.clone(),
        lean: lean.clone(),
    });
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("ContextShrink")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_main_window(app),
            "pause" => {
                let _ = claude_code::set_paused(true);
                refresh_tray(app);
            }
            "resume" => {
                let _ = claude_code::set_paused(false);
                refresh_tray(app);
            }
            "lean" => toggle_lean_from_tray(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

struct AppState {
    api: Api,
    client: Client,
    /// The app's bundled resources (the Code-tab launcher, status helper and
    /// plugin marketplace). `None` in the uninstall path, which only removes.
    resource_dir: Option<PathBuf>,
    /// Forwarder handles this app started, by loopback port. A forwarder that
    /// outlives the app is found again by its port, so this map is only for
    /// the handles it holds.
    forwarders: Mutex<HashMap<u16, Child>>,
    /// True while a signed-in session is open.
    session_open: AtomicBool,
    /// Serialises connect, disconnect and the watchdog's repairs, so they never
    /// undo each other. Always taken before `forwarders`.
    ops: Mutex<()>,
}

#[derive(Serialize)]
struct Session {
    user: User,
    device: String,
    dashboard_url: String,
}

/// Result of connecting Claude Code: its status and one row per target.
#[derive(Serialize)]
struct ClaudeCodeResult {
    status: String,
    targets: Vec<claude_code::Target>,
}

fn device_name() -> String {
    #[cfg(windows)]
    let host = std::env::var("COMPUTERNAME").ok();
    #[cfg(unix)]
    let host = unix::host_name();
    format!("Desktop: {}", host.unwrap_or_else(|| "this computer".into()))
}

/// Makes sure this device has a working proxy key, creating one if needed.
async fn ensure_device_key(state: &AppState, token: &str) -> Result<(), String> {
    if let Some(id) = secrets::device_key_id() {
        let active = state.api.key_is_active(token, &id).await.map_err(|e| e.text())?;
        if active && state.client.has_key() {
            return Ok(());
        }
    }
    let key = state
        .api
        .create_device_key(token, &device_name())
        .await
        .map_err(|e| e.text())?;
    if let Err(e) = state.client.store_key(&key.key) {
        // Never leave an unusable key active on the account.
        let _ = state.api.revoke_key(token, &key.id).await;
        return Err(e);
    }
    secrets::set_device_key_id(&key.id)
}

/// Makes sure the forwarder on `port` is the one this app's client starts: its
/// health answers with the bundled client's version. A ContextShrink forwarder
/// that does not match is stopped and replaced; another program on the port is
/// an error, and nothing is reused silently.
fn ensure_forwarder(state: &AppState, port: u16, upstream: Option<&str>) -> Result<(), String> {
    let expected = state.client.expected_version().ok_or(CLIENT_MISSING)?;
    let mut forwarders = state.forwarders.lock().unwrap();
    if Client::forwarder_healthy(port, &expected) {
        return Ok(());
    }
    if let Some(mut old) = forwarders.remove(&port) {
        let _ = old.kill();
        let _ = old.wait();
    }
    state.client.stop_forwarder_on_port(port)?;
    let child = state.client.start_forwarder(&api::proxy_url(), port, upstream)?;
    forwarders.insert(port, child);
    if Client::wait_for_healthy(port, &expected) {
        Ok(())
    } else {
        Err("The ContextShrink forwarder did not start. Restart the app and try again.".into())
    }
}

/// Stops the forwarder on `port`, whether this app started it or it outlived an
/// earlier run.
fn stop_forwarder(state: &AppState, port: u16) -> Result<(), String> {
    let child = state.forwarders.lock().unwrap().remove(&port);
    if let Some(mut child) = child {
        let _ = child.kill();
        let _ = child.wait();
    }
    state.client.stop_forwarder_on_port(port)
}

/// Stops every forwarder on the tool ports.
fn stop_forwarders(state: &AppState) {
    for port in client::forwarder_ports() {
        let _ = stop_forwarder(state, port);
    }
}

fn editor(id: &str) -> Result<&'static client::Tool, String> {
    client::TOOLS
        .iter()
        .find(|t| t.id == id && t.kind == client::Kind::Connect)
        .ok_or_else(|| "Unknown editor".to_string())
}

/// Connects Claude Code: the terminal settings and forwarder, the Code-tab
/// patch, and the watcher that keeps the patch current. Each target's result is
/// returned, so a partial success stays visible.
///
/// `repatch` runs the Code-tab install now. A reconnect (every sign-in and app
/// start) passes `false` when the Code tab is already patched: the watcher's
/// first pass verifies it anyway, and running the install again on each start
/// only touched Desktop's folder for nothing.
fn connect_claude_code(
    state: &AppState,
    repatch: bool,
) -> Result<Vec<claude_code::Target>, String> {
    let spec = editor(claude_code::ID)?;
    // The helpers must be in their shared places before the settings and the
    // Code-tab patch refer to them.
    let helper_problems = match &state.resource_dir {
        Some(dir) => claude_code::install_helpers(dir),
        None => Vec::new(),
    };
    ensure_forwarder(state, spec.port, spec.upstream)?;
    state.client.connect_editor(spec)?;
    state.client.set_wanted_editor(spec.id, true);
    let install = if repatch || !claude_code::code_tab_state_present() {
        state.client.run_horizon(&client::codetab_install_args(false)).map(|_| ())
    } else {
        Ok(())
    };
    let code_tab = match install.and_then(|_| state.client.start_codetab_watch()) {
        Err(e) => claude_code::CodeTab::Failed(e),
        Ok(()) if claude_code::code_tab_state_present() => claude_code::CodeTab::Patched,
        Ok(()) => claude_code::CodeTab::Pending,
    };
    if let Err(e) = claude_code::set_autostart(true) {
        eprintln!("autostart: {e}");
    }
    Ok(claude_code::targets(
        &claude_code::current_view(),
        &code_tab,
        &helper_problems,
    ))
}

/// Puts back what Connect changed for Claude Code: stops the watcher, restores
/// Desktop's Code-tab binaries and the terminal settings. Every step runs; the
/// errors are returned together.
fn restore_claude_code(state: &AppState) -> Vec<String> {
    let mut errors = Vec::new();
    state.client.stop_codetab_watch();
    if let Err(e) = state.client.run_horizon(&["codetab", "remove"]) {
        errors.push(e);
    }
    if let Ok(spec) = editor(claude_code::ID) {
        if let Err(e) = state.client.disconnect_editor(spec) {
            errors.push(e);
        }
    }
    errors
}

/// After a disconnect: keep the sign-in start only while something is
/// connected, and stop the forwarder when nothing needs it any more.
fn after_disconnect(state: &AppState) {
    let wanted = state.client.wanted_editors();
    if let Err(e) = claude_code::set_autostart(!wanted.is_empty()) {
        eprintln!("autostart: {e}");
    }
    if wanted.is_empty() && !state.session_open.load(Ordering::SeqCst) {
        let _ = stop_forwarder(state, client::FORWARDER_PORT);
    }
}

/// Re-applies the editors the user connected. They are restored on sign-out and
/// on update, not on quit (quitting restores nothing, by design), so this mostly
/// finds them in place. Caller holds `ops`.
fn reconnect_editors(state: &AppState) {
    for id in state.client.wanted_editors() {
        if id == claude_code::ID {
            let _ = connect_claude_code(state, false);
            continue;
        }
        if let Ok(spec) = editor(&id) {
            if ensure_forwarder(state, spec.port, spec.upstream).is_ok() {
                let _ = state.client.connect_editor(spec);
            }
        }
    }
}

/// Restores every editor's own settings, so nothing points at a forwarder that
/// is about to stop. The wanted list is kept, so the next sign-in reconnects.
/// Caller holds `ops`.
fn disconnect_editors(state: &AppState) {
    for spec in client::TOOLS.iter().filter(|t| t.kind == client::Kind::Connect) {
        if spec.id == claude_code::ID {
            let _ = restore_claude_code(state);
        } else {
            let _ = state.client.disconnect_editor(spec);
        }
    }
}

async fn open_session(state: &AppState, token: &str, user: User) -> Result<Session, String> {
    if !state.client.available() {
        return Err(CLIENT_MISSING.into());
    }
    ensure_device_key(state, token).await?;
    {
        let _ops = state.ops.lock().unwrap();
        ensure_forwarder(state, client::FORWARDER_PORT, None)?;
        reconnect_editors(state);
    }
    state.session_open.store(true, Ordering::SeqCst);
    Ok(Session {
        user,
        device: device_name(),
        dashboard_url: api::app_url(),
    })
}

/// Restores a saved session on startup; `None` means "show sign-in".
#[tauri::command]
async fn restore_session(state: State<'_, AppState>) -> Result<Option<Session>, String> {
    let Some(token) = secrets::session() else {
        return Ok(None);
    };
    match state.api.me(&token).await {
        Ok(user) => open_session(&state, &token, user).await.map(Some),
        Err(ApiError::Unauthorized) => {
            secrets::clear_all();
            state.client.clear_key();
            Ok(None)
        }
        Err(e) => Err(e.text()),
    }
}

#[tauri::command]
async fn login(
    state: State<'_, AppState>,
    email: String,
    password: String,
) -> Result<Session, String> {
    let (token, user) = state
        .api
        .login(email.trim(), &password)
        .await
        .map_err(|e| e.text())?;
    secrets::set_session(&token)?;
    // Fresh plan details (login returns only identity).
    let user = state.api.me(&token).await.unwrap_or(user);
    open_session(&state, &token, user).await
}

#[tauri::command]
async fn logout(state: State<'_, AppState>) -> Result<(), String> {
    {
        let _ops = state.ops.lock().unwrap();
        disconnect_editors(&state);
        state.client.clear_wanted_editors();
        stop_forwarders(&state);
        let _ = claude_code::set_autostart(false);
    }
    state.session_open.store(false, Ordering::SeqCst);
    if let Some(token) = secrets::session() {
        if let Some(id) = secrets::device_key_id() {
            let _ = state.api.revoke_key(&token, &id).await;
        }
        state.api.logout(&token).await;
    }
    state.client.clear_key();
    secrets::clear_all();
    Ok(())
}

/// Run by the Windows uninstaller as `--uninstall-cleanup`, before it
/// removes the app (upgrades skip it). Does what the Sign out link does, so an
/// uninstall leaves no saved session, proxy key or connected editor behind:
/// the session and device-key id live in the OS credential store, which an
/// uninstall otherwise never touches, and a reinstall would sign straight
/// back in as the previous account. It also stops the forwarder and the
/// Code-tab watcher, so their files can be removed, and clears the sign-in
/// start. No window opens; every step is best effort, since nothing may
/// stop the uninstall.
pub fn uninstall_cleanup() {
    if let Some(state) = headless_state() {
        disconnect_editors(&state);
        state.client.clear_wanted_editors();
        stop_forwarders(&state);
        let _ = claude_code::set_autostart(false);
        claude_code::remove_helpers();
        if let Some(token) = secrets::session() {
            tauri::async_runtime::block_on(async {
                if let Some(id) = secrets::device_key_id() {
                    let _ = state.api.revoke_key(&token, &id).await;
                }
                state.api.logout(&token).await;
            });
        }
        state.client.clear_key();
    }
    secrets::clear_all();
}

/// The paths `run()` gets from Tauri, without starting it: the bundled client
/// next to the executable and the app's local data folder.
fn headless_state() -> Option<AppState> {
    let install_dir = std::env::current_exe().ok()?.parent()?.to_path_buf();
    let exe = install_dir.join("horizon").join(client::CLIENT_EXE);
    let data_dir = local_data_dir()?.join("com.contextshrink.desktop");
    Some(AppState {
        api: Api::new(),
        client: Client::new(exe, data_dir),
        resource_dir: None,
        forwarders: Mutex::new(HashMap::new()),
        session_open: AtomicBool::new(false),
        ops: Mutex::new(()),
    })
}

#[cfg(windows)]
fn local_data_dir() -> Option<PathBuf> {
    std::env::var_os("LOCALAPPDATA").map(PathBuf::from)
}

#[cfg(not(windows))]
fn local_data_dir() -> Option<PathBuf> {
    // Only the Windows installer runs the cleanup; elsewhere clear secrets only.
    None
}

/// Plan, savings, Free cap and unpaid-fee details for the header.
#[tauri::command]
async fn account_summary(state: State<'_, AppState>) -> Result<Value, String> {
    let token = secrets::session().ok_or("Not signed in")?;
    let user = state.api.me(&token).await.map_err(|e| e.text())?;
    let estimate = state
        .api
        .billing_estimate(&token)
        .await
        .map_err(|e| e.text())?;
    Ok(serde_json::json!({ "user": user, "estimate": estimate }))
}

#[tauri::command]
fn list_tools() -> Vec<client::ToolInfo> {
    client::tools()
}

/// Called by the update banner after the new version has downloaded and just
/// before it installs: on Windows the installer closes the app without a normal
/// exit, so the forwarders and editor settings are let go of first. Quitting
/// itself restores nothing; this is the update path only. The next start
/// reconnects them.
#[tauri::command]
fn prepare_for_update(state: State<'_, AppState>) {
    let _ops = state.ops.lock().unwrap();
    disconnect_editors(&state);
    stop_forwarders(&state);
}

#[tauri::command]
async fn forwarder_running(state: State<'_, AppState>) -> Result<bool, String> {
    Ok(state
        .client
        .expected_version()
        .is_some_and(|v| Client::forwarder_healthy(client::FORWARDER_PORT, &v)))
}

#[tauri::command]
async fn launch_tool(
    state: State<'_, AppState>,
    tool: String,
    folder: String,
) -> Result<(), String> {
    let spec = client::TOOLS
        .iter()
        .find(|t| t.id == tool && t.kind == client::Kind::Terminal)
        .ok_or("Unknown tool")?;
    if !spec.installed() {
        return Err(format!("{} is not installed on this computer.", spec.name));
    }
    {
        let _ops = state.ops.lock().unwrap();
        ensure_forwarder(&state, spec.port, spec.upstream)?;
    }
    state.client.launch(spec, &PathBuf::from(folder))
}

#[tauri::command]
async fn editor_status(state: State<'_, AppState>, tool: String) -> Result<String, String> {
    state.client.editor_status(editor(&tool)?)
}

#[tauri::command]
async fn set_editor_connected(
    state: State<'_, AppState>,
    tool: String,
    connected: bool,
) -> Result<String, String> {
    let spec = editor(&tool)?;
    if spec.id == claude_code::ID {
        return Err("Claude Code connects from its own card.".into());
    }
    let _ops = state.ops.lock().unwrap();
    if connected {
        ensure_forwarder(&state, spec.port, spec.upstream)?;
        state.client.connect_editor(spec)?;
        state.client.set_wanted_editor(spec.id, true);
        if let Err(e) = claude_code::set_autostart(true) {
            eprintln!("autostart: {e}");
        }
    } else {
        state.client.disconnect_editor(spec)?;
        state.client.set_wanted_editor(spec.id, false);
        after_disconnect(&state);
    }
    state.client.editor_status(spec)
}

/// Keep sessions lean: the terminal and Code-tab state.
#[tauri::command]
async fn lean_status(state: State<'_, AppState>) -> Result<client::LeanStatus, String> {
    read_lean(&state.client)
}

/// Keep sessions lean: turns the switch on (200k) or off, and returns the new state.
#[tauri::command]
async fn set_lean(state: State<'_, AppState>, enabled: bool) -> Result<client::LeanStatus, String> {
    let _ops = state.ops.lock().unwrap();
    apply_lean(&state.client, enabled)
}

/// Connects or disconnects Claude Code (terminal, Code tab, status line,
/// plugin and the watcher) as one action. The tray's lean item follows.
#[tauri::command]
async fn set_claude_code_connected(
    app: AppHandle,
    state: State<'_, AppState>,
    connected: bool,
) -> Result<ClaudeCodeResult, String> {
    let result = change_claude_code(&state, connected);
    refresh_lean_tray(&app);
    result
}

fn change_claude_code(state: &AppState, connected: bool) -> Result<ClaudeCodeResult, String> {
    let _ops = state.ops.lock().unwrap();
    if connected {
        let targets = connect_claude_code(&state, true)?;
        return Ok(ClaudeCodeResult {
            status: "connected".into(),
            targets,
        });
    }
    state.client.set_wanted_editor(claude_code::ID, false);
    let errors = restore_claude_code(&state);
    after_disconnect(&state);
    if errors.is_empty() {
        Ok(ClaudeCodeResult {
            status: "off".into(),
            targets: Vec::new(),
        })
    } else {
        Err(errors.join(" "))
    }
}

/// Every `WATCHDOG_EVERY`: restarts the forwarder when a session is open or a
/// tool is connected, and restarts the Code-tab watcher while Claude Code is
/// connected. Runs at once on start, so a background start repairs straight away.
fn watchdog(app: AppHandle) {
    {
        let state = app.state::<AppState>();
        let _ = claude_code::set_autostart(!state.client.wanted_editors().is_empty());
    }
    loop {
        {
            let state = app.state::<AppState>();
            let _ops = state.ops.lock().unwrap();
            let wanted = state.client.wanted_editors();
            if state.session_open.load(Ordering::SeqCst) || !wanted.is_empty() {
                if let Err(e) = ensure_forwarder(&state, client::FORWARDER_PORT, None) {
                    eprintln!("forwarder watchdog: {e}");
                }
            }
            if wanted.iter().any(|id| id == claude_code::ID) {
                if let Err(e) = state.client.start_codetab_watch() {
                    eprintln!("code tab watchdog: {e}");
                }
            }
        }
        std::thread::sleep(WATCHDOG_EVERY);
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    #[cfg(target_os = "linux")]
    unix::display_workarounds();
    let app = tauri::Builder::default()
        // Registered first: a second launch hands over to this instance, which
        // shows its window, and the second process exits.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main_window(app);
        }))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .setup(|app| {
            // Desktop-menu launches miss the PATH the user's shell sets up.
            #[cfg(unix)]
            unix::import_login_path();
            // Tauri can return verbatim `\\?\C:\...` paths, which cmd.exe cannot
            // run ("The system cannot find the path specified"); use plain ones.
            let exe = dunce::simplified(&app.path().resource_dir()?)
                .join("horizon")
                .join(client::CLIENT_EXE);
            let data_dir = dunce::simplified(&app.path().app_local_data_dir()?).to_path_buf();
            let resource_dir = dunce::simplified(&app.path().resource_dir()?).to_path_buf();
            app.manage(AppState {
                api: Api::new(),
                client: Client::new(exe, data_dir),
                resource_dir: Some(resource_dir),
                forwarders: Mutex::new(HashMap::new()),
                session_open: AtomicBool::new(false),
                ops: Mutex::new(()),
            });
            // On Linux a tray icon needs libayatana-appindicator; without it the
            // app still runs, just without the icon.
            if let Err(e) = build_tray(app) {
                eprintln!("tray icon unavailable: {e}");
            }
            refresh_lean_tray(app.handle());
            // Started at sign-in while a tool is connected: stay in the tray.
            if !std::env::args().any(|a| a == "--background") {
                show_main_window(app.handle());
            }
            let watch_handle = app.handle().clone();
            std::thread::spawn(move || watchdog(watch_handle));
            let tray_handle = app.handle().clone();
            std::thread::spawn(move || loop {
                refresh_tray(&tray_handle);
                std::thread::sleep(TRAY_EVERY);
            });
            Ok(())
        })
        // Windows: minimise sends the app to the tray, so the window leaves the
        // taskbar while the forwarder keeps serving the tools launched from it.
        // Linux minimises normally: several desktops (stock GNOME) show no tray
        // icons, so a hidden window could not be brought back.
        .on_window_event(|window, event| {
            if let WindowEvent::Resized(_) = event {
                if cfg!(windows) && window.is_minimized().unwrap_or(false) {
                    let _ = window.hide();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            restore_session,
            login,
            logout,
            account_summary,
            list_tools,
            forwarder_running,
            prepare_for_update,
            launch_tool,
            editor_status,
            set_editor_connected,
            set_claude_code_connected,
            lean_status,
            set_lean
        ])
        .build(tauri::generate_context!())
        .expect("error while building the ContextShrink app");

    // Exiting leaves the forwarder and connected tools running on purpose; see
    // the module comment for what stops them.
    app.run(|_, _| {});
}
