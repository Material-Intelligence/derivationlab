import { useState } from "react";
import type { ProblemPresetsView } from "../api/generated";
import { useDirectProblemMessages } from "../directProblemMessages";
import { DirectRunStartForm } from "./DirectRunStartForm";
import { RunStartForm, type RunStartFormProps } from "./RunStartForm";

export function NewRunStart(props: RunStartFormProps & { loadPresets: () => Promise<ProblemPresetsView> }) {
  const m = useDirectProblemMessages();
  const [mode, setMode] = useState<"direct" | "intake">(props.initialSessionId ? "intake" : "direct");
  return <div className="new-run-entry">
    <nav className="run-entry-options" aria-label={m.entry}>
      <button type="button" aria-pressed={mode === "direct"} disabled={props.loading} onClick={() => setMode("direct")}>{m.direct}</button>
      <button type="button" aria-pressed={mode === "intake"} disabled={props.loading} onClick={() => setMode("intake")}>{m.intake}</button>
    </nav>
    {mode === "direct" ? <DirectRunStartForm loading={props.loading} defaults={props.defaults} loadPresets={props.loadPresets} onSubmit={props.onSubmit} /> : <RunStartForm {...props} />}
  </div>;
}
