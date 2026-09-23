import { useCallback, useEffect, useRef, useState } from "react";
import type { DerivationClient } from "./api";
import type { BranchKind, DerivationRun, ProductCreateRunRequest, RuntimeOverlay } from "./types";

export type PendingCommand = "create" | "pause" | "resume" | "interrupt" | "branch";

export function useDerivationRun(api: DerivationClient, initialRunId?: string) {
  const [run, setRun] = useState<DerivationRun | null>(null);
  const [runId, setRunId] = useState(initialRunId);
  const [loading, setLoading] = useState(Boolean(initialRunId));
  const [reloadGeneration, setReloadGeneration] = useState(0);
  const [actionError, setActionError] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [overlay, setOverlay] = useState<RuntimeOverlay>({ activeCalls: [], hard_interrupt_requested: false });
  const [pendingCommand, setPendingCommand] = useState<PendingCommand | null>(null);
  const commandPending = useRef(false);
  const selectedRunId = useRef(initialRunId);

  useEffect(() => {
    if (!runId) {
      setRun(null);
      setLoading(false);
      setActionError(null);
      setStreamError(null);
      setOverlay({ activeCalls: [], hard_interrupt_requested: false });
      return;
    }
    let active = true;
    let unsubscribe: () => void = () => undefined;
    setLoading(true);
    setStreamError(null);
    setOverlay({ activeCalls: [], hard_interrupt_requested: false });
    api.getRun(runId).then(
      (value) => {
        if (!active || selectedRunId.current !== runId) return;
        setRun(value);
        setActionError(null);
        setOverlay({ activeCalls: [], hard_interrupt_requested: value.hard_interrupt_requested });
        setLoading(false);
        unsubscribe = api.subscribe(
          runId,
          (event) => {
            if (!active || selectedRunId.current !== runId) return;
            setStreamError(null);
            if (active && event.run) setRun(event.run);
            if (active && event.overlay) setOverlay(event.overlay);
          },
          (reason) => active && selectedRunId.current === runId && setStreamError(reason.message),
          { lastEventId: value.canonical_event_id, onOpen: () => active && selectedRunId.current === runId && setStreamError(null) },
        );
      },
      (reason: unknown) => {
        if (!active || selectedRunId.current !== runId) return;
        setActionError(reason instanceof Error ? reason.message : "Unable to load run");
        setLoading(false);
      },
    );
    return () => {
      active = false;
      unsubscribe();
    };
  }, [api, runId, reloadGeneration]);

  const selectRun = useCallback((nextRunId?: string) => {
    if (selectedRunId.current === nextRunId) return;
    selectedRunId.current = nextRunId;
    setRun(null);
    setRunId(nextRunId);
    setLoading(Boolean(nextRunId));
    setActionError(null);
    setStreamError(null);
    setOverlay({ activeCalls: [], hard_interrupt_requested: false });
  }, []);

  const reloadRun = useCallback(() => {
    if (!selectedRunId.current) return;
    setActionError(null);
    setLoading(true);
    setReloadGeneration((generation) => generation + 1);
  }, []);

  const createRun = useCallback(async (request: ProductCreateRunRequest) => {
    if (commandPending.current) return null;
    commandPending.current = true;
    setPendingCommand("create");
    setLoading(true);
    setActionError(null);
    try {
      const created = await api.createRun(request);
      selectedRunId.current = created.id;
      setRun(created);
      setRunId(created.id);
      return created;
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Unable to create run");
      return null;
    } finally {
      setLoading(false);
      commandPending.current = false;
      setPendingCommand(null);
    }
  }, [api]);

  const replaceRun = useCallback(async (kind: PendingCommand, targetRunId: string, action: () => Promise<DerivationRun>) => {
    if (commandPending.current) return null;
    commandPending.current = true;
    setPendingCommand(kind);
    setActionError(null);
    try {
      const updated = await action();
      if (selectedRunId.current === targetRunId) setRun(updated);
      return updated;
    } catch (reason) {
      if (selectedRunId.current === targetRunId) {
        setActionError(reason instanceof Error ? reason.message : "Run action failed");
      }
      return null;
    } finally {
      commandPending.current = false;
      setPendingCommand(null);
    }
  }, []);

  const pause = useCallback(() => run && replaceRun("pause", run.id, () => api.pause(run.id)), [api, replaceRun, run]);
  const resume = useCallback(() => run && replaceRun("resume", run.id, () => api.resume(run.id)), [api, replaceRun, run]);
  const interrupt = useCallback(() => run && replaceRun("interrupt", run.id, () => api.interrupt(run.id)), [api, replaceRun, run]);
  const createBranch = useCallback((revisionId: string, kind: BranchKind, instruction: string) =>
    run && replaceRun("branch", run.id, () => api.createBranch(run.id, {
      from_step_revision_id: revisionId,
      kind,
      instruction,
    })), [api, replaceRun, run]);

  return { run, currentRunId: runId, overlay, loading, error: actionError ?? streamError, pendingCommand, selectRun, reloadRun, createRun, pause, resume, interrupt, createBranch };
}
