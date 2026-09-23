import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ProductHost } from "./host";
import { LocaleProvider, UI_LOCALE_STORAGE_KEY, messageCatalog, readStoredLocale, useLocale } from "./i18n";

const host: ProductHost = { environment: "web", copyText: vi.fn(async () => undefined), setUiLocale: vi.fn(async () => undefined) };

function Probe() {
  const { locale, messages, setLocale, formatDateTime } = useLocale();
  return <><span>{locale}</span><span>{messages.settings}</span><span>{formatDateTime("2026-08-31T12:30:00Z")}</span><button onClick={() => setLocale("zh-CN")}>switch</button></>;
}

describe("LocaleProvider", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.mocked(host.setUiLocale!).mockClear();
  });

  it("keeps both catalogs complete", () => {
    expect(Object.keys(messageCatalog.en).sort()).toEqual(Object.keys(messageCatalog["zh-CN"]).sort());
  });

  it("promises a bounded grill instead of unlimited rounds", () => {
    expect(messageCatalog.en.intakeRoundLimit).toContain("3 rounds");
    expect(messageCatalog["zh-CN"].intakeRoundLimit).toContain("最多 3 轮");
    for (const catalog of Object.values(messageCatalog)) {
      for (const value of Object.values(catalog)) {
        expect(value).not.toMatch(/no round limit|不会限制轮数|不限轮数/);
      }
    }
  });

  it("carries the live-run vocabulary in both catalogs", () => {
    const liveKeys = [
      "readerActivity", "roleWriter", "roleChecker", "roleJudge", "roleOther", "readerElapsed",
      "readerJumpToLatest", "readerNewStep", "readerSealedAnnouncement", "readerAwaitingFirstStep",
      "readerStepChecks", "budgetCalls", "treeRunningNode", "liveRunning",
    ] as const;
    for (const key of liveKeys) {
      expect(messageCatalog.en[key]).toBeTruthy();
      expect(messageCatalog["zh-CN"][key]).toBeTruthy();
    }
    // The four role badges have to stay distinguishable from one another in both languages.
    for (const catalog of Object.values(messageCatalog)) {
      const roles = [catalog.roleWriter, catalog.roleChecker, catalog.roleJudge, catalog.roleOther];
      expect(new Set(roles).size).toBe(roles.length);
    }
    expect(messageCatalog.en.roleWriter).toBe("Deriving");
    expect(messageCatalog["zh-CN"].roleWriter).toBe("推导中");
  });

  it("defaults invalid or unavailable storage to English", () => {
    window.localStorage.setItem(UI_LOCALE_STORAGE_KEY, "fr");
    expect(readStoredLocale()).toBe("en");
    expect(readStoredLocale({ getItem: () => { throw new Error("blocked"); } })).toBe("en");
  });

  it("switches immediately, persists, formats by locale, and syncs the host", async () => {
    const user = userEvent.setup();
    render(<LocaleProvider host={host}><Probe /></LocaleProvider>);
    expect(screen.getByText("en")).toBeVisible();
    expect(screen.getByText("Settings")).toBeVisible();
    expect(document.documentElement.lang).toBe("en");
    expect(screen.getByText(/Aug 31, 2026/)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "switch" }));
    expect(screen.getByText("zh-CN")).toBeVisible();
    expect(screen.getByText("设置")).toBeVisible();
    expect(window.localStorage.getItem(UI_LOCALE_STORAGE_KEY)).toBe("zh-CN");
    expect(document.documentElement.lang).toBe("zh-CN");
    await waitFor(() => expect(host.setUiLocale).toHaveBeenLastCalledWith("zh-CN"));
  });
});
