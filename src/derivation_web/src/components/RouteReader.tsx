import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { DerivationEdge, DerivationRoute, DerivationStep } from "../types";
import { forksAlongRoute, type RememberedForks, type RouteFork } from "../reading/forks";
import { useLocale } from "../i18n";
import { checkLabel, formatElapsed, roleOf, useCallElapsed, useNewlySealed, worstCheck, type LiveCall } from "../live";
import { readerLiveMessages, type ReaderLiveMessages } from "./readerLiveMessages";
import { BranchPicker } from "./BranchPicker";
import { ContextMenu, MoreButton, type ContextMenuAction, type ContextMenuState } from "./ContextMenu";
import { ScientificInlineTitle, ScientificText, scientificTextPlainTitle } from "./ScientificText";
import "./RouteReader.css";

/** Localized catalog handed down to every step; the desk never reads it from context twice. */
type Catalog = ReturnType<typeof useLocale>["messages"];

interface RouteReaderProps {
  route?: DerivationRoute;
  steps: DerivationStep[];
  selectedNodeId?: string;
  branchableStepRevisionIds: readonly string[];
  onSelectStep: (stepId: string) => void;
  onOpenDetails: (step: DerivationStep) => void;
  onOpenBranch: (step: DerivationStep) => void;
  onCopyText: (value: string) => void | Promise<void>;
  /**
   * Document title. The run question is what the derivation is *about*; a route
   * label (`Route 1` on a single-route run) tells a reader nothing, so it drops
   * to the subtitle line whenever a real title is supplied.
   */
  documentTitle?: string;
  /** Edges and routes enable the in-prose fork picker; omitting them renders plain prose. */
  edges?: readonly DerivationEdge[];
  routes?: readonly DerivationRoute[];
  remembered?: RememberedForks;
  onSelectRoute?: (routeId: string, forkStepId: string) => void;
  /** In-flight model calls from `RunEvent.overlay`; empty on a finished run. */
  activeCalls?: readonly LiveCall[];
  /** `RunView.phase`, used only to tell "nothing sealed yet" from "nothing running". */
  runPhase?: string;
}

/** Stable empty map so the fork memo does not recompute on every render. */
const NO_REMEMBERED: RememberedForks = {};
/** Stable empty list so an absent overlay never looks like a changed one. */
const NO_CALLS: readonly LiveCall[] = [];
/** Stable empty list for a desk with no route selected. */
const NO_NODE_IDS: readonly string[] = [];
/** Phases in which a reader is waiting for the first seal rather than reading. */
const PRE_SEAL_PHASES = new Set(["submitted", "autonomous_exploration"]);

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function scrollToStep(stepId: string) {
  document
    .getElementById(`reader-step-${stepId}`)
    ?.scrollIntoView({ block: "start", behavior: prefersReducedMotion() ? "auto" : "smooth" });
}

/** `03` — the same two-digit sequence the step index button shows. */
function sequenceLabel(index: number): string {
  return String(index + 1).padStart(2, "0");
}

interface StepArticleProps {
  step: DerivationStep;
  index: number;
  selected: boolean;
  /** Sealed within the last few seconds: the article carries a fading marker. */
  isNew: boolean;
  canBranch: boolean;
  /** The fork rooted at this step, or `undefined`. Identity is stabilized by the desk. */
  forkOptions?: RouteFork;
  titleId: string;
  stepsById: ReadonlyMap<string, DerivationStep>;
  messages: Catalog;
  live: ReaderLiveMessages;
  onSelectStep: (stepId: string) => void;
  onOpenDetails: (step: DerivationStep) => void;
  onOpenBranch: (step: DerivationStep) => void;
  onOpenMenu: (step: DerivationStep, canBranch: boolean, x: number, y: number, restoreFocus?: HTMLElement | SVGElement | null) => void;
  onChooseFork: (routeId: string, forkStepId: string) => void;
}

