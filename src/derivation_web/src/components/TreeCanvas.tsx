import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { dedupeEdges, layoutTree, type MergedEdge } from "../domain";
import { useLocale } from "../i18n";
import type { DerivationEdge, DerivationRoute, DerivationRun, DerivationStep } from "../types";
import { ContextMenu, type ContextMenuAction, type ContextMenuState } from "./ContextMenu";
import { scientificTextPlainTitle } from "./ScientificText";
import { treeMessages, type LiveRole } from "./treeCanvasMessages";
import { roleOf } from "../live";
import "./TreeCanvas.css";

interface ViewBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface CanvasSize {
  width: number;
  height: number;
}

/**
 * One model call the runtime reports as running right now.
 *
 * Declared structurally instead of importing the generated `ActiveCallOverlay`, so the shape the
 * map consumes stays stable when the overlay contract grows (`role`, `startedAt`, `preview`).
 */
export interface TreeActiveCall {
  callId: string;
  branchId: string;
  fromStepId: string;
  label: string;
  role?: string;
}

interface TreeCanvasProps {
  run: DerivationRun;
  routes: DerivationRoute[];
  selectedRouteId?: string;
  selectedNodeId?: string;
  onSelectNode: (nodeId: string) => void;
  onSelectEdge: (edge: DerivationEdge) => void;
  onSelectRoute: (routeId: string) => void;
  onOpenDetails: (nodeId: string) => void;
  onOpenBranch: (nodeId: string) => void;
  onCopyText: (value: string) => void | Promise<void>;
  canBranchNode: (nodeId: string) => boolean;
  /** Calls in flight. A step is only in `run.steps` once sealed, so these have no node yet. */
  activeCalls?: readonly TreeActiveCall[];
}

const NODE_WIDTH = 176;
const NODE_HEIGHT = 84;
const NODE_TITLE_MAX_UNITS = 23;
const OVERVIEW_TITLE_MAX_UNITS = 14;
const TOGGLE_RADIUS = 9;
/** Ghost placeholder for a call that has not sealed a step yet. */
const GHOST_WIDTH = NODE_WIDTH;
const GHOST_HEIGHT = 44;
/** Vertical offset of the first ghost below its parent; further ghosts stack by NODE_HEIGHT + 8. */
const GHOST_GAP = 26;
/** Extra fit height so `fit` keeps the deepest ghost on screen. */
const GHOST_FIT_RESERVE = NODE_HEIGHT + GHOST_GAP + 16;
/** A node narrower than this on screen is no longer readable, so `fit` stops shrinking there. */
const MIN_NODE_SCREEN_WIDTH = 88;
export const MIN_FIT_SCALE = MIN_NODE_SCREEN_WIDTH / NODE_WIDTH;
/** Below this scale the map switches to a title-only overview. */
export const OVERVIEW_SCALE = 0.45;
/** Off-route steps at or below this depth start collapsed. */
const AUTO_COLLAPSE_MIN_DEPTH = 3;

function glyphUnits(character: string): number {
  if (/\s/u.test(character)) return 0.6;
  if (/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(character)) return 2;
  if (/[A-Z0-9]/u.test(character)) return 1.15;
  return 1;
}

function lineUnits(value: string): number {
  return Array.from(value).reduce((total, character) => total + glyphUnits(character), 0);
}

function withEllipsis(value: string, maxUnits: number = NODE_TITLE_MAX_UNITS): string {
  let characters = Array.from(value.trimEnd());
  while (characters.length > 0 && lineUnits(`${characters.join("")}…`) > maxUnits) {
    characters = characters.slice(0, -1);
  }
  return `${characters.join("").trimEnd()}…`;
}

