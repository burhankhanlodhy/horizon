//! The bundled Horizon client (`horizon`, a frozen build of the Horizon CLI).
//!
//! It stores the device key (`vault`), runs the loopback forwarder that adds
//! the key to every request and relays it to the hosted proxy, and wraps the
//! user's own tools so they talk to that forwarder.

use std::io::Write;
use std::net::{SocketAddr, TcpStream};
#[cfg(windows)]
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use serde::Serialize;
use serde_json::Value;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
#[cfg(windows)]
const CREATE_NEW_CONSOLE: u32 = 0x0000_0010;

/// The client executable's file name inside the bundled `horizon` folder.
pub const CLIENT_EXE: &str = if cfg!(windows) { "horizon.exe" } else { "horizon" };

/// Where the device key is kept, as users know it.
pub const KEY_STORE: &str = if cfg!(windows) {
    "Windows Credential Manager"
} else {
    "the system keyring"
};

/// Loopback port of the main forwarder (kept clear of a local proxy's 8787).
/// It serves every tool that talks to Anthropic or OpenAI.
pub const FORWARDER_PORT: u16 = 18788;

/// How the app hooks a tool up to the forwarder.
#[derive(Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    /// Opened in a console through `horizon wrap`.
    Terminal,
    /// An editor whose settings the app points at the forwarder and restores
    /// (`horizon desktop connect/disconnect`).
    Connect,
    /// An editor configured in its own settings UI: the app shows what to paste.
    Settings,
}

/// One value an editor needs; `{port}` stands for the tool's forwarder port.
pub struct Setting {
    pub label: &'static str,
    pub value: &'static str,
}

/// A tool the launcher can wrap. Tools are never shipped with the app; they
/// must already be installed on the user's machine.
pub struct Tool {
    pub id: &'static str,
    pub name: &'static str,
    pub description: &'static str,
    /// Executables looked up on PATH to decide whether the tool is installed.
    pub commands: &'static [&'static str],
    pub install_url: &'static str,
    pub wrap: &'static [&'static str],
    pub unwrap: &'static [&'static str],
    /// Forwarder port the tool talks to.
    pub port: u16,
    /// Provider base URL for a tool on a provider the proxy is not configured
    /// for; its own forwarder tags model calls for it (`forward start --upstream`).
    pub upstream: Option<&'static str>,
    pub kind: Kind,
    /// Editors only: where the settings go, and the values to set there.
    pub steps: &'static str,
    pub settings: &'static [Setting],
}

impl Tool {
    /// Editor extensions are not on PATH, so editors always count as available.
    pub fn installed(&self) -> bool {
        self.commands.is_empty() || self.commands.iter().any(|c| which::which(c).is_ok())
    }
}

