import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, apiFetch } from "./api";
import { refreshUser, type SessionUser } from "./auth";

export type Plan = "free" | "pro" | "team";
export const PLANS: {
  id: Plan;
  name: string;
  description: string;
  features: string[];
  accent: string;
}[] = [
  {
    id: "free",
    name: "Free",
    description:
      "Compression until you save $20 in a month, then requests pass through.",
    features: [
      "Compression up to $20 saved / month",
      "Account API keys",
      "Usage & savings",
    ],
    accent: "from-ember to-ember-2",
  },
  {
    id: "pro",
    name: "Pro",
    description:
      "Unlimited compression. 5% of savings, only when you save over $20.",
    features: [
      "Unlimited compression",
      "Advanced Analytics",
      "Analytics CSV export",
    ],
    accent: "from-sage to-sage/70",
  },
  {
    id: "team",
    name: "Team",
    description:
      "Shared Team savings estimates, purchased seats, and member contribution tracking.",
    features: [
      "All Pro features",
      "Team subscription",
      "Private account analytics",
    ],
    accent: "from-ink to-ink/70",
  },
];
export const initials = (name: string) =>
  name
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((s) => s[0])
    .join("")
    .toUpperCase();

const AccountContext = createContext<{
  user: SessionUser;
  plan: Plan;
  advanced: boolean;
  changePlan: (plan: Plan) => Promise<void>;
  reloadAccount: () => Promise<SessionUser | null>;
} | null>(null);

export function AccountProvider({
  initialUser,
  children,
}: {
  initialUser: SessionUser;
  children: ReactNode;
}) {
  const [user, setUser] = useState(initialUser);
  const revision = useRef(0);
  const changing = useRef(false);
  const navigate = useNavigate();
  useEffect(() => {
    let active = true;
    let pending = false;
    async function refresh() {
      if (pending || changing.current || document.hidden) return;
      const startedAtRevision = revision.current;
      pending = true;
      try {
        const fresh = await refreshUser();
        if (!active || startedAtRevision !== revision.current) return;
        if (fresh) setUser(fresh);
        else navigate("/login", { replace: true });
      } catch {
        /* Keep the last verified account during a temporary outage. */
      } finally {
        pending = false;
      }
    }
    const timer = window.setInterval(refresh, 30000);
    const onStorage = (e: StorageEvent) => {
      if (e.key === "cs_session_token" || e.key === null)
        window.location.reload();
    };
    window.addEventListener("focus", refresh);
    window.addEventListener("storage", onStorage);
    return () => {
      active = false;
      clearInterval(timer);
      window.removeEventListener("focus", refresh);
      window.removeEventListener("storage", onStorage);
    };
  }, [navigate]);
  const plan = user.plan ?? "free";
  async function changePlan(next: Plan) {
    if (changing.current) return;
    changing.current = true;
    revision.current++;
    try {
      const saved = await apiFetch<{ plan: Plan; status: string }>(
        "/subscription",
        { method: "PUT", body: { plan: next } },
      );
      setUser((current) => ({
        ...current,
        plan: saved.plan,
        subscription_status: saved.status,
      }));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401)
        navigate("/login", { replace: true });
      throw error;
    } finally {
      revision.current++;
      changing.current = false;
    }
  }
  /** Re-read the plan now, e.g. while a Stripe webhook activates it. */
  async function reloadAccount() {
    const fresh = await refreshUser();
    if (fresh) {
      revision.current++;
      setUser(fresh);
    }
    return fresh;
  }
  return (
    <AccountContext.Provider
      value={{
        user,
        plan,
        advanced:
          (plan === "pro" || plan === "team") &&
          user.subscription_status === "active",
        changePlan,
        reloadAccount,
      }}
    >
      {children}
    </AccountContext.Provider>
  );
}

export function useAccount() {
  const account = useContext(AccountContext);
  if (!account) throw new Error("Account context is missing");
  return account;
}
