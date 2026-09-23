import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createFixtureApi } from "../fixtures";
import { useAccountRateLimits } from "./useAccountRateLimits";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("useAccountRateLimits", () => {
  it("reads immediately, polls each visible minute, and pauses while hidden", async () => {
    vi.useFakeTimers();
    let visibility: DocumentVisibilityState = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
    const api = createFixtureApi();
    const read = vi.spyOn(api, "getAccountRateLimits");

    const { unmount } = renderHook(() => useAccountRateLimits(api, true));
    await act(async () => Promise.resolve());
    expect(read).toHaveBeenCalledTimes(1);

    await act(async () => vi.advanceTimersByTimeAsync(59_999));
    expect(read).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(read).toHaveBeenCalledTimes(2);

    visibility = "hidden";
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await act(async () => vi.advanceTimersByTimeAsync(120_000));
    expect(read).toHaveBeenCalledTimes(2);

    visibility = "visible";
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await act(async () => Promise.resolve());
    expect(read).toHaveBeenCalledTimes(3);
    unmount();
  });

  it("never overlaps a pending refresh with timer or focus refreshes", async () => {
    vi.useFakeTimers();
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    const api = createFixtureApi();
    let resolveFirst: ((value: Awaited<ReturnType<typeof api.getAccountRateLimits>>) => void) | undefined;
    const first = new Promise<Awaited<ReturnType<typeof api.getAccountRateLimits>>>(
      (resolve) => {
        resolveFirst = resolve;
      },
    );
    const fixtureValue = await api.getAccountRateLimits();
    const read = vi
      .spyOn(api, "getAccountRateLimits")
      .mockImplementationOnce(() => first)
      .mockResolvedValue(fixtureValue);

    const { unmount } = renderHook(() => useAccountRateLimits(api, true));
    await act(async () => Promise.resolve());
    expect(read).toHaveBeenCalledTimes(1);

    act(() => window.dispatchEvent(new Event("focus")));
    await act(async () => vi.advanceTimersByTimeAsync(120_000));
    expect(read).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveFirst?.(fixtureValue);
      await first;
    });
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(read).toHaveBeenCalledTimes(2);
    unmount();
  });
});
