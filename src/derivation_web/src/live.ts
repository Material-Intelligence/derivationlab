/**
 * Live-run primitives for the reading desk.
 *
 * A derivation only enters `run.steps` once a StepRevision is sealed, which on a
 * real run is minutes apart. Everything that happens in between reaches the
 * client through `RunEvent.overlay` — a list of in-flight model calls that is
 * replaced wholesale on every event. This module owns the small amount of state
 * the UI has to keep *across* those snapshots (when a call was first seen, which
 * steps appeared just now) plus the pure mappings both the reading desk and the
 * details drawer need.
 *
 * The types here are structural on purpose: `ActiveCallOverlay` from the
 * generated client is assignable to `LiveCall` without an import, so
 * regenerating the contract (Phase B adds `role`, `startedAt`, `preview`) cannot
 * break this file or the components that consume it. The optional fields are
 * already declared, so Phase B only has to start populating them.
 */
import { useEffect, useMemo, useRef, useState } from "react";

/** One in-flight model call, as carried by `RuntimeOverlay.activeCalls`. */
export interface LiveCall {
  callId: string;
  branchId: string;
  fromStepId: string;
  label: string;
  /** Phase B: the role reported by the backend. Until then it is parsed from `label`. */
  role?: string;
  /** Phase B: server-side start time; the client falls back to first-seen. */
  startedAt?: string;
  /** Phase B: tail of the streaming output. Never part of a sealed Record. */
  preview?: string;
  previewTruncated?: boolean;
}

export type CallRole = "writer" | "checker" | "judge" | "other";

/**
 * Which role a call belongs to.
 *
 * `role` wins when the backend sends it; otherwise the only signal available is
 * `label`, which the service formats as `"Writer model call"` /
 * `"Checker model call"` / `"Judge model call"`. The match is case-insensitive
 * and substring-based so a relabelled call ("Re-checking step 3") still lands in
 * the right bucket instead of silently degrading every call to `other`.
 */
export function roleOf(call: LiveCall): CallRole {
  const declared = call.role?.trim().toLowerCase();
  if (declared === "writer" || declared === "checker" || declared === "judge") return declared;
  const label = call.label?.toLowerCase() ?? "";
  if (label.includes("writer") || label.includes("writ")) return "writer";
  if (label.includes("checker") || label.includes("check")) return "checker";
  if (label.includes("judge") || label.includes("judg")) return "judge";
  return "other";
}

/** `m:ss`, the shape a reader can compare at a glance while a call runs. */
export function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/**
 * Seconds each active call has been running, keyed by `callId`.
 *
 * The overlay array is a fresh object on every SSE event, so "how long has this
 * been running" cannot be derived from it: the hook remembers the first moment a
 * `callId` was seen (or `startedAt` when the backend supplies it) and ticks once
 * a second. A call that disappears from the overlay drops its origin, so a
 * `callId` that is ever reused starts from zero rather than from a stale clock.
 */
export function useCallElapsed(calls: readonly LiveCall[]): ReadonlyMap<string, number> {
  const origins = useRef(new Map<string, number>());
  const [now, setNow] = useState(() => Date.now());
  const idle = calls.length === 0;

  useEffect(() => {
    const origin = origins.current;
    const live = new Set(calls.map((call) => call.callId));
    let changed = false;
    for (const callId of [...origin.keys()]) {
      if (live.has(callId)) continue;
      origin.delete(callId);
      changed = true;
    }
    for (const call of calls) {
      if (origin.has(call.callId)) continue;
      const declared = call.startedAt ? Date.parse(call.startedAt) : Number.NaN;
      origin.set(call.callId, Number.isFinite(declared) ? declared : Date.now());
      changed = true;
    }
    // Only a membership change needs a new clock reading; a plain overlay
    // refresh must not add a render on top of the one the snapshot already causes.
    if (changed) setNow(Date.now());
  }, [calls]);

  useEffect(() => {
    if (idle) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [idle]);

  return useMemo(() => {
    const elapsed = new Map<string, number>();
    for (const call of calls) {
      const declared = call.startedAt ? Date.parse(call.startedAt) : Number.NaN;
      const origin = origins.current.get(call.callId) ?? (Number.isFinite(declared) ? declared : now);
      elapsed.set(call.callId, Math.max(0, Math.floor((now - origin) / 1000)));
    }
    return elapsed;
  }, [calls, now]);
}

const ID_SEPARATOR = "\u0000";

/**
 * Step ids that appeared since the previous render pass, held for `ms`.
 *
 * The first list a reader is shown is the run as it already exists, so nothing
 * in it is "new" — only ids that show up on a *later* pass are. Each id gets its
 * own timer, so a step sealed two seconds after the previous one cannot cut the
 * previous highlight short.
 */
export function useNewlySealed(stepIds: readonly string[], ms = 4000): ReadonlySet<string> {
  const known = useRef<Set<string> | null>(null);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const [fresh, setFresh] = useState<ReadonlySet<string>>(() => new Set<string>());
  const key = stepIds.join(ID_SEPARATOR);

  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const timer of pending.values()) clearTimeout(timer);
      pending.clear();
    };
  }, []);

  useEffect(() => {
    // Derived from `key`, not from `stepIds`: the array identity changes on every
    // snapshot, and re-running this effect for an unchanged list would restart
    // the highlight of steps that are already fading out.
    const next = new Set(key ? key.split(ID_SEPARATOR) : []);
    const previous = known.current;
    known.current = next;
    if (previous === null) return;

    const added = [...next].filter((id) => !previous.has(id));
    if (added.length === 0) return;
    setFresh((current) => {
      const merged = new Set(current);
      for (const id of added) merged.add(id);
      return merged;
    });
    for (const id of added) {
      const timer = setTimeout(() => {
        timers.current.delete(id);
        setFresh((current) => {
          if (!current.has(id)) return current;
          const remaining = new Set(current);
          remaining.delete(id);
          return remaining;
        });
      }, ms);
      timers.current.set(id, timer);
    }
  }, [key, ms]);

  return fresh;
}

export type CheckState = "failed" | "pending" | "not_requested" | "passed";

/** Structural view of `StepView.checks`, so this file stays free of generated types. */
export interface StepCheckStates {
  physics: CheckState;
  provenance: "failed" | "pending" | "passed";
  schema: "failed" | "pending" | "passed";
}

/**
 * The one state that describes a step's checks.
 *
 * Severity order is `failed > pending > not_requested > passed`: a reader must
 * never see a green mark on a step whose physics check failed, a step whose
 * Checker has not reported yet is not "passed", and a step on a checker-off run
 * (`not_requested`) was never verified at all — it only outranks a genuine pass.
 * `pending` sits above it because that check can still turn red.
 */
export function worstCheck(checks: StepCheckStates): CheckState {
  const states: CheckState[] = [checks.schema, checks.physics, checks.provenance];
  if (states.includes("failed")) return "failed";
  if (states.includes("pending")) return "pending";
  if (states.includes("not_requested")) return "not_requested";
  return "passed";
}

/**
 * State → caller-supplied label, lifted out of `DetailsDrawer` so the drawer's
 * audit list and the reading desk's check mark can never drift apart. The labels
 * come from the caller because the message catalogs are owned elsewhere.
 */
export function checkLabel(state: CheckState, labels: Readonly<Record<CheckState, string>>): string {
  return labels[state] ?? state;
}
