import { useEffect, useState } from "react";
import type { DerivationClient } from "../api";
import type { CapabilitiesView } from "../types";

export function useProductCapabilities(client: DerivationClient) {
  const [capabilities, setCapabilities] = useState<CapabilitiesView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    client.getCapabilities().then(
      (value) => {
        if (!active) return;
        setCapabilities(value);
        setError(null);
        setLoading(false);
      },
      (reason: unknown) => {
        if (!active) return;
        setCapabilities(null);
        setError(reason instanceof Error ? reason.message : "Unable to read product capabilities");
        setLoading(false);
      },
    );
    return () => { active = false; };
  }, [client]);

  return { capabilities, error, loading };
}
