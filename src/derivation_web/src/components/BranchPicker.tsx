import { useRef } from "react";
import type { KeyboardEvent } from "react";
import type { DerivationStep } from "../types";
import type { ForkOption, RouteFork } from "../reading/forks";
import { useLocale } from "../i18n";
import { ScientificInlineTitle, scientificTextPlainTitle } from "./ScientificText";
import "./BranchPicker.css";

interface BranchPickerProps {
  fork: RouteFork;
  steps: ReadonlyMap<string, DerivationStep>;
  /** Rendered only when the fork step itself can still be branched from. */
  canBranch: boolean;
  onSelectRoute: (routeId: string, forkStepId: string) => void;
  onOpenBranch: () => void;
}

interface OptionGroupProps {
  label: string;
  options: ForkOption[];
  steps: ReadonlyMap<string, DerivationStep>;
  forkStepId: string;
  onSelectRoute: (routeId: string, forkStepId: string) => void;
}

/**
 * One mutually exclusive set of directions. Radio semantics (not toggle
 * semantics) are correct here: exactly one direction is being read at a time.
 */
function OptionGroup({ label, options, steps, forkStepId, onSelectRoute }: OptionGroupProps) {
  const { messages: m } = useLocale();
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const currentIndex = Math.max(0, options.findIndex((option) => option.isCurrent));

  const statusLabel = (option: ForkOption) => {
    if (!option.route) return m.readerNoRouteYet;
    return { complete: m.complete, active: m.active, proposed: m.proposed, failed: m.failed }[option.route.status];
  };

  /** Arrow keys move and select, as ARIA radio groups are expected to. */
  const move = (event: KeyboardEvent<HTMLButtonElement>, from: number, delta: number) => {
    event.preventDefault();
    const next = (from + delta + options.length) % options.length;
    buttons.current[next]?.focus();
    const target = options[next];
    if (target.route) onSelectRoute(target.route.id, forkStepId);
  };

  return (
    <div className="branch-picker-group" role="radiogroup" aria-label={label}>
      <p className="branch-picker-legend">{label}</p>
      <div className="branch-picker-options">
        {options.map((option, index) => {
          const step = steps.get(option.toStepId);
          const title = step ? step.title : option.toStepId;
          const status = option.route?.status ?? "unrealized";
          return (
            <button
              key={option.edgeId}
              type="button"
              role="radio"
              ref={(element) => { buttons.current[index] = element; }}
              className="branch-picker-option"
              data-status={status}
              data-current={option.isCurrent ? "true" : undefined}
              aria-checked={option.isCurrent}
              tabIndex={index === currentIndex ? 0 : -1}
              disabled={!option.route}
              aria-label={`${m.readerSwitchDirection}: ${scientificTextPlainTitle(title, 96)} · ${statusLabel(option)}`}
              onClick={() => { if (option.route) onSelectRoute(option.route.id, forkStepId); }}
              onKeyDown={(event) => {
                if (event.key === "ArrowRight" || event.key === "ArrowDown") move(event, index, 1);
                if (event.key === "ArrowLeft" || event.key === "ArrowUp") move(event, index, -1);
              }}
            >
              <span className="branch-picker-option-title" aria-hidden="true">
                <ScientificInlineTitle value={title} />
              </span>
              <span className="branch-picker-option-status" data-status={status} aria-hidden="true">
                {statusLabel(option)}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/**
 * In-prose fork selector. It sits directly after the body of the step that
 * forks, so the decision is made where the derivation actually branches instead
 * of in a separate route switcher.
 */
export function BranchPicker({ fork, steps, canBranch, onSelectRoute, onOpenBranch }: BranchPickerProps) {
  const { messages: m } = useLocale();
  if (fork.continuations.length + fork.revisions.length < 2) return null;

  return (
    <div className="branch-picker" data-testid="branch-picker" data-fork-step={fork.stepId}>
      {fork.continuations.length > 0 && (
        <OptionGroup
          label={fork.continuations.length > 1
            ? `${m.readerContinueFromHere} · ${fork.continuations.length} ${m.readerDirections}`
            : m.readerContinueFromHere}
          options={fork.continuations}
          steps={steps}
          forkStepId={fork.stepId}
          onSelectRoute={onSelectRoute}
        />
      )}
      {fork.revisions.length > 0 && (
        <OptionGroup
          label={m.readerStepRevisions}
          options={fork.revisions}
          steps={steps}
          forkStepId={fork.stepId}
          onSelectRoute={onSelectRoute}
        />
      )}
      {canBranch && (
        <button type="button" className="branch-picker-new quiet-button" onClick={onOpenBranch}>
          {"\uFF0B"} {m.readerNewBranchFromHere}
        </button>
      )}
    </div>
  );
}