// Serena (code memory) needs a local Python and the retrieve MCP needs proxy
// routes the hosted gateway does not expose, so both stay off for remote use.
// Every wrap was checked against a sandboxed profile: tools without an unwrap
// get the forwarder URL from the environment only and write no files.
pub const TOOLS: &[Tool] = &[
    Tool {
        id: "claude",
        name: "Claude Code",
        description: "Anthropic's agentic coding tool for the terminal",
        commands: &["claude"],
        install_url: "https://docs.anthropic.com/en/docs/claude-code/setup",
        // Writes the base URL to the project's .claude/settings.local.json for
        // the session; unwrap restores it. --keep-mcp: the app registers no
        // MCP servers, so unwrap must not remove the user's own `horizon` or
        // code-memory registrations.
        wrap: &["wrap", "claude", "--no-proxy", "--no-mcp", "--code-memory", "none"],
        unwrap: &["unwrap", "claude", "--no-stop-proxy", "--keep-mcp"],
        port: FORWARDER_PORT,
        upstream: None,
        kind: Kind::Terminal,
        steps: "",
        settings: &[],
    },
    Tool {
        id: "codex",
        name: "Codex",
        description: "OpenAI's coding agent for the terminal",
        commands: &["codex"],
        install_url: "https://github.com/openai/codex",
        // With --no-mcp the wrap only passes the base URL on the command line
        // and in the environment; ~/.codex/config.toml is never touched (HTTP
        // and WebSocket Responses are both relayed by the forwarder). No
        // unwrap: `unwrap codex` would strip the user's own marker-delimited
        // Horizon MCP block from that file.
        wrap: &["wrap", "codex", "--no-proxy", "--no-mcp", "--code-memory", "none"],
        unwrap: &[],
        port: FORWARDER_PORT,
        upstream: None,
        kind: Kind::Terminal,
        steps: "",
        settings: &[],
    },
    Tool {
        id: "opencode",
        name: "OpenCode",
        description: "Open-source AI coding agent for the terminal",
        commands: &["opencode"],
        install_url: "https://opencode.ai/download",
        wrap: &["wrap", "opencode", "--no-proxy", "--no-mcp", "--no-serena"],
        unwrap: &["unwrap", "opencode", "--no-stop-proxy"],
        port: FORWARDER_PORT,
        upstream: None,
        kind: Kind::Terminal,
        steps: "",
        settings: &[],
    },
    Tool {
        id: "aider",
        name: "Aider",
        description: "AI pair programming in your terminal",
        commands: &["aider"],
        install_url: "https://aider.chat/docs/install.html",
        wrap: &["wrap", "aider", "--no-proxy"],
        unwrap: &[],
        port: FORWARDER_PORT,
        upstream: None,
        kind: Kind::Terminal,
        steps: "",
        settings: &[],
    },
    Tool {
        id: "copilot",
        name: "Copilot CLI",
        description: "GitHub Copilot CLI with your own Anthropic API key",
        commands: &["copilot"],
        install_url: "https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli",
        // BYOK only: the wrap needs ANTHROPIC_API_KEY (or COPILOT_PROVIDER_API_KEY)
        // and says so when it is missing. A Copilot subscription sign-in would
        // need a local proxy holding the user's GitHub token.
        wrap: &["wrap", "copilot", "--no-proxy"],
        unwrap: &[],
        port: FORWARDER_PORT,
        upstream: None,
        kind: Kind::Terminal,
        steps: "",
        settings: &[],
    },
    Tool {
        id: "goose",
        name: "Goose",
        description: "Block's open-source on-machine AI agent",
        commands: &["goose"],
        install_url: "https://block.github.io/goose/docs/getting-started/installation",
        wrap: &["wrap", "goose", "--no-proxy"],
        unwrap: &[],
        port: FORWARDER_PORT,
        upstream: None,
        kind: Kind::Terminal,
        steps: "",
        settings: &[],
    },
    Tool {
        id: "grok",
        name: "Grok CLI",
        description: "xAI's coding agent for the terminal, including Grok Build",
        commands: &["grok"],
        install_url: "https://docs.x.ai/docs/grok-cli",
        // --no-mcp leaves ~/.grok/config.toml alone, so there is nothing to undo.
        wrap: &["wrap", "grok", "--no-proxy", "--no-mcp", "--code-memory", "none"],
        unwrap: &[],
        port: 18791,
        upstream: Some("https://api.x.ai"),
        kind: Kind::Terminal,
        steps: "",
        settings: &[],
    },
    Tool {
        id: "kimi",
        name: "Kimi CLI",
        description: "Moonshot AI's coding agent (run /login once on first launch)",
        commands: &["kimi", "kimi-cli"],
        install_url: "https://github.com/MoonshotAI/kimi-cli",
        wrap: &["wrap", "kimi", "--no-proxy"],
        unwrap: &[],
        port: 18789,
        upstream: Some("https://api.kimi.com/coding/v1"),
        kind: Kind::Terminal,
        steps: "",
        settings: &[],
    },
    Tool {
        id: "vibe",
        name: "Mistral Vibe",
        description: "Mistral's coding agent for the terminal",
        commands: &["vibe"],
        install_url: "https://github.com/mistralai/mistral-vibe",
        wrap: &["wrap", "vibe", "--no-proxy"],
        unwrap: &[],
        port: 18790,
        upstream: Some("https://api.mistral.ai"),
        kind: Kind::Terminal,
        steps: "",
        settings: &[],
    },
    Tool {
        id: "omp",
        name: "Oh My Pi",
        description: "Pi coding agent with batteries included",
        commands: &["omp"],
        install_url: "https://www.npmjs.com/package/@oh-my-pi/pi-coding-agent",
        // Points ~/.omp/agent/models.yml at the forwarder (backed up first);
        // unwrap restores the backup.
        wrap: &["wrap", "omp", "--no-proxy"],
        unwrap: &["unwrap", "omp", "--no-stop-proxy"],
        port: FORWARDER_PORT,
        upstream: None,
        kind: Kind::Terminal,
        steps: "",
        settings: &[],
    },
    Tool {
        id: "openclaude",
        name: "OpenClaude",
        description: "Open-source Claude Code-style agent for any model",
        commands: &["openclaude"],
        install_url: "https://github.com/Gitlawb/openclaude",
        wrap: &["wrap", "openclaude", "--no-proxy"],
        unwrap: &[],
        port: FORWARDER_PORT,
        upstream: None,
        kind: Kind::Terminal,
        steps: "",
        settings: &[],
    },
    Tool {
        id: "openhands",
        name: "OpenHands",
        description: "All Hands AI's software agent in the terminal",
        commands: &["openhands"],
        install_url: "https://docs.all-hands.dev/",
        wrap: &["wrap", "openhands", "--no-proxy"],
        unwrap: &[],
        port: FORWARDER_PORT,
        upstream: None,
        kind: Kind::Terminal,
        steps: "",
        settings: &[],
    },
    Tool {
        id: "vscode-claude",
        name: "Claude Code for VS Code",
        description: "Anthropic's VS Code extension (also any Claude Code you start yourself)",
        commands: &[],
        install_url: "https://marketplace.visualstudio.com/items?itemName=anthropic.claude-code",
        // Sets ANTHROPIC_BASE_URL in the user-level ~/.claude/settings.json
        // (key by key, previous values saved). The app restores it on quit and
        // sign-out and re-applies it at the next sign-in.
        wrap: &[],
        unwrap: &[],
        port: FORWARDER_PORT,
        upstream: None,
        kind: Kind::Connect,
        steps: "Reload VS Code after connecting. Your Claude sign-in and model stay as they are.",
        settings: &[],
    },
    Tool {
        id: "cline",
        name: "Cline",
        description: "Autonomous coding agent for VS Code",
        commands: &[],
        install_url: "https://marketplace.visualstudio.com/items?itemName=saoudrizwan.claude-dev",
        wrap: &[],
        unwrap: &[],
        port: FORWARDER_PORT,
        upstream: None,
        kind: Kind::Settings,
        steps: "In Cline's settings, pick your API provider, turn on its custom base URL and paste the matching address. Keep your own API key.",
        settings: &[
            Setting { label: "Anthropic base URL", value: "http://127.0.0.1:{port}" },
            Setting { label: "OpenAI Compatible base URL", value: "http://127.0.0.1:{port}/v1" },
        ],
    },
    Tool {
        id: "continue",
        name: "Continue",
        description: "Open-source AI code assistant for VS Code and JetBrains",
        commands: &[],
        install_url: "https://docs.continue.dev/",
        wrap: &[],
        unwrap: &[],
        port: FORWARDER_PORT,
        upstream: None,
        kind: Kind::Settings,
        // Continue builds request URLs relative to apiBase, so the trailing
        // /v1/ is required for both providers.
        steps: "In Continue's config, add an apiBase line to each Anthropic or OpenAI model. Keep your own API key.",
        settings: &[Setting { label: "apiBase (Anthropic and OpenAI models)", value: "http://127.0.0.1:{port}/v1/" }],
    },
    Tool {
        id: "zcode",
        name: "ZCode",
        description: "Z.ai's desktop coding app",
        commands: &[],
        install_url: "https://zcode.z.ai/",
        wrap: &[],
        unwrap: &[],
        port: FORWARDER_PORT,
        upstream: None,
        kind: Kind::Settings,
        steps: "Open Settings > Model Settings > Add Provider and paste the base URL for your provider. Keep your own API key.",
        settings: &[
            Setting { label: "Anthropic base URL", value: "http://127.0.0.1:{port}" },
            Setting { label: "OpenAI base URL", value: "http://127.0.0.1:{port}/v1" },
        ],
    },
];