function StepArticleView({
  step,
  index,
  selected,
  isNew,
  canBranch,
  forkOptions,
  titleId,
  stepsById,
  messages: m,
  live,
  onSelectStep,
  onOpenDetails,
  onOpenBranch,
  onOpenMenu,
  onChooseFork,
}: StepArticleProps) {
  const plainTitle = scientificTextPlainTitle(step.title);
  const hasOutput = Boolean(step.output) && step.output !== step.reasoningSummary;
  const worst = worstCheck(step.checks);

  return (
    <article
      id={`reader-step-${step.id}`}
      className={`route-step route-step-v2${selected ? " is-current" : ""}${isNew ? " is-new" : ""}`}
      aria-current={selected ? "step" : undefined}
      aria-labelledby={titleId}
    >
      <div
        className="route-step-heading reader-step-head"
        onContextMenu={(event) => {
          // Right-clicking anywhere on the heading (index, title, more button) opens the step menu.
          event.preventDefault();
          const focusTarget = event.currentTarget.querySelector<HTMLButtonElement>(".reader-step-index");
          onOpenMenu(step, canBranch, event.clientX, event.clientY, focusTarget);
        }}
      >
        <button
          type="button"
          className="reader-step-index"
          aria-label={`${m.selectRouteStep}: ${plainTitle}`}
          onClick={() => onSelectStep(step.id)}
          onKeyDown={(event) => {
            if ((event.shiftKey && event.key === "F10") || event.key === "ContextMenu") {
              event.preventDefault();
              const rect = event.currentTarget.getBoundingClientRect();
              onOpenMenu(step, canBranch, rect.left + 20, rect.top + 20, event.currentTarget);
            }
          }}
        >
          {sequenceLabel(index)}
        </button>
        {/*
          The Checker's verdict used to live only inside the details drawer, so a
          reader scrolling a running derivation could not tell a step that passed
          from one still being checked. `physics` flips while the run is live.
        */}
        <span
          className={`step-check is-${worst}`}
          role="img"
          aria-label={live.checkSummary(checkLabel(worst, live.check))}
          data-testid={`step-check-${step.id}`}
        />
        <h3 className="route-step-title" id={titleId}>
          <ScientificInlineTitle value={step.title} />
          {/*
            The API substitutes a verified typeset layer's math for the sealed
            fragments it repaired. That is a rendering of the record, not the
            record, so the step carrying it says so rather than passing the
            corrected formula off as what the model wrote. It rides inside the
            title so the heading grid keeps its three columns.
          */}
          {step.typeset && (
            <span className="route-step-typeset" title={m.readerTypesetExplainer} data-testid={`step-typeset-${step.id}`}>
              {m.readerTypesetBadge}
            </span>
          )}
        </h3>
        <MoreButton
          label={`${m.moreRouteStepActions}: ${step.id}`}
          onClick={(button) => {
            const rect = button.getBoundingClientRect();
            onOpenMenu(step, canBranch, rect.right, rect.bottom, button);
          }}
        />
      </div>

      <div className="route-step-body">
        <ScientificText value={step.reasoningSummary || step.output} />
      </div>

      {hasOutput && (
        <div className="route-step-output">
          <p className="route-step-output-label">{m.readerResultLabel}</p>
          <ScientificText value={step.output} />
        </div>
      )}

      {selected && (
        <div className="selected-step-actions reader-step-actions">
          <div className="selected-step-buttons">
            <button type="button" className="quiet-button" onClick={() => onOpenDetails(step)}>{m.viewFiveDetails}</button>
            {canBranch && <button type="button" className="primary-button" onClick={() => onOpenBranch(step)}>{m.branchHere}</button>}
          </div>
        </div>
      )}

      {forkOptions && (
        <BranchPicker
          fork={forkOptions}
          steps={stepsById}
          canBranch={canBranch}
          onSelectRoute={onChooseFork}
          onOpenBranch={() => onOpenBranch(step)}
        />
      )}
    </article>
  );
}

/**
 * Every SSE event replaces the whole `RunView`, so every `StepView` object — and
 * with it every `steps` array, `byId` map and callback closure — is new even
 * when nothing about a sealed step changed. Without this gate a 2 Hz overlay
 * ticker re-parses Markdown and re-typesets KaTeX for a 30k-character document
 * several times a second.
 *
 * A sealed StepRevision is immutable, so `revisionId` plus `typeset` is the
 * complete identity of its rendered content: the one way a sealed step's text
 * can change is the API starting (or stopping) to serve it from a verified
 * typeset layer. Everything else in the comparison is presentation state the
 * desk owns. Callbacks and the step map are deliberately excluded: the desk
 * keeps them behind refs (or, for the locale, remounts the list), so a changed
 * identity there can never mean changed output.
 */
