import { useCallback, useEffect, useState, type FormEvent } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import { openUrl } from "@tauri-apps/plugin-opener";
import "./App.css";
import { ToolIcon } from "./ToolIcon";

interface User {
  id: string;
  name: string;
  email: string;
  plan?: "free" | "pro" | "team" | null;
  subscription_status?: string | null;
}

interface Session {
  user: User;
  device: string;
  dashboard_url: string;
}

interface Tool {
  id: string;
  name: string;
  description: string;
  installed: boolean;
  install_url: string;
  /** terminal: opened through `horizon wrap`; connect: settings the app sets
   *  and restores; settings: values the user pastes into the editor. */
  kind: "terminal" | "connect" | "settings";
  steps: string;
  settings: { label: string; value: string }[];
}

type EditorStatus = "connected" | "other" | "off";

interface Summary {
  user: User;
  estimate: {
    estimated_savings_usd: number;
    compression: {
      capped: boolean;
      cap_usd: number;
      cycle_savings_usd: number | null;
      compression_allowed: boolean;
    };
    payment_issue: { amount_usd: number; paused: boolean; pause_at: string } | null;
  };
}

const FOLDER_KEY = "cs_project_folder";
const usd = (n: number) =>
  `$${n.toLocaleString("en", { minimumFractionDigits: Number.isInteger(n) ? 0 : 2, maximumFractionDigits: 2 })}`;
const message = (e: unknown) => (typeof e === "string" ? e : e instanceof Error ? e.message : "Something went wrong");

function readFolder(): string {
  try {
    return localStorage.getItem(FOLDER_KEY) ?? "";
  } catch {
    return "";
  }
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [booting, setBooting] = useState(true);
  const [bootError, setBootError] = useState("");

  const restore = useCallback(async () => {
    setBooting(true);
    setBootError("");
    try {
      setSession(await invoke<Session | null>("restore_session"));
    } catch (e) {
      setBootError(message(e));
    } finally {
      setBooting(false);
    }
  }, []);

  useEffect(() => {
    void restore();
  }, [restore]);

  if (booting) return <Splash text="Connecting to ContextShrink…" />;
  if (bootError)
    return (
      <Splash text={bootError}>
        <button className="primary" onClick={() => void restore()}>
          Try again
        </button>
      </Splash>
    );
  if (!session) return <Login onSignedIn={setSession} />;
  return <Home session={session} onSignedOut={() => setSession(null)} />;
}

function Logo() {
  return (
    <div className="logo">
      <span className="mark">CS</span>
      <span>
        Context<span className="ember">Shrink</span>
      </span>
    </div>
  );
}

function Splash({ text, children }: { text: string; children?: React.ReactNode }) {
  return (
    <main className="center">
      <Logo />
      <p className="muted">{text}</p>
      {children}
    </main>
  );
}

function Login({ onSignedIn }: { onSignedIn: (s: Session) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      onSignedIn(await invoke<Session>("login", { email, password }));
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="center">
      <form className="card login" onSubmit={submit}>
        <Logo />
        <h1>Sign in</h1>
        <p className="muted">Use your ContextShrink account. This computer gets its own proxy key.</p>
        <label>
          Email
          <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && <p className="error">{error}</p>}
        <button className="primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="muted small">
          No account?{" "}
          <a href="#" onClick={() => void openUrl("https://app.contextshrink.com/signup")}>
            Create one
          </a>
        </p>
      </form>
    </main>
  );
}

