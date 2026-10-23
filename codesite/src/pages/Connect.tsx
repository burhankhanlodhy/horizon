import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { AuthShell, fieldLabelCls } from "../components/AuthShell";
import { RequireAuth } from "../components/RequireAuth";
import { Banner, Button, Card, Spinner } from "../components/ui";
import { ApiError, isUnauthorized } from "../lib/api";
import { clearSession, signOut, type SessionUser } from "../lib/auth";
import {
  approveCli,
  asDeviceLimit,
  disconnectDevice,
  getCliRequest,
  normaliseUserCode,
  type CliRequest,
  type Device,
} from "../lib/code";
import { DEVICE_LIMIT } from "../lib/constants";
import { fmtRelative, platformLabel, toolLabel } from "../lib/format";

export default function Connect() {
  const [params] = useSearchParams();
  const raw = params.get("code") ?? "";
  const code = normaliseUserCode(raw);
  const connectUrl = code ? `/connect?code=${code}` : "/connect";

  return (
    // Connect is sign-up first: a visitor without an account is sent to /signup.
    <RequireAuth signedOutTo="signup">
      {(user) =>
        code ? (
          <ConnectView key={code} code={code} user={user} />
        ) : (
          <ConnectShell
            user={user}
            connectUrl={connectUrl}
            title="Connect a computer"
            intro="Open the link from your terminal, or enter the code shown there."
          >
            {raw && <Banner tone="error">That code does not look right. It has 8 letters and numbers.</Banner>}
            <CodeEntry />
          </ConnectShell>
        )
      }
    </RequireAuth>
  );
}

/**
 * Auth-style card for every /connect state. It shows which account will be
 * approved, so nobody connects a computer to the wrong account by accident.
 */
function ConnectShell({
  user,
  connectUrl,
  title,
  intro,
  wide,
  children,
}: {
  user: SessionUser;
  connectUrl: string;
  title: string;
  intro?: ReactNode;
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <AuthShell title={title} intro={intro} wide={wide}>
      <div className="flex flex-col gap-6">
        <ApprovingAs user={user} connectUrl={connectUrl} />
        {children}
      </div>
    </AuthShell>
  );
}

function ApprovingAs({ user, connectUrl }: { user: SessionUser; connectUrl: string }) {
  const navigate = useNavigate();
  const [switching, setSwitching] = useState(false);

  const switchAccount = async () => {
    if (switching) return;
    setSwitching(true);
    await signOut();
    navigate(`/signup?next=${encodeURIComponent(connectUrl)}`, { replace: true });
  };

  return (
    <div className="flex flex-col gap-3 rounded-[2px] border border-line bg-paper-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
      <p className="min-w-0 truncate text-sm text-ink-2">
        Approving as <span className="font-medium text-ink">{user.email}</span>
      </p>
      <Button
        variant="ghost"
        onClick={() => void switchAccount()}
        disabled={switching}
        className="!py-2 text-[12px] sm:shrink-0"
      >
        {switching ? "Signing out…" : "Switch account"}
      </Button>
    </div>
  );
}

function CodeEntry() {
  const navigate = useNavigate();
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const code = normaliseUserCode(value);
    if (!code) {
      setError("Enter the 8-character code from your terminal.");
      return;
    }
    navigate(`/connect?code=${encodeURIComponent(code)}`);
  };

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      <div>
        <label htmlFor="connect-code" className={fieldLabelCls}>
          Code from your terminal
        </label>
        <input
          id="connect-code"
          name="code"
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="ABCD-EFGH"
          aria-describedby={error ? "connect-code-error" : undefined}
          className="field font-mono text-lg tracking-[0.12em]"
        />
      </div>
      {error && (
        <Banner tone="error">
          <span id="connect-code-error">{error}</span>
        </Banner>
      )}
      <Button type="submit" className="w-full">
        Continue
      </Button>
    </form>
  );
}

type LoadState =
  | { phase: "loading" }
  | { phase: "ready"; request: CliRequest }
  | { phase: "gone"; message: string }
  | { phase: "error"; message: string };

