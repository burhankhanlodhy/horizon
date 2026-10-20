//! The patcher's record of what it changed: `codetab-state.json` in the code
//! folder. It has the same schema `horizon/desktop_codetab.py` writes:
//!
//! ```json
//! {"v": 1, "launcher_sha256": ["<hex>"],
//!  "folders": {"<folder>": {"real_sha256": "...", "launcher_sha256": "...",
//!                           "patched_at": "...", "displaced": ["..."]}}}
//! ```
//!
//! A malformed file stops the run instead of being overwritten, because it is
//! the only record of what was patched. Keys this module does not know are kept.

use crate::fsx::{failed, io_fail, write_json_if_changed, Res};
use serde_json::{Map, Value};
use std::fs;
use std::path::Path;

pub const STATE_NAME: &str = "codetab-state.json";
const STATE_VERSION: u64 = 1;

#[derive(Debug, Clone, PartialEq)]
pub struct State {
    extra: Map<String, Value>,
    pub launchers: Vec<String>,
    folders: Map<String, Value>,
}

impl State {
    pub fn empty() -> State {
        State {
            extra: Map::new(),
            launchers: Vec::new(),
            folders: Map::new(),
        }
    }

    /// The state in `code_dir`, or an empty one when there is no file.
    pub fn load(code_dir: &Path) -> Res<State> {
        let path = code_dir.join(STATE_NAME);
        if !path.exists() {
            return Ok(State::empty());
        }
        let unreadable = |detail: String| {
            failed(format!(
                "{} is unreadable ({detail}); fix or remove it first",
                path.display()
            ))
        };
        let text = fs::read_to_string(&path).map_err(|err| unreadable(err.to_string()))?;
        let value: Value =
            serde_json::from_str(&text).map_err(|err| unreadable(err.to_string()))?;
        let shape = || {
            failed(format!(
                "{} has an unsupported shape; refusing to change it",
                path.display()
            ))
        };
        let Value::Object(mut object) = value else {
            return Err(shape());
        };
        if object.get("v").and_then(Value::as_u64) != Some(STATE_VERSION) {
            return Err(shape());
        }
        let Some(Value::Array(list)) = object.remove("launcher_sha256") else {
            return Err(shape());
        };
        let launchers = list
            .into_iter()
            .map(|item| match item {
                Value::String(text) => Ok(text),
                _ => Err(shape()),
            })
            .collect::<Res<Vec<String>>>()?;
        let Some(Value::Object(folders)) = object.remove("folders") else {
            return Err(shape());
        };
        object.remove("v");
        Ok(State {
            extra: object,
            launchers,
            folders,
        })
    }

    /// Writes the state, or deletes the file when nothing is recorded.
    pub fn save(&self, code_dir: &Path) -> Res<()> {
        let path = code_dir.join(STATE_NAME);
        if self.folders.is_empty() && self.launchers.is_empty() {
            if path.exists() {
                fs::remove_file(&path).map_err(|err| io_fail(path.display(), err))?;
            }
            return Ok(());
        }
        let mut object = self.extra.clone();
        object.insert("v".to_string(), Value::from(STATE_VERSION));
        object.insert(
            "launcher_sha256".to_string(),
            Value::Array(self.launchers.iter().cloned().map(Value::String).collect()),
        );
        object.insert("folders".to_string(), Value::Object(self.folders.clone()));
        write_json_if_changed(&path, &Value::Object(object))
    }

    pub fn has_launcher(&self, sha: &str) -> bool {
        self.launchers.iter().any(|known| known == sha)
    }

    pub fn add_launcher(&mut self, sha: &str) {
        if !self.has_launcher(sha) {
            self.launchers.push(sha.to_string());
        }
    }

    pub fn clear_launchers(&mut self) {
        self.launchers.clear();
    }

    pub fn folder(&self, key: &str) -> Option<&Map<String, Value>> {
        self.folders.get(key).and_then(Value::as_object)
    }

    pub fn folder_keys(&self) -> Vec<String> {
        self.folders.keys().cloned().collect()
    }

    pub fn folder_count(&self) -> usize {
        self.folders.len()
    }

    pub fn set_folder(&mut self, key: &str, record: Map<String, Value>) {
        self.folders.insert(key.to_string(), Value::Object(record));
    }

    /// Records `record` only when the folder has no record yet.
    pub fn ensure_folder(&mut self, key: &str, record: Map<String, Value>) {
        if !self.folders.contains_key(key) {
            self.set_folder(key, record);
        }
    }

    pub fn remove_folder(&mut self, key: &str) {
        self.folders.remove(key);
    }
}

/// The saved `codetab.json` as an object, or `None` when it is absent or not one.
pub fn read_config_object(path: &Path) -> Option<Map<String, Value>> {
    let text = fs::read_to_string(path).ok()?;
    match serde_json::from_str::<Value>(&text).ok()? {
        Value::Object(object) => Some(object),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("cs-state-unit-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn unknown_keys_survive_a_round_trip() {
        let dir = scratch("extra");
        fs::write(
            dir.join(STATE_NAME),
            r#"{"v":1,"launcher_sha256":["aa"],"folders":{},"future":{"x":1}}"#,
        )
        .unwrap();
        let mut state = State::load(&dir).unwrap();
        state.add_launcher("bb");
        state.save(&dir).unwrap();
        let text = fs::read_to_string(dir.join(STATE_NAME)).unwrap();
        assert!(text.contains("\"future\""), "{text}");
        assert!(text.contains("\"bb\""), "{text}");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_malformed_state_file_is_never_overwritten() {
        let dir = scratch("bad");
        fs::write(dir.join(STATE_NAME), "{not json").unwrap();
        assert!(State::load(&dir).is_err());
        fs::write(
            dir.join(STATE_NAME),
            r#"{"v":2,"launcher_sha256":[],"folders":{}}"#,
        )
        .unwrap();
        assert!(State::load(&dir).is_err());
        fs::write(
            dir.join(STATE_NAME),
            r#"{"v":1,"launcher_sha256":[3],"folders":{}}"#,
        )
        .unwrap();
        assert!(State::load(&dir).is_err());
        let _ = fs::remove_dir_all(&dir);
    }
}
