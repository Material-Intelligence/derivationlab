/**
 * Fork resolution for the paper-style reading desk.
 *
 * The backend `RouteView` stays the authority: a route owns an id, a label, a
 * status and the full `root → tip` node sequence, and `Export PDF` is bound to
 * `selectedRouteId`. This module never invents a parallel path model; it only
 * answers two questions on top of the contract:
 *
 *   1. `forksAlongRoute` — where does the currently read route branch, and which
 *      other route would the reader land on if they took a different direction?
 *   2. `resolveRouteSelection` — the reader clicked a node or an edge; which
 *      route should the desk switch to?
 *
 * The old rule was `routes.find(r => r.nodeIds.includes(nodeId))`, i.e. "the
 * first matching route in document order". On a shared trunk (demo-run shares
 * `question → constraints` across all three routes) that silently drags a reader
 * of route C back to route A as soon as they click an upstream step. The rules
 * below replace it with: keep what the reader already chose downstream, then
 * honour remembered fork choices, and only then fall back to a stable order.
 */
import type { DerivationEdge, DerivationRoute, DerivationRun } from "../types";

/** `remembered[forkStepId] = routeId` — which direction the reader took at a fork. */
export type RememberedForks = Readonly<Record<string, string>>;

/** The read-only projection of a `RunView` this module needs. */
export type ForkGraph = Pick<DerivationRun, "steps" | "edges" | "routes">;

export interface ForkOption {
  /** Edge id of the surviving edge after `(from, to)` de-duplication. */
  edgeId: string;
  toStepId: string;
  /** Strongest kind among the parallel edges that share this `(from, to)` pair. */
  kind: DerivationEdge["kind"];
  /** Kinds dropped by de-duplication, kept so the UI can explain itself if needed. */
  droppedKinds: DerivationEdge["kind"][];
  /** Route the reader lands on when taking this direction, when one exists. */
  route?: DerivationRoute;
  routeStatus?: DerivationRoute["status"];
  /** True when the route currently being read takes this direction. */
  isCurrent: boolean;
}

export interface RouteFork {
  stepId: string;
  /** Position of the fork step inside `route.nodeIds`. */
  index: number;
  /** Every realized direction out of this step, de-duplicated and ordered. */
  options: ForkOption[];
  /** `continuation | model_fork | human_direction` — parallel next directions. */
  continuations: ForkOption[];
  /** `human_revision | model_revision` — another version of *this* step, not another next step. */
  revisions: ForkOption[];
}

export interface RouteSelectionTarget {
  clickedNodeId?: string;
  clickedEdge?: Pick<DerivationEdge, "from" | "to">;
}

/**
 * `proposed` never enters the reading model: the backend never emits proposed
 * edges, so they are not part of the real derivation tree.
 */
const KIND_PRIORITY: Record<DerivationEdge["kind"], number> = {
  human_revision: 4,
  model_revision: 3,
  human_direction: 2,
  model_fork: 1,
  continuation: 0,
  proposed: -1,
};

/** Edge kinds that replace a step rather than continue from it. */
const REVISION_KINDS = new Set<DerivationEdge["kind"]>(["human_revision", "model_revision"]);

function isRealized(edge: DerivationEdge): boolean {
  return edge.kind !== "proposed";
}

function strongerKind(left: DerivationEdge["kind"], right: DerivationEdge["kind"]): DerivationEdge["kind"] {
  return KIND_PRIORITY[left] >= KIND_PRIORITY[right] ? left : right;
}

interface MergedEdge {
  edgeId: string;
  from: string;
  to: string;
  order: number;
  kind: DerivationEdge["kind"];
  droppedKinds: DerivationEdge["kind"][];
}

/**
 * Outgoing edges of a step, de-duplicated by `(from, to)`.
 *
 * `projection.py` can emit parallel edges for the same pair (a `continuation`
 * plus a `human_direction`, say). Rendering both would show a two-way fork as
 * three directions, two of which lead to the same step.
 */
export function outgoingOptions(graph: ForkGraph, stepId: string): MergedEdge[] {
  const known = new Set(graph.steps.map((step) => step.id));
  const merged = new Map<string, MergedEdge>();
  const ordered = [...graph.edges].sort((a, b) => a.order - b.order || a.id.localeCompare(b.id));
  for (const edge of ordered) {
    if (edge.from !== stepId || !isRealized(edge) || !known.has(edge.to)) continue;
    const existing = merged.get(edge.to);
    if (!existing) {
      merged.set(edge.to, { edgeId: edge.id, from: edge.from, to: edge.to, order: edge.order, kind: edge.kind, droppedKinds: [] });
      continue;
    }
    const winner = strongerKind(existing.kind, edge.kind);
    existing.droppedKinds.push(winner === existing.kind ? edge.kind : existing.kind);
    existing.kind = winner;
  }
  return [...merged.values()];
}

