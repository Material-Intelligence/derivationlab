import { useEffect, useRef, useState } from "react";
import type { CreateRunDefaultsView, CreateRunRequest, FrozenProblemInput } from "../types";
import type { ProblemPresetsView } from "../api/generated";
import { zFrozenProblemInput } from "../api/generated/zod.gen";
import { useDirectProblemMessages } from "../directProblemMessages";
import { DirectProblemPreview } from "./DirectProblemPreview";

interface Props {
  loading: boolean;
  defaults: CreateRunDefaultsView;
  loadPresets: () => Promise<ProblemPresetsView>;
  onSubmit: (request: CreateRunRequest) => Promise<void> | void;
}

/** Validate an import without inventing a confirmation or silently rewriting it. */
export function parseDirectProblem(text: string): FrozenProblemInput {
  const value: unknown = JSON.parse(text);
  const parsed = zFrozenProblemInput.parse(value);
  if (parsed.origin !== "direct_spec" || parsed.confirmed_by_user !== false) {
    throw new Error("direct_origin_required");
  }
  if (value && typeof value === "object") {
    const unknown = Object.keys(value).filter((key) => !(key in parsed));
    if (unknown.length) throw new Error(`Unknown fields: ${unknown.join(", ")}`);
  }
  // Preserve the imported payload: generated response validators may otherwise
  // strip nested unknown fields. The backend rejects unsupported input fields.
  return value as FrozenProblemInput;
}

