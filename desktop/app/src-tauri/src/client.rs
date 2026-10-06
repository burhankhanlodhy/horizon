//! The bundled Horizon client (`horizon.exe`, a frozen build of the Horizon CLI).
//!
//! It stores the device key (`vault`), runs the loopback forwarder that adds
//! the key to every request and relays it to the hosted proxy, and wraps the
//! user's own tools so they talk to that forwarder.

use std::io::Write;
use std::net::{SocketAddr, TcpStream};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use serde::Serialize;

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const CREATE_NEW_CONSOLE: u32 = 0x0000_0010;

/// Loopback port of the main forwarder (kept clear of a local proxy's 8787).
/// It serves every tool that talks to Anthropic or OpenAI.
pub const FORWARDER_PORT: u16 = 18788;

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
}

impl Tool {
    pub fn installed(&self) -> bool {
        self.commands.iter().any(|c| which::which(c).is_ok())
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
    },
    Tool {
        id: "grok",
        name: "Grok CLI",
        description: "xAI's coding agent for the terminal",
        commands: &["grok"],
        install_url: "https://docs.x.ai/docs/grok-cli",
        // --no-mcp leaves ~/.grok/config.toml alone, so there is nothing to undo.
        wrap: &["wrap", "grok", "--no-proxy", "--no-mcp", "--code-memory", "none"],
        unwrap: &[],
        port: 18791,
        upstream: Some("https://api.x.ai"),
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
    },
];

#[derive(Serialize)]
pub struct ToolInfo {
    pub id: &'static str,
    pub name: &'static str,
    pub description: &'static str,
    pub installed: bool,
    pub install_url: &'static str,
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
        let mut cmd = Command::new(&self.exe);
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
            Err("Could not save the device key to Windows Credential Manager".into())
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

    /// Opens a console window in `folder` running the wrapped tool, and
    /// restores the tool's own config when it exits.
    pub fn launch(&self, tool: &Tool, folder: &Path) -> Result<(), String> {
        if !folder.is_dir() {
            return Err("Choose an existing project folder".into());
        }
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
        std::fs::create_dir_all(&self.data_dir).map_err(|e| e.to_string())?;
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
}
