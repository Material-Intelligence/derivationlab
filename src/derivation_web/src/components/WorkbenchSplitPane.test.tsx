import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { WorkbenchSplitPane } from "./WorkbenchSplitPane";

const storedValues = new Map<string, string>();
const testStorage: Storage = {
  get length() { return storedValues.size; },
  clear() { storedValues.clear(); },
  getItem(key) { return storedValues.get(key) ?? null; },
  key(index) { return [...storedValues.keys()][index] ?? null; },
  removeItem(key) { storedValues.delete(key); },
  setItem(key, value) { storedValues.set(key, String(value)); },
};

beforeEach(() => {
  Object.defineProperty(window, "localStorage", { configurable: true, value: testStorage });
  window.localStorage.clear();
});

function renderSplitPane() {
  return render(<WorkbenchSplitPane tree={<div>Tree content</div>} route={<div>Route content</div>} />);
}

describe("WorkbenchSplitPane", () => {
  it("persists route collapse and restores it on a new mount", async () => {
    const user = userEvent.setup();
    const first = renderSplitPane();

    await user.click(screen.getByRole("button", { name: "Collapse route reader" }));
    expect(screen.getByText("Route content").closest(".workbench-split")).toHaveClass("is-route-collapsed");
    expect(window.localStorage.getItem("derivationlab.workbench.route-collapsed")).toBe("true");
    first.unmount();

    renderSplitPane();
    expect(screen.getByRole("button", { name: "Expand route reader" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Expand route reader" }));
    expect(screen.getByText("Route content")).toBeInTheDocument();
    expect(window.localStorage.getItem("derivationlab.workbench.route-collapsed")).toBe("false");
  });

  it("supports keyboard resizing and persists the constrained width", () => {
    renderSplitPane();
    const separator = screen.getByRole("separator", { name: "Resize derivation map" });

    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator).toHaveAttribute("aria-valuenow", "464");
    expect(window.localStorage.getItem("derivationlab.workbench.tree-width.v3")).toBe("464");

    fireEvent.keyDown(separator, { key: "End" });
    expect(separator).toHaveAttribute("aria-valuenow", "320");
  });

  it("enters reading focus mode by collapsing the tree rail, and remembers it", async () => {
    const user = userEvent.setup();
    const first = renderSplitPane();

    await user.click(screen.getByRole("button", { name: "Collapse derivation tree" }));
    const split = screen.getByText("Route content").closest(".workbench-split")!;
    expect(split).toHaveClass("is-tree-collapsed");
    expect(split).not.toHaveClass("is-route-collapsed");
    expect(window.localStorage.getItem("derivationlab.workbench.tree-collapsed")).toBe("true");
    first.unmount();

    renderSplitPane();
    await user.click(screen.getByRole("button", { name: "Expand derivation tree" }));
    expect(screen.getByText("Tree content").closest(".workbench-split")).not.toHaveClass("is-tree-collapsed");
    expect(window.localStorage.getItem("derivationlab.workbench.tree-collapsed")).toBe("false");
  });

  it("swaps the collapse controls instead of ever collapsing both panes", async () => {
    const user = userEvent.setup();
    renderSplitPane();

    await user.click(screen.getByRole("button", { name: "Collapse derivation tree" }));
    expect(screen.queryByRole("button", { name: "Collapse route reader" })).not.toBeInTheDocument();
    expect(screen.queryByRole("separator", { name: "Resize derivation map" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Expand derivation tree" }));
    await user.click(screen.getByRole("button", { name: "Collapse route reader" }));
    const split = screen.getByText("Tree content").closest(".workbench-split")!;
    expect(split).toHaveClass("is-route-collapsed");
    expect(split).not.toHaveClass("is-tree-collapsed");
  });

  it("starts the map rail at the flipped-workbench default width", () => {
    renderSplitPane();
    expect(screen.getByRole("separator", { name: "Resize derivation map" })).toHaveAttribute("aria-valuenow", "440");
  });

  it("puts the reading desk before the map in the document order", () => {
    const { container } = renderSplitPane();
    const panes = [...container.querySelectorAll(".workbench-pane")];
    expect(panes.map((pane) => pane.className)).toEqual([
      "workbench-pane workbench-route-pane",
      "workbench-pane workbench-tree-pane",
    ]);
    const route = container.querySelector(".workbench-route-pane")!;
    const tree = container.querySelector(".workbench-tree-pane")!;
    expect(route.compareDocumentPosition(tree) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("offers a narrow-workbench tab state without duplicating content", async () => {
    const user = userEvent.setup();
    renderSplitPane();
    const treeTab = screen.getByRole("tab", { name: "Derivation tree" });
    const routeTab = screen.getByRole("tab", { name: "Route reading" });

    expect(routeTab).toHaveAttribute("aria-selected", "true");
    await user.click(treeTab);
    expect(treeTab).toHaveAttribute("aria-selected", "true");
    expect(routeTab).toHaveAttribute("aria-selected", "false");
    expect(screen.getAllByText("Route content")).toHaveLength(1);
  });
});
