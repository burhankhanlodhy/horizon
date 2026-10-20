//! Where things live. Production uses the defaults below. The three environment
//! overrides exist for the tests and must never be set on a real machine:
//!
//! - `CONTEXTSHRINK_DATA_DIR`: the data folder (default `%LOCALAPPDATA%\ContextShrink`).
//!   The code folder is `<data>\claude-code`.
//! - `CONTEXTSHRINK_CODETAB_ROOTS`: `;`-separated Claude Code roots. When it is
//!   set, only these roots are used, even when empty. The Python patcher reads
//!   the same variable.
//! - `CONTEXTSHRINK_CODETAB_STARTUP_DIR`: the Startup folder used by `autostart`.

use std::fs;
use std::path::PathBuf;

pub const ENV_DATA_DIR: &str = "CONTEXTSHRINK_DATA_DIR";
pub const ENV_ROOTS: &str = "CONTEXTSHRINK_CODETAB_ROOTS";
pub const ENV_STARTUP: &str = "CONTEXTSHRINK_CODETAB_STARTUP_DIR";
pub const LAUNCHER_NAME: &str = "claude-code-launcher.exe";

fn env_nonempty(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .filter(|value| !value.trim().is_empty())
}

pub fn data_dir() -> PathBuf {
    if let Some(dir) = env_nonempty(ENV_DATA_DIR) {
        return PathBuf::from(dir);
    }
    let base = match env_nonempty("LOCALAPPDATA") {
        Some(local) => PathBuf::from(local),
        None => PathBuf::from(env_nonempty("USERPROFILE").unwrap_or_default())
            .join("AppData")
            .join("Local"),
    };
    base.join("ContextShrink")
}

/// `<data>\claude-code`: the launcher's config, the patch state and the npx binaries.
pub fn code_dir() -> PathBuf {
    data_dir().join("claude-code")
}

/// Claude Desktop's Code-tab roots that exist. Both the packaged
/// (`Packages\Claude_*`) and the classic (`%APPDATA%\Claude`) locations.
pub fn discover_roots() -> Vec<PathBuf> {
    if let Some(list) = std::env::var_os(ENV_ROOTS) {
        return list
            .to_string_lossy()
            .split(';')
            .filter(|part| !part.trim().is_empty())
            .map(PathBuf::from)
            .filter(|path| path.is_dir())
            .collect();
    }
    let mut candidates = Vec::new();
    if let Some(local) = env_nonempty("LOCALAPPDATA") {
        let packages = PathBuf::from(local).join("Packages");
        if let Ok(entries) = fs::read_dir(&packages) {
            let mut matches: Vec<PathBuf> = entries
                .filter_map(|entry| entry.ok().map(|entry| entry.path()))
                .filter(|path| {
                    path.file_name()
                        .map(|name| {
                            name.to_string_lossy()
                                .get(..7)
                                .is_some_and(|prefix| prefix.eq_ignore_ascii_case("Claude_"))
                        })
                        .unwrap_or(false)
                })
                .collect();
            matches.sort();
            for package in matches {
                candidates.push(
                    package
                        .join("LocalCache")
                        .join("Roaming")
                        .join("Claude")
                        .join("claude-code"),
                );
            }
        }
    }
    if let Some(appdata) = env_nonempty("APPDATA") {
        candidates.push(PathBuf::from(appdata).join("Claude").join("claude-code"));
    }
    candidates
        .into_iter()
        .filter(|path| path.is_dir())
        .collect()
}

/// `claude-code-launcher.exe` next to this helper, which is where the npx
/// installer puts it (`npx-bin`).
pub fn default_launcher() -> PathBuf {
    match std::env::current_exe() {
        Ok(exe) => exe
            .parent()
            .map(|dir| dir.join(LAUNCHER_NAME))
            .unwrap_or_else(|| PathBuf::from(LAUNCHER_NAME)),
        Err(_) => PathBuf::from(LAUNCHER_NAME),
    }
}

/// The Startup-folder override, if the test environment set one.
pub fn startup_override() -> Option<PathBuf> {
    env_nonempty(ENV_STARTUP).map(PathBuf::from)
}
