import type { DerivationStep } from "../types";
import { ScientificText } from "./ScientificText";
import { scientificTextPlainTitle } from "./ScientificText";
import { ModalDialog } from "./ModalDialog";
import { useLocale } from "../i18n";
import { useDirectProblemMessages } from "../directProblemMessages";

interface DetailsDrawerProps {
  step: DerivationStep | null;
  onClose: () => void;
}

export function DetailsDrawer({ step, onClose }: DetailsDrawerProps) {
  const { messages: m } = useLocale();
  const directMessages = useDirectProblemMessages();
  if (!step) return null;
  const checkLabel = { passed: m.passed, failed: m.failed, pending: m.pending, not_requested: directMessages.unchecked } as const;
  const content = step.content ?? {
    claim: m.unsealed,
    why: m.unsealed,
    source: m.unsealed,
    derivation: m.unsealed,
    scope: m.unsealed,
  };
  return (
    <ModalDialog labelledBy="details-title" variant="drawer" onClose={onClose}>
      <aside className="details-drawer">
        <div className="dialog-heading">
          <div>
            <p className="eyebrow">Step revision · sealed evidence</p>
            <h2 id="details-title">{scientificTextPlainTitle(step.title)}</h2>
            {/*
              The five fields below are the sealed evidence unless the API
              served this step from a verified typeset layer, in which case the
              drawer says so instead of letting the eyebrow speak for text the
              record did not seal.
            */}
            {step.typeset && <p className="details-typeset-note">{m.readerTypesetExplainer}</p>}
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label={m.closeDetails} data-modal-initial-focus>×</button>
        </div>
        <section><h3>1. Claim</h3><ScientificText value={content.claim} /></section>
        <section><h3>2. Why</h3><ScientificText value={content.why} /></section>
        <section><h3>3. Source</h3><ScientificText value={content.source} /></section>
        <section><h3>4. Derivation</h3><ScientificText value={content.derivation} /></section>
        <section><h3>5. Scope</h3><ScientificText value={content.scope} /></section>
        <details className="audit-details">
          <summary>{m.checksAndAudit}</summary>
          <ul className="check-list">
            <li>{m.structure}: {checkLabel[step.checks.schema]}</li>
            <li>{m.physics}: {checkLabel[step.checks.physics]}</li>
            <li>{m.provenance}: {checkLabel[step.checks.provenance]}</li>
          </ul>
          <dl>
            <div><dt>Revision</dt><dd>{step.revisionId}</dd></div>
            <div><dt>Model</dt><dd>{step.provenance?.model ?? "not recorded"}</dd></div>
            <div><dt>Thread</dt><dd>{step.provenance?.threadId ?? "not recorded"}</dd></div>
            <div><dt>Turn</dt><dd>{step.provenance?.turnId ?? "not recorded"}</dd></div>
            <div><dt>Created</dt><dd>{step.provenance?.createdAt ?? "not recorded"}</dd></div>
          </dl>
        </details>
      </aside>
    </ModalDialog>
  );
}