export function wrapNodeTitle(title: string): string[] {
  const normalized = title.replace(/\s+/gu, " ").trim();
  if (!normalized) return [""];
  const lines: string[] = [];
  let current = "";

  for (const character of Array.from(normalized)) {
    if (current && lineUnits(`${current}${character}`) > NODE_TITLE_MAX_UNITS) {
      // Break on a space so neither an English word nor a readable formula token
      // (`Z(β)=Σ_ne^{−βħω(n+½)}…`) is split down the middle; a single token wider than
      // the line still breaks hard, because there is nowhere else to break it.
      const breakAt = current.lastIndexOf(" ");
      if (breakAt > 0) {
        lines.push(current.slice(0, breakAt).trimEnd());
        current = `${current.slice(breakAt + 1)}${character}`;
      } else {
        lines.push(current.trimEnd());
        current = character.trimStart();
      }
    } else {
      current += character;
    }
  }
  if (current || lines.length === 0) lines.push(current.trimEnd());
  if (lines.length <= 2) return lines;
  return [lines[0], withEllipsis(lines.slice(1).join(" "))];
}

/** One larger line for the low-zoom overview, where meta text is hidden anyway. */
export function overviewNodeTitle(title: string): string {
  const normalized = title.replace(/\s+/gu, " ").trim();
  if (lineUnits(normalized) <= OVERVIEW_TITLE_MAX_UNITS) return normalized;
  return withEllipsis(normalized, OVERVIEW_TITLE_MAX_UNITS);
}

function edgePath(from: { x: number; y: number }, to: { x: number; y: number }): string {
  const startY = from.y + NODE_HEIGHT / 2;
  const endY = to.y - NODE_HEIGHT / 2;
  const middleY = startY + (endY - startY) * 0.52;
  return `M ${from.x} ${startY} C ${from.x} ${middleY}, ${to.x} ${middleY}, ${to.x} ${endY}`;
}

/**
 * `fit` with a floor: an SVG with `preserveAspectRatio="xMidYMid meet"` renders at
 * `min(elementWidth / viewBoxWidth, elementHeight / viewBoxHeight)`, so a viewBox matching the
 * element's aspect ratio pins that scale exactly. Below MIN_FIT_SCALE the whole map no longer fits;
 * the caller keeps panning instead of shrinking further and says so in the panel heading.
 *
 * `extraHeight` reserves room below the layout for ghost placeholders, which hang under sealed
 * nodes and are therefore outside `layoutTree`'s box. It only changes what "fit" computes; the
 * viewport itself is never reset from here.
 */
export function fitViewBox(
  layout: { width: number; height: number },
  size: CanvasSize | null,
  extraHeight = 0,
): { box: ViewBox; clamped: boolean } {
  const fitHeight = layout.height + Math.max(0, extraHeight);
  const box: ViewBox = { x: 0, y: 0, width: layout.width, height: fitHeight };
  if (!size || size.width < 1 || size.height < 1) return { box, clamped: false };
  const scale = Math.min(size.width / layout.width, size.height / fitHeight);
  if (scale >= MIN_FIT_SCALE) return { box, clamped: false };
  const width = size.width / MIN_FIT_SCALE;
  const height = size.height / MIN_FIT_SCALE;
  return {
    box: { x: layout.width / 2 - width / 2, y: fitHeight / 2 - height / 2, width, height },
    clamped: true,
  };
}

export function viewScale(viewBox: ViewBox, size: CanvasSize | null): number | null {
  if (!size || size.width < 1 || size.height < 1) return null;
  return Math.min(size.width / viewBox.width, size.height / viewBox.height);
}

function childrenOf(edges: MergedEdge[]): Map<string, string[]> {
  const map = new Map<string, string[]>();
  for (const edge of edges) {
    const group = map.get(edge.from) ?? [];
    group.push(edge.to);
    map.set(edge.from, group);
  }
  return map;
}

function parentsOf(edges: MergedEdge[]): Map<string, string[]> {
  const map = new Map<string, string[]>();
  for (const edge of edges) {
    const group = map.get(edge.to) ?? [];
    group.push(edge.from);
    map.set(edge.to, group);
  }
  return map;
}

function depthOf(children: Map<string, string[]>, steps: DerivationStep[], rootStepId: string | null): Map<string, number> {
  const depths = new Map<string, number>();
  const walk = (id: string, depth: number) => {
    if (depths.has(id)) return;
    depths.set(id, depth);
    for (const child of children.get(id) ?? []) walk(child, depth + 1);
  };
  if (rootStepId) walk(rootStepId, 0);
  for (const step of steps) walk(step.id, 0);
  return depths;
}

