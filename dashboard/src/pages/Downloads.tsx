import { useEffect, useState } from "react";
import { Download, ExternalLink, ShieldAlert } from "lucide-react";
import { Badge, Card, CodeBlock, CopyButton, SectionHeader } from "../components/ui";

type Os = "windows" | "macos" | "linux";

/** One downloadable file of a release. */
interface Asset {
  os: Os;
  arch: string;
  kind: "installer" | "dmg" | "deb" | "rpm" | "appimage";
  file: string;
  size_bytes: number;
  sha256: string;
}

/** Release metadata published next to the installers (desktop/publish-installer.ps1).
 *  `assets` lists every file; the top-level file fields are the Windows
 *  installer, kept for releases published before Linux support. */
interface Release {
  version: string;
  released: string;
  file?: string;
  size_bytes?: number;
  sha256?: string;
  assets?: Asset[];
}

const RELEASES_URL = "/releases";

const assetsOf = (r: Release): Asset[] =>
  r.assets ??
  (r.file
    ? [{ os: "windows", arch: "x86_64", kind: "installer", file: r.file, size_bytes: r.size_bytes ?? 0, sha256: r.sha256 ?? "" }]
    : []);

const LINUX_FORMATS: { kind: Asset["kind"]; label: string; note: string; install: (f: string) => string }[] = [
  {
    kind: "deb",
    label: "Ubuntu, Debian, Mint, Pop!_OS",
    note: ".deb package",
    install: (f) => `sudo apt install ./${f}`,
  },
  {
    kind: "rpm",
    label: "Fedora, RHEL, openSUSE",
    note: ".rpm package",
    install: (f) => `sudo dnf install ./${f}`,
  },
  {
    kind: "appimage",
    label: "Any other distribution",
    note: "AppImage, no install needed",
    install: (f) => `chmod +x ${f}\n./${f}`,
  },
];

function detectOs(): Os {
  const ua = navigator.userAgent;
  if (/Macintosh|Mac OS X/.test(ua) && !/iPhone|iPad/.test(ua)) return "macos";
  return /Linux/.test(ua) && !/Android/.test(ua) ? "linux" : "windows";
}

const OS_LABEL: Record<Os, string> = { windows: "Windows", macos: "macOS", linux: "Linux" };

/** The app isn't notarized by Apple yet, so macOS quarantines the download. */
const MAC_UNQUARANTINE = "xattr -dr com.apple.quarantine /Applications/ContextShrink.app";

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

const firstStep: Record<Os, string> = {
  windows: "Run the installer. It installs for your Windows user only; no administrator rights needed.",
  macos: "Open the .dmg, drag ContextShrink to Applications, run the Terminal command above once, then open ContextShrink.",
  linux: "Install the package for your distribution (commands above), then open ContextShrink from your app menu.",
};

const STEPS = [
  "Sign in with this account. The app creates a key for this computer automatically; it appears on API Keys as “Desktop: <your computer name>”.",
  "Choose your project folder.",
  "Click Launch next to an installed tool, such as Claude Code, Codex or OpenCode. It opens in a new window, routed through ContextShrink. Closing it restores the tool's own settings.",
];

const fmtSize = (bytes: number) => `${(bytes / 1024 / 1024).toFixed(0)} MB`;

function DownloadButton({ asset, label }: { asset: Asset; label: string }) {
  return (
    <a
      href={`${RELEASES_URL}/${encodeURIComponent(asset.file)}`}
      download
      className="btn-ink inline-flex items-center gap-2 rounded-lg px-5 py-2.5 text-sm font-semibold"
    >
      <Download size={16} /> {label}
    </a>
  );
}

function Checksum({ asset }: { asset: Asset }) {
  return (
    <div className="flex items-center gap-2">
      <code className="break-all font-mono text-xs text-ink">{asset.sha256}</code>
      <CopyButton text={asset.sha256} />
    </div>
  );
}

