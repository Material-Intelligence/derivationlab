import { describe, expect, it } from "vitest";
import { dedupeEdges, descendantCount, firstRouteForEdge, firstRouteForNode, layoutTree } from "./domain";
import { fixtureRun } from "./fixtures";
import { deriveFixtureRoutes } from "./test/deriveFixtureRoutes";
import type { DerivationEdge, DerivationRoute, DerivationStep } from "./types";

const template = fixtureRun.steps[0];

function step(id: string, order: number): DerivationStep {
  return { ...structuredClone(template), id, revisionId: `revision-${id}`, order, title: id.toUpperCase() };
}

function edge(id: string, from: string, to: string, order: number, kind: DerivationEdge["kind"]): DerivationEdge {
  return { id, from, to, order, kind };
}

/**
 * Branch-of-branch, the shape `fixtureRun` cannot express: root R=[s1,s2], branch B inherits
 * [s1,s2] and adds [s3,s4], branch C inherits [s1,s2,s3] and adds [s5]. The backend projection
 * keys its edge dedupe on (from, to, kind), so s2→s3 arrives twice — once as the owning branch's
 * `human_direction`, once as the inheriting branch's `continuation`.
 */
const branchOfBranchSteps = ["s1", "s2", "s3", "s4", "s5"].map((id, index) => step(id, index));
const branchOfBranchEdges: DerivationEdge[] = [
  edge("e1", "s1", "s2", 0, "continuation"),
  edge("e2", "s2", "s3", 0, "human_direction"),
  edge("e3", "s3", "s4", 0, "continuation"),
  edge("e4", "s2", "s3", 1, "continuation"),
  edge("e5", "s3", "s5", 1, "model_fork"),
];

describe("derivation route ordering and selection", () => {
  it("derives root-to-tip routes in deterministic edge order", () => {
    const routes = deriveFixtureRoutes(fixtureRun.steps, fixtureRun.edges, fixtureRun.rootStepId);
    expect(routes.map((route) => route.nodeIds.at(-1))).toEqual([
      "weak-result",
      "boundary-result",
      "symmetry-result",
    ]);
  });

  it("preserves the canonical route order supplied by the API", () => {
    const supplied: DerivationRoute[] = [
      { id: "z", label: "later", nodeIds: ["question", "symmetry"], status: "complete", branch_id: "branch-z", status_history: [{ seq: 9, status: "completed" }] },
      { id: "b", label: "same seq second", nodeIds: ["question", "conservation"], status: "complete", branch_id: "branch-b", status_history: [{ seq: 2, status: "completed" }] },
      { id: "a", label: "same seq first", nodeIds: ["question", "constraints"], status: "complete", branch_id: "branch-a", status_history: [{ seq: 2, status: "completed" }] },
    ];
    expect(supplied.map((route) => route.branch_id)).toEqual(["branch-z", "branch-b", "branch-a"]);
  });

  it("uses the first ordered route for a shared node and shared edge", () => {
    const routes = deriveFixtureRoutes(fixtureRun.steps, fixtureRun.edges, fixtureRun.rootStepId);
    expect(firstRouteForNode(routes, "constraints")?.nodeIds.at(-1)).toBe("weak-result");
    expect(firstRouteForEdge(routes, { from: "question", to: "constraints" })?.nodeIds.at(-1)).toBe("weak-result");
  });

  it("selects the matching route for exclusive nodes and edges", () => {
    const routes = deriveFixtureRoutes(fixtureRun.steps, fixtureRun.edges, fixtureRun.rootStepId);
    expect(firstRouteForNode(routes, "counterexample")?.nodeIds.at(-1)).toBe("boundary-result");
    expect(firstRouteForEdge(routes, { from: "symmetry", to: "representation" })?.nodeIds.at(-1)).toBe("symmetry-result");
  });

  it("centers a narrow route within the minimum canvas instead of pinning it to the top-left", () => {
    const steps = fixtureRun.steps.slice(0, 1);
    const layout = layoutTree(steps, [], steps[0].id);

    expect(layout.width).toBe(740);
    expect(layout.height).toBe(480);
    expect(layout.nodes[0].x).toBe(layout.width / 2);
    expect(layout.nodes[0].y).toBe(layout.height / 2);
  });
});