/** Every step above `nodeId`, following all parents (a step can have in-degree > 1). */
function ancestorsOf(parents: Map<string, string[]>, nodeId: string): string[] {
  const seen = new Set<string>();
  const queue = [...(parents.get(nodeId) ?? [])];
  while (queue.length > 0) {
    const current = queue.shift() as string;
    if (seen.has(current)) continue;
    seen.add(current);
    queue.push(...(parents.get(current) ?? []));
  }
  return [...seen];
}

/**
 * Default view: the highlighted route is fully expanded, every other branch that starts at
 * AUTO_COLLAPSE_MIN_DEPTH or deeper is folded away behind a "+" and a hidden-step count.
 */
export function defaultCollapsedNodes(
  steps: DerivationStep[],
  edges: MergedEdge[],
  rootStepId: string | null,
  onRoute: ReadonlySet<string>,
): Set<string> {
  const children = childrenOf(edges);
  const depths = depthOf(children, steps, rootStepId);
  const collapsed = new Set<string>();
  for (const step of steps) {
    if (onRoute.has(step.id)) continue;
    if ((children.get(step.id)?.length ?? 0) === 0) continue;
    if ((depths.get(step.id) ?? 0) < AUTO_COLLAPSE_MIN_DEPTH) continue;
    collapsed.add(step.id);
  }
  return collapsed;
}