export default function Downloads() {
  const [release, setRelease] = useState<Release | null>(null);
  const [error, setError] = useState("");
  const [os, setOs] = useState<Os>(detectOs);

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

  const assets = release ? assetsOf(release) : [];
  const windows = assets.find((a) => a.os === "windows");
  const mac = assets.find((a) => a.os === "macos");
  const linux = LINUX_FORMATS.map((f) => ({ ...f, asset: assets.find((a) => a.os === "linux" && a.kind === f.kind) }));
  const hasLinux = linux.some((f) => f.asset);
  const released = release ? new Date(release.released).toLocaleDateString() : "";

  return (
    <div className="flex flex-col gap-6">
      <Card hairline className="p-7">
        <div className="flex flex-wrap items-start justify-between gap-6">
          <div className="max-w-xl">
            <SectionHeader eyebrow="Desktop app" title="ContextShrink for Windows, macOS and Linux" />
            <p className="text-sm leading-relaxed text-ink-3">
              Run the coding tools you already use through ContextShrink: sign in once,
              pick a project and click Launch. Your prompts are compressed on the way to
              your provider, and every saving shows up on this dashboard.
            </p>
          </div>
          <div className="flex rounded-lg border border-ink/10 p-1 text-sm" role="tablist">
            {(["windows", "macos", "linux"] as const).map((o) => (
              <button
                key={o}
                role="tab"
                aria-selected={os === o}
                onClick={() => setOs(o)}
                className={`rounded-md px-4 py-1.5 font-semibold ${os === o ? "bg-ember-soft text-ember" : "text-ink-3"}`}
              >
                {OS_LABEL[o]}
              </button>
            ))}
          </div>
        </div>

        <div className="mt-6">
          {!release && (
            <span className="text-sm text-ink-3">
              {error ? `The download isn't available right now (${error}). Please try again later.` : "Loading…"}
            </span>
          )}

          {release && os === "windows" && (
            windows ? (
              <div className="flex flex-col items-start gap-2">
                <DownloadButton asset={windows} label="Download for Windows" />
                <span className="text-xs text-ink-3">
                  Version {release.version} · {fmtSize(windows.size_bytes)} · Windows 10/11 (64-bit) · {released}
                </span>
              </div>
            ) : (
              <span className="text-sm text-ink-3">The Windows installer isn't available for this release.</span>
            )
          )}

          {release && os === "macos" && (
            mac ? (
              <div className="flex flex-col items-start gap-3">
                <DownloadButton asset={mac} label="Download for Mac" />
                <span className="text-xs text-ink-3">
                  Version {release.version} · {fmtSize(mac.size_bytes)} · Apple Silicon (M1 or newer) · macOS 12 or newer · {released}
                </span>
              </div>
            ) : (
              <span className="text-sm text-ink-3">The Mac version isn't available for this release yet.</span>
            )
          )}

          {release && os === "linux" && (
            hasLinux ? (
              <div className="flex flex-col gap-4">
                <span className="text-xs text-ink-3">
                  Version {release.version} · 64-bit x86 · Ubuntu 22.04, Debian 12, Fedora 38 or newer · {released}
                </span>
                {linux.map(({ kind, label, note, install, asset }) =>
                  asset ? (
                    <div key={kind} className="grid grid-cols-1 gap-3 rounded-lg border border-ink/10 p-4 lg:grid-cols-[minmax(0,240px)_1fr]">
                      <div className="flex flex-col items-start gap-2">
                        <div>
                          <div className="text-sm font-semibold text-ink">{label}</div>
                          <div className="text-xs text-ink-3">
                            {note} · {fmtSize(asset.size_bytes)}
                          </div>
                        </div>
                        <DownloadButton asset={asset} label="Download" />
                      </div>
                      <div className="min-w-0">
                        <CodeBlock code={`cd ~/Downloads\n${install(asset.file)}`} />
                        <div className="mt-2">
                          <Checksum asset={asset} />
                        </div>
                      </div>
                    </div>
                  ) : null,
                )}
              </div>
            ) : (
              <span className="text-sm text-ink-3">Linux packages aren't available for this release yet.</span>
            )
          )}
        </div>
      </Card>

      {os === "macos" && (
        <Card hairline className="flex gap-4 p-6">
          <ShieldAlert size={20} className="mt-0.5 shrink-0 text-ember" />
          <div className="min-w-0 text-sm leading-relaxed text-ink-2">
            <strong className="text-ink">macOS may say the app “is damaged” or “can't be opened”.</strong>{" "}
            The app isn't notarized by Apple yet, so macOS quarantines it. After dragging it
            to Applications, run this once in Terminal, then open ContextShrink as usual:
            <div className="mt-3">
              <CodeBlock code={MAC_UNQUARANTINE} />
            </div>
            The app keeps this Mac's key in your Keychain. To check the download is genuine,
            compare <code className="font-mono text-xs">shasum -a 256 ~/Downloads/{mac?.file ?? "ContextShrink.dmg"}</code>{" "}
            with:
            {mac && (
              <div className="mt-2">
                <Checksum asset={mac} />
              </div>
            )}
          </div>
        </Card>
      )}

      {os === "windows" && (
        <Card hairline className="flex gap-4 p-6">
          <ShieldAlert size={20} className="mt-0.5 shrink-0 text-ember" />
          <div className="text-sm leading-relaxed text-ink-2">
            <strong className="text-ink">Windows may show “Windows protected your PC”.</strong>{" "}
            The installer isn't code-signed yet, so SmartScreen doesn't recognise it. Click{" "}
            <strong>More info</strong>, then <strong>Run anyway</strong>. To check the download
            is genuine, compare its SHA-256 checksum below with PowerShell:
            <div className="mt-3">
              <CodeBlock
                code={`Get-FileHash "$env:USERPROFILE\\Downloads\\${windows?.file ?? "ContextShrink_x64-setup.exe"}" -Algorithm SHA256`}
              />
            </div>
            {windows && (
              <div className="mt-3">
                <Checksum asset={windows} />
              </div>
            )}
          </div>
        </Card>
      )}

      {os === "linux" && (
        <Card hairline className="flex gap-4 p-6">
          <ShieldAlert size={20} className="mt-0.5 shrink-0 text-ember" />
          <div className="text-sm leading-relaxed text-ink-2">
            <strong className="text-ink">Before you install.</strong> The app keeps this
            computer's key in your desktop's keyring (GNOME Keyring or KWallet, included with
            most desktops). The AppImage also needs FUSE 2 (
            <code className="font-mono text-xs">libfuse2</code>, or{" "}
            <code className="font-mono text-xs">libfuse2t64</code> on Ubuntu 24.04 and newer). To
            check a download is genuine, compare{" "}
            <code className="font-mono text-xs">sha256sum &lt;file&gt;</code> with the checksum
            shown next to it.
          </div>
        </Card>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card hairline className="p-6">
          <SectionHeader eyebrow="Getting started" title="Set up in a minute" />
          <ol className="flex flex-col gap-3">
            {[firstStep[os], ...STEPS].map((step, i) => (
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
            action={<Badge tone="slate">Windows, macOS, Linux</Badge>}
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
