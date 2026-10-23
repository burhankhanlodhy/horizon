import { useSyncExternalStore } from "react";

/** The selected workspace is dashboard context only; authorization stays on the API. */
const WORKSPACE_KEY = "cs_selected_team";
export const WORKSPACE_CHANGE_EVENT = "cs-workspace-change";

function subscribeWorkspace(listener: () => void): () => void {
  window.addEventListener(WORKSPACE_CHANGE_EVENT, listener);
  window.addEventListener("storage", listener);
  return () => {
    window.removeEventListener(WORKSPACE_CHANGE_EVENT, listener);
    window.removeEventListener("storage", listener);
  };
}

export function getSelectedTeamId(): string | null {
  try {
    return localStorage.getItem(WORKSPACE_KEY) || null;
  } catch {
    return null;
  }
}

export function useSelectedTeamId(): string | null {
  return useSyncExternalStore(subscribeWorkspace, getSelectedTeamId, () => null);
}

export function setSelectedTeamId(teamId: string | null): void {
  const nextTeamId = teamId || null;
  if (getSelectedTeamId() === nextTeamId) return;
  try {
    if (nextTeamId) localStorage.setItem(WORKSPACE_KEY, nextTeamId);
    else localStorage.removeItem(WORKSPACE_KEY);
    window.dispatchEvent(new CustomEvent(WORKSPACE_CHANGE_EVENT, { detail: nextTeamId }));
  } catch {
    /* storage unavailable */
  }
}
