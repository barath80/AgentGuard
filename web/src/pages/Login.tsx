import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Building2, Fingerprint, KeyRound, Scale, ShieldCheck } from "lucide-react";
import { useState, type FormEvent } from "react";
import { ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { getAuthConfig } from "../lib/oidc";
import { Button, Skeleton } from "../components/ui";

const PRINCIPLES = [
  {
    icon: Fingerprint,
    title: "Identity from verified context",
    body: "Never from what the model says it is.",
  },
  {
    icon: Scale,
    title: "Versioned policy, enforced server-side",
    body: "Every decision names the policy version that made it.",
  },
  {
    icon: KeyRound,
    title: "Approvals bound and single-use",
    body: "Tied to the exact action hash, resource and policy version.",
  },
];

// Synthetic dev principals from .env.example — shown only in `vite dev`, never in a build.
const DEV_TOKENS = [
  ["Operator · all tenants", "dev-only-operator-token"],
  ["Support lead · tenant-a", "dev-only-console-dana"],
  ["Viewer · all tenants", "dev-only-console-sam"],
] as const;

export function Login() {
  const { signIn, signInWithSso, error: authError } = useAuth();
  const config = useQuery({ queryKey: ["auth-config"], queryFn: getAuthConfig, staleTime: Infinity });
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [redirecting, setRedirecting] = useState(false);
  const sso = config.data?.oidc;
  const tokens = config.data?.static_tokens ?? false;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(token.trim());
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? "That token wasn't recognised."
          : "Couldn't reach the gateway. Is it running?",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-screen lg:grid-cols-[1.1fr_1fr]">
      <section className="relative hidden overflow-hidden border-r border-line bg-surface lg:flex lg:flex-col lg:justify-between lg:p-12">
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-60"
          style={{
            backgroundImage:
              "radial-gradient(circle at 20% 15%, var(--accent-soft), transparent 45%), linear-gradient(var(--line) 1px, transparent 1px), linear-gradient(90deg, var(--line) 1px, transparent 1px)",
            backgroundSize: "auto, 32px 32px, 32px 32px",
            maskImage: "linear-gradient(to bottom, black, transparent 85%)",
          }}
        />
        <div className="relative flex items-center gap-2.5">
          <div className="grid size-9 place-items-center rounded-lg bg-accent text-on-accent">
            <ShieldCheck aria-hidden className="size-5" />
          </div>
          <span className="text-[15px] font-semibold">AgentGuard</span>
        </div>
        <div className="relative max-w-lg">
          <h1 className="text-4xl font-semibold leading-[1.1] tracking-tight text-ink">
            Authorization for AI agents, enforced where tools actually run.
          </h1>
          <p className="mt-4 text-[15px] text-ink-2">
            See which authenticated agent asked for what, which policy decided, who approved it and what happened —
            and test policy changes against real traffic before you ship them.
          </p>
          <ul className="mt-10 space-y-5">
            {PRINCIPLES.map(({ icon: Icon, title, body }) => (
              <li key={title} className="flex gap-3.5">
                <div className="grid size-9 shrink-0 place-items-center rounded-lg bg-page text-accent ring-1 ring-line">
                  <Icon aria-hidden className="size-[18px]" />
                </div>
                <div>
                  <p className="font-medium text-ink">{title}</p>
                  <p className="text-[13px] text-ink-3">{body}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>
        <p className="relative text-xs text-ink-3">Synthetic data and mock credentials only in this environment.</p>
      </section>

      <section className="flex items-center justify-center px-4 py-12 sm:px-6">
        <div className="w-full max-w-sm">
          <div className="mb-8 lg:hidden">
            <div className="grid size-10 place-items-center rounded-lg bg-accent text-on-accent">
              <ShieldCheck aria-hidden className="size-5" />
            </div>
          </div>
          <h2 className="text-2xl font-semibold tracking-tight">Sign in to the console</h2>
          <p className="mt-1.5 text-ink-2">
            {sso ? "Use your organisation account." : "Use the console access token issued to you."}
          </p>

          {authError && (
            <p role="alert" className="mt-6 rounded-lg bg-critical-soft px-3 py-2 text-[13px] text-critical-ink">
              {authError}
            </p>
          )}

          {!config.data && <Skeleton className="mt-8 h-10" />}

          {sso && (
            <Button
              variant="primary"
              icon={Building2}
              loading={redirecting}
              className="mt-8 h-10 w-full"
              onClick={() => {
                setRedirecting(true);
                signInWithSso().catch(() => {
                  setRedirecting(false);
                  setError("Couldn't reach the identity provider.");
                });
              }}
            >
              Continue with single sign-on
            </Button>
          )}

          {sso && tokens && (
            <div className="my-6 flex items-center gap-3 text-xs text-ink-3">
              <span className="h-px flex-1 bg-line" />
              or use an access token
              <span className="h-px flex-1 bg-line" />
            </div>
          )}

          {tokens && (
          <form onSubmit={submit} className={sso ? "space-y-4" : "mt-8 space-y-4"}>
            <div>
              <label htmlFor="token" className="mb-1.5 block text-[13px] font-medium text-ink">
                Access token
              </label>
              <input
                id="token"
                type="password"
                autoComplete="current-password"
                autoFocus
                required
                value={token}
                onChange={(e) => setToken(e.target.value)}
                className="h-10 w-full rounded-lg border border-line-strong bg-surface px-3 font-mono text-[13px] text-ink outline-none transition placeholder:text-ink-3 focus:border-accent focus:ring-3 focus:ring-accent/20"
                placeholder="••••••••••••"
              />
            </div>
            {error && (
              <p role="alert" className="text-[13px] text-critical-ink">
                {error}
              </p>
            )}
            <Button type="submit" variant={sso ? "secondary" : "primary"} loading={busy} className="h-10 w-full">
              Continue <ArrowRight aria-hidden className="size-4" />
            </Button>
          </form>
          )}
          {error && sso && !tokens && (
            <p role="alert" className="mt-4 text-[13px] text-critical-ink">
              {error}
            </p>
          )}

          {import.meta.env.DEV && tokens && (
            <div className="mt-8 rounded-xl border border-dashed border-line-strong p-4">
              <p className="mb-2 text-xs font-medium text-ink-2">Local development principals</p>
              <div className="space-y-1">
                {DEV_TOKENS.map(([label, value]) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setToken(value)}
                    className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-xs text-ink-2 hover:bg-surface-hover hover:text-ink"
                  >
                    {label}
                    <span className="font-mono text-ink-3">{value.slice(9, 24)}</span>
                  </button>
                ))}
              </div>
            </div>
          )}
          <p className="mt-8 text-xs text-ink-3">
            Sessions end when you close this tab.
          </p>
        </div>
      </section>
    </div>
  );
}
