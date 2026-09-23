import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { checkLabel, formatElapsed, roleOf, useCallElapsed, useNewlySealed, worstCheck, type LiveCall } from "./live";

const call = (overrides: Partial<LiveCall> = {}): LiveCall => ({
  callId: "call-1",
  branchId: "branch-a",
  fromStepId: "conservation",
  label: "Writer model call",
  ...overrides,
});

describe("roleOf", () => {
  it("prefers the role the backend declares", () => {
    // Phase B populates `role`; the label may still say something else.
    expect(roleOf(call({ role: "judge", label: "Writer model call" }))).toBe("judge");
    expect(roleOf(call({ role: "  Checker  " }))).toBe("checker");
  });

  it("falls back to the label the service formats today", () => {
    expect(roleOf(call({ label: "Writer model call" }))).toBe("writer");
    expect(roleOf(call({ label: "Checker model call" }))).toBe("checker");
    expect(roleOf(call({ label: "Judge model call" }))).toBe("judge");
  });

  it("degrades to `other` rather than guessing", () => {
    expect(roleOf(call({ label: "Tool call" }))).toBe("other");
    expect(roleOf(call({ role: "planner", label: "Tool call" }))).toBe("other");
  });
});

describe("formatElapsed", () => {
  it("reads as m:ss", () => {
    expect(formatElapsed(0)).toBe("0:00");
    expect(formatElapsed(9)).toBe("0:09");
    expect(formatElapsed(75)).toBe("1:15");
    expect(formatElapsed(-4)).toBe("0:00");
  });
});

describe("useCallElapsed", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("counts from the moment a call is first seen and ticks every second", () => {
    const { result, rerender } = renderHook(({ calls }: { calls: LiveCall[] }) => useCallElapsed(calls), {
      initialProps: { calls: [call()] },
    });
    expect(result.current.get("call-1")).toBe(0);

    act(() => vi.advanceTimersByTime(5_000));
    expect(result.current.get("call-1")).toBe(5);

    // A fresh overlay array for the same call must not restart the clock.
    rerender({ calls: [call()] });
    expect(result.current.get("call-1")).toBe(5);
  });

  it("uses `startedAt` when the backend supplies it", () => {
    const startedAt = new Date(Date.now() - 30_000).toISOString();
    const { result } = renderHook(() => useCallElapsed([call({ startedAt })]));
    expect(result.current.get("call-1")).toBe(30);
  });

  it("forgets a call that leaves the overlay", () => {
    const { result, rerender } = renderHook(({ calls }: { calls: LiveCall[] }) => useCallElapsed(calls), {
      initialProps: { calls: [call()] },
    });
    act(() => vi.advanceTimersByTime(5_000));

    rerender({ calls: [] });
    expect(result.current.size).toBe(0);

    rerender({ calls: [call()] });
    expect(result.current.get("call-1")).toBe(0);
  });
});

describe("useNewlySealed", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("treats nothing on the first pass as new", () => {
    const { result } = renderHook(() => useNewlySealed(["a", "b", "c"]));
    expect([...result.current]).toEqual([]);
  });

  it("marks ids that appear later and drops them after the hold", () => {
    const { result, rerender } = renderHook(({ ids }: { ids: string[] }) => useNewlySealed(ids, 4_000), {
      initialProps: { ids: ["a", "b"] },
    });

    rerender({ ids: ["a", "b", "c"] });
    expect([...result.current]).toEqual(["c"]);

    // A second seal must not cut the first highlight short.
    act(() => vi.advanceTimersByTime(2_000));
    rerender({ ids: ["a", "b", "c", "d"] });
    expect([...result.current].sort()).toEqual(["c", "d"]);

    act(() => vi.advanceTimersByTime(2_000));
    expect([...result.current]).toEqual(["d"]);

    act(() => vi.advanceTimersByTime(2_000));
    expect([...result.current]).toEqual([]);
  });

  it("ignores a re-rendered list whose contents did not change", () => {
    const { result, rerender } = renderHook(({ ids }: { ids: string[] }) => useNewlySealed(ids), {
      initialProps: { ids: ["a", "b"] },
    });
    rerender({ ids: ["a", "b"] });
    expect([...result.current]).toEqual([]);
  });
});

describe("worstCheck", () => {
  it("ranks failed over pending over not_requested over passed", () => {
    expect(worstCheck({ schema: "passed", physics: "passed", provenance: "passed" })).toBe("passed");
    expect(worstCheck({ schema: "passed", physics: "pending", provenance: "passed" })).toBe("pending");
    expect(worstCheck({ schema: "passed", physics: "failed", provenance: "pending" })).toBe("failed");
    expect(worstCheck({ schema: "failed", physics: "passed", provenance: "passed" })).toBe("failed");
  });

  it("never reads a checker-off step as passed", () => {
    expect(worstCheck({ schema: "passed", physics: "not_requested", provenance: "passed" })).toBe("not_requested");
    expect(worstCheck({ schema: "passed", physics: "not_requested", provenance: "pending" })).toBe("pending");
    expect(worstCheck({ schema: "failed", physics: "not_requested", provenance: "passed" })).toBe("failed");
  });

  it("labels a state from the caller's catalog", () => {
    const labels = { passed: "Passed", failed: "Failed", pending: "Pending", not_requested: "Unchecked" };
    expect(checkLabel(worstCheck({ schema: "passed", physics: "pending", provenance: "passed" }), labels)).toBe("Pending");
    expect(checkLabel(worstCheck({ schema: "passed", physics: "not_requested", provenance: "passed" }), labels)).toBe("Unchecked");
  });
});
