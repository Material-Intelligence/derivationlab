import { useCallback, useEffect, useRef, useState } from "react";
import type { DerivationClient } from "../api";
import type { AccountRateLimitsView } from "../api/generated";

const UI_REFRESH_MS = 60_000;

export function useAccountRateLimits(client: DerivationClient, enabled: boolean) {
  const [rateLimits, setRateLimits] = useState<AccountRateLimitsView | null>(null);
  const inFlight = useRef<Promise<void> | null>(null);

  const refresh = useCallback(() => {
    if (!enabled || inFlight.current) return inFlight.current;
    const request = client.getAccountRateLimits()
      .then((value) => setRateLimits(value))
      .catch(() => undefined)
      .finally(() => {
        if (inFlight.current === request) inFlight.current = null;
      });
    inFlight.current = request;
    return request;
  }, [client, enabled]);

  useEffect(() => {
    if (!enabled) {
      setRateLimits(null);
      return;
    }
    let active = true;
    let cycleRunning = false;
    let timer: number | undefined;
    const clearTimer = () => {
      if (timer !== undefined) window.clearTimeout(timer);
      timer = undefined;
    };
    const refreshVisible = async () => {
      if (!active || cycleRunning || document.visibilityState !== "visible") return;
      cycleRunning = true;
      clearTimer();
      await refresh();
      cycleRunning = false;
      if (active && document.visibilityState === "visible") {
        timer = window.setTimeout(() => void refreshVisible(), UI_REFRESH_MS);
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") void refreshVisible();
      else clearTimer();
    };
    const onFocus = () => void refreshVisible();
    void refreshVisible();
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      active = false;
      clearTimer();
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [enabled, refresh]);

  return { rateLimits, refresh };
}
