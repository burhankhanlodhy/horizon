import { Link } from "react-router-dom";
import { Badge, Card, CodeBlock, SectionHeader } from "../components/ui";
import { PROXY_URL } from "../lib/api";

const ENDPOINTS = [
  { method: "POST", path: "/v1/messages", desc: "Anthropic Messages (Claude)", scope: "messages" },
  { method: "POST", path: "/v1/messages/count_tokens", desc: "Anthropic token counting", scope: "messages" },
  { method: "POST", path: "/v1/chat/completions", desc: "OpenAI-compatible Chat Completions", scope: "messages" },
  { method: "POST", path: "/v1/responses", desc: "OpenAI Responses (incl. ChatGPT sign-in)", scope: "responses" },
  {
    method: "POST",
    path: "/v1beta/models/{model}:generateContent",
    desc: "Gemini (also :streamGenerateContent, :countTokens)",
    scope: "messages",
  },
  { method: "GET", path: "/v1/models", desc: "Model list (passed through)", scope: "messages" },
];

const CLAUDE_MANUAL = `# macOS, servers, or any computer without the app
export ANTHROPIC_BASE_URL=${PROXY_URL}
export ANTHROPIC_CUSTOM_HEADERS="X-Horizon-Proxy-Token: cs_live_..."
claude`;

const CLAUDE_NPX = `npx contextshrink-claude@latest
npx contextshrink-claude@latest status`;

const CODEX_NPX = `npx contextshrink-codex@latest
npx contextshrink-codex@latest status`;

const CODEX_CHATS = `npx contextshrink-codex@latest chats list
npx contextshrink-codex@latest chats migrate CHAT_ID`;

const OPENAI_SDK = `from openai import OpenAI

client = OpenAI(
    base_url="${PROXY_URL}/v1",
    api_key="sk-...",  # your own OpenAI (or compatible) key
    default_headers={"X-Horizon-Proxy-Token": "cs_live_..."},
)`;

const CURL = `curl ${PROXY_URL}/v1/messages \\
  -H "X-Horizon-Proxy-Token: cs_live_..." \\
  -H "x-api-key: $ANTHROPIC_API_KEY" \\
  -H "anthropic-version: 2023-06-01" \\
  -H "content-type: application/json" \\
  -d '{"model":"claude-sonnet-5-5","max_tokens":256,
       "messages":[{"role":"user","content":"Hello"}]}'`;

const TROUBLESHOOTING: { problem: string; fix: string }[] = [
  {
    problem: "401 proxy_auth_error",
    fix: "The account key is missing, mistyped or revoked. Check it on API Keys. The desktop app replaces a revoked device key automatically the next time it starts.",
  },
  {
    problem: "403 proxy_auth_error",
    fix: "The key is valid but lacks the scope for this endpoint: Responses requests need the responses scope; Messages, Chat Completions and Gemini need the messages scope. Create a key with both.",
  },
  {
    problem: "404 Not Found",
    fix: "ContextShrink only accepts the model endpoints listed above. Sign-in and other provider pages go to the provider directly.",
  },
  {
    problem: "Prompts work, but nothing appears on Usage",
    fix: "The tool sent that request to its provider directly. Providers on your own computer (localhost) are never routed through ContextShrink. In the app, check that you launched the tool from ContextShrink, and that you are signed in to the same account as this dashboard.",
  },
  {
    problem: "Responses include X-ContextShrink-Compression: paused",
    fix: "Your Free plan has used this month's $20 compression allowance. Requests still work, without compression, until the 1st (UTC). Pro has no allowance limit.",
  },
  {
    problem: "Something else in the desktop app",
    fix: "The app's connection log is at %LOCALAPPDATA%\\com.contextshrink.desktop\\forwarder.log. Include its last lines when you contact support@contextshrink.com.",
  },
];

function MethodChip({ method }: { method: string }) {
  return (
    <span
      className={
        method === "GET"
          ? "inline-block rounded-md border border-sage/25 bg-sage-soft px-2 py-0.5 font-mono text-[10px] font-semibold text-sage"
          : "inline-block rounded-md border border-ember/25 bg-ember-soft px-2 py-0.5 font-mono text-[10px] font-semibold text-ember"
      }
    >
      {method}
    </span>
  );
}