describe("parallel edges from branch-of-branch projections", () => {
  it("keeps one connection per step pair, lowest order first, remembering every merged kind", () => {
    const merged = dedupeEdges(branchOfBranchEdges);

    expect(merged.map((item) => `${item.from}->${item.to}`)).toEqual(["s1->s2", "s2->s3", "s3->s4", "s3->s5"]);
    const shared = merged.find((item) => item.from === "s2" && item.to === "s3");
    expect(shared?.id).toBe("e2");
    expect(shared?.kind).toBe("human_direction");
    expect(shared?.kinds).toEqual(["human_direction", "continuation"]);
  });

  it("lays a branch-of-branch tree out without letting the duplicate edge inflate the canvas", () => {
    const layout = layoutTree(branchOfBranchSteps, branchOfBranchEdges, "s1");

    expect(layout.nodes.map((node) => node.id).sort()).toEqual(["s1", "s2", "s3", "s4", "s5"]);
    expect(layout.nodes.filter((node) => node.id === "s3")).toHaveLength(1);
    expect(layout.nodes.find((node) => node.id === "s3")?.depth).toBe(2);
    // Two leaves (s4, s5), so the placed steps span exactly one horizontal gap.
    const xs = layout.nodes.map((node) => node.x);
    expect(Math.max(...xs) - Math.min(...xs)).toBe(210);
    expect(layout.warnings.multiParentNodeIds).toEqual([]);
  });

  it("counts every distinct descendant once", () => {
    expect(descendantCount(branchOfBranchEdges, "s2")).toBe(3);
    expect(descendantCount(branchOfBranchEdges, "s4")).toBe(0);
    expect(descendantCount(fixtureRun.edges, "conservation")).toBe(4);
  });
});

describe("layoutTree multi-parent handling", () => {
  it("places a step with in-degree > 1 once and reports it as a warning", () => {
    const steps = ["a", "b", "c", "d"].map((id, index) => step(id, index));
    const edges = [
      edge("e1", "a", "b", 0, "continuation"),
      edge("e2", "a", "c", 1, "model_fork"),
      edge("e3", "b", "d", 0, "continuation"),
      edge("e4", "c", "d", 0, "continuation"),
    ];

    const layout = layoutTree(steps, edges, "a");

    expect(layout.nodes.filter((node) => node.id === "d")).toHaveLength(1);
    expect(layout.warnings.multiParentNodeIds).toEqual(["d"]);
    expect(layout.nodes.find((node) => node.id === "d")?.parents).toEqual(["b", "c"]);
    expect(layout.nodes.find((node) => node.id === "b")?.parents).toEqual(["a"]);
    // "d" occupies a single leaf slot, so the whole graph spans no more than one gap.
    const xs = layout.nodes.map((node) => node.x);
    expect(Math.max(...xs) - Math.min(...xs)).toBeLessThanOrEqual(210);
  });
});

describe("layoutTree collapsing", () => {
  it("drops collapsed descendants from the layout and frees their leaf slots", () => {
    const open = layoutTree(fixtureRun.steps, fixtureRun.edges, fixtureRun.rootStepId);
    const folded = layoutTree(fixtureRun.steps, fixtureRun.edges, fixtureRun.rootStepId, {
      collapsed: new Set(["conservation"]),
    });

    expect(open.nodes).toHaveLength(10);
    expect(folded.nodes).toHaveLength(6);
    expect(folded.nodes.map((node) => node.id)).not.toContain("perturbation");
    expect(folded.nodes.map((node) => node.id)).toContain("conservation");

    const span = (nodes: { x: number }[]) => Math.max(...nodes.map((node) => node.x)) - Math.min(...nodes.map((node) => node.x));
    expect(span(open.nodes)).toBe(420);
    expect(span(folded.nodes)).toBe(210);

    expect(folded.hiddenDescendants.get("conservation")).toBe(4);
    expect(open.hiddenDescendants.size).toBe(0);
  });

  it("reports nothing hidden for a collapsed leaf", () => {
    const folded = layoutTree(fixtureRun.steps, fixtureRun.edges, fixtureRun.rootStepId, {
      collapsed: new Set(["weak-result"]),
    });

    expect(folded.nodes).toHaveLength(10);
    expect(folded.hiddenDescendants.has("weak-result")).toBe(false);
  });
});
