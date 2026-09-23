/* eslint-disable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex -- WAI-ARIA focusable separator owns keyboard and pointer resizing. */
import { useEffect, useId, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, PointerEvent, ReactNode } from "react";
import { useLocale } from "../i18n";

// v3: the workbench flipped in 2026-09-09. The reading desk is now the left main
// column and the derivation map is the right rail, so the stored width changed
// meaning entirely (reader width -> map width). Old v2 values are legal numbers
// in the new range, so the key is versioned rather than migrated.
const TREE_WIDTH_KEY = "derivationlab.workbench.tree-width.v3";
const ROUTE_COLLAPSED_KEY = "derivationlab.workbench.route-collapsed";
const TREE_COLLAPSED_KEY = "derivationlab.workbench.tree-collapsed";
const DEFAULT_TREE_WIDTH = 440;
const MIN_TREE_WIDTH = 320;
const MAX_TREE_WIDTH = 720;
/** The reading column keeps enough room for the 820px measure to breathe. */
const MIN_READER_WIDTH = 480;
const SASH_WIDTH = 5;
const KEYBOARD_STEP = 24;

interface WorkbenchSplitPaneProps {
  tree: ReactNode;
  route: ReactNode;
}

type CompactPane = "tree" | "route";

function readStoredWidth(): number {
  try {
    const value = Number(window.localStorage.getItem(TREE_WIDTH_KEY));
    return Number.isFinite(value) && value >= MIN_TREE_WIDTH && value <= MAX_TREE_WIDTH
      ? value
      : DEFAULT_TREE_WIDTH;
  } catch {
    return DEFAULT_TREE_WIDTH;
  }
}

function readStoredFlag(key: string): boolean {
  try {
    return window.localStorage.getItem(key) === "true";
  } catch {
    return false;
  }
}

function clampTreeWidth(width: number, containerWidth: number): number {
  const available = Math.max(MIN_TREE_WIDTH, containerWidth - MIN_READER_WIDTH - SASH_WIDTH);
  return Math.round(Math.min(MAX_TREE_WIDTH, available, Math.max(MIN_TREE_WIDTH, width)));
}

