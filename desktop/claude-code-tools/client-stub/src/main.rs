//! horizon.exe: the launcher of the Windows desktop client.
//!
//! The desktop app runs `<install>\horizon\horizon.exe` for every client command
//! (forward, vault, wrap and unwrap, codetab). The client is the official,
//! Python-Software-Foundation-signed embeddable Python beside this file, so this
//! program only starts it:
//!
//! ```text
//! <folder>\python\python.exe -I -s -X utf8 <folder>\app\horizon_main.py <args...>
//! ```
//!
//! - Arguments pass through unchanged. std quotes them for the Windows command
//!   line and the child's C runtime reverses that. stdin, stdout, stderr and the
//!   environment are inherited, and the working directory is unchanged.
//! - The child is put in a Job Object with kill-on-close, so killing this process
//!   (`taskkill /F`, or the app stopping a forwarder by this process's PID) ends
//!   Python too. Breakaway is allowed, because `wrap` starts helpers that must
//!   outlive it, and the job is released when Python exits normally, so anything
//!   it left running keeps running as it did with the PyInstaller client. If the
//!   job cannot be set up, Python runs without one, as the Claude Code launcher
//!   does.
//! - Two variables are set for Python. `HORIZON_CLIENT_EXE` is this file, so the
//!   Python side can run the CLI through the same entry point. `HORIZON_LAUNCHER_PID`
//!   is this process's PID: the app identifies a forwarder by a process whose image
//!   is `horizon.exe`, so the forwarder reports this PID as its own.
//! - The exit code is Python's exit code. A missing `python.exe` is an error with
//!   exit code 9009.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};

/// Set for the Python child: the path of this launcher.
pub const CLIENT_EXE_ENV: &str = "HORIZON_CLIENT_EXE";
/// Set for the Python child: the process ID of this launcher.
pub const LAUNCHER_PID_ENV: &str = "HORIZON_LAUNCHER_PID";

/// Interpreter flags: `-I` isolates from PYTHON* variables and the user site.
const PYTHON_FLAGS: [&str; 4] = ["-I", "-s", "-X", "utf8"];

/// The exit code cmd.exe uses for "command not found".
const MISSING_PYTHON_EXIT: i32 = 9009;

fn main() {
    let exe = match std::env::current_exe() {
        Ok(path) => path,
        Err(err) => {
            eprintln!("horizon: cannot find its own folder: {err}");
            std::process::exit(1);
        }
    };
    let Some(folder) = exe.parent().map(Path::to_path_buf) else {
        eprintln!("horizon: cannot find its own folder: {}", exe.display());
        std::process::exit(1);
    };
    let python = python_path(&folder);
    let script = script_path(&folder);
    if !python.is_file() {
        eprintln!(
            "horizon: the embedded Python is missing: {}",
            python.display()
        );
        eprintln!("horizon: reinstall ContextShrink to restore it.");
        std::process::exit(MISSING_PYTHON_EXIT);
    }
    std::process::exit(run(&python, &script, &exe, std::env::args_os().skip(1)));
}

/// `<folder>\python\python.exe`.
pub fn python_path(folder: &Path) -> PathBuf {
    folder.join("python").join("python.exe")
}

/// `<folder>\app\horizon_main.py`.
pub fn script_path(folder: &Path) -> PathBuf {
    folder.join("app").join("horizon_main.py")
}

/// Runs the embedded Python on `script` with `args` and returns its exit code.
fn run(python: &Path, script: &Path, exe: &Path, args: impl Iterator<Item = OsString>) -> i32 {
    let mut command = Command::new(python);
    command.args(PYTHON_FLAGS).arg(script).args(args);
    command.env(CLIENT_EXE_ENV, exe);
    command.env(LAUNCHER_PID_ENV, std::process::id().to_string());

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(err) => {
            eprintln!("horizon: cannot start {}: {err}", python.display());
            return 1;
        }
    };
    // Held until Python is done; killing this process closes the job, which
    // kills Python and anything in it that did not break away.
    let job = guard(&child);
    let status = match child.wait() {
        Ok(status) => status,
        Err(err) => {
            eprintln!("horizon: waiting for Python failed: {err}");
            return 1;
        }
    };
    // Python finished normally: let whatever it left running survive this
    // launcher, as it would without a job.
    release(job);
    // Keep the exact code, including large Windows codes such as 0xC0000005,
    // which do not fit in a u8 ExitCode.
    status.code().unwrap_or(1)
}

