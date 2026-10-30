import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, setUnauthorizedHandler, tokenStore, type Me } from "./api";
import { CALLBACK_PATH, getUserManager } from "./oidc";

interface AuthState {
  me: Me | null;
  loading: boolean;
  error: string | null;
  signIn: (token: string) => Promise<void>;
  signInWithSso: () => Promise<void>;
  signOut: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(
    () => tokenStore.get() !== null || window.location.pathname === CALLBACK_PATH,
  );

  const clear = useCallback(() => {
    tokenStore.set(null);
    setMe(null);
    queryClient.clear();
  }, [queryClient]);

  const signOut = useCallback(() => {
    clear();
    getUserManager().then(async (manager) => {
      if (manager && (await manager.getUser())) {
        await manager.removeUser();
        await manager.signoutRedirect().catch(() => undefined);
      }
    });
  }, [clear]);

  const signIn = useCallback(async (token: string) => {
    // Verify before storing, so a bad token never lingers in storage.
    const principal = await api.me(token);
    tokenStore.set(token);
    setMe(principal);
  }, []);

  const signInWithSso = useCallback(async () => {
    const manager = await getUserManager();
    if (manager) await manager.signinRedirect();
  }, []);

  useEffect(() => {
    // An expired or revoked session anywhere in the app lands back on sign-in.
    setUnauthorizedHandler(clear);
    let unsubscribe = () => {};

    (async () => {
      const manager = await getUserManager();
      if (manager) {
        // Silent renewals hand us a fresh access token; keep the API layer using it.
        const onLoaded = (user: { access_token: string }) => tokenStore.set(user.access_token);
        manager.events.addUserLoaded(onLoaded);
        unsubscribe = () => manager.events.removeUserLoaded(onLoaded);
      }
      try {
        if (manager && window.location.pathname === CALLBACK_PATH) {
          const user = await manager.signinRedirectCallback();
          window.history.replaceState(null, "", "/");
          await signIn(user.access_token);
        } else if (tokenStore.get()) {
          setMe(await api.me());
        }
      } catch (err) {
        clear();
        if (window.location.pathname === CALLBACK_PATH) {
          window.history.replaceState(null, "", "/");
          setError(err instanceof Error && err.message ? `Sign-in failed: ${err.message}` : "Sign-in failed.");
        }
      } finally {
        setLoading(false);
      }
    })();
    return () => unsubscribe();
  }, [clear, signIn]);

  const value = useMemo(
    () => ({ me, loading, error, signIn, signInWithSso, signOut }),
    [me, loading, error, signIn, signInWithSso, signOut],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}

export function useMe(): Me {
  const { me } = useAuth();
  if (!me) throw new Error("useMe needs a signed-in principal");
  return me;
}