function Home({ session, onSignedOut }: { session: Session; onSignedOut: () => void }) {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [tools, setTools] = useState<Tool[]>([]);
  const [folder, setFolder] = useState(readFolder);
  const [connected, setConnected] = useState(true);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [launching, setLaunching] = useState("");

  const refresh = useCallback(async () => {
    // Installed tools first; the sort is stable, so each group keeps its order.
    const all = await invoke<Tool[]>("list_tools");
    setTools([...all].sort((a, b) => Number(b.installed) - Number(a.installed)));
    setConnected(await invoke<boolean>("forwarder_running"));
    try {
      setSummary(await invoke<Summary>("account_summary"));
    } catch {
      /* keep the last summary during a brief outage */
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 30000);
    window.addEventListener("focus", refresh);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", refresh);
    };
  }, [refresh]);

  async function chooseFolder() {
    const picked = await open({ directory: true, multiple: false, title: "Choose your project folder" });
    if (typeof picked === "string") {
      setFolder(picked);
      try {
        localStorage.setItem(FOLDER_KEY, picked);
      } catch {
        /* remembered for this session only */
      }
    }
  }

  async function launch(tool: Tool) {
    setError("");
    setNotice("");
    if (!folder) {
      setError("Choose a project folder first.");
      return;
    }
    setLaunching(tool.id);
    try {
      await invoke("launch_tool", { tool: tool.id, folder });
      setNotice(`${tool.name} opened in a new window, routed through ContextShrink.`);
      setConnected(true);
    } catch (e) {
      setError(message(e));
    } finally {
      setLaunching("");
    }
  }

  async function signOut() {
    await invoke("logout").catch(() => undefined);
    onSignedOut();
  }

  const terminalTools = tools.filter((t) => t.kind === "terminal");
  const editors = tools.filter((t) => t.kind !== "terminal");
  const user = summary?.user ?? session.user;
  const plan = user.plan ?? "free";
  const est = summary?.estimate;
  const cap = est?.compression;

  return (
    <main className="home">
      <header>
        <Logo />
        <div className="who">
          <span>{user.name}</span>
          <span className="muted small">{user.email}</span>
        </div>
      </header>

      {est?.payment_issue && (
        <div className="banner warn">
          {est.payment_issue.paused
            ? `Pro features are paused until your ${usd(est.payment_issue.amount_usd)} savings fee is paid.`
            : `Your ${usd(est.payment_issue.amount_usd)} savings fee payment failed. Pay it from the dashboard to keep Pro.`}{" "}
          <a href="#" onClick={() => void openUrl(`${session.dashboard_url}/subscriptions`)}>
            Open billing
          </a>
        </div>
      )}

      <section className="stats">
        <div className="card stat">
          <span className="label">Plan</span>
          <strong className="cap">{plan}</strong>
          {user.subscription_status && user.subscription_status !== "active" && (
            <span className="muted small">{user.subscription_status.replace("_", " ")}</span>
          )}
        </div>
        <div className="card stat">
          <span className="label">Saved this cycle</span>
          <strong>{est ? usd(est.estimated_savings_usd) : "—"}</strong>
        </div>
        <div className="card stat">
          <span className="label">Compression</span>
          <strong>
            {!cap ? "—" : !cap.capped ? "Unlimited" : cap.compression_allowed ? "Active" : "Paused"}
          </strong>
          {cap?.capped && cap.cycle_savings_usd !== null && (
            <span className="muted small">
              {usd(cap.cycle_savings_usd)} of {usd(cap.cap_usd)} Free allowance
            </span>
          )}
        </div>
      </section>

      <section className="card folder">
        <div>
          <span className="label">Project folder</span>
          <p className={folder ? "path" : "muted"}>{folder || "No folder chosen"}</p>
        </div>
        <button onClick={() => void chooseFolder()}>{folder ? "Change" : "Choose folder"}</button>
      </section>

      {notice && <p className="ok">{notice}</p>}
      {error && <p className="error">{error}</p>}

      <h2 className="section-title">Terminal tools</h2>
      <section className="tools">
        {terminalTools.map((tool) => (
          <div key={tool.id} className="card tool">
            <div className="tool-head">
              <ToolIcon id={tool.id} name={tool.name} />
              <div>
                <strong>{tool.name}</strong>
                <p className="muted small">{tool.description}</p>
              </div>
            </div>
            {tool.installed ? (
              <button className="primary" disabled={launching === tool.id} onClick={() => void launch(tool)}>
                {launching === tool.id ? "Opening…" : `Launch ${tool.name}`}
              </button>
            ) : (
              <div className="missing">
                <span className="muted small">Not installed on this computer</span>
                <button onClick={() => void openUrl(tool.install_url)}>Install {tool.name}</button>
              </div>
            )}
          </div>
        ))}
      </section>

      <h2 className="section-title">Editors</h2>
      <p className="muted small section-note">
        Editors stay connected while ContextShrink is running, including when it is minimised.
      </p>
      <section className="tools">
        {editors.map((tool) =>
          tool.kind === "connect" ? (
            <ConnectCard key={tool.id} tool={tool} onConnected={() => setConnected(true)} />
          ) : (
            <SettingsCard key={tool.id} tool={tool} />
          ),
        )}
      </section>

      <footer>
        <span className={connected ? "status on" : "status"}>
          {connected ? "Connected" : "Not connected"} · {session.device}
        </span>
        <span className="links">
          <a href="#" onClick={() => void openUrl(session.dashboard_url)}>
            Open dashboard
          </a>
          <a href="#" onClick={() => void signOut()}>
            Sign out
          </a>
        </span>
      </footer>
    </main>
  );
}