#[derive(Serialize)]
pub struct SettingInfo {
    pub label: &'static str,
    pub value: String,
}

#[derive(Serialize)]
pub struct ToolInfo {
    pub id: &'static str,
    pub name: &'static str,
    pub description: &'static str,
    pub installed: bool,
    pub install_url: &'static str,
    pub kind: Kind,
    pub steps: &'static str,
    pub settings: Vec<SettingInfo>,
}

pub fn tools() -> Vec<ToolInfo> {
    TOOLS
        .iter()
        .map(|t| ToolInfo {
            id: t.id,
            name: t.name,
            description: t.description,
            installed: t.installed(),
            install_url: t.install_url,
            kind: t.kind,
            steps: t.steps,
            settings: t
                .settings
                .iter()
                .map(|s| SettingInfo {
                    label: s.label,
                    value: s.value.replace("{port}", &t.port.to_string()),
                })
                .collect(),
        })
        .collect()
}

pub struct Client {
    exe: PathBuf,
    data_dir: PathBuf,
}

impl Client {
    pub fn new(exe: PathBuf, data_dir: PathBuf) -> Self {
        Client { exe, data_dir }
    }

    fn command(&self) -> Command {
        #[allow(unused_mut)]
        let mut cmd = Command::new(&self.exe);
        #[cfg(windows)]
        cmd.creation_flags(CREATE_NO_WINDOW);
        cmd
    }

