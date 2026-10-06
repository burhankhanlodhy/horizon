import { useEffect, useState } from "react";
import { Download, ExternalLink, ShieldAlert } from "lucide-react";
import { Badge, Card, CodeBlock, CopyButton, SectionHeader } from "../components/ui";

/** Release metadata published next to the installers (desktop/publish-installer.ps1). */
interface Release {
  version: string;
  file: string;
  size_bytes: number;
  sha256: string;
  released: string;
}

const RELEASES_URL = "/releases";

const TOOLS = [
  {
    name: "Claude Code",
    note: "Anthropic's agentic coding tool for the terminal",
    url: "https://docs.anthropic.com/en/docs/claude-code/setup",
  },
  {
    name: "Codex",
    note: "OpenAI's coding agent for the terminal",
    url: "https://github.com/openai/codex",
  },
  {
    name: "OpenCode",
    note: "Open-source AI coding agent for the terminal",
    url: "https://opencode.ai/download",
  },
  {
    name: "Aider",
    note: "AI pair programming in your terminal",
    url: "https://aider.chat/docs/install.html",
  },
  {
    name: "Copilot CLI",
    note: "GitHub Copilot CLI with your own Anthropic API key",
    url: "https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli",
  },
  {
    name: "Goose",
    note: "Block's open-source on-machine AI agent",
    url: "https://block.github.io/goose/docs/getting-started/installation",
  },
  {
    name: "Grok CLI",
    note: "xAI's coding agent for the terminal",
    url: "https://docs.x.ai/docs/grok-cli",
  },
  {
    name: "Kimi CLI",
    note: "Moonshot AI's coding agent (run /login once on first launch)",
    url: "https://github.com/MoonshotAI/kimi-cli",
  },
  {
    name: "Mistral Vibe",
    note: "Mistral's coding agent for the terminal",
    url: "https://github.com/mistralai/mistral-vibe",
  },
  {
    name: "Oh My Pi",
    note: "Pi coding agent with batteries included",
    url: "https://www.npmjs.com/package/@oh-my-pi/pi-coding-agent",
  },
  {
    name: "OpenClaude",
    note: "Open-source Claude Code-style agent for any model",
    url: "https://github.com/Gitlawb/openclaude",
  },
  {
    name: "OpenHands",
    note: "All Hands AI's software agent in the terminal",
    url: "https://docs.all-hands.dev/",
  },
  {
    name: "Claude Code for VS Code",
    note: "Editor: connected and restored by the app",
    url: "https://marketplace.visualstudio.com/items?itemName=anthropic.claude-code",
  },
  {
    name: "Cline",
    note: "Editor: the app shows the base URL to paste",
    url: "https://marketplace.visualstudio.com/items?itemName=saoudrizwan.claude-dev",
  },
  {
    name: "Continue",
    note: "Editor: the app shows the apiBase to paste",
    url: "https://docs.continue.dev/",
  },
  {
    name: "ZCode",
    note: "Editor: the app shows the base URL to paste",
    url: "https://zcode.z.ai/",
  },
];

const STEPS = [
  "Run the installer. It installs for your Windows user only; no administrator rights needed.",
  "Open ContextShrink and sign in with this account. The app creates a key for this computer automatically; it appears on API Keys as “Desktop: <your PC name>”.",
  "Choose your project folder.",
  "Click Launch next to an installed tool, such as Claude Code, Codex or OpenCode. It opens in a new window, routed through ContextShrink. Closing it restores the tool's own settings.",
];

const fmtSize = (bytes: number) => `${(bytes / 1024 / 1024).toFixed(0)} MB`;