#[cfg(windows)]
type Guard = job::KillOnClose;
#[cfg(not(windows))]
type Guard = ();

#[cfg(windows)]
fn guard(child: &Child) -> Option<Guard> {
    use std::os::windows::io::AsRawHandle;
    job::KillOnClose::attach(child.as_raw_handle())
}

#[cfg(not(windows))]
fn guard(_child: &Child) -> Option<Guard> {
    None
}

#[cfg(windows)]
fn release(guard: Option<Guard>) {
    if let Some(job) = guard {
        job.release();
    }
}

#[cfg(not(windows))]
fn release(_guard: Option<Guard>) {}

/// Kill-on-close Job Object through raw FFI, so no crate is needed.
#[cfg(windows)]
mod job {
    use std::ffi::c_void;

    type Handle = *mut c_void;

    const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: i32 = 9;
    const JOB_OBJECT_LIMIT_BREAKAWAY_OK: u32 = 0x0000_0800;
    const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;

    #[repr(C)]
    struct BasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }

    #[repr(C)]
    struct IoCounters {
        read_operation_count: u64,
        read_transfer_count: u64,
        write_operation_count: u64,
        write_transfer_count: u64,
        other_operation_count: u64,
        other_transfer_count: u64,
    }

    #[repr(C)]
    struct ExtendedLimitInformation {
        basic: BasicLimitInformation,
        io: IoCounters,
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> Handle;
        fn SetInformationJobObject(
            job: Handle,
            class: i32,
            info: *const c_void,
            length: u32,
        ) -> i32;
        fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
        fn CloseHandle(handle: Handle) -> i32;
    }

    /// Owns a job whose only member is the child. Dropping it (or this process
    /// dying) closes the last handle, which kills the child and what is in the job.
    pub struct KillOnClose(Handle);

    impl KillOnClose {
        /// `None` when the job cannot be created or the child cannot be assigned
        /// (for example when this process's own job forbids nesting).
        pub fn attach(process: Handle) -> Option<KillOnClose> {
            // SAFETY: plain Win32 calls on handles this function creates or was given.
            unsafe {
                let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if job.is_null() {
                    return None;
                }
                let mut info: ExtendedLimitInformation = std::mem::zeroed();
                info.basic.limit_flags =
                    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK;
                let set = SetInformationJobObject(
                    job,
                    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    &info as *const ExtendedLimitInformation as *const c_void,
                    std::mem::size_of::<ExtendedLimitInformation>() as u32,
                );
                if set == 0 || AssignProcessToJobObject(job, process) == 0 {
                    CloseHandle(job);
                    return None;
                }
                Some(KillOnClose(job))
            }
        }

        /// Stops the job from killing its members when it closes, then closes it.
        pub fn release(self) {
            // SAFETY: the handle is the one CreateJobObjectW returned; the info
            // struct is the same layout used to set the limit in `attach`.
            unsafe {
                let mut info: ExtendedLimitInformation = std::mem::zeroed();
                info.basic.limit_flags = JOB_OBJECT_LIMIT_BREAKAWAY_OK;
                SetInformationJobObject(
                    self.0,
                    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    &info as *const ExtendedLimitInformation as *const c_void,
                    std::mem::size_of::<ExtendedLimitInformation>() as u32,
                );
            }
        }
    }

    impl Drop for KillOnClose {
        fn drop(&mut self) {
            // SAFETY: the handle was returned by CreateJobObjectW and is closed once.
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}
