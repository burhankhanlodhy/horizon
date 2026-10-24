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
use std::path::{Path, PathBuf};

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

/// Claude Desktop's Code-tab roots that exist, newest layout first:
///
/// - `%LOCALAPPDATA%\Claude-Data\claude_<publisher>\claude-code` (Desktop 3.6556 and later);
/// - `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code` (older packaged builds);
/// - `%APPDATA%\Claude\claude-code` (classic installs).
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
    existing_roots(
        env_nonempty("LOCALAPPDATA").map(PathBuf::from).as_deref(),
        env_nonempty("APPDATA").map(PathBuf::from).as_deref(),
    )
}

/// The candidate roots under the given `LOCALAPPDATA` and `APPDATA`, in the order
/// listed on `discover_roots`, keeping only the ones that are folders. The publisher
/// folder names are read from disk, never hard-coded.
pub fn existing_roots(local_app_data: Option<&Path>, app_data: Option<&Path>) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(local) = local_app_data {
        let claude_data = local.join("Claude-Data");
        for publisher in prefixed_children(&claude_data, "claude_") {
            candidates.push(publisher.join("claude-code"));
        }
        for package in prefixed_children(&local.join("Packages"), "Claude_") {
            candidates.push(
                package
                    .join("LocalCache")
                    .join("Roaming")
                    .join("Claude")
                    .join("claude-code"),
            );
        }
    }
    if let Some(appdata) = app_data {
        candidates.push(appdata.join("Claude").join("claude-code"));
    }
    candidates
        .into_iter()
        .filter(|path| path.is_dir())
        .collect()
}

/// The folders directly inside `dir` whose names start with `prefix`, ignoring case,
/// sorted by name. Empty when `dir` cannot be read.
fn prefixed_children(dir: &Path, prefix: &str) -> Vec<PathBuf> {
    let Ok(entries) = fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut matches: Vec<PathBuf> = entries
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| {
            path.is_dir()
                && path
                    .file_name()
                    .map(|name| {
                        name.to_string_lossy()
                            .get(..prefix.len())
                            .is_some_and(|head| head.eq_ignore_ascii_case(prefix))
                    })
                    .unwrap_or(false)
        })
        .collect();
    matches.sort();
    matches
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    static COUNTER: AtomicU64 = AtomicU64::new(0);

    /// A fresh folder under the system temp dir. Tests never use the real profile.
    fn temp_base() -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let n = COUNTER.fetch_add(1, Ordering::SeqCst);
        let base =
            std::env::temp_dir().join(format!("cs-paths-test-{}-{nanos}-{n}", std::process::id()));
        fs::create_dir_all(&base).unwrap();
        base
    }

    fn mkdirs(parts: &[&str]) -> PathBuf {
        let path = parts
            .iter()
            .fold(PathBuf::new(), |acc, part| acc.join(part));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn claude_data_root_is_found_with_the_publisher_folder_read_from_disk() {
        let base = temp_base();
        let local = base.join("Local");
        let current = mkdirs(&[
            local.to_str().unwrap(),
            "Claude-Data",
            "claude_pzs8sxrjxfjjc",
            "claude-code",
        ]);
        // Not a publisher folder, and a publisher folder without claude-code.
        mkdirs(&[
            local.to_str().unwrap(),
            "Claude-Data",
            "other",
            "claude-code",
        ]);
        mkdirs(&[local.to_str().unwrap(), "Claude-Data", "claude_empty"]);

        assert_eq!(existing_roots(Some(&local), None), vec![current]);
        fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn publisher_prefix_is_case_insensitive() {
        let base = temp_base();
        let local = base.join("Local");
        let upper = mkdirs(&[
            local.to_str().unwrap(),
            "Claude-Data",
            "CLAUDE_ABC123",
            "claude-code",
        ]);
        assert_eq!(existing_roots(Some(&local), None), vec![upper]);
        fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn older_packaged_and_classic_roots_are_kept_after_the_new_one() {
        let base = temp_base();
        let local = base.join("Local");
        let appdata = base.join("Roaming");
        let current = mkdirs(&[
            local.to_str().unwrap(),
            "Claude-Data",
            "claude_pzs8sxrjxfjjc",
            "claude-code",
        ]);
        let packaged = mkdirs(&[
            local.to_str().unwrap(),
            "Packages",
            "Claude_test",
            "LocalCache",
            "Roaming",
            "Claude",
            "claude-code",
        ]);
        let classic = mkdirs(&[appdata.to_str().unwrap(), "Claude", "claude-code"]);

        assert_eq!(
            existing_roots(Some(&local), Some(&appdata)),
            vec![current.clone(), packaged.clone(), classic.clone()]
        );
        assert_eq!(existing_roots(None, Some(&appdata)), vec![classic]);
        assert_eq!(existing_roots(Some(&local), None), vec![current, packaged]);
        fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn nothing_is_found_when_claude_desktop_is_absent() {
        let base = temp_base();
        let local = base.join("Local");
        fs::create_dir_all(&local).unwrap();
        assert!(existing_roots(Some(&local), Some(&base.join("Roaming"))).is_empty());
        assert!(existing_roots(None, None).is_empty());
        fs::remove_dir_all(&base).unwrap();
    }
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
