import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { clearSession } from "./auth";

/**
 * Returns a function that drops an expired session and sends the visitor to
 * /login, coming back to `nextPath` after sign-in.
 */
export function useNavigateToLogin(nextPath: string): () => void {
  const navigate = useNavigate();
  return useCallback(() => {
    clearSession();
    navigate(`/login?next=${encodeURIComponent(nextPath)}`, { replace: true });
  }, [navigate, nextPath]);
}