function ConnectView({ code, user }: { code: string; user: SessionUser }) {
  const navigate = useNavigate();
  const connectUrl = `/connect?code=${code}`;
  const [load, setLoad] = useState<LoadState>({ phase: "loading" });
  /** Devices from a 409 device_limit response. Cleared once a slot frees up. */
  const [limitDevices, setLimitDevices] = useState<Device[] | null>(null);
  const [approving, setApproving] = useState(false);
  const [approved, setApproved] = useState(false);
  const [approveError, setApproveError] = useState<string | null>(null);
  const [busyDevice, setBusyDevice] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // An expired session means signed out, so send the visitor to sign-up and
  // bring them back to this approval afterwards.
  const expireToSignup = useCallback(() => {
    clearSession();
    navigate(`/signup?next=${encodeURIComponent(connectUrl)}`, { replace: true });
  }, [connectUrl, navigate]);

  const refresh = useCallback(async () => {
    try {
      const request = await getCliRequest(code);
      setLoad({ phase: "ready", request });
    } catch (e) {
      if (isUnauthorized(e)) return expireToSignup();
      if (e instanceof ApiError && e.status === 404) {
        setLoad({
          phase: "gone",
          message:
            "This code is unknown or has expired. Run the install command again in your terminal to get a new code.",
        });
        return;
      }
      setLoad({
        phase: "error",
        message: e instanceof Error ? e.message : "Could not load this request.",
      });
    }
  }, [code, expireToSignup]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const approve = async () => {
    if (approving) return;
    setApproving(true);
    setApproveError(null);
    setNotice(null);
    try {
      await approveCli(code);
      setApproved(true);
    } catch (e) {
      if (isUnauthorized(e)) return expireToSignup();
      const limit = asDeviceLimit(e instanceof ApiError ? e.detail : undefined);
      if (limit) {
        setLimitDevices(limit.devices);
        setApproveError(
          `This account already has ${DEVICE_LIMIT} devices connected. Disconnect one below to free a slot, then approve this computer.`,
        );
      } else {
        setApproveError(e instanceof Error ? e.message : "Approval failed. Try again.");
      }
    } finally {
      setApproving(false);
    }
  };

  const disconnect = async (device: Device) => {
    if (busyDevice) return;
    if (!window.confirm(`Disconnect ${device.name}? It stops compression on that computer.`)) {
      return;
    }
    setBusyDevice(device.id);
    setApproveError(null);
    try {
      await disconnectDevice(device.id);
      setLimitDevices(null);
      setNotice(`Disconnected ${device.name}. You can approve this computer now.`);
      await refresh();
    } catch (e) {
      if (isUnauthorized(e)) return expireToSignup();
      setApproveError(e instanceof Error ? e.message : "Could not disconnect that computer.");
    } finally {
      setBusyDevice(null);
    }
  };

  if (approved && load.phase === "ready") {
    return (
      <ConnectShell user={user} connectUrl={connectUrl} title="Approved. Go back to your terminal.">
        <div className="flex flex-col gap-5">
          <p className="text-[15px] leading-relaxed text-ink-2">
            <span className="font-medium text-ink">{load.request.device_name}</span> is connected to{" "}
            {toolLabel(load.request.tool)}. The install finishes in your terminal.
          </p>
          <Banner tone="success">Nothing else needs to happen here.</Banner>
          <Link
            to="/account"
            className="text-sm font-medium text-ember-2 underline-offset-2 hover:underline"
          >
            Open your account to see plan and devices
          </Link>
        </div>
      </ConnectShell>
    );
  }

  if (load.phase === "loading") {
    return (
      <ConnectShell user={user} connectUrl={connectUrl} title="Connect this computer">
        <Spinner label="Loading this request" />
      </ConnectShell>
    );
  }

  if (load.phase === "gone" || load.phase === "error") {
    return (
      <ConnectShell user={user} connectUrl={connectUrl} title="Connect this computer">
        <div className="flex flex-col gap-5">
          <Banner tone="error">{load.message}</Banner>
          <Button variant="ghost" onClick={() => void refresh()} className="w-full">
            Try again
          </Button>
          <Link to="/connect" className="text-center text-sm text-ink-3 underline-offset-2 hover:underline">
            Enter a different code
          </Link>
        </div>
      </ConnectShell>
    );
  }

  const { request } = load;
  const devices = limitDevices ?? (request.device_limit.reached ? request.device_limit.devices : null);

  return (
    <ConnectShell
      user={user}
      connectUrl={connectUrl}
      title="Approve this computer?"
      intro="Approving connects this computer to your ContextShrink account. Only the computer name and platform are shown here."
      wide
    >
      <div className="flex flex-col gap-6">
        <Card className="!p-0 !shadow-none">
          <dl className="divide-y divide-line">
            <Row label="Computer" value={request.device_name} />
            <Row label="Platform" value={platformLabel(request.platform)} />
            <Row label="Tool" value={toolLabel(request.tool)} />
          </dl>
        </Card>

        <p className="text-sm leading-relaxed text-ink-3">
          Free: one week every month, starting the first time you use ContextShrink that month. Pro: $5/month
          for unlimited use, from your account page. Up to {DEVICE_LIMIT} computers per account.
        </p>

        {notice && <Banner tone="success">{notice}</Banner>}

        {devices && (
          <section aria-labelledby="limit-title" className="flex flex-col gap-3">
            <h2 id="limit-title" className="font-display text-lg font-semibold text-ink">
              Your connected computers
            </h2>
            <ul className="flex flex-col gap-3">
              {devices.map((d) => (
                <li
                  key={d.id}
                  className="paper-panel flex flex-col gap-3 rounded-[2px] p-4 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div className="min-w-0">
                    <div className="truncate font-medium text-ink">{d.name}</div>
                    <div className="text-xs text-ink-3">
                      {platformLabel(d.platform)} · last seen {fmtRelative(d.last_seen_at)}
                    </div>
                  </div>
                  <Button
                    variant="danger"
                    onClick={() => void disconnect(d)}
                    disabled={busyDevice !== null}
                    aria-label={`Disconnect ${d.name}`}
                    className="!py-2 text-[12px] sm:shrink-0"
                  >
                    {busyDevice === d.id ? "Disconnecting…" : "Disconnect"}
                  </Button>
                </li>
              ))}
            </ul>
          </section>
        )}

        {approveError && <Banner tone="error">{approveError}</Banner>}

        <Button onClick={() => void approve()} disabled={approving || busyDevice !== null} className="w-full">
          {approving ? "Approving…" : "Approve this computer"}
        </Button>

        <p className="text-center text-xs text-ink-3">
          Not you? Close this page. Nothing is connected until you approve.
        </p>
      </div>
    </ConnectShell>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 px-4 py-3 sm:px-5">
      <dt className="text-sm text-ink-3">{label}</dt>
      <dd className="min-w-0 truncate text-right font-medium text-ink">{value}</dd>
    </div>
  );
}