function takesEdge(route: DerivationRoute, from: string, to: string): boolean {
  return route.nodeIds.some((nodeId, index) => nodeId === from && route.nodeIds[index + 1] === to);
}

function firstStatusSeq(route: DerivationRoute): number {
  return route.status_history[0]?.seq ?? Number.MAX_SAFE_INTEGER;
}

/**
 * The pre-existing stable order, made explicit: oldest `status_history[0].seq`
 * first, then `branch_id`, then `id`. Used only as the last resort so the desk
 * never ends up without a route (Export PDF depends on one existing).
 */
function stableOrder(routes: readonly DerivationRoute[]): DerivationRoute[] {
  return [...routes].sort(
    (a, b) => firstStatusSeq(a) - firstStatusSeq(b) || a.branch_id.localeCompare(b.branch_id) || a.id.localeCompare(b.id),
  );
}

/** Does `candidate` agree with `reference` on every step up to and including `index`? */
function sharesPrefix(candidate: DerivationRoute, reference: DerivationRoute, index: number): boolean {
  if (index < 0 || candidate.nodeIds.length <= index || reference.nodeIds.length <= index) return false;
  for (let cursor = 0; cursor <= index; cursor += 1) {
    if (candidate.nodeIds[cursor] !== reference.nodeIds[cursor]) return false;
  }
  return true;
}

function sharedPrefixLength(candidate: DerivationRoute, reference: DerivationRoute): number {
  let length = 0;
  while (
    length < candidate.nodeIds.length &&
    length < reference.nodeIds.length &&
    candidate.nodeIds[length] === reference.nodeIds[length]
  ) length += 1;
  return length;
}

/**
 * How many remembered fork choices a candidate honours. A remembered entry is
 * honoured when the candidate leaves the fork step through the same successor as
 * the remembered route did — comparing successors rather than route ids keeps
 * the memory useful after a deeper fork sends the reader onto a sibling route.
 */
function rememberedScore(candidate: DerivationRoute, routes: readonly DerivationRoute[], remembered: RememberedForks): number {
  let score = 0;
  for (const [forkStepId, routeId] of Object.entries(remembered)) {
    const index = candidate.nodeIds.indexOf(forkStepId);
    if (index === -1) continue;
    if (candidate.id === routeId) { score += 1; continue; }
    const rememberedRoute = routes.find((route) => route.id === routeId);
    if (!rememberedRoute) continue;
    const rememberedIndex = rememberedRoute.nodeIds.indexOf(forkStepId);
    if (rememberedIndex === -1) continue;
    if (rememberedRoute.nodeIds[rememberedIndex + 1] === candidate.nodeIds[index + 1]) score += 1;
  }
  return score;
}

function bestByMemory(
  candidates: readonly DerivationRoute[],
  routes: readonly DerivationRoute[],
  remembered: RememberedForks,
): DerivationRoute | undefined {
  if (Object.keys(remembered).length === 0) return undefined;
  let best: DerivationRoute | undefined;
  let bestScore = 0;
  for (const candidate of stableOrder(candidates)) {
    const score = rememberedScore(candidate, routes, remembered);
    if (score > bestScore) { best = candidate; bestScore = score; }
  }
  return best;
}

/** Index of the step the click ultimately points at, inside `route`. */
function anchorIndex(route: DerivationRoute, target: RouteSelectionTarget): number {
  if (target.clickedEdge) {
    const { from, to } = target.clickedEdge;
    const index = route.nodeIds.findIndex((nodeId, cursor) => nodeId === from && route.nodeIds[cursor + 1] === to);
    return index === -1 ? -1 : index + 1;
  }
  if (target.clickedNodeId) return route.nodeIds.indexOf(target.clickedNodeId);
  return -1;
}

/**
 * Pick the route a click should switch to.
 *
 * Order of preference, most specific first:
 *   1. a route that passes through the clicked object **and** agrees with the
 *      route currently being read on every step up to it — the current route
 *      itself wins outright, so clicking an upstream step never rewrites a
 *      downstream choice;
 *   2. among several such routes, the one honouring the most remembered fork
 *      choices, then the one sharing the longest prefix with the current route;
 *   3. otherwise the same rules applied to every route through the object;
 *   4. finally the stable order (`status_history[0].seq`, then `branch_id`).
 *
 * Returns `undefined` only when no route passes through the clicked object; the
 * caller should then keep the route it already had.
 */
