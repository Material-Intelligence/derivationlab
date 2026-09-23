import { useCallback, useEffect, useState } from "react";
import type { DerivationClient } from "../api";
import type { DerivationRun, RunSummary } from "../types";

export function useRunCatalog(client: DerivationClient, run: DerivationRun | null) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setRuns(await client.listRuns());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to list runs");
    } finally {
      setLoading(false);
    }
  }, [client]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (!run) return;
    const summary: RunSummary = {
      id: run.id,
      question: run.question,
      phase: run.phase,
      status: run.status,
      updated_at: run.updated_at,
      created_at: run.created_at,
      step_count: run.steps.length,
      route_count: run.routes.length,
      read_only: run.read_only,
    };
    setRuns((current) => [summary, ...current.filter((item) => item.id !== run.id)]
      .sort((left, right) => right.updated_at.localeCompare(left.updated_at)));
  }, [run]);

  return { runs, loading, error, refresh };
}