function EditorHead({ tool }: { tool: Tool }) {
  return (
    <div className="tool-head">
      <ToolIcon id={tool.id} name={tool.name} />
      <div>
        <strong>{tool.name}</strong>
        <p className="muted small">{tool.description}</p>
      </div>
    </div>
  );
}

/** An editor whose settings the app points at ContextShrink and restores. */
function ConnectCard({ tool, onConnected }: { tool: Tool; onConnected: () => void }) {
  const [status, setStatus] = useState<EditorStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    invoke<EditorStatus>("editor_status", { tool: tool.id })
      .then(setStatus)
      .catch((e) => setError(message(e)));
  }, [tool.id]);

  async function toggle(connected: boolean) {
    setBusy(true);
    setError("");
    try {
      setStatus(await invoke<EditorStatus>("set_editor_connected", { tool: tool.id, connected }));
      if (connected) onConnected();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card tool">
      <EditorHead tool={tool} />
      {status === "connected" && <p className="ok small">Connected. {tool.steps}</p>}
      {status === "other" && (
        <p className="muted small">
          Already routed through a Horizon proxy set up outside this app, so it is left alone.
        </p>
      )}
      {error && <p className="error small">{error}</p>}
      {status === "connected" ? (
        <button disabled={busy} onClick={() => void toggle(false)}>
          {busy ? "Restoring…" : "Disconnect"}
        </button>
      ) : (
        <button className="primary" disabled={busy || status !== "off"} onClick={() => void toggle(true)}>
          {busy ? "Connecting…" : `Connect ${tool.name}`}
        </button>
      )}
    </div>
  );
}

/** An editor configured in its own settings: show what to paste. */
function SettingsCard({ tool }: { tool: Tool }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState("");

  async function copy(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(value);
      window.setTimeout(() => setCopied(""), 1500);
    } catch {
      /* the value stays selectable */
    }
  }

  return (
    <div className="card tool">
      <EditorHead tool={tool} />
      {open ? (
        <div className="settings">
          <p className="small">{tool.steps}</p>
          {tool.settings.map((s) => (
            <div key={s.label} className="setting">
              <span className="label">{s.label}</span>
              <div className="setting-row">
                <code>{s.value}</code>
                <button onClick={() => void copy(s.value)}>{copied === s.value ? "Copied" : "Copy"}</button>
              </div>
            </div>
          ))}
          <span className="links small">
            <a href="#" onClick={() => setOpen(false)}>
              Hide
            </a>
            <a href="#" onClick={() => void openUrl(tool.install_url)}>
              Get {tool.name}
            </a>
          </span>
        </div>
      ) : (
        <button className="primary" onClick={() => setOpen(true)}>
          Set up {tool.name}
        </button>
      )}
    </div>
  );
}