export function WorkbenchSplitPane({ tree, route }: WorkbenchSplitPaneProps) {
  const { messages: m } = useLocale();
  const [treeWidth, setTreeWidth] = useState(readStoredWidth);
  const [collapsed, setCollapsed] = useState(() => readStoredFlag(ROUTE_COLLAPSED_KEY));
  const [treeCollapsed, setTreeCollapsed] = useState(() => readStoredFlag(TREE_COLLAPSED_KEY));
  const [compactPane, setCompactPane] = useState<CompactPane>("route");
  const containerRef = useRef<HTMLDivElement>(null);
  const resizing = useRef(false);
  const treePanelId = useId();
  const routePanelId = useId();

  const persistWidth = (nextWidth: number) => {
    const measuredWidth = containerRef.current?.getBoundingClientRect().width ?? 0;
    const containerWidth = measuredWidth > 0 ? measuredWidth : window.innerWidth;
    const clamped = clampTreeWidth(nextWidth, containerWidth);
    setTreeWidth(clamped);
    try {
      window.localStorage.setItem(TREE_WIDTH_KEY, String(clamped));
    } catch {
      // Storage can be unavailable in privacy-restricted webviews. Layout still works in memory.
    }
  };

  const persistFlag = (key: string, value: boolean) => {
    try {
      window.localStorage.setItem(key, String(value));
    } catch {
      // Storage is a preference enhancement, never a Record dependency.
    }
  };

  /** Map focus mode: folding the reading desk hands the main area to the map. */
  const setRouteCollapsed = (nextCollapsed: boolean) => {
    setCollapsed(nextCollapsed);
    persistFlag(ROUTE_COLLAPSED_KEY, nextCollapsed);
    if (nextCollapsed && treeCollapsed) {
      setTreeCollapsed(false);
      persistFlag(TREE_COLLAPSED_KEY, false);
    }
  };

  /** Reading focus mode: folding the map rail leaves the desk alone on the page. */
  const setTreePaneCollapsed = (nextCollapsed: boolean) => {
    setTreeCollapsed(nextCollapsed);
    persistFlag(TREE_COLLAPSED_KEY, nextCollapsed);
    if (nextCollapsed && collapsed) {
      setCollapsed(false);
      persistFlag(ROUTE_COLLAPSED_KEY, false);
    }
  };

  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (!entry) return;
      setTreeWidth((current) => clampTreeWidth(current, entry.contentRect.width));
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  // The sash still sizes the right-hand rail, so the pointer/keyboard maths are
  // measured from the container's right edge exactly as before the flip.
  const resizeFromPointer = (event: PointerEvent<HTMLDivElement>) => {
    if (!resizing.current || !containerRef.current) return;
    const bounds = containerRef.current.getBoundingClientRect();
    persistWidth(bounds.right - event.clientX);
  };

  const resizeFromKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    let nextWidth: number | undefined;
    if (event.key === "ArrowLeft") nextWidth = treeWidth + KEYBOARD_STEP;
    if (event.key === "ArrowRight") nextWidth = treeWidth - KEYBOARD_STEP;
    if (event.key === "Home") nextWidth = MAX_TREE_WIDTH;
    if (event.key === "End") nextWidth = MIN_TREE_WIDTH;
    if (nextWidth === undefined) return;
    event.preventDefault();
    persistWidth(nextWidth);
  };

  return (
    <div
      ref={containerRef}
      className={`workbench-split${collapsed ? " is-route-collapsed" : ""}${treeCollapsed ? " is-tree-collapsed" : ""}`}
      style={{ "--tree-pane-width": `${treeWidth}px` } as CSSProperties}
    >
      <div className="workbench-tabs" role="tablist" aria-label={m.workspaceViews}>
        <button
          type="button"
          role="tab"
          aria-selected={compactPane === "route"}
          aria-controls={routePanelId}
          onClick={() => setCompactPane("route")}
        >
          {m.routeReadingTab}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={compactPane === "tree"}
          aria-controls={treePanelId}
          onClick={() => setCompactPane("tree")}
        >
          {m.derivationTreeTab}
        </button>
      </div>

      {collapsed && (
        <button
          type="button"
          className="workbench-restore-route"
          aria-label={m.expandReader}
          title={m.expandReader}
          onClick={() => setRouteCollapsed(false)}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6 3 5 5-5 5" /></svg>
        </button>
      )}

      <div id={routePanelId} className="workbench-pane workbench-route-pane" data-compact-active={compactPane === "route"}>
        {route}
      </div>

      {/* In either focus mode the sash and both collapse controls make no sense:
          one pane owns the main area and the restore strip is the way back. */}
      {!collapsed && !treeCollapsed && (
        <>
          <div
            className="workbench-sash"
            role="separator"
            tabIndex={0}
            aria-label={m.resizeTree}
            aria-orientation="vertical"
            aria-valuemin={MIN_TREE_WIDTH}
            aria-valuemax={MAX_TREE_WIDTH}
            aria-valuenow={treeWidth}
            onKeyDown={resizeFromKeyboard}
            onPointerDown={(event) => {
              resizing.current = true;
              event.currentTarget.setPointerCapture?.(event.pointerId);
            }}
            onPointerMove={resizeFromPointer}
            onPointerUp={(event) => {
              resizing.current = false;
              event.currentTarget.releasePointerCapture?.(event.pointerId);
            }}
            onPointerCancel={() => { resizing.current = false; }}
          />
          <button
            type="button"
            className="workbench-collapse-button"
            aria-label={m.collapseReader}
            title={m.collapseReader}
            onClick={() => setRouteCollapsed(true)}
          >
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m10 3-5 5 5 5" /></svg>
          </button>
          <button
            type="button"
            className="workbench-collapse-tree-button"
            aria-label={m.collapseTree}
            title={m.collapseTree}
            onClick={() => setTreePaneCollapsed(true)}
          >
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6 3 5 5-5 5" /></svg>
          </button>
        </>
      )}

      <div id={treePanelId} className="workbench-pane workbench-tree-pane" data-compact-active={compactPane === "tree"}>
        {tree}
      </div>

      {treeCollapsed && (
        <button
          type="button"
          className="workbench-restore-tree"
          aria-label={m.expandTree}
          title={m.expandTree}
          onClick={() => setTreePaneCollapsed(false)}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m10 3-5 5 5 5" /></svg>
        </button>
      )}
    </div>
  );
}
