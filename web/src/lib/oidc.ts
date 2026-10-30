import { UserManager, WebStorageStateStore } from "oidc-client-ts";

export interface AuthConfig {
  oidc: { authority: string; client_id: string } | null;
  static_tokens: boolean;
}

export const CALLBACK_PATH = "/auth/callback";

let configPromise: Promise<AuthConfig> | null = null;
let manager: UserManager | null = null;

export function getAuthConfig(): Promise<AuthConfig> {
  configPromise ??= fetch("/v1/console/auth-config")
    .then((r) => (r.ok ? (r.json() as Promise<AuthConfig>) : { oidc: null, static_tokens: true }))
    .catch(() => ({ oidc: null, static_tokens: true }));
  return configPromise;
}

/** Authorization Code + PKCE with a public client: no client secret ever reaches the browser. */
export async function getUserManager(): Promise<UserManager | null> {
  const config = await getAuthConfig();
  if (!config.oidc) return null;
  manager ??= new UserManager({
    authority: config.oidc.authority,
    client_id: config.oidc.client_id,
    redirect_uri: `${window.location.origin}${CALLBACK_PATH}`,
    post_logout_redirect_uri: `${window.location.origin}/`,
    response_type: "code",
    scope: "openid profile email",
    // Session-scoped, like the static token: closing the tab ends the console session.
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
    automaticSilentRenew: true,
  });
  return manager;
}