export default function Downloads() {
  const [release, setRelease] = useState<Release | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${RELEASES_URL}/latest.json`, { cache: "no-store", signal: controller.signal })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<Release>;
      })
      .then(setRelease)
      .catch((e) => {
        if (!controller.signal.aborted)
          setError(e instanceof Error ? e.message : "unavailable");
      });
    return () => controller.abort();
  }, []);

  const href = release ? `${RELEASES_URL}/${encodeURIComponent(release.file)}` : undefined;

  return (
    <div className="flex flex-col gap-6">
      <Card hairline className="p-7">
        <div className="flex flex-wrap items-start justify-between gap-6">
          <div className="max-w-xl">
            <SectionHeader eyebrow="Desktop app" title="ContextShrink for Windows" />
            <p className="text-sm leading-relaxed text-ink-3">
              Run the coding tools you already use through ContextShrink: sign in once,
              pick a project and click Launch. Your prompts are compressed on the way to
              your provider, and every saving shows up on this dashboard.
            </p>
          </div>
          <div className="flex flex-col items-start gap-2">
            {href ? (
              <a
                href={href}
                download
                className="btn-ink inline-flex items-center gap-2 rounded-lg px-5 py-2.5 text-sm font-semibold"
              >
                <Download size={16} /> Download for Windows
              </a>
            ) : (
              <span className="btn-ghost inline-flex cursor-default items-center gap-2 rounded-lg px-5 py-2.5 text-sm font-medium opacity-70">
                <Download size={16} /> {error ? "Download unavailable" : "Loading…"}
              </span>
            )}
            {release && (
              <span className="text-xs text-ink-3">
                Version {release.version} · {fmtSize(release.size_bytes)} · Windows 10/11 (64-bit) ·{" "}
                {new Date(release.released).toLocaleDateString()}
              </span>
            )}
            {error && (
              <span className="text-xs text-ember">
                The installer isn't available right now ({error}). Please try again later.
              </span>
            )}
          </div>
        </div>
      </Card>

      <Card hairline className="flex gap-4 p-6">
        <ShieldAlert size={20} className="mt-0.5 shrink-0 text-ember" />
        <div className="text-sm leading-relaxed text-ink-2">
          <strong className="text-ink">Windows may show “Windows protected your PC”.</strong>{" "}
          The installer isn't code-signed yet, so SmartScreen doesn't recognise it. Click{" "}
          <strong>More info</strong>, then <strong>Run anyway</strong>. To check the download
          is genuine, compare its SHA-256 checksum below with PowerShell:
          <div className="mt-3">
            <CodeBlock
              code={`Get-FileHash "$env:USERPROFILE\\Downloads\\${release?.file ?? "ContextShrink_x64-setup.exe"}" -Algorithm SHA256`}
            />
          </div>
          {release && (
            <div className="mt-3 flex items-center gap-2">
              <code className="break-all font-mono text-xs text-ink">{release.sha256}</code>
              <CopyButton text={release.sha256} />
            </div>
          )}
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card hairline className="p-6">
          <SectionHeader eyebrow="Getting started" title="Set up in a minute" />
          <ol className="flex flex-col gap-3">
            {STEPS.map((step, i) => (
              <li key={i} className="flex gap-3 text-sm leading-relaxed text-ink-2">
                <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-ember-soft text-xs font-bold text-ember">
                  {i + 1}
                </span>
                <span>{step}</span>
              </li>
            ))}
          </ol>
        </Card>

        <Card hairline className="p-6">
          <SectionHeader
            eyebrow="Supported tools"
            title="Bring your own tools"
            action={<Badge tone="slate">macOS & Linux coming soon</Badge>}
          />
          <p className="mb-4 text-sm leading-relaxed text-ink-3">
            The app doesn't include these tools. Install the ones you use; the app finds
            them automatically and keeps using your own provider login or API key.
          </p>
          <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {TOOLS.map((tool) => (
              <li
                key={tool.name}
                className="flex items-center justify-between gap-3 rounded-lg border border-ink/10 px-4 py-3"
              >
                <div>
                  <div className="text-sm font-semibold text-ink">{tool.name}</div>
                  <div className="text-xs text-ink-3">{tool.note}</div>
                </div>
                <a
                  href={tool.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 text-xs font-semibold text-ember hover:underline"
                >
                  Install <ExternalLink size={12} />
                </a>
              </li>
            ))}
          </ul>
        </Card>
      </div>
    </div>
  );
}
