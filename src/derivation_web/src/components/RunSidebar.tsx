import { useCallback, useEffect, useMemo, useState } from "react";
import type { ProductHost } from "../host";
import { useLocale } from "../i18n";
import type { RunSummary } from "../types";
import { applyTheme, readStoredTheme, writeStoredTheme, type ThemeName } from "../theme";
import { ContextMenu, MenuIcon, MoreButton, type ContextMenuAction, type ContextMenuState } from "./ContextMenu";

interface RunSidebarProps {
  runs: RunSummary[];
  currentRunId?: string;
  open: boolean;
  loading: boolean;
  onClose: () => void;
  onNew: () => void;
  onSelect: (runId: string) => void;
  onExport: (runId: string) => void;
  host: ProductHost;
}

const PINNED_RUNS_STORAGE_KEY = "derivationlab.ui.pinned-runs.v1";

function shortTime(value: string, locale: string) {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(locale, { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}

/** The applied theme, which `main.tsx` stamps on `<html>` before the first paint. */
function activeTheme(): ThemeName {
  const stamped = document.documentElement.dataset.theme;
  return stamped === "light" || stamped === "paper" ? stamped : readStoredTheme();
}

function chooseTheme(name: ThemeName) {
  applyTheme(name);
  writeStoredTheme(name);
}

function readPinnedRuns(): Set<string> {
  try {
    const value = JSON.parse(window.localStorage.getItem(PINNED_RUNS_STORAGE_KEY) ?? "[]");
    return new Set(Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []);
  } catch {
    return new Set();
  }
}

export function RunSidebar({ runs, currentRunId, open, loading, onClose, onNew, onSelect, onExport, host }: RunSidebarProps) {
  const { locale, messages: m, setLocale } = useLocale();
  const [pinnedRunIds, setPinnedRunIds] = useState(readPinnedRuns);
  const [menu, setMenu] = useState<ContextMenuState | null>(null);
  const phaseLabels: Record<RunSummary["phase"], string> = {
    submitted: m.phaseSubmitted,
    autonomous_exploration: m.phaseExploring,
    human_expansion: m.phaseExpansion,
    paused: m.phasePaused,
    recovering: m.phaseRecovering,
    review_ready: m.phaseReviewReady,
    review_ready_due_to_cap: m.phaseCapped,
    interrupted: m.phaseInterrupted,
    error: m.phaseError,
  };
  const sortedRuns = useMemo(() => [...runs].sort((left, right) => {
    const pinOrder = Number(pinnedRunIds.has(right.id)) - Number(pinnedRunIds.has(left.id));
    return pinOrder || right.updated_at.localeCompare(left.updated_at);
  }), [pinnedRunIds, runs]);

  useEffect(() => {
    try {
      window.localStorage?.setItem(PINNED_RUNS_STORAGE_KEY, JSON.stringify([...pinnedRunIds]));
    } catch {
      // Persistence is a convenience; private/locked browser storage must not break navigation.
    }
  }, [pinnedRunIds]);

  const closeMenu = useCallback(() => setMenu(null), []);

  const togglePin = useCallback((runId: string) => {
    setPinnedRunIds((current) => {
      const next = new Set(current);
      if (next.has(runId)) next.delete(runId);
      else next.add(runId);
      return next;
    });
  }, []);

  const openRunMenu = useCallback((run: RunSummary, x: number, y: number, restoreFocus?: HTMLElement | SVGElement | null) => {
    const pinned = pinnedRunIds.has(run.id);
    const stableUrl = new URL(window.location.href);
    stableUrl.searchParams.set("run", run.id);
    const actions: ContextMenuAction[] = [
      { id: "open", label: m.open, icon: "open", onSelect: () => onSelect(run.id) },
      { id: "pin", label: pinned ? m.unpin : m.pin, icon: "pin", onSelect: () => togglePin(run.id) },
      { id: "export", label: m.exportPdf, icon: "export", separatorBefore: true, onSelect: () => onExport(run.id) },
      { id: "copy-link", label: m.copyStableLink, icon: "copy", onSelect: () => host.copyText(stableUrl.toString()) },
      { id: "copy-id", label: m.copyRunId, icon: "copy", onSelect: () => host.copyText(run.id) },
      {
        id: "evidence",
        label: host.revealRunEvidence ? m.revealEvidence : m.evidenceDesktopOnly,
        icon: "folder",
        disabled: !host.revealRunEvidence,
        separatorBefore: true,
        onSelect: () => host.revealRunEvidence?.(run.id),
      },
    ];
    setMenu({ x, y, label: `${m.moreRunActions}: ${run.question}`, actions, restoreFocus });
  }, [host, m, onExport, onSelect, pinnedRunIds, togglePin]);

  const openSettingsMenu = useCallback((button: HTMLButtonElement) => {
    const rect = button.getBoundingClientRect();
    const theme = activeTheme();
    setMenu({
      x: rect.left,
      y: rect.top,
      placement: "above-start",
      label: m.settings,
      groupLabel: m.displayLanguage,
      restoreFocus: button,
      actions: [
        { id: "locale-en", label: m.english, icon: "settings", radio: true, checked: locale === "en", onSelect: () => setLocale("en") },
        { id: "locale-zh", label: m.simplifiedChinese, icon: "settings", radio: true, checked: locale === "zh-CN", onSelect: () => setLocale("zh-CN") },
        { id: "theme-paper", label: m.themePaper, icon: "settings", radio: true, checked: theme === "paper", separatorBefore: true, groupLabel: m.appearance, onSelect: () => chooseTheme("paper") },
        { id: "theme-light", label: m.themeLight, icon: "settings", radio: true, checked: theme === "light", onSelect: () => chooseTheme("light") },
      ],
    });
  }, [locale, m, setLocale]);

  return (
    <>
      <button type="button" className={`sidebar-scrim ${open ? "open" : ""}`} aria-label={m.closeNavigation} onClick={onClose} />
      <aside className={`run-sidebar ${open ? "open" : ""}`} aria-label={m.navigation}>
        <header>
          <div className="sidebar-brand"><strong>Derivation Lab</strong></div>
          <button type="button" className="sidebar-close" aria-label={m.closeNavigation} onClick={onClose}>
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15" /></svg>
          </button>
        </header>
        <button type="button" className="new-run-button" onClick={onNew}>
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 4v12M4 10h12" /></svg>
          {m.newDerivation}
        </button>
        <nav aria-label={m.existingDerivations}>
          <h2>{m.existingDerivations}</h2>
          {loading && runs.length === 0 && <p className="sidebar-empty">{m.loading}</p>}
          {!loading && runs.length === 0 && <p className="sidebar-empty">{m.noRuns}</p>}
          {sortedRuns.map((run) => (
            <div
              key={run.id}
              className={`run-list-row ${run.id === currentRunId ? "current" : ""}`}
              onContextMenu={(event) => {
                event.preventDefault();
                const focusTarget = event.target instanceof Element
                  ? event.target.closest<HTMLElement>("button") ?? event.currentTarget.querySelector<HTMLElement>(".run-list-item")
                  : event.currentTarget.querySelector<HTMLElement>(".run-list-item");
                openRunMenu(run, event.clientX, event.clientY, focusTarget);
              }}
            >
              <button
                type="button"
                className="run-list-item"
                aria-current={run.id === currentRunId ? "page" : undefined}
                onClick={() => onSelect(run.id)}
                onKeyDown={(event) => {
                  if ((event.shiftKey && event.key === "F10") || event.key === "ContextMenu") {
                    event.preventDefault();
                    const rect = event.currentTarget.getBoundingClientRect();
                    openRunMenu(run, rect.left + 18, rect.top + 18, event.currentTarget);
                  }
                }}
              >
                <strong>{pinnedRunIds.has(run.id) && <MenuIcon name="pin" />}{run.question}</strong>
                <span><i className={run.status} />{run.read_only ? m.readOnly : phaseLabels[run.phase]} · {shortTime(run.updated_at, locale)}</span>
                <small>{run.step_count} {m.nodes} · {run.route_count} {m.routes}</small>
              </button>
              <MoreButton
                label={`${m.moreRunActions}: ${run.id}`}
                onClick={(button) => {
                  const rect = button.getBoundingClientRect();
                  openRunMenu(run, rect.right, rect.bottom, button);
                }}
              />
            </div>
          ))}
        </nav>
        <footer className="sidebar-footer">
          <button type="button" className="settings-button" aria-label={m.settings} aria-haspopup="menu" onClick={(event) => openSettingsMenu(event.currentTarget)}>
            <MenuIcon name="settings" /><span>{m.settings}</span>
          </button>
        </footer>
      </aside>
      <ContextMenu menu={menu} onClose={closeMenu} />
    </>
  );
}
