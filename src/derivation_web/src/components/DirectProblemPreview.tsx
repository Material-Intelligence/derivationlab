import type { FrozenProblemInput } from "../types";
import { useDirectProblemMessages } from "../directProblemMessages";
import { ScientificText } from "./ScientificText";

export function DirectProblemPreview({ problem }: { problem: FrozenProblemInput }) {
  const m = useDirectProblemMessages();
  const sections: Array<[string, string[]]> = [
    [m.givens, problem.givens],
    [m.assumptions, problem.assumptions],
    [m.decisions, problem.accepted_decisions ?? []],
    [m.defaults, (problem.declared_defaults ?? []).map((item) => `${item.title}: ${item.statement}`)],
    [m.ladder, (problem.refinement_ladder ?? []).map((item) => `${item.rung} — ${item.name}: ${item.relaxes}${item.parallel_branch ? ` [${m.parallel}]` : ""}`)],
    [m.scope, [problem.scope]],
    [m.deliverable, [problem.deliverable]],
    [m.criteria, problem.success_criteria],
    [m.tools, problem.allowed_tools],
  ];
  return <section className="direct-problem-preview" aria-label={m.objective}>
    <ScientificText value={problem.objective} />
    <details className="draft-summary">
      <summary>{m.fullProblem}</summary>
      <div>{sections.map(([label, content]) => <section key={label}>
        <h3>{label}</h3>
        {content.length ? content.map((item, index) => <ScientificText key={index} value={item} />) : <p>{m.notDeclared}</p>}
      </section>)}</div>
    </details>
  </section>;
}
