//! The sign-in task: `<Startup>\ContextShrink Code tab.lnk` runs
//! `contextshrink-codetab.exe repair --quiet` at logon. The shortcut is written
//! with IShellLinkW, as the desktop app does (`desktop/app/src-tauri/src/claude_code.rs`),
//! and only when it differs, so repeated calls write nothing.

use crate::paths;
use std::path::{Path, PathBuf};

pub const SHORTCUT_NAME: &str = "ContextShrink Code tab.lnk";
pub const SHORTCUT_ARGS: &str = "repair --quiet";
const DESCRIPTION: &str = "ContextShrink Code tab";

/// `<Startup>\ContextShrink Code tab.lnk`. The Startup folder can be overridden
/// by `CONTEXTSHRINK_CODETAB_STARTUP_DIR`, for the tests only.
pub fn shortcut_path() -> Result<PathBuf, String> {
    let folder = match paths::startup_override() {
        Some(folder) => folder,
        None => startup_folder()?,
    };
    Ok(folder.join(SHORTCUT_NAME))
}

/// True when the shortcut exists and starts `exe` with the repair arguments.
pub fn is_enabled(exe: &Path) -> bool {
    let Ok(path) = shortcut_path() else {
        return false;
    };
    match read_shortcut(&path) {
        Ok((target, args)) => same_path(&target, exe) && args == SHORTCUT_ARGS,
        Err(_) => false,
    }
}

/// Makes the shortcut start `exe` with the repair arguments. Writes nothing when
/// it already does.
pub fn enable(exe: &Path) -> Result<PathBuf, String> {
    let path = shortcut_path()?;
    if is_enabled(exe) {
        return Ok(path);
    }
    if let Some(folder) = path.parent() {
        std::fs::create_dir_all(folder)
            .map_err(|err| format!("Could not create {}: {err}", folder.display()))?;
    }
    write_shortcut(&path, exe)?;
    Ok(path)
}

/// Removes the shortcut. Succeeds when it is already gone.
pub fn disable() -> Result<(), String> {
    let path = shortcut_path()?;
    match std::fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(format!("Could not remove {}: {err}", path.display())),
    }
}

fn same_path(a: &Path, b: &Path) -> bool {
    a.to_string_lossy()
        .eq_ignore_ascii_case(&b.to_string_lossy())
}

#[cfg(windows)]
pub use imp::{read_shortcut, startup_folder, write_shortcut};

#[cfg(not(windows))]
fn startup_folder() -> Result<PathBuf, String> {
    Err("the sign-in shortcut needs Windows".to_string())
}

#[cfg(not(windows))]
fn read_shortcut(_path: &Path) -> Result<(PathBuf, String), String> {
    Err("the sign-in shortcut needs Windows".to_string())
}

#[cfg(not(windows))]
fn write_shortcut(_path: &Path, _exe: &Path) -> Result<(), String> {
    Err("the sign-in shortcut needs Windows".to_string())
}

#[cfg(windows)]
mod imp {
    use super::{DESCRIPTION, SHORTCUT_ARGS, SHORTCUT_NAME};
    use std::fs;
    use std::path::{Path, PathBuf};
    use windows::core::{Interface, PCWSTR};
    use windows::Win32::Foundation::RPC_E_CHANGED_MODE;
    use windows::Win32::System::Com::{
        CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize, IPersistFile,
        CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED, STGM_READ,
    };
    use windows::Win32::UI::Shell::{
        FOLDERID_Startup, IShellLinkW, SHGetKnownFolderPath, ShellLink, KF_FLAG_DEFAULT,
    };

    pub fn startup_folder() -> Result<PathBuf, String> {
        let raw = unsafe { SHGetKnownFolderPath(&FOLDERID_Startup, KF_FLAG_DEFAULT, None) }
            .map_err(|err| format!("Could not find the Windows Startup folder: {err}"))?;
        let folder = unsafe { raw.to_string() };
        unsafe { CoTaskMemFree(Some(raw.0 as *const _)) };
        folder
            .map(PathBuf::from)
            .map_err(|err| format!("Could not read the Windows Startup folder: {err}"))
    }

