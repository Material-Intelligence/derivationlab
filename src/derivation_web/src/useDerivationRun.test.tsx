import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { createFixtureApi, fixtureRun } from "./fixtures";
import { useDerivationRun } from "./useDerivationRun";
import type { DerivationRun } from "./types";

describe("run detail recovery", () => {
  it("keeps an in-flight same-run read attached", async () => {
    const api = createFixtureApi();
    let resolve!: (run: DerivationRun) => void;
    api.getRun = vi.fn(() => new Promise<DerivationRun>((done) => { resolve = done; }));
    const { result } = renderHook(() => useDerivationRun(api, "demo-run"));
    act(() => result.current.selectRun("demo-run"));
    await act(async () => resolve(fixtureRun));
    expect(result.current.run).toEqual(fixtureRun);
    expect(result.current.loading).toBe(false);
    expect(api.getRun).toHaveBeenCalledTimes(1);
  });
  it("keeps same-run selection stable and preserves the tree on stream errors", async () => {
    const api = createFixtureApi();
    const getRun = vi.spyOn(api, "getRun");
    let streamError: (error: Error) => void = () => undefined;
    api.subscribe = vi.fn((_id, _event, error) => { streamError = error; return () => undefined; });
    const { result } = renderHook(() => useDerivationRun(api, "demo-run"));
    await waitFor(() => expect(result.current.run).not.toBeNull());
    const loaded = result.current.run;
    act(() => result.current.selectRun("demo-run"));
    expect(result.current.run).toBe(loaded);
    expect(result.current.loading).toBe(false);
    expect(getRun).toHaveBeenCalledTimes(1);
    act(() => streamError(new Error("Disconnected")));
    expect(result.current.run).toBe(loaded);
    expect(result.current.error).toBe("Disconnected");
  });

  it("explicitly reloads a failed GET without issuing a model command", async () => {
    const api = createFixtureApi();
    api.getRun = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(fixtureRun);
    const create = vi.spyOn(api, "createRun");
    const resume = vi.spyOn(api, "resume");
    const { result } = renderHook(() => useDerivationRun(api, "demo-run"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => result.current.reloadRun());
    await waitFor(() => expect(result.current.run).toEqual(fixtureRun));
    expect(api.getRun).toHaveBeenCalledTimes(2);
    expect(create).not.toHaveBeenCalled();
    expect(resume).not.toHaveBeenCalled();
  });

  it("ignores an old detail response after selecting another run", async () => {
    const api = createFixtureApi();
    let resolveOld!: (run: DerivationRun) => void;
    api.getRun = vi.fn().mockImplementationOnce(() => new Promise<DerivationRun>((resolve) => { resolveOld = resolve; }))
      .mockResolvedValue({ ...fixtureRun, id: "new-run" });
    const { result } = renderHook(() => useDerivationRun(api, "demo-run"));
    act(() => result.current.selectRun("new-run"));
    await waitFor(() => expect(result.current.run?.id).toBe("new-run"));
    await act(async () => resolveOld(fixtureRun));
    expect(result.current.run?.id).toBe("new-run");
  });
});
