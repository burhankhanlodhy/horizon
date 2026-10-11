// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // The Windows uninstaller signs the device out first (see hooks.nsh).
    if std::env::args().any(|a| a == "--uninstall-cleanup") {
        contextshrink_desktop_lib::uninstall_cleanup();
        return;
    }
    contextshrink_desktop_lib::run()
}
