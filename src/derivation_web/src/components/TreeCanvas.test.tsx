import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { fixtureRun } from "../fixtures";
import type { DerivationRun } from "../types";
import { TreeCanvas, fitViewBox, wrapNodeTitle } from "./TreeCanvas";

function baseProps(run: DerivationRun = fixtureRun) {
  return {
    run,
    routes: run.routes,
    selectedRouteId: run.routes[0].id,
    onSelectNode: vi.fn(),
    onSelectEdge: vi.fn(),
    onSelectRoute: vi.fn(),
    onOpenDetails: vi.fn(),
    onOpenBranch: vi.fn(),
    onCopyText: vi.fn(),
    canBranchNode: () => false,
  };
}

describe("TreeCanvas long node titles", () => {
  it("limits visual labels to two lines while keeping the full accessible title", () => {
    const longTitle = "Starting from a very long one-dimensional tight-binding chain, derive the complete electronic band structure without clipping the scientific meaning";
    const lines = wrapNodeTitle(longTitle);
    expect(lines).toHaveLength(2);
    expect(lines[1]).toMatch(/…$/u);
    // Words survive the wrap: the first line ends on a word boundary.
    expect(longTitle.startsWith(`${lines[0]} `)).toBe(true);
    expect(wrapNodeTitle("Let ℓ≡√(ħ/mω), ξ≡x/ℓ, ψ_0(ξ)=π^{-1/4}e^{−ξ²/2}, …")[0]).toBe("Let ℓ≡√(ħ/mω), ξ≡x/ℓ,");

    const run = structuredClone(fixtureRun);
    run.steps[0].title = longTitle;
    const { container } = render(
      <TreeCanvas
        run={run}
        routes={run.routes}
        selectedRouteId={run.routes[0].id}
        onSelectNode={vi.fn()}
        onSelectEdge={vi.fn()}
        onSelectRoute={vi.fn()}
        onOpenDetails={vi.fn()}
        onOpenBranch={vi.fn()}
        onCopyText={vi.fn()}
        canBranchNode={() => false}
      />,
    );

    expect(screen.getByRole("button", { name: `Open: ${longTitle}` })).toBeVisible();
    const node = container.querySelector(`[aria-label="Open: ${longTitle}"]`);
    expect(node?.querySelector("title")).toHaveTextContent(longTitle);
    expect(node?.querySelectorAll(".node-title tspan")).toHaveLength(2);
  });

  it("opens custom menus for nodes, edges, and blank canvas while keeping route tabs in the panel", () => {
    const props = { ...baseProps(), selectedNodeId: fixtureRun.rootStepId ?? undefined, canBranchNode: () => true };
    const { container } = render(<TreeCanvas {...props} />);

    expect(screen.getByRole("navigation", { name: "Explicitly select a derivation route" })).toBeVisible();
    fireEvent.contextMenu(screen.getByRole("button", { name: "Open: Research question" }), { clientX: 20, clientY: 30 });
    expect(screen.getByRole("menu", { name: "More actions for route step: Research question" })).toBeVisible();
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Open in route reader" }), { key: "Escape" });

    fireEvent.contextMenu(screen.getByRole("button", { name: "Select matching route: Research question → Define states and constraints" }), { clientX: 30, clientY: 40 });
    expect(screen.getByRole("menu", { name: /Copy connection information: Research question → Define states and constraints/ })).toBeVisible();
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Select matching route" }), { key: "Escape" });

    const svg = container.querySelector(".tree-svg");
    expect(svg).not.toBeNull();
    fireEvent.contextMenu(svg!, { clientX: 40, clientY: 50 });
    expect(screen.getByRole("menu", { name: "Canvas actions" })).toBeVisible();
  });
});

