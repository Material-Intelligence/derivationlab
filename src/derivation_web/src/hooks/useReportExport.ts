import { useCallback, useState } from "react";
import type { DerivationClient } from "../api";
import type { DerivationRoute, ReportBundleView } from "../types";

export function useReportExport(client: DerivationClient, runId: string | undefined, route: DerivationRoute | undefined) {
  const [open, setOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [result, setResult] = useState<ReportBundleView | null>(null);
  const [error, setError] = useState<string | null>(null);

  const show = useCallback(() => {
    setResult(null);
    setError(null);
    setOpen(true);
  }, []);
  const close = useCallback(() => {
    if (!exporting) setOpen(false);
  }, [exporting]);
  const reset = useCallback(() => {
    setOpen(false);
    setResult(null);
    setError(null);
  }, []);
  const confirm = useCallback(async () => {
    if (!runId || !route) return;
    setExporting(true);
    setResult(null);
    setError(null);
    try {
      setResult(await client.exportReport(runId, { selected_route_id: route.id, confirm_selected_route: true }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "ReportBundle export failed");
    } finally {
      setExporting(false);
    }
  }, [client, route, runId]);

  return { open, exporting, result, error, show, close, reset, confirm };
}
