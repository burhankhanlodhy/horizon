//! App secrets in the OS credential store: Windows Credential Manager, or the
//! Secret Service keyring on Linux (GNOME Keyring, KWallet).
//!
//! Holds the dashboard session token and the ID of this device's proxy key
//! (needed to revoke it on sign-out). The proxy key itself is stored by the
//! bundled Horizon client (`horizon vault`), which the forwarder reads.

use keyring::Entry;

const SERVICE: &str = "ContextShrink Desktop";
const SESSION: &str = "session";
const DEVICE_KEY_ID: &str = "device-key-id";

fn entry(name: &str) -> Option<Entry> {
    Entry::new(SERVICE, name).ok()
}

fn get(name: &str) -> Option<String> {
    entry(name)?.get_password().ok().filter(|v| !v.is_empty())
}

/// Creates the keyring's default collection when there is none.
///
/// A desktop login normally creates it ("Login"); minimal sessions, a fresh
/// keyring and WSL have none, and saving then fails with "no result found".
/// Creating it shows the keyring's own prompt to choose a password.
#[cfg(target_os = "linux")]
fn ensure_default_collection() {
    use dbus_secret_service::{EncryptionType, SecretService};

    if let Ok(ss) = SecretService::connect(EncryptionType::Dh) {
        if ss.get_default_collection().is_err() {
            let _ = ss.create_collection("Login", "default");
        }
    }
}

fn set(name: &str, value: &str) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    ensure_default_collection();
    entry(name)
        .ok_or_else(|| format!("{} is unavailable", crate::client::KEY_STORE))?
        .set_password(value)
        .map_err(|e| format!("Could not save to {}: {e}", crate::client::KEY_STORE))
}

fn clear(name: &str) {
    if let Some(e) = entry(name) {
        let _ = e.delete_credential();
    }
}

pub fn session() -> Option<String> {
    get(SESSION)
}

pub fn set_session(token: &str) -> Result<(), String> {
    set(SESSION, token)
}

pub fn device_key_id() -> Option<String> {
    get(DEVICE_KEY_ID)
}

pub fn set_device_key_id(id: &str) -> Result<(), String> {
    set(DEVICE_KEY_ID, id)
}

pub fn clear_all() {
    clear(SESSION);
    clear(DEVICE_KEY_ID);
}
