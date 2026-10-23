import { useEffect } from "react";
import { Route, Routes, useLocation } from "react-router-dom";
import { ButtonLink } from "./components/ui";
import { SiteFooter, SiteHeader } from "./components/SiteChrome";
import Account from "./pages/Account";
import Connect from "./pages/Connect";
import Landing from "./pages/Landing";
import Login from "./pages/Login";
import Privacy from "./pages/Privacy";
import Signup from "./pages/Signup";

export default function App() {
  useScrollToTopOnNavigate();
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/signup" element={<Signup />} />
      <Route path="/login" element={<Login />} />
      <Route path="/connect" element={<Connect />} />
      <Route path="/account" element={<Account />} />
      <Route path="/privacy" element={<Privacy />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}

/** Start each page at the top, except when a #section link was followed. */
function useScrollToTopOnNavigate() {
  const { pathname, hash } = useLocation();
  useEffect(() => {
    if (!hash) window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [pathname, hash]);
}

function NotFound() {
  return (
    <div className="flex min-h-screen flex-col">
      <SiteHeader />
      <main className="flex flex-1 flex-col items-center justify-center gap-6 px-4 py-20 text-center">
        <p className="font-mono text-sm text-ink-3">404</p>
        <h1 className="font-display text-3xl font-bold text-ink">This page does not exist.</h1>
        <ButtonLink to="/">Back to ContextShrink Code</ButtonLink>
      </main>
      <SiteFooter />
    </div>
  );
}
