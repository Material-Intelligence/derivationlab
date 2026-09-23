import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ContextMenu, type ContextMenuState } from "./ContextMenu";

describe("ContextMenu", () => {
  it("supports arrow navigation, selection, and focus restoration", async () => {
    const user = userEvent.setup();
    const restoreFocus = document.createElement("button");
    restoreFocus.textContent = "source";
    document.body.append(restoreFocus);
    const first = vi.fn();
    const second = vi.fn();
    const onClose = vi.fn();
    const menu: ContextMenuState = {
      x: 12,
      y: 18,
      label: "Test menu",
      restoreFocus,
      actions: [
        { id: "first", label: "First", icon: "open", onSelect: first },
        { id: "second", label: "Second", icon: "copy", onSelect: second },
      ],
    };

    render(<ContextMenu menu={menu} onClose={onClose} />);
    await waitFor(() => expect(screen.getByRole("menuitem", { name: "First" })).toHaveFocus());
    await user.keyboard("{ArrowDown}{Enter}");

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(restoreFocus).toHaveFocus();
    restoreFocus.remove();
  });

  it("gives every entry exactly one element in the leading column", () => {
    const menu: ContextMenuState = {
      x: 4,
      y: 4,
      label: "Mixed menu",
      actions: [
        { id: "plain", label: "Plain", icon: "open", onSelect: vi.fn() },
        { id: "on", label: "On", icon: "settings", radio: true, checked: true, onSelect: vi.fn() },
        { id: "off", label: "Off", icon: "settings", radio: true, checked: false, groupLabel: "Group", onSelect: vi.fn() },
      ],
    };
    render(<ContextMenu menu={menu} onClose={vi.fn()} />);

    // A radio always keeps `.context-menu-check`; a plain entry keeps its icon
    // instead. Two elements would push the label out of the 2-column grid, and
    // none would squeeze the label into the 20px slot.
    expect(screen.getByRole("menuitem", { name: "Plain" }).querySelector(".context-menu-check")).toBeNull();
    for (const name of ["On", "Off"]) {
      const radio = screen.getByRole("menuitemradio", { name });
      expect(radio.querySelector(".context-menu-check")).toBeInTheDocument();
      expect(radio.querySelector(".context-menu-icon:not(.context-menu-check *)")).toBeNull();
    }
    expect(screen.getByText("Group")).toBeVisible();
  });

  it("closes on Escape and restores the invoking control", async () => {
    const user = userEvent.setup();
    const restoreFocus = document.createElement("button");
    document.body.append(restoreFocus);
    const onClose = vi.fn();
    render(<ContextMenu menu={{ x: 0, y: 0, label: "Test menu", restoreFocus, actions: [{ id: "open", label: "Open", icon: "open", onSelect: vi.fn() }] }} onClose={onClose} />);

    await waitFor(() => expect(screen.getByRole("menuitem", { name: "Open" })).toHaveFocus());
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(restoreFocus).toHaveFocus();
    restoreFocus.remove();
  });
});
