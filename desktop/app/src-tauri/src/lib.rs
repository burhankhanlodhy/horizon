//! ContextShrink desktop launcher.
//!
//! Sign in, get a per-device proxy key (created through the API and kept in
//! the OS credential store), keep a loopback forwarder running, and launch
//! the user's installed coding tools through the ContextShrink proxy.

mod api;
mod client;
mod secrets;
#[cfg(unix)]
mod unix;

use std::path::PathBuf;
use std::collections::HashMap;
use std::process::Child;
use std::sync::Mutex;

use api::{Api, ApiError, User};
use client::Client;
use serde::Serialize;
use serde_json::Value;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, RunEvent, State, WindowEvent};

/// Bring the main window back from the tray (or from behind other windows).
fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// Tray icon: left-click restores the window; the menu can also quit, which
/// stops the forwarder like closing the window does.
fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open ContextShrink", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &quit])?;
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("ContextShrink")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_main_window(app),
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
    /// Forwarders by loopback port: the main one, plus one per provider the
    /// proxy is not configured for, started the first time a tool needs it.
    forwarders: Mutex<HashMap<u16, Child>>,
}

#[derive(Serialize)]
struct Session {
    user: User,
    device: String,
    dashboard_url: String,
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

fn ensure_forwarder(state: &AppState, port: u16, upstream: Option<&str>) -> Result<(), String> {
    let mut forwarders = state.forwarders.lock().unwrap();
    let running = match forwarders.get_mut(&port) {
        Some(child) => matches!(child.try_wait(), Ok(None)),
        None => false,
    };
    if running && Client::forwarder_listening(port) {
        return Ok(());
    }
    if let Some(mut old) = forwarders.remove(&port) {
        let _ = old.kill();
    }
    let child = state.client.start_forwarder(&api::proxy_url(), port, upstream)?;
    forwarders.insert(port, child);
    drop(forwarders);
    if Client::wait_for_forwarder(port) {
        Ok(())
    } else {
        Err("The ContextShrink forwarder did not start. Restart the app and try again.".into())
    }
}

fn stop_forwarders(state: &AppState) {
    let children: Vec<Child> = state.forwarders.lock().unwrap().drain().map(|(_, c)| c).collect();
    for mut child in children {
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn editor(id: &str) -> Result<&'static client::Tool, String> {
    client::TOOLS
        .iter()
        .find(|t| t.id == id && t.kind == client::Kind::Connect)
        .ok_or_else(|| "Unknown editor".to_string())
}

/// Re-applies the editors the user connected (they were restored on quit).
fn reconnect_editors(state: &AppState) {
    for id in state.client.wanted_editors() {
        if let Ok(spec) = editor(&id) {
            if ensure_forwarder(state, spec.port, spec.upstream).is_ok() {
                let _ = state.client.connect_editor(spec);
            }
        }
    }
}

/// Restores every editor's own settings, so nothing points at a forwarder
/// that is about to stop.
fn disconnect_editors(state: &AppState) {
    for spec in client::TOOLS.iter().filter(|t| t.kind == client::Kind::Connect) {
        let _ = state.client.disconnect_editor(spec);
    }
}

async fn open_session(state: &AppState, token: &str, user: User) -> Result<Session, String> {
    if !state.client.available() {
        return Err("The ContextShrink client is missing. Reinstall the app.".into());
    }
    ensure_device_key(state, token).await?;
    ensure_forwarder(state, client::FORWARDER_PORT, None)?;
    reconnect_editors(state);
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
    disconnect_editors(&state);
    state.client.clear_wanted_editors();
    stop_forwarders(&state);
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

#[tauri::command]
fn forwarder_running() -> bool {
    Client::forwarder_listening(client::FORWARDER_PORT)
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
    ensure_forwarder(&state, spec.port, spec.upstream)?;
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
    if connected {
        ensure_forwarder(&state, spec.port, spec.upstream)?;
        state.client.connect_editor(spec)?;
    } else {
        state.client.disconnect_editor(spec)?;
    }
    state.client.set_wanted_editor(spec.id, connected);
    state.client.editor_status(spec)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    #[cfg(target_os = "linux")]
    unix::display_workarounds();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
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
            app.manage(AppState {
                api: Api::new(),
                client: Client::new(exe, data_dir),
                forwarders: Mutex::new(HashMap::new()),
            });
            // On Linux a tray icon needs libayatana-appindicator; without it the
            // app still runs, just without the icon.
            if let Err(e) = build_tray(app) {
                eprintln!("tray icon unavailable: {e}");
            }
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
            launch_tool,
            editor_status,
            set_editor_connected
        ])
        .build(tauri::generate_context!())
        .expect("error while building the ContextShrink app");

    app.run(|handle: &AppHandle, event| {
        if let RunEvent::Exit = event {
            let state = handle.state::<AppState>();
            disconnect_editors(&state);
            stop_forwarders(&state);
        }
    });
}
