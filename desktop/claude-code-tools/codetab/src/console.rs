//! The helper is a GUI-subsystem binary, so logon starts it without a console
//! window. When it is started from a terminal, output still has to show there:
//! attach to the parent's console, and point the standard handles at it only
//! when they are not already redirected (a test or a pipe keeps its own).

#[cfg(windows)]
pub fn attach() {
    use windows::core::w;
    use windows::Win32::Foundation::{GENERIC_READ, GENERIC_WRITE, HANDLE};
    use windows::Win32::Storage::FileSystem::{
        CreateFileW, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
    };
    use windows::Win32::System::Console::{
        AttachConsole, GetStdHandle, SetStdHandle, ATTACH_PARENT_PROCESS, STD_ERROR_HANDLE,
        STD_OUTPUT_HANDLE,
    };

    unsafe {
        // Fails when there is no parent console (a double-click or a logon task): nothing to show.
        if AttachConsole(ATTACH_PARENT_PROCESS).is_err() {
            return;
        }
        for id in [STD_OUTPUT_HANDLE, STD_ERROR_HANDLE] {
            let current = GetStdHandle(id).unwrap_or(HANDLE::default());
            if !(current.is_invalid() || current.0.is_null()) {
                continue;
            }
            if let Ok(console) = CreateFileW(
                w!("CONOUT$"),
                (GENERIC_READ | GENERIC_WRITE).0,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                Default::default(),
                None,
            ) {
                let _ = SetStdHandle(id, console);
            }
        }
    }
}

#[cfg(not(windows))]
pub fn attach() {}
