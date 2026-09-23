import type { DerivationEdge, DerivationRoute, DerivationStep } from "../types";

/** Fixture-only compatibility for graph samples that predate backend routes. */
export function deriveFixtureRoutes(steps: DerivationStep[], edges: DerivationEdge[], rootStepId: string | null): DerivationRoute[] {
  const stepIds = new Set(steps.map((step) => step.id));
  if (!rootStepId || !stepIds.has(rootStepId)) return [];
  const outgoing = new Map<string, DerivationEdge[]>();
  for (const edge of [...edges].sort((left, right) => left.order - right.order || left.id.localeCompare(right.id))) {
    if (!stepIds.has(edge.from) || !stepIds.has(edge.to)) continue;
    outgoing.set(edge.from, [...(outgoing.get(edge.from) ?? []), edge]);
  }
  const routes: DerivationRoute[] = [];
  const visit = (nodeId: string, path: string[], seen: Set<string>) => {
    if (seen.has(nodeId)) return;
    const nextPath = [...path, nodeId];
    const children = outgoing.get(nodeId) ?? [];
    if (children.length === 0) {
      const tip = steps.find((step) => step.id === nodeId);
      routes.push({
        id: `fixture-route-${routes.length + 1}`,
        label: tip?.title ?? nodeId,
        nodeIds: nextPath,
        status: tip?.status === "proposed" ? "proposed" : tip?.status === "failed" ? "failed" : "complete",
        branch_id: tip?.branch_id ?? `fixture-branch-${routes.length + 1}`,
        status_history: [{ seq: routes.length + 1, status: tip?.status === "failed" ? "killed" : tip?.status === "proposed" ? "parked" : "completed" }],
      });
      return;
    }
    const nextSeen = new Set(seen).add(nodeId);
    for (const edge of children) visit(edge.to, nextPath, nextSeen);
  };
  visit(rootStepId, [], new Set());
  return routes;
}