const StepArticle = memo(StepArticleView, (previous, next) =>
  previous.step.revisionId === next.step.revisionId
  && Boolean(previous.step.typeset) === Boolean(next.step.typeset)
  && previous.selected === next.selected
  && previous.isNew === next.isNew
  && previous.index === next.index
  && previous.canBranch === next.canBranch
  && previous.forkOptions === next.forkOptions);

/**
 * Paper-style reading desk: the whole route is rendered as one continuous
 * document, and `selectedNodeId` only highlights and scrolls. The previous
 * timeline rendered the same `reasoningSummary` twice (collapsed `<small>` plus
 * the expanded body) and showed prose for one step at a time, which made the
 * pane a table of contents rather than something a derivation can be read in.
 */
export function RouteReader({
  route,
  steps,
  selectedNodeId,
  branchableStepRevisionIds,
  onSelectStep,
  onOpenDetails,
  onOpenBranch,
  onCopyText,
  documentTitle,
  edges,
  routes,
  remembered = NO_REMEMBERED,
  onSelectRoute,
  activeCalls = NO_CALLS,
  runPhase,
}: RouteReaderProps) {
  const { locale, messages: m } = useLocale();
  const live = useMemo(() => readerLiveMessages(locale), [locale]);
  const forkAnchor = useRef<string | null>(null);
  const [menu, setMenu] = useState<ContextMenuState | null>(null);
  const titleBaseId = useId();
  const byId = useMemo(() => new Map(steps.map((step) => [step.id, step])), [steps]);

  // Latest-value refs: the step articles are memoized on content identity, so a
  // callback whose identity changed must still be the one that runs on click.
  const handlers = useRef({ onSelectStep, onOpenDetails, onOpenBranch, onSelectRoute });
  useEffect(() => {
    handlers.current = { onSelectStep, onOpenDetails, onOpenBranch, onSelectRoute };
  }, [onSelectStep, onOpenDetails, onOpenBranch, onSelectRoute]);

  const selectStep = useCallback((stepId: string) => handlers.current.onSelectStep(stepId), []);
  const openDetails = useCallback((step: DerivationStep) => handlers.current.onOpenDetails(step), []);
  const openBranch = useCallback((step: DerivationStep) => handlers.current.onOpenBranch(step), []);

  const forks = useMemo<RouteFork[]>(() => {
    if (!route || !edges || !routes) return [];
    return forksAlongRoute({ steps, edges: [...edges], routes: [...routes] }, route, remembered);
  }, [route, steps, edges, routes, remembered]);

  /*
   * `forksAlongRoute` rebuilds its result from the replaced `RunView`, so an
   * unchanged fork arrives as a new object and would defeat the step memo at
   * exactly the steps that are most expensive to re-render. Forks are small, so
   * a serialized signature is enough to hand back the previous object.
   */
  const forkCache = useRef(new Map<string, { signature: string; fork: RouteFork }>());
  const forkByStep = useMemo(() => {
    const cache = forkCache.current;
    const stable = new Map<string, RouteFork>();
    for (const fork of forks) {
      const signature = JSON.stringify(fork);
      const cached = cache.get(fork.stepId);
      const value = cached && cached.signature === signature ? cached.fork : fork;
      cache.set(fork.stepId, { signature, fork: value });
      stable.set(fork.stepId, value);
    }
    for (const stepId of [...cache.keys()]) if (!stable.has(stepId)) cache.delete(stepId);
    return stable;
  }, [forks]);

  const routeNodeIds = route?.nodeIds;
  const nodeIds = useMemo(() => routeNodeIds ?? NO_NODE_IDS, [routeNodeIds]);
  const newlySealed = useNewlySealed(nodeIds);
  const elapsedByCall = useCallElapsed(activeCalls);

  const [scrollRoot, setScrollRoot] = useState<HTMLElement | null>(null);
  const [endSentinel, setEndSentinel] = useState<HTMLElement | null>(null);
  const [documentEndVisible, setDocumentEndVisible] = useState(true);

  useEffect(() => {
    if (!endSentinel || typeof IntersectionObserver === "undefined") {
      // No observer (jsdom, older engines): treat the end of the document as
      // visible so the jump affordance never appears with no way to dismiss it.
      setDocumentEndVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) setDocumentEndVisible(entry.isIntersecting);
      },
      { root: scrollRoot, rootMargin: "0px 0px 48px 0px" },
    );
    observer.observe(endSentinel);
    return () => observer.disconnect();
  }, [endSentinel, scrollRoot]);

  useEffect(() => {
    if (!selectedNodeId) return;
    scrollToStep(selectedNodeId);
  }, [selectedNodeId]);

  // After a fork switch the viewport stays on the fork, not at the top of the
  // rewritten document: the reader just made a decision there.
  useEffect(() => {
    const anchor = forkAnchor.current;
    if (!anchor) return;
    forkAnchor.current = null;
    scrollToStep(anchor);
  }, [route?.id]);

  const closeMenu = useCallback(() => setMenu(null), []);

  const openStepMenu = useCallback((step: DerivationStep, canBranch: boolean, x: number, y: number, restoreFocus?: HTMLElement | SVGElement | null) => {
    const title = scientificTextPlainTitle(step.title);
    const markdown = `## ${title}\n\n${step.reasoningSummary || step.output}${step.output && step.output !== step.reasoningSummary ? `\n\n### Output\n\n${step.output}` : ""}`;
    const actions: ContextMenuAction[] = [
      { id: "locate", label: m.locateInTree, icon: "center", onSelect: () => handlers.current.onSelectStep(step.id) },
      { id: "details", label: m.viewDetails, icon: "details", onSelect: () => handlers.current.onOpenDetails(step) },
      { id: "branch", label: m.branchHere, icon: "branch", disabled: !canBranch, onSelect: () => handlers.current.onOpenBranch(step) },
      { id: "copy-title", label: m.copyTitle, icon: "copy", separatorBefore: true, onSelect: () => onCopyText(title) },
      { id: "copy-markdown", label: m.copyStepMarkdown, icon: "copy", onSelect: () => onCopyText(markdown) },
      { id: "copy-id", label: m.copyNodeId, icon: "copy", onSelect: () => onCopyText(step.id) },
    ];
    setMenu({ x, y, label: `${m.moreRouteStepActions}: ${title}`, actions, restoreFocus });
  }, [m, onCopyText]);

  const chooseFork = useCallback((routeId: string, forkStepId: string) => {
    forkAnchor.current = forkStepId;
    handlers.current.onSelectRoute?.(routeId, forkStepId);
  }, []);

  /** The last step that entered the route, announced once for assistive tech. */
  const announcement = useMemo(() => {
    if (newlySealed.size === 0) return "";
    for (let index = nodeIds.length - 1; index >= 0; index -= 1) {
      const nodeId = nodeIds[index];
      if (!newlySealed.has(nodeId)) continue;
      const title = byId.get(nodeId)?.title ?? nodeId;
      return live.sealedAnnouncement(sequenceLabel(index), scientificTextPlainTitle(title, 80));
    }
    return "";
  }, [newlySealed, nodeIds, byId, live]);

  if (!route) {
    return (
      <aside className="route-reader empty-reader" data-testid="route-reader" aria-label={m.routeReader}>
        <h2>{m.chooseRoute}</h2>
        <p>{m.chooseRouteCopy}</p>
      </aside>
    );
  }

  const routeStatusLabel = {
    complete: m.complete,
    active: m.active,
    proposed: m.proposed,
    failed: m.failed,
  }[route.status];

  // A document always tells the reader where they are: with nothing selected the
  // first step is current, so `aria-current="step"` is never absent.
  const currentStepId = selectedNodeId ?? route.nodeIds[0];

  // A single-route run has no fork, so `Current path` never renders there.
  const breadcrumb = forks
    .map((fork) => fork.options.find((option) => option.isCurrent))
    .filter((option) => option !== undefined)
    .map((option) => scientificTextPlainTitle(byId.get(option.toStepId)?.title ?? option.toStepId, 40));

  const positionOf = new Map(route.nodeIds.map((nodeId, index) => [nodeId, index]));
  const awaitingFirstStep = activeCalls.length === 0 && route.nodeIds.length === 0 && PRE_SEAL_PHASES.has(runPhase ?? "");
  const showActivity = activeCalls.length > 0 || awaitingFirstStep;
  const showJumpToLatest = !documentEndVisible && newlySealed.size > 0;
  const lastNodeId = route.nodeIds[route.nodeIds.length - 1];

  return (
    <aside className="route-reader reader-doc" data-testid="route-reader" aria-label={m.routeReader} ref={setScrollRoot}>
      {/* Sticky strip: what a reader needs while a 30k-character body scrolls —
          the route status and which direction was taken at each fork. */}
      <div className="reader-heading reader-status-strip">
        <div className="reader-document-inner">
          <span className={`route-status ${route.status}`}>{routeStatusLabel}</span>
          <p className="reader-document-meta">
            {documentTitle && <span className="reader-route-label">{route.label}</span>}
            <span>{route.nodeIds.length} {m.readerSteps}</span>
            {breadcrumb.length > 0 && (
              <span className="reader-path" aria-live="polite">
                {m.readerCurrentPath}: {breadcrumb.join(" › ")}
              </span>
            )}
          </p>
        </div>
      </div>

      {/* The path strip above is a different message; a seal must not overwrite it. */}
      <p className="visually-hidden" aria-live="polite" data-testid="reader-seal-announcement">{announcement}</p>

      <div className="route-timeline reader-doc-body">
        <header className="reader-document">
          <h2 className="reader-document-title">{documentTitle ?? route.label}</h2>
        </header>

        {route.nodeIds.map((nodeId, index) => {
          const step = byId.get(nodeId);
          if (!step) return null;
          return (
            <StepArticle
              // The locale is part of the key rather than of the memo comparison:
              // switching language is rare and must repaint every article, while a
              // snapshot must repaint none of them.
              key={`${locale}:${step.id}`}
              step={step}
              index={index}
              selected={currentStepId === step.id}
              isNew={newlySealed.has(step.id)}
              canBranch={branchableStepRevisionIds.includes(step.revisionId)}
              forkOptions={forkByStep.get(step.id)}
              titleId={`${titleBaseId}-${step.id}`}
              stepsById={byId}
              messages={m}
              live={live}
              onSelectStep={selectStep}
              onOpenDetails={openDetails}
              onOpenBranch={openBranch}
              onOpenMenu={openStepMenu}
              onChooseFork={chooseFork}
            />
          );
        })}

        {/*
          What the run is doing right now, in the reader's column rather than in
          the header: on a live run the next step is minutes away, and the only
          other evidence a reader had was an "N active calls" counter.
          `aria-live="off"` — the elapsed readout ticks every second.
        */}
        {showActivity && (
          <section className="reader-activity" data-testid="reader-activity" aria-live="off">
            <h3 className="reader-activity-title">{live.activity}</h3>
            {activeCalls.length > 0 ? (
              <ul className="reader-activity-list">
                {activeCalls.map((call) => {
                  const role = roleOf(call);
                  const position = positionOf.get(call.fromStepId);
                  const seconds = elapsedByCall.get(call.callId) ?? 0;
                  return (
                    <li key={call.callId} className="reader-activity-row" data-role={role}>
                      <span className={`reader-activity-role is-${role}`}>{live.role[role]}</span>
                      {position === undefined ? (
                        <span className="reader-activity-anchor is-off-route" title={live.offRouteStep}>
                          {call.fromStepId.startsWith("task_") ? live.fromStart : call.fromStepId}
                        </span>
                      ) : (
                        <button
                          type="button"
                          className="reader-activity-anchor"
                          aria-label={live.stepAnchor(sequenceLabel(position))}
                          onClick={() => scrollToStep(call.fromStepId)}
                        >
                          STEP {sequenceLabel(position)}
                        </button>
                      )}
                      <span className="reader-activity-elapsed" aria-label={live.elapsed(formatElapsed(seconds))}>
                        {formatElapsed(seconds)}
                      </span>
                      {/* Reserved for a streaming tail the backend does not send yet. */}
                      {call.preview && <pre className="reader-activity-preview">{call.preview}</pre>}
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="reader-activity-empty">{live.awaitingFirstStep}</p>
            )}
          </section>
        )}

        <div className="reader-doc-end" ref={setEndSentinel} aria-hidden="true" />
      </div>

      {/*
        Reading is never interrupted by an arriving step: the desk offers the way
        down instead of taking it, and only while the reader is away from the end.
      */}
      {showJumpToLatest && lastNodeId && (
        <div className="reader-jump-dock">
          <button type="button" className="reader-jump-latest" onClick={() => scrollToStep(lastNodeId)}>
            {live.jumpToLatest}
          </button>
        </div>
      )}
      <ContextMenu menu={menu} onClose={closeMenu} />
    </aside>
  );
}