export function resolveRouteSelection(
  routes: readonly DerivationRoute[],
  target: RouteSelectionTarget,
  currentRoute?: DerivationRoute,
  remembered: RememberedForks = {},
): DerivationRoute | undefined {
  const candidates = routes.filter((route) => anchorIndex(route, target) !== -1);
  if (candidates.length === 0) return undefined;
  if (candidates.length === 1) return candidates[0];

  if (currentRoute) {
    const continuous = candidates.filter((candidate) =>
      sharesPrefix(candidate, currentRoute, anchorIndex(candidate, target)),
    );
    if (continuous.some((candidate) => candidate.id === currentRoute.id)) return currentRoute;
    if (continuous.length > 0) {
      const remembered_ = bestByMemory(continuous, routes, remembered);
      if (remembered_) return remembered_;
      return [...continuous].sort(
        (a, b) =>
          sharedPrefixLength(b, currentRoute) - sharedPrefixLength(a, currentRoute) ||
          firstStatusSeq(a) - firstStatusSeq(b) ||
          a.branch_id.localeCompare(b.branch_id) ||
          a.id.localeCompare(b.id),
      )[0];
    }
  }

  return bestByMemory(candidates, routes, remembered) ?? stableOrder(candidates)[0];
}

/** Route to offer for one direction out of a fork on `route` at `index`. */
function routeForOption(
  graph: ForkGraph,
  route: DerivationRoute,
  index: number,
  option: MergedEdge,
  remembered: RememberedForks,
): DerivationRoute | undefined {
  // The direction the reader is already on always resolves to the route being read.
  if (takesEdge(route, option.from, option.to)) return route;
  const through = graph.routes.filter((candidate) => takesEdge(candidate, option.from, option.to));
  if (through.length === 0) return undefined;
  const continuous = through.filter((candidate) => sharesPrefix(candidate, route, index));
  const pool = continuous.length > 0 ? continuous : through;
  return (
    bestByMemory(pool, graph.routes, remembered) ??
    [...pool].sort(
      (a, b) =>
        sharedPrefixLength(b, route) - sharedPrefixLength(a, route) ||
        firstStatusSeq(a) - firstStatusSeq(b) ||
        a.branch_id.localeCompare(b.branch_id) ||
        a.id.localeCompare(b.id),
    )[0]
  );
}

/**
 * Every step on `route` that offers more than one realized direction.
 *
 * Revision edges (`human_revision`, and the runtime's own `model_revision`) are
 * grouped separately: a revision is another version
 * of the same step, not a parallel next step, and listing it under
 * "continue from here" would misrepresent the record.
 */
export function forksAlongRoute(graph: ForkGraph, route: DerivationRoute, remembered: RememberedForks = {}): RouteFork[] {
  // A single-route run has no fork to choose, and offering directions that
  // resolve to no route is noise.
  if (graph.routes.length < 2) return [];
  const forks: RouteFork[] = [];
  route.nodeIds.forEach((stepId, index) => {
    const outgoing = outgoingOptions(graph, stepId);
    if (outgoing.length < 2) return;
    const options: ForkOption[] = outgoing.map((edge) => {
      const optionRoute = routeForOption(graph, route, index, edge, remembered);
      return {
        edgeId: edge.edgeId,
        toStepId: edge.to,
        kind: edge.kind,
        droppedKinds: edge.droppedKinds,
        route: optionRoute,
        routeStatus: optionRoute?.status,
        isCurrent: route.nodeIds[index + 1] === edge.to,
      };
    });
    forks.push({
      stepId,
      index,
      options,
      continuations: options.filter((option) => !REVISION_KINDS.has(option.kind)),
      revisions: options.filter((option) => REVISION_KINDS.has(option.kind)),
    });
  });
  return forks;
}

/** Record that the reader took `routeId` at `forkStepId`. Returns a new map. */
export function rememberChoice(remembered: RememberedForks, forkStepId: string, routeId: string): Record<string, string> {
  if (remembered[forkStepId] === routeId) return { ...remembered };
  return { ...remembered, [forkStepId]: routeId };
}

/**
 * Record a whole route as the choice at every fork it passes, so that a later
 * click on a shared upstream step keeps the reader on it.
 */
export function rememberRoute(graph: ForkGraph, route: DerivationRoute, remembered: RememberedForks = {}): Record<string, string> {
  let next: Record<string, string> = { ...remembered };
  for (const fork of forksAlongRoute(graph, route, remembered)) {
    next = rememberChoice(next, fork.stepId, route.id);
  }
  return next;
}
