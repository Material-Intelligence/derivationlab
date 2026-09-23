import { describe, expect, it } from "vitest";
import { fixtureRun, stressRun } from "../fixtures";
import type { DerivationRoute, DerivationRun } from "../types";
import { forksAlongRoute, outgoingOptions, rememberChoice, rememberRoute, resolveRouteSelection } from "./forks";

const routeOf = (run: DerivationRun, id: string): DerivationRoute =>
  run.routes.find((route) => route.id === id)!;

describe("forksAlongRoute", () => {
  it("finds every fork on the route and marks the direction the route takes", () => {
    const forks = forksAlongRoute(fixtureRun, routeOf(fixtureRun, "route-a"));

    expect(forks.map((fork) => fork.stepId)).toEqual(["constraints", "conservation"]);
    expect(forks[0].options.map((option) => option.toStepId)).toEqual(["conservation", "symmetry"]);
    expect(forks[0].options.map((option) => option.isCurrent)).toEqual([true, false]);
    expect(forks[0].options.map((option) => option.route?.id)).toEqual(["route-a", "route-c"]);
    expect(forks[1].options.map((option) => option.route?.id)).toEqual(["route-a", "route-b"]);
  });

  it("offers the route that shares the prefix, not merely the first route through the edge", () => {
    const forks = forksAlongRoute(fixtureRun, routeOf(fixtureRun, "route-b"));
    const atConstraints = forks.find((fork) => fork.stepId === "constraints")!;

    // Reading route B, "keep going through Conservation-law route" must stay on route B.
    expect(atConstraints.options[0].route?.id).toBe("route-b");
    expect(atConstraints.options[0].isCurrent).toBe(true);
  });

  it("de-duplicates parallel edges and keeps the strongest kind", () => {
    const options = outgoingOptions(stressRun, "s-hamiltonian");

    expect(stressRun.edges.filter((edge) => edge.from === "s-hamiltonian")).toHaveLength(6);
    expect(options.map((option) => option.to)).toEqual([
      "s-ladder",
      "s-wkb",
      "s-series",
      "s-classical",
      "s-grid",
    ]);
    const series = options.find((option) => option.to === "s-series")!;
    expect(series.kind).toBe("human_direction");
    expect(series.droppedKinds).toEqual(["model_fork"]);
  });

  it("groups a human_revision as another version of the step, not another direction", () => {
    const forks = forksAlongRoute(stressRun, routeOf(stressRun, "sroute-thermo"));
    const atMeanEnergy = forks.find((fork) => fork.stepId === "s-mean-energy")!;

    expect(atMeanEnergy.continuations.map((option) => option.toStepId)).toEqual(["s-result-b"]);
    expect(atMeanEnergy.revisions.map((option) => option.toStepId)).toEqual(["s-mean-energy-v2"]);
    expect(atMeanEnergy.revisions[0].route?.id).toBe("sroute-thermo-v2");
  });

  it("carries the route status of every direction, including failed and proposed", () => {
    const forks = forksAlongRoute(stressRun, routeOf(stressRun, "sroute-levels"));
    const atHamiltonian = forks.find((fork) => fork.stepId === "s-hamiltonian")!;

    expect(atHamiltonian.options).toHaveLength(5);
    expect(atHamiltonian.options.map((option) => option.routeStatus)).toEqual([
      "complete",
      "complete",
      "active",
      "failed",
      "proposed",
    ]);
  });

  it("never offers a proposed edge as a direction", () => {
    const run: DerivationRun = {
      ...fixtureRun,
      edges: [
        ...fixtureRun.edges,
        { id: "e-proposed", from: "conservation", to: "symmetry", order: 9, kind: "proposed" },
      ],
    };

    expect(outgoingOptions(run, "conservation").map((option) => option.to)).toEqual([
      "perturbation",
      "counterexample",
    ]);
  });
});

describe("resolveRouteSelection", () => {
  const routes = fixtureRun.routes;

  it("keeps the route being read when a shared upstream step is clicked", () => {
    const routeC = routeOf(fixtureRun, "route-c");

    expect(resolveRouteSelection(routes, { clickedNodeId: "question" }, routeC)?.id).toBe("route-c");
    expect(resolveRouteSelection(routes, { clickedNodeId: "constraints" }, routeC)?.id).toBe("route-c");
  });

  it("falls back to the stable order when nothing is being read", () => {
    expect(resolveRouteSelection(routes, { clickedNodeId: "question" }, undefined)?.id).toBe("route-a");
  });

  it("honours a remembered fork choice once the prefix no longer decides", () => {
    const routeC = routeOf(fixtureRun, "route-c");

    // Conservation-law route is off route C, so the prefix rule cannot pick; memory can.
    expect(resolveRouteSelection(routes, { clickedNodeId: "conservation" }, routeC)?.id).toBe("route-a");
    expect(
      resolveRouteSelection(routes, { clickedNodeId: "conservation" }, routeC, { conservation: "route-b" })?.id,
    ).toBe("route-b");
  });

  it("resolves an edge by the hop it represents", () => {
    expect(
      resolveRouteSelection(routes, { clickedEdge: { from: "symmetry", to: "representation" } }, routeOf(fixtureRun, "route-a"))?.id,
    ).toBe("route-c");
  });

  it("returns undefined when no route passes through the clicked object", () => {
    expect(resolveRouteSelection(routes, { clickedNodeId: "not-a-step" }, undefined)).toBeUndefined();
  });
});

describe("fork memory", () => {
  it("records one choice without touching the others", () => {
    const first = rememberChoice({}, "constraints", "route-c");
    const second = rememberChoice(first, "conservation", "route-b");

    expect(first).toEqual({ constraints: "route-c" });
    expect(second).toEqual({ constraints: "route-c", conservation: "route-b" });
    expect(first).not.toBe(second);
  });

  it("records a whole route at every fork it passes", () => {
    expect(rememberRoute(fixtureRun, routeOf(fixtureRun, "route-b"))).toEqual({
      constraints: "route-b",
      conservation: "route-b",
    });
    expect(rememberRoute(fixtureRun, routeOf(fixtureRun, "route-c"))).toEqual({ constraints: "route-c" });
  });
});