export function TreeCanvas({ run, routes, selectedRouteId, selectedNodeId, onSelectNode, onSelectEdge, onSelectRoute, onOpenDetails, onOpenBranch, onCopyText, canBranchNode, activeCalls }: TreeCanvasProps) {
  const { messages: m, locale } = useLocale();
  const tm = useMemo(() => treeMessages(locale), [locale]);

  const edges = useMemo(() => dedupeEdges(run.edges), [run.edges]);
  const children = useMemo(() => childrenOf(edges), [edges]);
  const parents = useMemo(() => parentsOf(edges), [edges]);
  const selectedRoute = useMemo(() => routes.find((route) => route.id === selectedRouteId), [routes, selectedRouteId]);
  const routeNodes = useMemo(() => new Set(selectedRoute?.nodeIds ?? []), [selectedRoute]);
  const routeEdges = useMemo(() => new Set(
    selectedRoute?.nodeIds.slice(0, -1).map((nodeId, index) => `${nodeId}->${selectedRoute.nodeIds[index + 1]}`) ?? [],
  ), [selectedRoute]);

  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(
    () => defaultCollapsedNodes(run.steps, edges, run.rootStepId, routeNodes),
  );

  const layout = useMemo(
    () => layoutTree(run.steps, run.edges, run.rootStepId, { collapsed }),
    [run.steps, run.edges, run.rootStepId, collapsed],
  );
  const byId = useMemo(() => new Map(layout.nodes.map((node) => [node.id, node])), [layout]);

  /**
   * Placeholders for the steps being written right now.
   *
   * A running call has no node in `run.steps` (a step enters the record only once sealed), so the
   * only anchor is `fromStepId`. A call whose anchor is folded away or absent from the layout is
   * dropped rather than positioned by guesswork -- `layoutTree` stays untouched by this feature.
   */
  const ghosts = useMemo(() => {
    if (!activeCalls || activeCalls.length === 0) return [];
    const perParent = new Map<string, number>();
    const placed: {
      callId: string;
      fromStepId: string;
      role: LiveRole;
      parentTitle: string;
      x: number;
      y: number;
      linkFromY: number;
    }[] = [];
    for (const call of activeCalls) {
      const parent = byId.get(call.fromStepId);
      if (!parent) {
        // Before the first step is sealed the call hangs off the task itself: show it at the
        // top of the empty canvas so the map is never blank while the model is working.
        if (call.fromStepId.startsWith("task_") && layout.nodes.length === 0) {
          const index = perParent.get(call.fromStepId) ?? 0;
          perParent.set(call.fromStepId, index + 1);
          const y = GHOST_GAP + GHOST_HEIGHT / 2 + index * (NODE_HEIGHT + 8);
          placed.push({
            callId: call.callId,
            fromStepId: call.fromStepId,
            role: roleOf(call),
            parentTitle: tm.rootTask,
            x: layout.width / 2,
            y,
            linkFromY: y - GHOST_HEIGHT / 2,
          });
        }
        continue;
      }
      if (collapsed.has(call.fromStepId)) continue;
      const index = perParent.get(call.fromStepId) ?? 0;
      perParent.set(call.fromStepId, index + 1);
      const y = parent.y + NODE_HEIGHT + GHOST_GAP + index * (NODE_HEIGHT + 8);
      placed.push({
        callId: call.callId,
        fromStepId: call.fromStepId,
        role: roleOf(call),
        parentTitle: scientificTextPlainTitle(parent.title, 10_000),
        x: parent.x,
        y,
        linkFromY: index === 0
          ? parent.y + NODE_HEIGHT / 2
          : y - (NODE_HEIGHT + 8) + GHOST_HEIGHT / 2,
      });
    }
    return placed;
  }, [activeCalls, byId, collapsed, layout.nodes.length, layout.width, tm.rootTask]);

  const [size, setSize] = useState<CanvasSize | null>(null);
  const [viewBox, setViewBox] = useState<ViewBox>(() => ({ x: 0, y: 0, width: layout.width, height: layout.height }));
  const [menu, setMenu] = useState<ContextMenuState | null>(null);
  const drag = useRef<{ x: number; y: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const fit = useMemo(() => fitViewBox(layout, size, ghosts.length > 0 ? GHOST_FIT_RESERVE : 0), [ghosts.length, layout, size]);
  const scale = viewScale(viewBox, size);
  const isOverview = scale !== null && scale < OVERVIEW_SCALE;

  // Snapshots the view resets need, so that those effects depend on identity-stable values only and
  // an SSE snapshot (new `run.steps`, same `run.id`) never drags the user's viewport back to fit.
  const latest = useMemo(
    () => ({ fit, steps: run.steps, edges, rootStepId: run.rootStepId, routeNodes, parents }),
    [edges, fit, parents, routeNodes, run.rootStepId, run.steps],
  );
  const latestRef = useRef(latest);
  useEffect(() => { latestRef.current = latest; }, [latest]);

  useEffect(() => {
    const element = svgRef.current;
    if (!element) return;
    const measure = () => {
      const rect = element.getBoundingClientRect();
      if (rect.width < 1 || rect.height < 1) return;
      setSize((current) => (
        current && current.width === rect.width && current.height === rect.height
          ? current
          : { width: rect.width, height: rect.height }
      ));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const initialFitApplied = useRef(false);
  useEffect(() => {
    if (initialFitApplied.current || !size) return;
    initialFitApplied.current = true;
    setViewBox(fit.box);
  }, [fit, size]);

  const lastRunId = useRef(run.id);
  useEffect(() => {
    if (lastRunId.current === run.id) return;
    lastRunId.current = run.id;
    const snapshot = latestRef.current;
    setCollapsed(defaultCollapsedNodes(snapshot.steps, snapshot.edges, snapshot.rootStepId, snapshot.routeNodes));
    setViewBox(snapshot.fit.box);
  }, [run.id]);

  // Selecting a step or a route reveals it; nodes the user expanded elsewhere stay expanded, and a
  // manually collapsed ancestor stays collapsed until the selection actually moves.
  useEffect(() => {
    const snapshot = latestRef.current;
    const reveal = new Set<string>(snapshot.routeNodes);
    if (selectedNodeId) for (const ancestor of ancestorsOf(snapshot.parents, selectedNodeId)) reveal.add(ancestor);
    setCollapsed((current) => {
      let changed = false;
      const next = new Set(current);
      for (const id of reveal) changed = next.delete(id) || changed;
      return changed ? next : current;
    });
  }, [selectedNodeId, selectedRouteId]);

  const toggleNode = useCallback((nodeId: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (!next.delete(nodeId)) next.add(nodeId);
      return next;
    });
  }, []);

  const expandAll = useCallback(() => setCollapsed(new Set<string>()), []);
  const collapseBranches = useCallback(() => {
    const snapshot = latestRef.current;
    setCollapsed(defaultCollapsedNodes(snapshot.steps, snapshot.edges, snapshot.rootStepId, snapshot.routeNodes));
  }, []);

  // Collapsed steps that hide the current selection are labelled instead of stealing it.
  const holdsSelection = useMemo(() => {
    if (!selectedNodeId) return new Set<string>();
    return new Set(ancestorsOf(parents, selectedNodeId).filter((id) => collapsed.has(id)));
  }, [collapsed, parents, selectedNodeId]);

  const zoom = (factor: number) => {
    setViewBox((current) => {
      const width = Math.min(layout.width * 2, Math.max(280, current.width * factor));
      const height = Math.min(layout.height * 2, Math.max(220, current.height * factor));
      return {
        x: current.x + (current.width - width) / 2,
        y: current.y + (current.height - height) / 2,
        width,
        height,
      };
    });
  };

  const closeMenu = useCallback(() => setMenu(null), []);

  const centerNode = useCallback((nodeId: string) => {
    const node = byId.get(nodeId);
    if (!node) return;
    setViewBox((current) => ({ ...current, x: node.x - current.width / 2, y: node.y - current.height / 2 }));
  }, [byId]);

  const applyFit = useCallback(() => setViewBox(fit.box), [fit]);

  const resetZoom = useCallback(() => {
    setViewBox((current) => ({
      x: current.x + (current.width - fit.box.width) / 2,
      y: current.y + (current.height - fit.box.height) / 2,
      width: fit.box.width,
      height: fit.box.height,
    }));
  }, [fit]);

  const openNodeMenu = useCallback((nodeId: string, x: number, y: number, restoreFocus?: HTMLElement | SVGElement | null) => {
    const step = run.steps.find((candidate) => candidate.id === nodeId);
    if (!step) return;
    const title = scientificTextPlainTitle(step.title, 10_000);
    const route = routes.find((candidate) => candidate.nodeIds.includes(nodeId));
    const actions: ContextMenuAction[] = [
      { id: "open", label: m.openInReader, icon: "open", onSelect: () => onSelectNode(nodeId) },
      { id: "details", label: m.viewDetails, icon: "details", onSelect: () => onOpenDetails(nodeId) },
      { id: "center", label: m.centerNode, icon: "center", onSelect: () => centerNode(nodeId) },
      { id: "route", label: m.highlightRoute, icon: "route", disabled: !route, onSelect: () => route && onSelectRoute(route.id) },
      { id: "branch", label: m.branchHere, icon: "branch", disabled: !canBranchNode(nodeId), separatorBefore: true, onSelect: () => onOpenBranch(nodeId) },
      { id: "copy-title", label: m.copyTitle, icon: "copy", separatorBefore: true, onSelect: () => onCopyText(title) },
      { id: "copy-id", label: m.copyNodeId, icon: "copy", onSelect: () => onCopyText(nodeId) },
    ];
    setMenu({ x, y, label: `${m.moreRouteStepActions}: ${title}`, actions, restoreFocus });
  }, [canBranchNode, centerNode, m, onCopyText, onOpenBranch, onOpenDetails, onSelectNode, onSelectRoute, routes, run.steps]);

  const openEdgeMenu = useCallback((edge: DerivationEdge, x: number, y: number, restoreFocus?: HTMLElement | SVGElement | null) => {
    const from = byId.get(edge.from);
    const to = byId.get(edge.to);
    if (!from || !to) return;
    const fromLabel = scientificTextPlainTitle(from.title, 10_000);
    const toLabel = scientificTextPlainTitle(to.title, 10_000);
    const actions: ContextMenuAction[] = [
      { id: "select", label: m.selectMatchingRoute, icon: "route", onSelect: () => onSelectEdge(edge) },
      { id: "from", label: m.locateStart, icon: "center", onSelect: () => { onSelectNode(edge.from); centerNode(edge.from); } },
      { id: "to", label: m.locateEnd, icon: "center", onSelect: () => { onSelectNode(edge.to); centerNode(edge.to); } },
      { id: "copy", label: m.copyConnection, icon: "copy", separatorBefore: true, onSelect: () => onCopyText(`${fromLabel} → ${toLabel} (${edge.id})`) },
    ];
    setMenu({ x, y, label: `${m.copyConnection}: ${fromLabel} → ${toLabel}`, actions, restoreFocus });
  }, [byId, centerNode, m, onCopyText, onSelectEdge, onSelectNode]);

  const openCanvasMenu = useCallback((x: number, y: number, restoreFocus?: HTMLElement | SVGElement | null) => {
    const actions: ContextMenuAction[] = [
      { id: "fit", label: m.fitCanvas, icon: "fit", onSelect: applyFit },
      { id: "reset", label: m.resetZoom, icon: "reset", onSelect: resetZoom },
      { id: "center", label: m.centerCurrentNode, icon: "center", disabled: !selectedNodeId, onSelect: () => { if (selectedNodeId) centerNode(selectedNodeId); } },
    ];
    setMenu({ x, y, label: m.canvasActions, actions, restoreFocus });
  }, [applyFit, centerNode, m, resetZoom, selectedNodeId]);

  /**
   * A ghost outside the current viewport is work the user cannot see. The map never moves the
   * viewport on its own (that would fight panning during a live run), so the `fit` button is
   * marked instead and the user decides.
   */
  const ghostOffscreen = ghosts.some((ghost) => (
    ghost.y - GHOST_HEIGHT / 2 < viewBox.y || ghost.y + GHOST_HEIGHT / 2 > viewBox.y + viewBox.height
  ));

  const headingCopy = fit.clamped
    ? tm.zoomFloorHint
    : ["submitted", "autonomous_exploration", "human_expansion"].includes(run.phase) ? m.treeRunningCopy : m.treeDoneCopy;

  return (
    <section className="tree-panel" aria-label={m.fullTree}>
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Derivation topology</p>
          <h2>{m.fullTree}</h2>
          <p>{headingCopy}</p>
        </div>
        <div className="canvas-tools" aria-label={m.canvasControls}>
          <button type="button" onClick={() => zoom(0.82)} aria-label={m.zoomIn}>{"\uFF0B"}</button>
          <button type="button" onClick={() => zoom(1.22)} aria-label={m.zoomOut}>−</button>
          <button type="button" className={ghostOffscreen ? "is-live" : undefined} onClick={applyFit}><span className="fit-label-long">{m.fitCanvas}</span><span className="fit-label-short">{m.fitShort}</span></button>
          <button type="button" onClick={expandAll} aria-label={tm.expandAll}>{tm.expandAllShort}</button>
          <button type="button" onClick={collapseBranches} aria-label={tm.collapseBranches}>{tm.collapseBranchesShort}</button>
        </div>
      </div>

      <nav className="route-tabs" aria-label={m.selectDerivationRoute}>
        <span>{m.highlightedRoute}</span>
        {routes.map((route, index) => (
          <button
            key={route.id}
            type="button"
            className={route.id === selectedRoute?.id ? "active" : ""}
            title={route.label}
            aria-label={`${m.selectDerivationRoute} ${String.fromCharCode(65 + index)}: ${route.label}`}
            aria-pressed={route.id === selectedRoute?.id}
            onClick={() => onSelectRoute(route.id)}
          >
            {String.fromCharCode(65 + index)}
          </button>
        ))}
      </nav>

      <svg
        ref={svgRef}
        className={`tree-svg${isOverview ? " is-overview" : ""}`}
        viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.width} ${viewBox.height}`}
        aria-label={m.selectableTree}
        data-multi-parent-steps={layout.warnings.multiParentNodeIds.join(" ") || undefined}
        tabIndex={0}
        onContextMenu={(event) => {
          if (event.target !== event.currentTarget) return;
          event.preventDefault();
          openCanvasMenu(event.clientX, event.clientY, event.currentTarget);
        }}
        onKeyDown={(event) => {
          if ((event.shiftKey && event.key === "F10") || event.key === "ContextMenu") {
            event.preventDefault();
            const rect = event.currentTarget.getBoundingClientRect();
            openCanvasMenu(rect.left + rect.width / 2, rect.top + rect.height / 2, event.currentTarget);
          }
        }}
        onWheel={(event) => {
          event.preventDefault();
          zoom(event.deltaY < 0 ? 0.9 : 1.1);
        }}
        onPointerDown={(event) => {
          if (event.target !== event.currentTarget) return;
          drag.current = { x: event.clientX, y: event.clientY };
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove={(event) => {
          if (!drag.current || !svgRef.current) return;
          const rect = svgRef.current.getBoundingClientRect();
          const dx = (event.clientX - drag.current.x) * viewBox.width / rect.width;
          const dy = (event.clientY - drag.current.y) * viewBox.height / rect.height;
          drag.current = { x: event.clientX, y: event.clientY };
          setViewBox((current) => ({ ...current, x: current.x - dx, y: current.y - dy }));
        }}
        onPointerUp={(event) => {
          if (!drag.current) return;
          drag.current = null;
          event.currentTarget.releasePointerCapture?.(event.pointerId);
        }}
      >
        <g className="edge-layer">
          {edges.map((edge) => {
            const from = byId.get(edge.from);
            const to = byId.get(edge.to);
            if (!from || !to) return null;
            const path = edgePath(from, to);
            const isActive = routeEdges.has(`${edge.from}->${edge.to}`);
            const fromLabel = scientificTextPlainTitle(from.title, 10_000);
            const toLabel = scientificTextPlainTitle(to.title, 10_000);
            return (
              <g key={edge.id}>
                <title>{tm.connection(fromLabel, toLabel, edge.kinds.map(tm.edgeKind).join(" · "))}</title>
                <path
                  d={path}
                  className="edge-hit"
                  role="button"
                  tabIndex={0}
                  aria-label={`${m.selectMatchingRoute}: ${fromLabel} → ${toLabel}`}
                  onClick={() => onSelectEdge(edge)}
                  onContextMenu={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    openEdgeMenu(edge, event.clientX, event.clientY, event.currentTarget);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onSelectEdge(edge);
                    } else if ((event.shiftKey && event.key === "F10") || event.key === "ContextMenu") {
                      event.preventDefault();
                      const rect = event.currentTarget.getBoundingClientRect();
                      openEdgeMenu(edge, rect.left + rect.width / 2, rect.top + rect.height / 2, event.currentTarget);
                    }
                  }}
                />
                <path d={path} className={`tree-edge ${isActive ? "active" : "dim"}`} />
              </g>
            );
          })}
        </g>
        <g className="node-layer">
          {layout.nodes.map((node) => {
            const isOnRoute = routeNodes.has(node.id);
            const isSelected = selectedNodeId === node.id;
            const isCollapsed = collapsed.has(node.id);
            const hidden = layout.hiddenDescendants.get(node.id) ?? 0;
            const childCount = children.get(node.id)?.length ?? 0;
            const plainTitle = scientificTextPlainTitle(node.title, 10_000);
            const titleLines = isOverview ? [overviewNodeTitle(plainTitle)] : wrapNodeTitle(plainTitle);
            const meta = isCollapsed && hidden > 0
              ? tm.hiddenSteps(hidden)
              : childCount > 0 ? tm.directChildren(childCount) : null;
            return (
              <g
                key={node.id}
                role="button"
                tabIndex={0}
                aria-label={node.status === "running" ? `${m.open}: ${plainTitle} · ${tm.runningStep}` : `${m.open}: ${plainTitle}`}
                aria-pressed={isSelected}
                className={`tree-node ${isOnRoute ? "on-route" : "dim"} ${isSelected ? "selected" : ""} ${holdsSelection.has(node.id) ? "holds-selection" : ""} ${node.status}`}
                transform={`translate(${node.x - NODE_WIDTH / 2} ${node.y - NODE_HEIGHT / 2})`}
                onClick={() => onSelectNode(node.id)}
                onContextMenu={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  openNodeMenu(node.id, event.clientX, event.clientY, event.currentTarget);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelectNode(node.id);
                  } else if ((event.shiftKey && event.key === "F10") || event.key === "ContextMenu") {
                    event.preventDefault();
                    const rect = event.currentTarget.getBoundingClientRect();
                    openNodeMenu(node.id, rect.left + rect.width / 2, rect.top + rect.height / 2, event.currentTarget);
                  }
                }}
              >
                <title>{plainTitle}</title>
                <rect width={NODE_WIDTH} height={NODE_HEIGHT} rx="8" />
                <text className="node-order" x="12" y="18">STEP {String(node.order).padStart(2, "0")}</text>
                {holdsSelection.has(node.id) ? (
                  <text className="node-holds-selection" x={NODE_WIDTH - 24} y="18" textAnchor="end">{tm.containsCurrentStep}</text>
                ) : null}
                <circle className="node-status" cx={NODE_WIDTH - 13} cy="15" r="3" />
                <text className="node-title" x="12" y={isOverview ? 50 : 36}>
                  {titleLines.map((line, index) => (
                    <tspan key={`${node.id}-${index}`} x="12" dy={index === 0 ? 0 : 14}>{line}</tspan>
                  ))}
                </text>
                {meta ? <text className="node-meta" x="12" y="68">{meta}</text> : null}
              </g>
            );
          })}
        </g>
        <g className="tree-ghost-layer">
          {ghosts.map((ghost) => {
            const roleLabel = tm.role(ghost.role);
            return (
              <g
                key={ghost.callId}
                className={`tree-ghost ${ghost.role}`}
                role="status"
                aria-label={tm.ghostCall(roleLabel, ghost.parentTitle)}
                data-ghost-for={ghost.fromStepId}
              >
                <path className="tree-ghost-link" d={`M ${ghost.x} ${ghost.linkFromY} L ${ghost.x} ${ghost.y - GHOST_HEIGHT / 2}`} />
                <rect
                  x={ghost.x - GHOST_WIDTH / 2}
                  y={ghost.y - GHOST_HEIGHT / 2}
                  width={GHOST_WIDTH}
                  height={GHOST_HEIGHT}
                  rx="8"
                />
                <circle className="tree-ghost-pulse" cx={ghost.x - GHOST_WIDTH / 2 + 17} cy={ghost.y} r="4" />
                <text className="tree-ghost-role" x={ghost.x - GHOST_WIDTH / 2 + 31} y={ghost.y} dominantBaseline="central">{roleLabel}</text>
              </g>
            );
          })}
        </g>
        <g className="toggle-layer">
          {layout.nodes.map((node) => {
            if ((children.get(node.id)?.length ?? 0) === 0) return null;
            const isCollapsed = collapsed.has(node.id);
            const hidden = layout.hiddenDescendants.get(node.id) ?? 0;
            const plainTitle = scientificTextPlainTitle(node.title, 10_000);
            return (
              <g
                key={`toggle-${node.id}`}
                className={`tree-toggle ${isCollapsed ? "collapsed" : "expanded"}`}
                role="button"
                tabIndex={0}
                aria-label={isCollapsed ? tm.expandNode(plainTitle, hidden) : tm.collapseNode(plainTitle)}
                aria-expanded={!isCollapsed}
                transform={`translate(${node.x} ${node.y + NODE_HEIGHT / 2})`}
                onClick={(event) => {
                  event.stopPropagation();
                  toggleNode(node.id);
                }}
                onKeyDown={(event) => {
                  if (event.key !== "Enter" && event.key !== " ") return;
                  event.preventDefault();
                  event.stopPropagation();
                  toggleNode(node.id);
                }}
              >
                <circle r={TOGGLE_RADIUS} />
                <text textAnchor="middle" dominantBaseline="central">{isCollapsed ? "+" : "−"}</text>
              </g>
            );
          })}
        </g>
      </svg>
      <ContextMenu menu={menu} onClose={closeMenu} />
    </section>
  );
}
