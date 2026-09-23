import { useCallback, useEffect, useState } from "react";
import type { DerivationClient } from "../api";
import type { AccountView, DeviceLoginStartView, DeviceLoginStatusView } from "../api/generated";

export interface DeviceLoginState {
  request: DeviceLoginStartView;
  status: DeviceLoginStatusView;
}

export function useProductAccount(client: DerivationClient, enabled: boolean) {
  const [account, setAccount] = useState<AccountView | null>(null);
  const [login, setLogin] = useState<DeviceLoginState | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loginId = login?.request.login_id;
  const loginStatus = login?.status.status;

  const refresh = useCallback(async () => {
    if (!enabled) return null;
    setLoading(true);
    try {
      const value = await client.getAccount();
      setAccount(value);
      setError(null);
      return value;
    } catch (reason) {
      setAccount(null);
      setError(reason instanceof Error ? reason.message : "Unable to read product account");
      return null;
    } finally {
      setLoading(false);
    }
  }, [client, enabled]);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    void refresh();
  }, [enabled, refresh]);

  useEffect(() => {
    if (!enabled || !loginId || loginStatus !== "pending") return;
    let active = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const status = await client.getDeviceLogin(loginId);
        if (!active) return;
        setError(null);
        setLogin((current) => current?.request.login_id === loginId ? { request: current.request, status } : current);
        if (status.status === "signed_in") await refresh();
        else if (status.status === "pending") timer = window.setTimeout(() => void poll(), 1200);
      } catch (reason) {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : "Unable to read device login status");
        timer = window.setTimeout(() => void poll(), 2000);
      }
    };
    timer = window.setTimeout(() => void poll(), 300);
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [client, enabled, loginId, loginStatus, refresh]);

  const importExisting = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const value = await client.importExistingAccount({ confirm_import: true });
      setAccount(value);
      return value;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to import existing Codex login");
      return null;
    } finally {
      setBusy(false);
    }
  }, [client]);

  const startLogin = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const request = await client.startDeviceLogin();
      setLogin({ request, status: { status: "pending", diagnostic: null } });
      return request;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to start device login");
      return null;
    } finally {
      setBusy(false);
    }
  }, [client]);

  const cancelLogin = useCallback(async () => {
    if (!login) return;
    setBusy(true);
    setError(null);
    try {
      await client.cancelDeviceLogin(login.request.login_id);
      setLogin((current) => current ? { request: current.request, status: { status: "canceled", diagnostic: null } } : current);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to cancel device login");
    } finally {
      setBusy(false);
    }
  }, [client, login]);

  return { account, login, loading, busy, error, refresh, importExisting, startLogin, cancelLogin };
}
