//! File-system helpers shared by the patcher: reparse-point checks, hashing, the
//! exclusive lock (the same `codetab.lock` the Python patcher takes), atomic
//! writes, and the two failure classes the patcher reports. A locked file is
//! retried on the next run; anything else is a failure for that step.

use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const LOCK_NAME: &str = "codetab.lock";
const LOCK_TIMEOUT: Duration = Duration::from_secs(5);
const STALE_LOCK: Duration = Duration::from_secs(120);
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
const ERROR_ACCESS_DENIED: i32 = 5;
const ERROR_SHARING_VIOLATION: i32 = 32;
const ERROR_LOCK_VIOLATION: i32 = 33;

/// Why a step did not finish.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Fail {
    /// A file is in use. The step is skipped and retried on the next run.
    Locked(String),
    /// Anything else. The folder or command is reported as failed.
    Failed(String),
}

pub type Res<T> = Result<T, Fail>;

pub fn failed(message: impl Into<String>) -> Fail {
    Fail::Failed(message.into())
}

/// Classifies an I/O error with the context it happened in.
pub fn io_fail(context: impl std::fmt::Display, err: io::Error) -> Fail {
    let message = format!("{context}: {err}");
    if is_locked(&err) {
        Fail::Locked(message)
    } else {
        Fail::Failed(message)
    }
}

/// Access denied or a sharing/lock violation: what Windows reports for a file
/// that another process has open.
pub fn is_locked(err: &io::Error) -> bool {
    matches!(
        err.raw_os_error(),
        Some(ERROR_ACCESS_DENIED | ERROR_SHARING_VIOLATION | ERROR_LOCK_VIOLATION)
    )
}

/// True for symlinks, junctions and any other reparse point. A missing path is not one.
pub fn is_reparse(path: &Path) -> bool {
    match fs::symlink_metadata(path) {
        Ok(meta) => is_reparse_meta(&meta),
        Err(_) => false,
    }
}

pub fn is_reparse_meta(meta: &fs::Metadata) -> bool {
    if meta.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
    }
    #[cfg(not(windows))]
    {
        false
    }
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Lower-case hex sha256 of a file. Reads in 1 MiB chunks.
pub fn sha256_file(path: &Path) -> io::Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 1024 * 1024];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(hex(&hasher.finalize()))
}

/// Holds `codetab.lock` in the code folder until dropped. Waits up to five
/// seconds; a lock older than two minutes is taken over, because its owner is gone.
pub struct Lock {
    path: PathBuf,
}

impl Lock {
    pub fn take(folder: &Path) -> Res<Lock> {
        fs::create_dir_all(folder).map_err(|err| io_fail(folder.display(), err))?;
        let path = folder.join(LOCK_NAME);
        let deadline = Instant::now() + LOCK_TIMEOUT;
        loop {
            match OpenOptions::new().write(true).create_new(true).open(&path) {
                Ok(mut file) => {
                    let _ = writeln!(file, "{} {}", std::process::id(), iso_utc_now());
                    return Ok(Lock { path });
                }
                Err(err) if err.kind() == io::ErrorKind::AlreadyExists => {
                    if is_stale(&path) && fs::remove_file(&path).is_ok() {
                        continue;
                    }
                    if Instant::now() >= deadline {
                        return Err(failed(format!(
                            "another codetab operation holds {}; try again, or delete the file if no install is running",
                            path.display()
                        )));
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
                Err(err) => return Err(io_fail(path.display(), err)),
            }
        }
    }
}

impl Drop for Lock {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

fn is_stale(path: &Path) -> bool {
    fs::metadata(path)
        .and_then(|meta| meta.modified())
        .ok()
        .and_then(|modified| SystemTime::now().duration_since(modified).ok())
        .is_some_and(|age| age > STALE_LOCK)
}

/// A file name next to `path` that no other write uses: same folder, with the
/// process id and a timestamp in it.
pub fn unique_sibling(path: &Path, suffix: &str) -> PathBuf {
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or(0);
    path.with_file_name(format!("{name}.{}.{nanos}.{suffix}", std::process::id()))
}

/// Writes `text` through a uniquely named temp file in the same folder, then
/// renames it into place. A failed write leaves no temp file behind.
pub fn write_text_atomic(path: &Path, text: &str) -> Res<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|err| io_fail(parent.display(), err))?;
    }
    let tmp = unique_sibling(path, "tmp");
    let result = (|| -> io::Result<()> {
        let mut file = OpenOptions::new().write(true).create_new(true).open(&tmp)?;
        file.write_all(text.as_bytes())?;
        drop(file);
        fs::rename(&tmp, path)
    })();
    if let Err(err) = result {
        let _ = fs::remove_file(&tmp);
        return Err(io_fail(path.display(), err));
    }
    Ok(())
}

/// Writes `value` as pretty JSON with a trailing newline, unless the file
/// already holds exactly that text. Repeated passes therefore write nothing.
pub fn write_json_if_changed(path: &Path, value: &Value) -> Res<()> {
    let mut text = serde_json::to_string_pretty(value).map_err(|err| failed(err.to_string()))?;
    text.push('\n');
    if fs::read_to_string(path).ok().as_deref() == Some(text.as_str()) {
        return Ok(());
    }
    write_text_atomic(path, &text)
}

/// Seconds since the Unix epoch, or 0 when the clock is before it.
pub fn unix_now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or(0)
}

/// The current time as `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`, the format
/// `datetime.isoformat()` writes for a UTC datetime, so both patchers agree.
pub fn iso_utc_now() -> String {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let (year, month, day) = civil_from_days((elapsed.as_secs() / 86_400) as i64);
    let second_of_day = elapsed.as_secs() % 86_400;
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:06}+00:00",
        second_of_day / 3600,
        (second_of_day % 3600) / 60,
        second_of_day % 60,
        elapsed.subsec_micros()
    )
}

/// Days since 1970-01-01 to (year, month, day), proleptic Gregorian.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let day_of_era = z - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let shifted_month = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * shifted_month + 2) / 5 + 1;
    let month = if shifted_month < 10 {
        shifted_month + 3
    } else {
        shifted_month - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    (year, month as u32, day as u32)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_dates_match_known_days() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        // 2024-01-01 is 19723 days after the epoch; 2000-02-29 is 11016.
        assert_eq!(civil_from_days(19_723), (2024, 1, 1));
        assert_eq!(civil_from_days(11_016), (2000, 2, 29));
    }

    #[test]
    fn iso_timestamps_have_the_python_shape() {
        let text = iso_utc_now();
        assert_eq!(text.len(), 32, "{text}");
        assert!(text.ends_with("+00:00"), "{text}");
        assert_eq!(&text[10..11], "T", "{text}");
    }

    #[test]
    fn hex_is_lower_case_and_padded() {
        assert_eq!(hex(&[0x00, 0xab, 0x0f]), "00ab0f");
    }
}