export function DirectRunStartForm({ loading, defaults, loadPresets, onSubmit }: Props) {
  const m = useDirectProblemMessages();
  const [catalog, setCatalog] = useState<ProblemPresetsView | null>(null);
  const [catalogError, setCatalogError] = useState(false);
  const [generation, setGeneration] = useState(0);
  const [selectedId, setSelectedId] = useState("");
  const [imported, setImported] = useState<FrozenProblemInput | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submitLock = useRef(false);
  const importGeneration = useRef(0);
  const [sources, setSources] = useState<"methods" | "none">("methods");
  const [checkerEnabled, setCheckerEnabled] = useState(true);
  const [unlimited, setUnlimited] = useState(true);
  const [callLimit, setCallLimit] = useState("100");
  const [model, setModel] = useState("gpt-5.5");
  const [effort, setEffort] = useState("high");
  const [serviceTier, setServiceTier] = useState<"standard" | "fast">(defaults.runtime.service_tier ?? "standard");

  useEffect(() => {
    let active = true;
    setCatalogError(false);
    void loadPresets().then((value) => {
      if (active) setCatalog(value);
    }).catch(() => {
      if (active) setCatalogError(true);
    });
    return () => { active = false; };
  }, [loadPresets, generation]);

  const preset = catalog?.presets.find((item) => item.id === selectedId);
  // A build without a method-source pack lists no references; the run then has none.
  const methodsOffered = (catalog?.method_references.length ?? 0) > 0;
  const referenceMode = methodsOffered ? sources : "none";
  const original = imported ?? preset?.problem ?? null;
  const problem = original && catalog ? {
    ...structuredClone(original),
    allowed_references: referenceMode === "methods" ? [...catalog.method_references] : [],
    source_pack: referenceMode === "methods" ? structuredClone(catalog.method_source_pack) : null,
  } : null;
  const modelOption = defaults.model_options.find((item) => item.model === model);
  const efforts = modelOption?.supported_efforts ?? [];
  const tiers = modelOption?.supported_service_tiers ?? ["standard"];
  const available = Boolean(modelOption && efforts.includes(effort) && tiers.includes(serviceTier));
  const validLimit = unlimited || (Number.isSafeInteger(Number(callLimit)) && Number(callLimit) > 0 && Number(callLimit) <= 10000);
  const disabled = loading || busy;
  const ready = problem !== null && available && validLimit && !disabled;

  async function importFile(file: File | undefined) {
    if (!file) return;
    const importId = ++importGeneration.current;
    setError(null);
    // A failed new import must not leave the previous problem armed for submission.
    setImported(null);
    setSelectedId("");
    if (file.size > 2 * 1024 * 1024) { setError(m.tooLarge); return; }
    try {
      const parsed = parseDirectProblem(await file.text());
      if (importGeneration.current === importId) setImported(parsed);
    } catch (reason) {
      if (importGeneration.current !== importId) return;
      setError(reason instanceof Error && reason.message === "direct_origin_required"
        ? m.directOnly : `${m.invalid}: ${reason instanceof Error ? reason.message : ""}`);
    }
  }

  function download() {
    if (!problem) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(problem, null, 2) + "\n"], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${problem.problem_id}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function start() {
    if (!ready || !problem || !catalog || submitLock.current) return;
    submitLock.current = true;
    setBusy(true);
    setError(null);
    const role = { ...defaults.config.writer, model, effort };
    try {
      await onSubmit({
        problem,
        config: {
          ...structuredClone(defaults.config),
          writer: { ...role }, checker: { ...role }, judge: { ...role },
          granularity: "one_task", checker_enabled: checkerEnabled,
          record_version: "1.1",
          max_model_calls: unlimited ? null : Number(callLimit),
          max_local_repairs: 3,
          reference_allowed: referenceMode === "methods",
          allowed_paths: [],
        },
        runtime: { ...structuredClone(defaults.runtime), max_run_seconds: null, service_tier: serviceTier, capability_profile: catalog.capability_profile },
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : m.failed);
    } finally {
      submitLock.current = false;
      setBusy(false);
    }
  }

  return <main className="start-shell direct-start-page">
    <form className="start-card direct-run-form" onSubmit={(event) => { event.preventDefault(); void start(); }}>
      <header><h1>{m.title}</h1><p>{m.intro}</p></header>
      {catalogError ? <div className="error-banner" role="alert">{m.loadFailed} <button type="button" onClick={() => setGeneration((value) => value + 1)}>{m.retry}</button></div>
        : !catalog ? <p role="status">{m.loading}</p> : null}
      <div className="direct-source-grid">
        <label>{m.preset}<select value={imported ? "__imported" : selectedId} disabled={disabled || !catalog} onChange={(event) => { importGeneration.current += 1; setSelectedId(event.target.value); setImported(null); setError(null); }}>
          <option value="">{m.choose}</option>
          {imported && <option value="__imported">{m.imported}: {imported.problem_id}</option>}
          {catalog?.presets.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
        </select></label>
        <label>{m.import}<input type="file" accept=".json,application/json" disabled={disabled} onChange={(event) => { void importFile(event.target.files?.[0]); event.target.value = ""; }} /></label>
      </div>
      {preset && !imported && <p>{preset.description}</p>}
      {problem && <>
        <DirectProblemPreview problem={problem} />
        <button type="button" className="quiet-button" onClick={download}>{m.download}</button>
      </>}
      <fieldset disabled={disabled} className="direct-settings">
        <legend>{m.references}</legend>
        <label><select aria-label={m.references} value={referenceMode} onChange={(event) => setSources(event.target.value as "methods" | "none")}>
          {methodsOffered && <option value="methods">{m.methods}</option>}<option value="none">{m.none}</option>
        </select></label>
        <p>{referenceMode === "methods" ? m.methodsHelp : m.noneHelp}</p>
        {referenceMode === "methods" && catalog && <ul className="direct-reference-list">{catalog.method_references.map((item) => <li key={item}>{item}</li>)}</ul>}
      </fieldset>
      <fieldset disabled={disabled} className="direct-settings">
        <legend>{m.model}</legend>
        <div className="config-grid">
          <label>{m.model}<select value={model} onChange={(event) => setModel(event.target.value)}>
            {!modelOption && <option value={model}>{model}</option>}
            {defaults.model_options.map((item) => <option key={item.model} value={item.model}>{item.display_name}</option>)}
          </select></label>
          <label>{m.effort}<select value={effort} onChange={(event) => setEffort(event.target.value)}>
            {!efforts.includes(effort) && <option value={effort}>{effort}</option>}
            {efforts.map((item) => <option key={item}>{item}</option>)}
          </select></label>
          <label>{m.speed}<select value={serviceTier} onChange={(event) => setServiceTier(event.target.value as "standard" | "fast")}>
            {["standard", "fast"].map((item) => <option key={item} value={item}>{item === "fast" ? m.fast : m.standard}</option>)}
          </select></label>
        </div>
        <p>{m.modelHelp}</p>
        {!available && <p className="intake-error" role="alert">{m.unavailable}</p>}
      </fieldset>
      <fieldset disabled={disabled} className="direct-settings">
        <label className="direct-checkbox"><input type="checkbox" checked={checkerEnabled} onChange={(event) => setCheckerEnabled(event.target.checked)} />{m.checker}</label>
        <p role="status">{checkerEnabled ? m.checkerOn : m.checkerOff}</p>
        <label className="direct-checkbox"><input type="checkbox" checked={unlimited} onChange={(event) => setUnlimited(event.target.checked)} />{m.unlimited}</label>
        {unlimited ? <p>{m.unlimitedHelp}</p> : <label>{m.callLimit}<input type="number" min={1} max={10000} value={callLimit} onChange={(event) => setCallLimit(event.target.value)} required /></label>}
      </fieldset>
      {error && <p className="intake-error" role="alert">{error}</p>}
      <div className="direct-start-actions"><p>{m.unchanged}</p><button type="submit" className="primary-button" disabled={!ready}>{disabled ? m.starting : m.start}</button></div>
    </form>
  </main>;
}