    pub fn available(&self) -> bool {
        self.exe.is_file()
    }

    /// Hands the key over on stdin so it never appears in a command line.
    pub fn store_key(&self, key: &str) -> Result<(), String> {
        let mut child = self
            .command()
            .args(["vault", "set", "--stdin"])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("Could not start the ContextShrink client: {e}"))?;
        child
            .stdin
            .take()
            .ok_or("Client stdin unavailable")?
            .write_all(format!("{key}\n").as_bytes())
            .map_err(|e| e.to_string())?;
        let status = child.wait().map_err(|e| e.to_string())?;
        if status.success() {
            Ok(())
        } else {
            Err(if cfg!(windows) {
                format!("Could not save the device key to {KEY_STORE}")
            } else {
                format!(
                    "Could not save the device key to {KEY_STORE}. Make sure a keyring \
                     service such as GNOME Keyring or KWallet is running."
                )
            })
        }
    }

    pub fn has_key(&self) -> bool {
        // Output is discarded: only the exit status matters.
        self.command()
            .args(["vault", "get"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }

    pub fn clear_key(&self) {
        let _ = self
            .command()
            .args(["vault", "clear"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }

    pub fn start_forwarder(
        &self,
        remote: &str,
        port: u16,
        upstream: Option<&str>,
    ) -> Result<Child, String> {
        std::fs::create_dir_all(&self.data_dir).map_err(|e| e.to_string())?;
        // One log for both streams: uvicorn writes request lines to stdout and
        // startup/errors to stderr.
        let log_name = if port == FORWARDER_PORT {
            "forwarder.log".to_string()
        } else {
            format!("forwarder-{port}.log")
        };
        let log = std::fs::File::create(self.data_dir.join(log_name)).ok();
        let mut cmd = self.command();
        cmd.args(["forward", "start", "--remote", remote, "--port"])
            .arg(port.to_string())
            .stdin(Stdio::null());
        if let Some(upstream) = upstream {
            cmd.args(["--upstream", upstream]);
        }
        match log.as_ref().and_then(|f| Some((f.try_clone().ok()?, f.try_clone().ok()?))) {
            Some((out, err)) => cmd.stdout(out).stderr(err),
            None => cmd.stdout(Stdio::null()).stderr(Stdio::null()),
        };
        cmd.spawn()
            .map_err(|e| format!("Could not start the ContextShrink forwarder: {e}"))
    }

    pub fn wait_for_forwarder(port: u16) -> bool {
        let addr = SocketAddr::from(([127, 0, 0, 1], port));
        (0..40).any(|_| {
            if TcpStream::connect_timeout(&addr, Duration::from_millis(250)).is_ok() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(250));
            false
        })
    }

    pub fn forwarder_listening(port: u16) -> bool {
        let addr = SocketAddr::from(([127, 0, 0, 1], port));
        TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok()
    }

    /// Runs `horizon desktop <action> <editor> --port <port>`; returns stdout.
    fn desktop(&self, action: &str, tool: &Tool) -> Result<String, String> {
        let out = self
            .command()
            .args(["desktop", action, tool.id, "--port"])
            .arg(tool.port.to_string())
            .stdin(Stdio::null())
            .output()
            .map_err(|e| format!("Could not start the ContextShrink client: {e}"))?;
        if out.status.success() {
            Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
        } else {
            let err = String::from_utf8_lossy(&out.stderr);
            let err = err.trim().trim_start_matches("Error: ");
            Err(if err.is_empty() { format!("Could not update {}", tool.name) } else { err.to_string() })
        }
    }

    /// "connected", "other" (set up by something else) or "off".
    pub fn editor_status(&self, tool: &Tool) -> Result<String, String> {
        let out = self.desktop("status", tool)?;
        let parsed: Value = serde_json::from_str(&out).map_err(|e| e.to_string())?;
        Ok(parsed["status"].as_str().unwrap_or("off").to_string())
    }

    pub fn connect_editor(&self, tool: &Tool) -> Result<(), String> {
        self.desktop("connect", tool).map(|_| ())
    }

    pub fn disconnect_editor(&self, tool: &Tool) -> Result<(), String> {
        self.desktop("disconnect", tool).map(|_| ())
    }

    fn editors_file(&self) -> PathBuf {
        self.data_dir.join("editors.json")
    }

    /// Editors the user connected, re-applied at the next sign-in.
    pub fn wanted_editors(&self) -> Vec<String> {
        std::fs::read_to_string(self.editors_file())
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default()
    }

    pub fn set_wanted_editor(&self, id: &str, wanted: bool) {
        let mut ids = self.wanted_editors();
        ids.retain(|i| i != id);
        if wanted {
            ids.push(id.to_string());
        }
        let _ = std::fs::create_dir_all(&self.data_dir);
        let _ = std::fs::write(self.editors_file(), serde_json::to_string(&ids).unwrap_or_default());
    }

    pub fn clear_wanted_editors(&self) {
        let _ = std::fs::remove_file(self.editors_file());
    }

    /// Opens a terminal window in `folder` running the wrapped tool, and
    /// restores the tool's own config when it exits.
    pub fn launch(&self, tool: &Tool, folder: &Path) -> Result<(), String> {
        if !folder.is_dir() {
            return Err("Choose an existing project folder".into());
        }
        std::fs::create_dir_all(&self.data_dir).map_err(|e| e.to_string())?;
        self.launch_in_terminal(tool, folder)
    }

    #[cfg(windows)]
    fn launch_in_terminal(&self, tool: &Tool, folder: &Path) -> Result<(), String> {
        let port = tool.port.to_string();
        let quote = |args: &[&str]| args.join(" ");
        // Tools whose wrap leaves their config untouched have no unwrap step.
        let unwrap_line = if tool.unwrap.is_empty() {
            String::new()
        } else {
            format!("\"%CS_HORIZON%\" {} --port {port} >nul 2>&1\r\n", quote(tool.unwrap))
        };
        let script = format!(
            "@echo off\r\n\
             title ContextShrink - {name}\r\n\
             echo Starting {name} through ContextShrink...\r\n\
             \"%CS_HORIZON%\" {wrap} --port {port}\r\n\
             set CS_EXIT=%ERRORLEVEL%\r\n\
             {unwrap_line}\
             if not \"%CS_EXIT%\"==\"0\" (echo. & echo {name} exited with an error. & pause)\r\n",
            name = tool.name,
            wrap = quote(tool.wrap),
        );
        let path = self.data_dir.join(format!("launch-{}.cmd", tool.id));
        std::fs::write(&path, script).map_err(|e| e.to_string())?;
        Command::new("cmd")
            .arg("/c")
            .arg(&path)
            .current_dir(folder)
            .env("CS_HORIZON", &self.exe)
            .creation_flags(CREATE_NEW_CONSOLE)
            .spawn()
            .map(|_| ())
            .map_err(|e| format!("Could not open a terminal for {}: {e}", tool.name))
    }

    #[cfg(unix)]
    fn launch_in_terminal(&self, tool: &Tool, folder: &Path) -> Result<(), String> {
        use crate::unix::{open_in_terminal, sh_quote};

        let script = unix_launch_script(
            tool,
            &sh_quote(&self.exe.to_string_lossy()),
            folder,
            &std::env::var("PATH").unwrap_or_default(),
        );
        let path = self.data_dir.join(format!("launch-{}.sh", tool.id));
        open_in_terminal(&script, &path, folder, tool.name)
    }
}

/// The `sh` script a terminal runs for `tool`: wrap, then unwrap on exit, and
/// keep the window open on an error. PATH is the one the app found the tool on
/// (see `unix::import_login_path`).
#[cfg(unix)]
fn unix_launch_script(tool: &Tool, horizon: &str, folder: &Path, path: &str) -> String {
    use crate::unix::sh_quote;

    let port = tool.port.to_string();
    let quote = |args: &[&str]| args.iter().map(|a| sh_quote(a)).collect::<Vec<_>>().join(" ");
    // Tools whose wrap leaves their config untouched have no unwrap step.
    let unwrap_line = if tool.unwrap.is_empty() {
        String::new()
    } else {
        format!("{horizon} {} --port {port} >/dev/null 2>&1\n", quote(tool.unwrap))
    };
    let mut script = String::from("#!/bin/sh\n");
    script += &format!("printf '\\033]0;%s\\007' {}\n", sh_quote(&format!("ContextShrink - {}", tool.name)));
    script += &format!("export PATH={}\n", sh_quote(path));
    script += &format!("cd -- {} || exit 1\n", sh_quote(&folder.to_string_lossy()));
    script += &format!("echo {}\n", sh_quote(&format!("Starting {} through ContextShrink...", tool.name)));
    script += &format!("{horizon} {} --port {port}\n", quote(tool.wrap));
    script += "status=$?\n";
    script += &unwrap_line;
    script += "if [ \"$status\" -ne 0 ]; then\n";
    script += "  echo\n";
    script += &format!("  echo {}\n", sh_quote(&format!("{} exited with an error.", tool.name)));
    script += "  printf 'Press Enter to close this window. '\n";
    script += "  read _\n";
    script += "fi\n";
    script
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn launch_script_runs_wrap_then_unwrap_with_quoted_values() {
        let omp = TOOLS.iter().find(|t| t.id == "omp").unwrap();
        let script = unix_launch_script(omp, "'/opt/cs/horizon'", Path::new("/home/me/it's here"), "/usr/bin:/x");
        assert!(script.contains("cd -- '/home/me/it'\\''s here' || exit 1\n"));
        assert!(script.contains("export PATH='/usr/bin:/x'\n"));
        assert!(script.contains("'/opt/cs/horizon' 'wrap' 'omp' '--no-proxy' --port 18788\n"));
        assert!(script.contains("'/opt/cs/horizon' 'unwrap' 'omp' '--no-stop-proxy' --port 18788 >/dev/null 2>&1\n"));
        let wrap_at = script.find("'wrap'").unwrap();
        assert!(script.find("'unwrap'").unwrap() > wrap_at);
    }

    #[test]
    fn tools_without_unwrap_have_no_unwrap_line() {
        let aider = TOOLS.iter().find(|t| t.id == "aider").unwrap();
        let script = unix_launch_script(aider, "'h'", Path::new("/p"), "/usr/bin");
        assert!(!script.contains("unwrap"));
    }
}