describe("TreeCanvas collapsing", () => {
  it("starts with the highlighted route open and deep side branches folded", () => {
    render(<TreeCanvas {...baseProps()} />);

    // Route A is fully visible.
    expect(screen.getByRole("button", { name: "Open: Result A: weak coupling" })).toBeInTheDocument();
    // The two off-route branches at depth 3 are folded away behind a toggle.
    expect(screen.queryByRole("button", { name: "Open: Result B: validity boundary" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open: Result C: symmetry closure" })).toBeNull();
    expect(screen.getByRole("button", { name: "Expand: Numerical counterexample check (1 following step)" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.getAllByText("1 following step hidden")).toHaveLength(2);
  });

  it("expands and re-collapses a branch from its toggle", () => {
    render(<TreeCanvas {...baseProps()} />);

    fireEvent.click(screen.getByRole("button", { name: /^Expand: Numerical counterexample check/ }));
    expect(screen.getByRole("button", { name: "Open: Result B: validity boundary" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Collapse: Numerical counterexample check" })).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(screen.getByRole("button", { name: "Collapse: Numerical counterexample check" }));
    expect(screen.queryByRole("button", { name: "Open: Result B: validity boundary" })).toBeNull();
  });

  it("expands everything and folds back to the default with the toolbar", () => {
    render(<TreeCanvas {...baseProps()} />);

    fireEvent.click(screen.getByRole("button", { name: "Expand all branches" }));
    expect(screen.getByRole("button", { name: "Open: Result B: validity boundary" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open: Result C: symmetry closure" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Collapse side branches" }));
    expect(screen.queryByRole("button", { name: "Open: Result B: validity boundary" })).toBeNull();
  });

  it("reveals the ancestors of a newly selected step without touching other branches", () => {
    const props = baseProps();
    const { rerender } = render(<TreeCanvas {...props} />);
    expect(screen.queryByRole("button", { name: "Open: Result C: symmetry closure" })).toBeNull();

    rerender(<TreeCanvas {...props} selectedNodeId="symmetry-result" />);

    expect(screen.getByRole("button", { name: "Open: Result C: symmetry closure" })).toBeInTheDocument();
    // The unrelated branch that was folded by default stays folded.
    expect(screen.queryByRole("button", { name: "Open: Result B: validity boundary" })).toBeNull();
  });

  it("keeps the selection when an ancestor is folded and marks the ancestor instead", () => {
    const props = baseProps();
    render(<TreeCanvas {...props} selectedNodeId="weak-result" />);
    expect(screen.getByRole("button", { name: "Open: Result A: weak coupling" })).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: "Collapse: Perturbative expansion" }));

    expect(screen.queryByRole("button", { name: "Open: Result A: weak coupling" })).toBeNull();
    expect(props.onSelectNode).not.toHaveBeenCalled();
    expect(screen.getByText("Has current step")).toBeInTheDocument();
  });
});

describe("TreeCanvas viewport stability", () => {
  it("keeps the viewport when a live snapshot adds steps to the same run", () => {
    const props = baseProps();
    const { container, rerender } = render(<TreeCanvas {...props} />);
    const before = container.querySelector(".tree-svg")?.getAttribute("viewBox");
    expect(before).toBeTruthy();

    const grown = structuredClone(fixtureRun);
    grown.steps = [...grown.steps, { ...structuredClone(fixtureRun.steps[9]), id: "follow-up", revisionId: "revision-follow-up", order: 10, title: "Follow-up step" }];
    grown.edges = [...grown.edges, { id: "e10", from: "weak-result", to: "follow-up", order: 0, kind: "continuation" }];
    rerender(<TreeCanvas {...props} run={grown} routes={grown.routes} />);

    expect(screen.getByRole("button", { name: "Open: Follow-up step" })).toBeInTheDocument();
    expect(container.querySelector(".tree-svg")?.getAttribute("viewBox")).toBe(before);
  });

  it("re-applies the default folding when a different run is opened", () => {
    const props = baseProps();
    const { rerender } = render(<TreeCanvas {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Expand all branches" }));
    expect(screen.getByRole("button", { name: "Open: Result B: validity boundary" })).toBeInTheDocument();

    const other = structuredClone(fixtureRun);
    other.id = "another-run";
    rerender(<TreeCanvas {...props} run={other} routes={other.routes} />);

    expect(screen.queryByRole("button", { name: "Open: Result B: validity boundary" })).toBeNull();
  });

  it("stops shrinking once a node would render narrower than 88px", () => {
    expect(fitViewBox({ width: 740, height: 480 }, { width: 800, height: 600 })).toEqual({
      box: { x: 0, y: 0, width: 740, height: 480 },
      clamped: false,
    });

    const wide = fitViewBox({ width: 3000, height: 1200 }, { width: 800, height: 600 });
    expect(wide.clamped).toBe(true);
    expect(wide.box.width).toBe(1600);
    expect(wide.box.height).toBe(1200);
    expect((800 / wide.box.width) * 176).toBeCloseTo(88);

    const tall = fitViewBox({ width: 800, height: 2000 }, { width: 800, height: 600 });
    expect(tall.clamped).toBe(true);
    expect((600 / tall.box.height) * 176).toBeCloseTo(88);

    // Without a measured canvas the map falls back to the plain fit box.
    expect(fitViewBox({ width: 3000, height: 1200 }, null).clamped).toBe(false);
  });
});

describe("TreeCanvas edge rendering", () => {
  it("draws one connection per step pair and names every merged kind", () => {
    const run = structuredClone(fixtureRun);
    run.edges = [
      ...run.edges,
      { id: "e0-parallel", from: "question", to: "constraints", order: 1, kind: "human_direction" },
    ];
    const { container } = render(<TreeCanvas {...baseProps(run)} />);

    // Seven connections are on screen in the default view; the parallel question→constraints edge
    // is merged into one path instead of being drawn twice.
    expect(container.querySelectorAll(".tree-edge")).toHaveLength(7);
    expect(container.querySelectorAll(".edge-hit")).toHaveLength(7);
    expect(container.querySelectorAll('[aria-label="Select matching route: Research question → Define states and constraints"]')).toHaveLength(1);
    const tooltips = [...container.querySelectorAll(".edge-layer title")].map((node) => node.textContent);
    expect(tooltips).toContain("Research question → Define states and constraints · continuation · human direction");
  });

  it("never draws proposed alternatives into the real tree", () => {
    const run = structuredClone(fixtureRun);
    run.steps = [...run.steps, { ...structuredClone(fixtureRun.steps[9]), id: "sketch", revisionId: "revision-sketch", order: 10, title: "Alternative draft" }];
    run.edges = [...run.edges, { id: "e-proposed", from: "weak-result", to: "sketch", order: 0, kind: "proposed" }];
    const { container } = render(<TreeCanvas {...baseProps(run)} />);

    expect(screen.getByRole("button", { name: "Select matching route: Result A: weak coupling → Alternative draft" })).toBeInTheDocument();
    expect(container.querySelectorAll(".tree-edge.proposed")).toHaveLength(0);
    expect(container.querySelectorAll(".proposed")).toHaveLength(0);
  });
});

describe("TreeCanvas live calls", () => {
  /**
   * Geometry the ghost layer promises (see the GHOST_* constants in TreeCanvas.tsx):
   * a ghost centre sits `NODE_HEIGHT + GHOST_GAP` below its parent's centre, and further ghosts on
   * the same parent stack by `NODE_HEIGHT + 8`.
   */
  const NODE_HEIGHT = 84;
  const GHOST_GAP = 26;
  const GHOST_HEIGHT = 44;
  const GHOST_STACK = NODE_HEIGHT + 8;

  const call = (fromStepId: string, callId = "call-1", label = "Writer model call") =>
    ({ callId, branchId: "branch-a", fromStepId, label });

  const rectY = (element: Element | null) => Number(element?.querySelector("rect")?.getAttribute("y"));
  const nodeTop = (container: HTMLElement, name: string) => {
    const transform = container.querySelector(`[aria-label="Open: ${name}"]`)?.getAttribute("transform") ?? "";
    return Number(/translate\([-\d.]+ ([-\d.]+)\)/u.exec(transform)?.[1]);
  };

  it("hangs a ghost placeholder under the sealed step the call continues", () => {
    const { container } = render(<TreeCanvas {...baseProps()} activeCalls={[call("weak-result")]} />);

    const ghost = container.querySelector('.tree-ghost[data-ghost-for="weak-result"]');
    expect(ghost).not.toBeNull();
    expect(ghost?.querySelector(".tree-ghost-role")).toHaveTextContent("Deriving");
    expect(screen.getByRole("status", { name: "In progress: Deriving · under Result A: weak coupling" })).toBeInTheDocument();
    // Below the parent's bottom edge, at the documented offset.
    const parentTop = nodeTop(container, "Result A: weak coupling");
    expect(rectY(ghost)).toBeGreaterThan(parentTop + NODE_HEIGHT);
    expect(rectY(ghost) - parentTop).toBe(NODE_HEIGHT / 2 + NODE_HEIGHT + GHOST_GAP - GHOST_HEIGHT / 2);
  });

  it("stacks several calls on the same step and names each role", () => {
    const { container } = render(
      <TreeCanvas
        {...baseProps()}
        activeCalls={[call("weak-result", "c1", "Writer model call"), call("weak-result", "c2", "Checker model call")]}
      />,
    );

    const ghosts = [...container.querySelectorAll(".tree-ghost")];
    expect(ghosts).toHaveLength(2);
    expect(rectY(ghosts[1]) - rectY(ghosts[0])).toBe(GHOST_STACK);
    expect(ghosts.map((ghost) => ghost.querySelector(".tree-ghost-role")?.textContent)).toEqual(["Deriving", "Checking"]);
  });

  it("drops a call whose anchor step is unknown or folded away", () => {
    const { container } = render(
      <TreeCanvas {...baseProps()} activeCalls={[call("no-such-step", "c1"), call("counterexample", "c2")]} />,
    );

    // `counterexample` is collapsed by default, so its ghost has nowhere honest to hang.
    expect(screen.getByRole("button", { name: /^Expand: Numerical counterexample check/ })).toBeInTheDocument();
    expect(container.querySelectorAll(".tree-ghost")).toHaveLength(0);
  });

  it("never moves the viewport when calls appear, and flags fit instead", () => {
    const props = baseProps();
    const { container, rerender } = render(<TreeCanvas {...props} />);
    const before = container.querySelector(".tree-svg")?.getAttribute("viewBox");
    expect(before).toBeTruthy();
    expect(container.querySelector(".canvas-tools button.is-live")).toBeNull();

    rerender(<TreeCanvas {...props} activeCalls={[call("weak-result")]} />);

    expect(container.querySelectorAll(".tree-ghost")).toHaveLength(1);
    expect(container.querySelector(".tree-svg")?.getAttribute("viewBox")).toBe(before);
    // The ghost hangs below the laid-out box, so the fit control is marked instead of panning.
    expect(container.querySelector(".canvas-tools button.is-live")).not.toBeNull();

    rerender(<TreeCanvas {...props} activeCalls={[]} />);
    expect(container.querySelectorAll(".tree-ghost")).toHaveLength(0);
    expect(container.querySelector(".tree-svg")?.getAttribute("viewBox")).toBe(before);
  });

  it("marks a step the record itself still reports as running", () => {
    const run = structuredClone(fixtureRun);
    run.steps[7].status = "running";
    const { container } = render(<TreeCanvas {...baseProps(run)} />);

    expect(container.querySelectorAll(".tree-node.running")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Open: Result A: weak coupling · Running" })).toBeInTheDocument();
  });

  it("reserves fit height for the ghosts hanging below the layout", () => {
    expect(fitViewBox({ width: 740, height: 480 }, { width: 800, height: 600 }, 126)).toEqual({
      box: { x: 0, y: 0, width: 740, height: 606 },
      clamped: false,
    });
    // Same call without the reserve is unchanged, so existing callers keep their box.
    expect(fitViewBox({ width: 740, height: 480 }, { width: 800, height: 600 }).box.height).toBe(480);
  });
});
