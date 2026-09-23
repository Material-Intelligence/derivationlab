import { useEffect, useState } from "react";
import type { BranchKind, DerivationStep, StepContent } from "../types";
import { scientificTextPlainTitle } from "./ScientificText";
import { ModalDialog } from "./ModalDialog";
import { useLocale } from "../i18n";

interface BranchDialogProps {
  step: DerivationStep | null;
  submitting?: boolean;
  onClose: () => void;
  onSubmit: (kind: BranchKind, instruction: string) => Promise<boolean> | boolean;
}

export function BranchDialog({ step, submitting = false, onClose, onSubmit }: BranchDialogProps) {
  const { messages: m } = useLocale();
  const [kind, setKind] = useState<BranchKind>("human_direction");
  const [directionInstruction, setDirectionInstruction] = useState("");
  const [revision, setRevision] = useState<StepContent>({ claim: "", why: "", source: "", derivation: "", scope: "" });

  useEffect(() => {
    setKind("human_direction");
    setDirectionInstruction("");
    setRevision(step?.content ?? { claim: "", why: "", source: "", derivation: "", scope: "" });
  }, [step]);

  if (!step) return null;
  const revisionComplete = Object.values(revision).every((value) => value.trim());
  const canSubmit = kind === "human_direction" ? Boolean(directionInstruction.trim()) : revisionComplete;
  const setRevisionField = (field: keyof StepContent, value: string) => setRevision((current) => ({ ...current, [field]: value }));

  return (
    <ModalDialog labelledBy="branch-title" dismissible={!submitting} onClose={onClose}>
      <form
        className="branch-dialog"
        onSubmit={async (event) => {
          event.preventDefault();
          if (!canSubmit || submitting) return;
          const instruction = kind === "human_direction"
            ? directionInstruction.trim()
            : JSON.stringify({
                claim: revision.claim.trim(),
                why: revision.why.trim(),
                source: revision.source.trim(),
                derivation: revision.derivation.trim(),
                scope: revision.scope.trim(),
              });
          if (await onSubmit(kind, instruction)) onClose();
        }}
      >
        <div className="dialog-heading">
          <div><p className="eyebrow">Human expansion</p><h2 id="branch-title">{m.branchDialogTitle} “{scientificTextPlainTitle(step.title)}”</h2></div>
          <button type="button" className="icon-button" onClick={onClose} disabled={submitting} aria-label={m.closeBranch}>×</button>
        </div>
        <p className="dialog-copy">{m.branchDialogCopy}</p>
        <fieldset>
          <legend>{m.interventionType}</legend>
          <label><input type="radio" name="kind" checked={kind === "human_direction"} onChange={() => setKind("human_direction")} /> {m.newDirection}</label>
          <label><input type="radio" name="kind" checked={kind === "human_revision"} onChange={() => setKind("human_revision")} /> {m.reviseStep}</label>
        </fieldset>
        {kind === "human_direction" ? (
          <label className="instruction-label">{m.branchInstruction}
            <textarea value={directionInstruction} onChange={(event) => setDirectionInstruction(event.target.value)} rows={5} placeholder={m.branchPlaceholder} data-modal-initial-focus />
          </label>
        ) : (
          <div className="revision-editor" aria-label={m.revisedFiveFields}>
            <p>{m.replaceAllFields}</p>
            <label>Claim<textarea rows={2} value={revision.claim} onChange={(event) => setRevisionField("claim", event.target.value)} /></label>
            <label>Why<textarea rows={2} value={revision.why} onChange={(event) => setRevisionField("why", event.target.value)} /></label>
            <label>Source<textarea rows={2} value={revision.source} onChange={(event) => setRevisionField("source", event.target.value)} /></label>
            <label>Derivation<textarea rows={3} value={revision.derivation} onChange={(event) => setRevisionField("derivation", event.target.value)} /></label>
            <label>Scope<textarea rows={2} value={revision.scope} onChange={(event) => setRevisionField("scope", event.target.value)} /></label>
          </div>
        )}
        <div className="dialog-actions"><button type="button" className="quiet-button" onClick={onClose} disabled={submitting}>{m.cancel}</button><button type="submit" className="primary-button" disabled={!canSubmit || submitting}>{submitting ? m.creating : kind === "human_revision" ? m.submitRevision : m.createAutonomousBranch}</button></div>
      </form>
    </ModalDialog>
  );
}
