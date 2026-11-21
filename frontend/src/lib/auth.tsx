import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { ApiError, api } from "./api";
import type { Session } from "./types";

/**
 * Who the caller is, and which workspace they are acting in.
 *
 * The session lives in an httpOnly cookie the JavaScript cannot read, so the
 * only source of truth is the server. This provider asks once on mount and
 * caches the answer for the tree; anything that changes the session (login,
 * logout, a workspace switch) refreshes it rather than guessing at the new
 * state locally.
 */
interface AuthValue {
  session: Session | null;
  /** True until the first `/me` resolves, so the UI does not flash a login page. */
  loading: boolean;
  signup: (body: {
    email: string;
    password: string;
    display_name?: string;
    workspace_name?: string;
  }) => Promise<void>;
  login: (body: { email: string; password: string }) => Promise<void>;
  logout: () => Promise<void>;
  switchWorkspace: (workspaceId: string) => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      setSession(await api.me());
    } catch (err) {
      // A 401 is the normal "not signed in" answer, not an error worth showing.
      if (err instanceof ApiError && err.status === 401) setSession(null);
      else setSession(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo<AuthValue>(
    () => ({
      session,
      loading,
      refresh,
      signup: async (body) => setSession(await api.signup(body)),
      login: async (body) => setSession(await api.login(body)),
      logout: async () => {
        await api.logout();
        setSession(null);
      },
      switchWorkspace: async (workspaceId) =>
        setSession(await api.switchWorkspace(workspaceId)),
    }),
    [session, loading, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside an AuthProvider");
  return value;
}