    /// The target and the arguments of the shortcut at `path`.
    pub fn read_shortcut(path: &Path) -> Result<(PathBuf, String), String> {
        let _com = ComScope::new()?;
        let link: IShellLinkW = unsafe { CoCreateInstance(&ShellLink, None, CLSCTX_INPROC_SERVER) }
            .map_err(|err| format!("Could not open the shortcut helper: {err}"))?;
        let file: IPersistFile = link.cast().map_err(|err| err.to_string())?;
        let wide_path = wide(&path.to_string_lossy());
        unsafe { file.Load(PCWSTR(wide_path.as_ptr()), STGM_READ) }
            .map_err(|err| format!("Could not read {}: {err}", path.display()))?;
        let target = link_text(|buf| unsafe { link.GetPath(buf, std::ptr::null_mut(), 0) })?;
        let args = link_text(|buf| unsafe { link.GetArguments(buf) })?;
        Ok((PathBuf::from(target), args))
    }

    /// Writes the shortcut to `path` through a temp file that is renamed into
    /// place, so a failed save never leaves a half-written shortcut.
    pub fn write_shortcut(path: &Path, exe: &Path) -> Result<(), String> {
        let _com = ComScope::new()?;
        let link: IShellLinkW = unsafe { CoCreateInstance(&ShellLink, None, CLSCTX_INPROC_SERVER) }
            .map_err(|err| format!("Could not open the shortcut helper: {err}"))?;
        let target = wide(&exe.to_string_lossy());
        let args = wide(SHORTCUT_ARGS);
        let working_dir = wide(
            &exe.parent()
                .map(|dir| dir.to_string_lossy().into_owned())
                .unwrap_or_default(),
        );
        let description = wide(DESCRIPTION);
        let set = |result: windows::core::Result<()>| {
            result.map_err(|err| format!("Could not write {}: {err}", path.display()))
        };
        unsafe {
            set(link.SetPath(PCWSTR(target.as_ptr())))?;
            set(link.SetArguments(PCWSTR(args.as_ptr())))?;
            set(link.SetWorkingDirectory(PCWSTR(working_dir.as_ptr())))?;
            set(link.SetDescription(PCWSTR(description.as_ptr())))?;
        }
        let file: IPersistFile = link.cast().map_err(|err| err.to_string())?;
        let tmp = path.with_file_name(format!("{SHORTCUT_NAME}.new"));
        let wide_tmp = wide(&tmp.to_string_lossy());
        if let Err(err) = unsafe { file.Save(PCWSTR(wide_tmp.as_ptr()), false) } {
            let _ = fs::remove_file(&tmp);
            return Err(format!("Could not write {}: {err}", path.display()));
        }
        fs::rename(&tmp, path).map_err(|err| {
            let _ = fs::remove_file(&tmp);
            format!("Could not write {}: {err}", path.display())
        })
    }

    fn wide(text: &str) -> Vec<u16> {
        text.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn link_text(
        get: impl FnOnce(&mut [u16]) -> windows::core::Result<()>,
    ) -> Result<String, String> {
        let mut buf = vec![0u16; 1024];
        get(&mut buf).map_err(|err| err.to_string())?;
        let len = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
        Ok(String::from_utf16_lossy(&buf[..len]))
    }

    /// Initializes COM on this thread for as long as the guard lives. A thread
    /// that already uses COM in another mode keeps it, and is not uninitialized here.
    struct ComScope {
        initialized: bool,
    }

    impl ComScope {
        fn new() -> Result<Self, String> {
            let hr = unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED) };
            if hr == RPC_E_CHANGED_MODE {
                Ok(Self { initialized: false })
            } else if hr.is_ok() {
                Ok(Self { initialized: true })
            } else {
                Err(format!("Could not start COM: {hr:?}"))
            }
        }
    }

    impl Drop for ComScope {
        fn drop(&mut self) {
            if self.initialized {
                unsafe { CoUninitialize() };
            }
        }
    }
}
