//! Test-only stand-in for contextshrink-codetab.exe.
//!
//! The launcher's self-heal starts `npx-bin\contextshrink-codetab.exe repair
//! --quiet`. Tests copy this binary there and read the log it writes, so no
//! real Claude folder is ever touched. Each run appends its arguments, joined
//! by spaces, as one line to the file named by `CS_TEST_HELPER_LOG`.

use std::fs::OpenOptions;
use std::io::Write;

fn main() {
    let Some(path) = std::env::var_os("CS_TEST_HELPER_LOG") else {
        return;
    };
    let line: Vec<String> = std::env::args_os()
        .skip(1)
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .expect("open CS_TEST_HELPER_LOG");
    writeln!(file, "{}", line.join(" ")).expect("write CS_TEST_HELPER_LOG");
}
