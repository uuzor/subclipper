import { useEffect } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../lib/auth";

/**
 * Keep unauthenticated visitors out of the app, and signed-in ones out of the
 * sign-in page.
 *
 * While the session is still being resolved this renders nothing rather than a
 * redirect: bouncing to `/signin` and back on every refresh would be visible and
 * wrong. The 401 broadcast from the API client covers the case where the session
 * dies mid-session, which a route guard alone cannot see.
 */
export default function RequireAuth() {
  const { session, loading, refresh } = useAuth();
  const location = useLocation();

  useEffect(() => {
    const onUnauthorized = () => void refresh();
    window.addEventListener("quickreel:unauthorized", onUnauthorized);
    return () => window.removeEventListener("quickreel:unauthorized", onUnauthorized);
  }, [refresh]);

  if (loading) {
    return (
      <div className="shell route-loading" aria-live="polite">
        Loading…
      </div>
    );
  }
  if (!session) {
    return <Navigate to="/signin" replace state={{ from: location.pathname }} />;
  }
  return <Outlet />;
}
