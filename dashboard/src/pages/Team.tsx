import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Badge, Card, GradientButton, SectionHeader } from "../components/ui";
import { apiFetch, ApiError } from "../lib/api";
import { getSelectedTeamId, setSelectedTeamId } from "../lib/workspace";
import { useAccount } from "../lib/account";
import TeamSso from "./TeamSso";

type TeamSummary = { id: string; name: string; role: "owner" | "admin" | "member"; status: string; seat_count: number; pending_seat_count?: number | null; member_count: number; reserved_count: number };
type TeamMember = { membership_id: string; user_id: string; email: string; name?: string | null; role: "owner" | "admin" | "member"; joined_at?: string | null };
type Invitation = { id: string; email: string; role: string; status: string; expires_at?: string | null; created_at?: string | null };
type TeamDetail = { team: TeamSummary; members: TeamMember[]; invitations: Invitation[] };

export default function Team() {
  const [teams, setTeams] = useState<TeamSummary[]>([]);
  const [detail, setDetail] = useState<TeamDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [featureEnabled, setFeatureEnabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [name, setName] = useState("");
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"admin" | "member">("member");
  const [seats, setSeats] = useState(3);
  const [search, setSearch] = useSearchParams();
  const navigate = useNavigate();
  const { user } = useAccount();
  const canManage = detail?.team.role === "owner" || detail?.team.role === "admin";
  const isOwner = detail?.team.role === "owner";
  const canRemove = (member: TeamMember) => isOwner ? member.role !== "owner" : canManage && member.role === "member";
  const effectiveSeatCount = detail ? Math.min(detail.team.seat_count, detail.team.pending_seat_count ?? detail.team.seat_count) : 0;
  const available = useMemo(() => detail ? Math.max(0, effectiveSeatCount - detail.team.member_count - detail.team.reserved_count) : 0, [detail, effectiveSeatCount]);

  async function load(preferredId?: string) {
    setLoading(true);
    setError("");
    try {
      const result = await apiFetch<{ teams: TeamSummary[] }>("/teams");
      setTeams(result.teams ?? []);
      setFeatureEnabled(true);
      const requestedId = preferredId || search.get("id") || getSelectedTeamId();
      const nextId = requestedId && result.teams?.some((team) => team.id === requestedId) ? requestedId : null;
      if (nextId) {
        const response = await apiFetch<TeamDetail>(`/teams/${encodeURIComponent(nextId)}`);
        setDetail(response);
        if ((preferredId || search.get("id")) && response.team.id !== getSelectedTeamId()) setSelectedTeamId(response.team.id);
      } else {
        setDetail(null);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to load teams");
      setFeatureEnabled(false);
      setDetail(null);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { void load(); }, []);

  async function run(action: () => Promise<unknown>, success: string) {
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try { await action(); setNotice(success); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "Team action failed"); }
    finally { setBusy(false); }
  }
  async function createTeam(event: FormEvent) {
    event.preventDefault();
    if (busy || !name.trim()) return;
    setBusy(true); setError("");
    try {
      const result = await apiFetch<{ team: TeamSummary }>("/teams", { method: "POST", body: { name: name.trim(), seats: Math.max(3, seats) } });
      setSelectedTeamId(result.team.id); setSearch({ id: result.team.id }); setName(""); setNotice("Team created. Complete Team checkout to activate paid seats.");
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Unable to create team"); }
    finally { setBusy(false); }
  }
  async function invite(event: FormEvent) {
    event.preventDefault();
    if (!detail || busy || !inviteEmail.trim()) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const response = await apiFetch<{ invitation: Invitation; email_sent?: boolean }>(`/teams/${encodeURIComponent(detail.team.id)}/invitations`, { method: "POST", body: { email: inviteEmail.trim(), role: inviteRole } });
      setInviteEmail(""); setNotice(response.email_sent === false ? "Invitation saved and one purchased seat is reserved, but the email could not be delivered. Check the recipient address and email settings, then use Resend below." : `Invitation sent to ${response.invitation.email}.`); await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Unable to send invitation"); }
    finally { setBusy(false); }
  }
  function selectTeam(id: string) {
    setSelectedTeamId(id); setSearch({ id }); void load(id);
  }
  async function leaveTeam(member: TeamMember) {
    if (!detail || busy || !window.confirm(`Leave ${detail.team.name}? Your Team API keys will be revoked and you will lose access to this workspace.`)) return;
    setBusy(true); setError("");
    try {
      await apiFetch(`/teams/${encodeURIComponent(detail.team.id)}/members/${encodeURIComponent(member.membership_id)}`, { method: "DELETE" });
      setSelectedTeamId(null);
      navigate("/", { replace: true });
    } catch (e) { setError(e instanceof Error ? e.message : "Unable to leave Team"); }
    finally { setBusy(false); }
  }
  if (loading) return <Card className="p-6 text-sm text-ink-3">Loading team details…</Card>;
  if (!featureEnabled) return <Card className="p-6"><SectionHeader eyebrow="Teams" title="Team workspaces unavailable" /><p className="mt-3 text-sm text-ink-3">Team workspaces are not enabled for this account yet. Your Personal workspace remains available.</p></Card>;
  if (teams.length > 0 && !detail) return <div className="flex flex-col gap-6"><Card hairline className="p-6"><SectionHeader eyebrow="Teams" title="Choose a Team workspace" /><p className="mt-3 text-sm text-ink-3">Select a Team to switch the dashboard into that workspace.</p><div className="mt-4 flex flex-wrap gap-3">{teams.map((team) => <button key={team.id} onClick={() => selectTeam(team.id)} className="rounded-lg border border-ink/15 px-4 py-2 text-sm text-ink hover:border-ember">{team.name}</button>)}</div></Card></div>;
  if (!teams.length) return (
    <div className="flex flex-col gap-6">
      <Card hairline className="p-6">
        <SectionHeader eyebrow="Teams" title="Create your Team workspace" />
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-ink-3">Team billing starts with at least three purchased seats, including the owner. Each seat is $5 per month; invitations reserve purchased capacity without changing the seat charge.</p>
        <form onSubmit={createTeam} className="mt-5 grid gap-3 sm:grid-cols-[1fr_9rem_auto]">
          <input required maxLength={100} value={name} onChange={(event) => setName(event.target.value)} placeholder="Team name" className="rounded-lg border border-ink/15 bg-card px-3 py-2.5 text-sm text-ink" />
          <label className="flex items-center gap-2 rounded-lg border border-ink/15 px-3 py-2 text-xs text-ink-3">Seats <input type="number" min={3} value={seats} onChange={(event) => setSeats(Math.max(3, Number(event.target.value) || 3))} className="w-14 bg-transparent text-sm text-ink" /></label>
          <GradientButton disabled={busy}>{busy ? "Creating…" : "Create Team"}</GradientButton>
        </form>
        {notice && <p role="status" className="mt-3 text-sm text-sage">{notice}</p>}
        {error && <p role="alert" className="mt-3 text-sm text-ember">{error}</p>}
      </Card>
      <Card className="p-6"><SectionHeader eyebrow="Workspace" title="Keep personal work separate" /><p className="mt-3 text-sm text-ink-3">Your existing personal plan, keys and usage remain personal. Team members use their own Team keys, and Team savings are billed to the Team owner.</p></Card>
    </div>
  );
  if (!detail) return <Card className="p-6 text-sm text-ink-3">Select a Team to continue.</Card>;

  return (
    <div className="flex flex-col gap-6">
      {teams.length > 1 && <label className="flex items-center gap-2 text-sm text-ink-3">Team <select value={detail.team.id} onChange={(event) => selectTeam(event.target.value)} className="rounded-lg border border-ink/15 bg-card px-3 py-2 text-ink">{teams.map((team) => <option key={team.id} value={team.id}>{team.name}</option>)}</select></label>}
      <Card hairline className="p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div><Badge tone={detail.team.status === "active" ? "green" : "slate"}>{detail.team.status}</Badge><h2 className="mt-3 font-display text-2xl font-bold text-ink">{detail.team.name}</h2><p className="mt-1 text-sm text-ink-3">Your role: {detail.team.role}</p><p className="mt-2 text-xs text-ink-3">Team ID: <code className="select-all text-ink">{detail.team.id}</code></p></div>
{canManage && <div className="grid grid-cols-2 gap-3 text-center sm:grid-cols-4">{[["Purchased", detail.team.seat_count], ["Occupied", detail.team.member_count], ["Reserved", detail.team.reserved_count], ["Available", available]].map(([label, value]) => <div key={label} className="rounded-lg border border-ink/10 bg-ink/5 px-4 py-3"><div className="font-display text-xl font-bold text-ink">{value}</div><div className="mt-1 text-[11px] text-ink-3">{label}</div></div>)}</div>}
        </div>
        {notice && <p role="status" className="mt-4 text-sm text-sage">{notice}</p>}{error && <p role="alert" className="mt-4 text-sm text-ember">{error}</p>}
      </Card>
      {canManage && <Card hairline className="p-6"><SectionHeader eyebrow="Invitations" title="Invite someone" action={<Badge tone="slate">{available} seat{available === 1 ? "" : "s"} available</Badge>} />{detail.team.status === "active" ? <form onSubmit={invite} className="mt-4 grid gap-3 sm:grid-cols-[1fr_10rem_auto]"><input type="email" required value={inviteEmail} onChange={(event) => setInviteEmail(event.target.value)} placeholder="name@example.com" className="rounded-lg border border-ink/15 bg-card px-3 py-2.5 text-sm text-ink" /><select value={inviteRole} onChange={(event) => setInviteRole(event.target.value as "admin" | "member")} className="rounded-lg border border-ink/15 bg-card px-3 py-2 text-sm text-ink"><option value="member">Member</option>{isOwner && <option value="admin">Admin</option>}</select><GradientButton disabled={busy || available < 1}>{busy ? "Sending…" : "Send invitation"}</GradientButton></form> : <p className="mt-4 text-sm text-ink-3">Invitations are available after Team checkout activates the workspace.</p>}<p className="mt-2 text-xs text-ink-3">Invitations reserve an available purchased seat. Adding seats requires the owner’s explicit billing action.</p></Card>}
      <TeamSso teamId={detail.team.id} isOwner={isOwner} />
      <Card className="overflow-hidden"><div className="p-6"><SectionHeader eyebrow="Roster" title="Members" action={<Badge tone="slate">{detail.members.length}</Badge>} /></div><div className="divide-y divide-ink/5">{detail.members.map((member) => <div key={member.membership_id} className="flex flex-wrap items-center justify-between gap-3 px-6 py-4"><div><div className="text-sm font-medium text-ink">{member.name || member.email}</div><div className="text-xs text-ink-3">{member.email}</div></div><div className="flex items-center gap-3"><select aria-label={`Role for ${member.email}`} value={member.role} disabled={!isOwner || member.role === "owner" || busy} onChange={(event) => void run(() => apiFetch(`/teams/${detail.team.id}/members/${member.membership_id}`, { method: "PATCH", body: { role: event.target.value } }), "Member role updated.")} className="rounded-lg border border-ink/15 bg-card px-2 py-1.5 text-xs text-ink"><option value="member">Member</option><option value="admin">Admin</option></select>{isOwner && member.role !== "owner" && <button disabled={busy} onClick={() => { if (window.confirm(`Transfer Team ownership to ${member.email}?`)) void run(() => apiFetch(`/teams/${detail.team.id}/transfer-ownership`, { method: "POST", body: { membership_id: member.membership_id } }), "Ownership transferred."); }} className="text-xs text-ember hover:underline">Transfer owner</button>}{member.user_id === user.id && member.role !== "owner" && <button disabled={busy} onClick={() => void leaveTeam(member)} className="text-xs text-ember hover:underline">Leave Team</button>}{canRemove(member) && <button disabled={busy} onClick={() => { if (window.confirm(`Remove ${member.email} from this Team?`)) void run(() => apiFetch(`/teams/${detail.team.id}/members/${member.membership_id}`, { method: "DELETE" }), "Member removed. Their Team keys have been revoked."); }} className="text-xs text-ember hover:underline">Remove</button>}</div></div>)}{detail.members.length === 0 && <p className="px-6 pb-6 text-sm text-ink-3">Member details are available to Team admins and owners.</p>}</div></Card>
      {canManage && <Card className="overflow-hidden"><div className="p-6"><SectionHeader eyebrow="Pending" title="Invitations" action={<Badge tone="slate">{detail.invitations.length}</Badge>} /></div><div className="divide-y divide-ink/5">{detail.invitations.map((invitation) => <div key={invitation.id} className="flex flex-wrap items-center justify-between gap-3 px-6 py-4"><div><div className="text-sm font-medium text-ink">{invitation.email}</div><div className="text-xs text-ink-3">{invitation.role} · {invitation.status}{invitation.expires_at ? ` · expires ${new Date(invitation.expires_at).toLocaleDateString()}` : ""}</div></div><div className="flex gap-3"><button disabled={busy} onClick={() => void run(() => apiFetch(`/teams/${detail.team.id}/invitations/${invitation.id}/resend`, { method: "POST" }), `Invitation resent to ${invitation.email}.`)} className="text-xs text-ember hover:underline">Resend</button><button disabled={busy} onClick={() => void run(() => apiFetch(`/teams/${detail.team.id}/invitations/${invitation.id}`, { method: "DELETE" }), "Invitation revoked.")} className="text-xs text-ember hover:underline">Revoke</button></div></div>)}{detail.invitations.length === 0 && <p className="px-6 pb-6 text-sm text-ink-3">No pending invitations.</p>}</div></Card>}
      {isOwner && <Card className="p-6"><SectionHeader eyebrow="Billing" title="Seats and payment" action={<button onClick={() => navigate("/subscriptions")} className="text-sm font-medium text-ember hover:underline">Manage Team billing →</button>} /><p className="mt-3 text-sm text-ink-3">Team billing is managed by the owner. Purchased seats cost $5 each per month, with a three-seat minimum. Reducing seats takes effect at renewal and must preserve current members and invitations.</p></Card>}
    </div>
  );
}

export function AcceptTeamInvitation() {
  const [params] = useSearchParams();
  const [state, setState] = useState<"ready" | "working" | "done" | "error">("ready");
  const [message, setMessage] = useState("");
  const navigate = useNavigate();
  const token = params.get("token") || "";
  async function accept() {
    if (!token || state === "working") return;
    setState("working"); setMessage("");
    try {
      const result = await apiFetch<{ team: TeamSummary }>("/team-invitations/accept", { method: "POST", body: { token } });
      setSelectedTeamId(result.team.id); setState("done"); setMessage(`You joined ${result.team.name}.`);
    } catch (e) {
      setState("error");
      setMessage(e instanceof ApiError && e.status === 403 ? "Sign in with the verified email address the invitation was sent to." : e instanceof Error ? e.message : "Invitation could not be accepted.");
    }
  }
  return <Card hairline className="mx-auto max-w-xl p-6"><SectionHeader eyebrow="Team invitation" title="Join your Team" />{message && <p role={state === "error" ? "alert" : "status"} className={`mt-4 text-sm ${state === "error" ? "text-ember" : "text-sage"}`}>{message}</p>}{!token ? <p className="mt-4 text-sm text-ink-3">This invitation link is missing its token. Ask the Team owner to resend it.</p> : state !== "done" ? <><p className="mt-4 text-sm text-ink-3">Accepting requires a signed-in account with a verified email matching the invitation.</p><GradientButton disabled={state === "working"} className="mt-5" onClick={() => void accept()}>{state === "working" ? "Joining…" : "Accept invitation"}</GradientButton></> : <GradientButton className="mt-5" onClick={() => navigate("/team")}>Open Team</GradientButton>}</Card>;
}