export default function Documentation() {
  return (
    <div className="flex flex-col gap-6">
      <Card hairline className="p-7">
        <SectionHeader eyebrow="Docs" title="How ContextShrink works" />
        <p className="max-w-3xl text-sm leading-relaxed text-ink-3">
          ContextShrink sits between your coding tool and your AI provider. It
          compresses what your tool sends (long files, logs, tool output) while keeping
          the answer the same, then forwards the request to your provider with your own
          provider login or API key. Every request and every token saved is recorded
          on this dashboard. Usage analytics record request details such as model,
          tokens and savings, not your prompt or response text.
        </p>
      </Card>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card hairline className="p-6">
          <SectionHeader
            eyebrow="Recommended"
            title="Use the desktop app"
            action={<Badge tone="green">Windows &amp; Linux</Badge>}
          />
          <ol className="flex list-decimal flex-col gap-2 pl-5 text-sm leading-relaxed text-ink-2">
            <li>
              Download it from{" "}
              <Link to="/downloads" className="font-semibold text-ember hover:underline">
                Downloads
              </Link>{" "}
              and install it.
            </li>
            <li>
              Sign in with this account. The app creates a key for this computer
              (“Desktop: &lt;computer name&gt;” on{" "}
              <Link to="/keys" className="font-semibold text-ember hover:underline">
                API Keys
              </Link>
              ) and keeps it in Windows Credential Manager, or the desktop keyring on
              Linux.
            </li>
            <li>
              Choose your project folder and click Launch next to one of the supported
              tools, such as Claude Code, Codex or OpenCode. The full list is on{" "}
              <Link to="/downloads" className="font-semibold text-ember hover:underline">
                Downloads
              </Link>
              .
            </li>
          </ol>
          <p className="mt-4 text-xs leading-relaxed text-ink-3">
            The tool opens in its own window, pointed at ContextShrink for that session
            only. When you close it, the tool's own settings are restored. Signing out
            of the app revokes the computer's key.
          </p>
        </Card>

        <Card hairline className="p-6">
          <SectionHeader eyebrow="Under the hood" title="What Launch does" />
          <ul className="flex flex-col gap-3 text-sm leading-relaxed text-ink-2">
            <li>
              <strong className="text-ink">Claude Code</strong> is started with its API
              address set to the app, which adds your key and forwards model calls to
              ContextShrink. It keeps your Claude subscription or API key.
            </li>
            <li>
              <strong className="text-ink">Codex</strong> is started with its OpenAI
              address set to the app, for both API keys and a ChatGPT sign-in. Its
              settings file is left untouched.
            </li>
            <li>
              <strong className="text-ink">OpenCode</strong> gets a small plugin that
              routes every provider you have configured, including custom ones and
              OpenAI with a ChatGPT sign-in, through ContextShrink. Sign-in and model
              catalogue requests still go to the provider directly.
            </li>
            <li>
              <strong className="text-ink">Aider, Goose, OpenClaude, OpenHands and Copilot
              CLI</strong> are started with their Anthropic and OpenAI addresses set to the
              app. Copilot CLI needs your own Anthropic API key; a Copilot subscription
              sign-in is not supported.
            </li>
            <li>
              <strong className="text-ink">Grok CLI, Kimi CLI and Mistral Vibe</strong>{" "}
              each get their own connection that sends model calls through ContextShrink
              to xAI, Moonshot or Mistral. Kimi asks you to run{" "}
              <code className="font-mono text-ember">/login</code> once after the first
              launch.
            </li>
            <li>
              <strong className="text-ink">Oh My Pi</strong> has its models file pointed at
              the app for the session; the original is restored when it exits.
            </li>
            <li>
              <strong className="text-ink">Claude Code for VS Code</strong> is connected
              from the app's Editors section. The app points Claude Code's user settings at
              itself, restores them when you quit or sign out, and reconnects at your next
              sign-in. While connected, Claude Code you start yourself goes through
              ContextShrink too.
            </li>
            <li>
              <strong className="text-ink">Cline, Continue and ZCode</strong> are set up in
              their own settings: the app shows the base URL to paste, and keeps it working
              while it runs.
            </li>
            <li>
              Your tools are never bundled with the app. Install them yourself; the app
              finds them automatically.
            </li>
          </ul>
        </Card>
      </div>

      <section className="flex flex-col gap-4" aria-label="npx connectors">
        <div>
          <SectionHeader eyebrow="Command-line setup" title="Connect with npx" />
          <p className="max-w-3xl text-sm leading-relaxed text-ink-3">
            Requires Node.js 18+ and Claude Code or Codex already installed. Run the
            commands in PowerShell or your terminal and sign in with your ContextShrink
            account when prompted. Keep your usual provider login. The ContextShrink
            desktop app is optional.
          </p>
        </div>

        <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-2">
          <Card hairline className="p-6">
            <SectionHeader title="contextshrink-claude" />
            <p className="mb-4 text-sm leading-relaxed text-ink-2">
              Connects Claude Code in a terminal or VS Code. On Windows, it also
              connects Claude Desktop&apos;s Code tab; close Claude Desktop before setup.
              If the ContextShrink app already manages that tab, it keeps managing it.
            </p>
            <CodeBlock code={CLAUDE_NPX} />
            <p className="mt-4 text-sm leading-relaxed text-ink-2">
              After connecting, start a new Claude Code or Code-tab session. A savings
              line is added where supported; an existing terminal status line is kept.
            </p>
            <p className="mb-2 mt-4 text-sm font-semibold text-ink">Disconnect</p>
            <CodeBlock code="npx contextshrink-claude@latest disconnect" />
          </Card>

          <Card hairline className="p-6">
            <SectionHeader title="contextshrink-codex" />
            <p className="mb-4 text-sm leading-relaxed text-ink-2">
              Connects local Codex CLI and Codex desktop chats using your existing
              ChatGPT sign-in. Close Codex and its CLI sessions before setup. After
              connecting, reopen Codex and start a new local chat.
            </p>
            <CodeBlock code={CODEX_NPX} />
            <p className="mt-4 text-sm leading-relaxed text-ink-2">
              Run <code className="font-mono text-ember">npx contextshrink-codex@latest savings</code>{" "}
              for a savings summary, or replace <code className="font-mono text-ember">savings</code>{" "}
              with <code className="font-mono text-ember">savings-window</code> for an
              optional live Windows companion window.
            </p>
            <p className="mb-2 mt-4 text-sm font-semibold text-ink">Disconnect</p>
            <CodeBlock code="npx contextshrink-codex@latest disconnect" />
            <details className="mt-4 border-t border-ink/10 pt-4">
              <summary className="cursor-pointer text-sm font-semibold text-ink">
                Connect an existing Codex chat (experimental)
              </summary>
              <p className="my-3 text-sm leading-relaxed text-ink-2">
                Requires Node.js 22.13+. Connect first, then close Codex, ChatGPT Desktop
                and all Codex CLI sessions. List your chats and replace CHAT_ID below
                with one chat&apos;s ID. The second command previews the change.
              </p>
              <CodeBlock code={CODEX_CHATS} />
              <p className="my-3 text-sm leading-relaxed text-ink-2">
                Check the preview, then apply it:
              </p>
              <CodeBlock code="npx contextshrink-codex@latest chats migrate CHAT_ID --apply" />
              <p className="mt-3 text-xs leading-relaxed text-ink-3">
                Reopen that same chat and verify its next request on Usage. Disconnect
                restores migrated chats&apos; previous connection while preserving their
                conversation history. Close Codex before disconnecting.
              </p>
            </details>
          </Card>
        </div>

        <p className="max-w-3xl text-sm leading-relaxed text-ink-3">
          To verify either connector, send a prompt and check{" "}
          <Link to="/usage" className="font-semibold text-ember hover:underline">Usage</Link>.
          Savings displays cover this connector&apos;s API key for today (UTC), across
          its chats. Close your coding tool before disconnecting and reopen it
          afterwards. Disconnect keeps the account key active; use the connector&apos;s{" "}
          <code className="font-mono text-ember">logout</code>{" "}
          command to disconnect and revoke the key it created.
        </p>
      </section>

      <Card hairline className="p-6">
        <SectionHeader
          eyebrow="Manual setup"
          title="Other tools, macOS and servers"
          action={<Badge tone="slate">Needs an account key</Badge>}
        />
        <p className="mb-4 max-w-3xl text-sm leading-relaxed text-ink-3">
          Point any Anthropic-, OpenAI- or Gemini-compatible client at{" "}
          <code className="font-mono text-ember">{PROXY_URL}</code> and send an account
          key from{" "}
          <Link to="/keys" className="font-semibold text-ember hover:underline">
            API Keys
          </Link>{" "}
          in the <code className="font-mono text-ember">X-Horizon-Proxy-Token</code>{" "}
          header. Keep your provider key or login in its usual header; ContextShrink
          removes the account key before forwarding.
        </p>
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <div>
            <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-3">
              Claude Code
            </div>
            <CodeBlock code={CLAUDE_MANUAL} />
          </div>
          <div>
            <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-3">
              OpenAI SDK (Python)
            </div>
            <CodeBlock code={OPENAI_SDK} />
          </div>
        </div>
        <div className="mt-6">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-3">
            Any HTTP client
          </div>
          <CodeBlock code={CURL} />
        </div>
      </Card>

      <Card className="overflow-hidden">
        <div className="px-6 pt-6">
          <SectionHeader eyebrow="Reference" title="Supported endpoints" />
          <p className="-mt-2 mb-4 text-xs text-ink-3">
            Base URL <code className="font-mono text-ember">{PROXY_URL}</code>. The key
            scope column is the permission your account key needs.
          </p>
        </div>
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-y border-ink/10 text-[11px] uppercase tracking-wider text-ink-3">
              <th className="px-6 py-3 font-medium">Method</th>
              <th className="py-3 font-medium">Path</th>
              <th className="py-3 font-medium">Description</th>
              <th className="px-6 py-3 font-medium">Key scope</th>
            </tr>
          </thead>
          <tbody>
            {ENDPOINTS.map((e) => (
              <tr key={e.path} className="border-b border-ink/5 last:border-0">
                <td className="px-6 py-3">
                  <MethodChip method={e.method} />
                </td>
                <td className="py-3 font-mono text-xs text-ink">{e.path}</td>
                <td className="py-3 text-ink-2">{e.desc}</td>
                <td className="px-6 py-3 font-mono text-xs text-ink-3">{e.scope}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card hairline className="p-6">
          <SectionHeader eyebrow="Plans" title="Compression limits" />
          <ul className="flex flex-col gap-3 text-sm leading-relaxed text-ink-2">
            <li>
              <strong className="text-ink">Free:</strong> compression until you have
              saved $20 in a calendar month (UTC). After that, requests keep working
              without compression until the 1st, and responses carry{" "}
              <code className="font-mono text-ember">X-ContextShrink-Compression: paused</code>.
            </li>
            <li>
              <strong className="text-ink">Pro:</strong> unlimited compression and
              Advanced Analytics. No base fee: 5% of a month's savings, only when they
              exceed $20. See{" "}
              <Link to="/subscriptions" className="font-semibold text-ember hover:underline">
                Subscriptions
              </Link>
              .
            </li>
          </ul>
        </Card>

        <Card hairline className="p-6">
          <SectionHeader eyebrow="Keys" title="Account keys" />
          <ul className="flex flex-col gap-3 text-sm leading-relaxed text-ink-2">
            <li>
              Keys start with <code className="font-mono text-ember">cs_live_</code> and
              are shown once. ContextShrink stores only a hash.
            </li>
            <li>
              Revoking a key on API Keys blocks it immediately for new requests.
            </li>
            <li>
              The desktop app and npx connectors create their own keys per computer;
              you only need to create keys yourself for manual setups.
            </li>
          </ul>
        </Card>
      </div>

      <Card className="overflow-hidden">
        <div className="px-6 pt-6">
          <SectionHeader eyebrow="Help" title="Troubleshooting" />
        </div>
        <dl className="divide-y divide-ink/10 border-t border-ink/10">
          {TROUBLESHOOTING.map((t) => (
            <div key={t.problem} className="grid grid-cols-1 gap-1 px-6 py-4 md:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] md:gap-6">
              <dt className="font-mono text-xs text-ink">{t.problem}</dt>
              <dd className="text-sm leading-relaxed text-ink-2">{t.fix}</dd>
            </div>
          ))}
        </dl>
      </Card>
    </div>
  );
}
