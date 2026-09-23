import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ProductHost } from "../host";
import { LocaleProvider } from "../i18n";
import type { RunSummary } from "../types";
import { RunSidebar } from "./RunSidebar";

const runs: RunSummary[] = [
  { id: "newer", question: "Newer run", phase: "review_ready", status: "review_ready", updated_at: "2026-08-31T12:00:00Z", created_at: "2026-08-31T11:00:00Z", step_count: 4, route_count: 1, read_only: false },
  { id: "older", question: "Older run", phase: "review_ready", status: "review_ready", updated_at: "2026-08-30T12:00:00Z", created_at: "2026-08-30T11:00:00Z", step_count: 3, route_count: 1, read_only: false },
];

function renderSidebar(host: ProductHost = { environment: "web", copyText: vi.fn(async () => undefined) }) {
  return render(<RunSidebar runs={runs} currentRunId="newer" open loading={false} onClose={vi.fn()} onNew={vi.fn()} onSelect={vi.fn()} onExport={vi.fn()} host={host} />);
}

function runQuestions() {
  return [...screen.getByRole("navigation", { name: "Existing derivations" }).querySelectorAll<HTMLElement>(".run-list-item strong")].map((item) => item.textContent);
}

describe("RunSidebar object actions", () => {
  it("pins in the same list, sorts pinned runs first, and persists the preference", async () => {
    const user = userEvent.setup();
    const view = renderSidebar();
    expect(runQuestions()).toEqual(["Newer run", "Older run"]);

    await user.click(screen.getByRole("button", { name: "More actions for run: older" }));
    await user.click(screen.getByRole("menuitem", { name: "Pin" }));
    expect(runQuestions()).toEqual(["Older run", "Newer run"]);
    expect(window.localStorage.getItem("derivationlab.ui.pinned-runs.v1")).toContain("older");

    view.unmount();
    renderSidebar();
    expect(runQuestions()).toEqual(["Older run", "Newer run"]);
  });

  it("uses the same Run menu for right click and Shift+F10 without selecting the row", async () => {
    const onSelect = vi.fn();
    render(<RunSidebar runs={runs} currentRunId="newer" open loading={false} onClose={vi.fn()} onNew={vi.fn()} onSelect={onSelect} onExport={vi.fn()} host={{ environment: "web", copyText: vi.fn(async () => undefined) }} />);
    const olderButton = screen.getByRole("button", { name: /Older run/ });
    const olderRow = olderButton.closest(".run-list-row");
    expect(olderRow).not.toBeNull();

    fireEvent.contextMenu(olderRow!);
    expect(screen.getByRole("menu", { name: "More actions for run: Older run" })).toBeVisible();
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Open" }), { key: "Escape" });
    expect(olderButton).toHaveFocus();

    olderButton.focus();
    fireEvent.keyDown(olderButton, { key: "F10", shiftKey: true });
    expect(screen.getByRole("menu", { name: "More actions for run: Older run" })).toBeVisible();
    expect(within(screen.getByRole("menu")).getByRole("menuitem", { name: "Export PDF" })).toBeEnabled();
  });

  it("opens the language menu above Settings with radio semantics and restores focus", async () => {
    const user = userEvent.setup();
    const host: ProductHost = { environment: "electron-macos", copyText: vi.fn(async () => undefined), setUiLocale: vi.fn(async () => undefined) };
    render(<LocaleProvider host={host}><RunSidebar runs={runs} currentRunId="newer" open loading={false} onClose={vi.fn()} onNew={vi.fn()} onSelect={vi.fn()} onExport={vi.fn()} host={host} /></LocaleProvider>);
    const settings = screen.getByRole("button", { name: "Settings" });
    await user.click(settings);
    expect(screen.getByText("Display language")).toBeVisible();
    expect(screen.getByRole("menuitemradio", { name: "English" })).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(await screen.findByRole("button", { name: "设置" })).toHaveFocus();
    expect(window.localStorage.getItem("derivationlab.ui.locale.v1")).toBe("zh-CN");
    expect(host.setUiLocale).toHaveBeenLastCalledWith("zh-CN");
  });

  it("switches the theme from the same Settings menu and remembers the choice", async () => {
    const user = userEvent.setup();
    const host: ProductHost = { environment: "web", copyText: vi.fn(async () => undefined) };
    render(<LocaleProvider host={host}><RunSidebar runs={runs} currentRunId="newer" open loading={false} onClose={vi.fn()} onNew={vi.fn()} onSelect={vi.fn()} onExport={vi.fn()} host={host} /></LocaleProvider>);

    await user.click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.getByText("Appearance")).toBeVisible();
    expect(screen.getByRole("menuitemradio", { name: "Warm paper" })).toHaveAttribute("aria-checked", "true");

    await user.click(screen.getByRole("menuitemradio", { name: "Light" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(window.localStorage.getItem("derivationlab.ui.theme.v1")).toBe("light");

    await user.click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.getByRole("menuitemradio", { name: "Light" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("menuitemradio", { name: "Warm paper" })).toHaveAttribute("aria-checked", "false");
    // Every radio keeps the leading column, checked or not, so the label never
    // collapses into the 20px icon slot.
    for (const radio of screen.getAllByRole("menuitemradio")) expect(radio.querySelector(".context-menu-check")).toBeInTheDocument();
  });
});
