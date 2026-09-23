import type { ZodMiniType } from "zod/mini";
import type { DeviceLoginStartView, ReportBundleView, RunEvent, RunView } from "./generated";
import { allowedOpenAiHttpsUrl } from "../security/externalUrl";

export interface ContractContext {
  requestId?: string;
  runId?: string;
  eventId?: string;
}

export interface ContractIssue {
  path: string;
  message: string;
}

export class ContractMismatchError extends Error {
  readonly endpoint: string;
  readonly schema: string;
  readonly issues: ContractIssue[];
  readonly context: ContractContext;

  constructor(endpoint: string, schema: string, issues: ContractIssue[], context: ContractContext = {}) {
    const summary = issues.map((issue) => `${issue.path}: ${issue.message}`).join("; ");
    super(`Contract mismatch at ${endpoint} (${schema}): ${summary}`);
    this.name = "ContractMismatchError";
    this.endpoint = endpoint;
    this.schema = schema;
    this.issues = issues;
    this.context = context;
  }
}

export function parseContract<T>(
  schema: ZodMiniType<T>,
  value: unknown,
  endpoint: string,
  schemaName: string,
  context: ContractContext = {},
): T {
  const result = schema.safeParse(value);
  if (result.success) return result.data;
  throw new ContractMismatchError(
    endpoint,
    schemaName,
    result.error.issues.map((issue) => ({
      path: issue.path.length ? issue.path.join(".") : "$",
      message: issue.message,
    })),
    context,
  );
}

export function assertRunSemantics(run: RunView, endpoint: string, context: ContractContext = {}): RunView {
  const issues: ContractIssue[] = [];
  const steps = new Set<string>();
  const revisions = new Map<string, RunView["steps"][number]>();
  if (context.runId !== undefined && run.id !== context.runId) {
    issues.push({ path: "id", message: `expected run ${context.runId}, received ${run.id}` });
  }
  for (const step of run.steps) {
    if (steps.has(step.id)) issues.push({ path: "steps", message: `duplicate node id ${step.id}` });
    if (revisions.has(step.revisionId)) issues.push({ path: "steps", message: `duplicate revision id ${step.revisionId}` });
    steps.add(step.id);
    revisions.set(step.revisionId, step);
  }

  if (run.rootStepId !== null && !steps.has(run.rootStepId)) {
    issues.push({ path: "rootStepId", message: `unknown node ${run.rootStepId}` });
  }

  const edgePairs = new Set<string>();
  for (const [index, edge] of run.edges.entries()) {
    if (!steps.has(edge.from)) issues.push({ path: `edges.${index}.from`, message: `unknown node ${edge.from}` });
    if (!steps.has(edge.to)) issues.push({ path: `edges.${index}.to`, message: `unknown node ${edge.to}` });
    edgePairs.add(`${edge.from}\u0000${edge.to}`);
  }

  let nonEmptyRouteCount = 0;
  for (const [routeIndex, route] of run.routes.entries()) {
    if (route.nodeIds.length > 0) nonEmptyRouteCount += 1;
    for (const [nodeIndex, nodeId] of route.nodeIds.entries()) {
      if (!steps.has(nodeId)) {
        issues.push({ path: `routes.${routeIndex}.nodeIds.${nodeIndex}`, message: `unknown node ${nodeId}` });
      }
      const next = route.nodeIds[nodeIndex + 1];
      if (next !== undefined && !edgePairs.has(`${nodeId}\u0000${next}`)) {
        issues.push({ path: `routes.${routeIndex}.nodeIds.${nodeIndex}`, message: `no edge from ${nodeId} to ${next}` });
      }
    }
  }
  if (run.steps.length > 0 && nonEmptyRouteCount === 0) {
    issues.push({ path: "routes", message: "a run with steps must contain at least one non-empty route" });
  }

  const commands = run.commands;
  if (run.read_only && (
    commands.can_pause
    || commands.can_resume
    || commands.can_interrupt
    || commands.branchable_step_revision_ids.length > 0
  )) {
    issues.push({ path: "commands", message: "a read-only run cannot advertise mutation commands" });
  }
  if (commands.can_pause && commands.can_resume) {
    issues.push({ path: "commands", message: "pause and resume cannot both be available" });
  }
  if (run.pause_requested && commands.can_pause) {
    issues.push({ path: "commands.can_pause", message: "pause is already requested" });
  }
  if (run.hard_interrupt_requested && commands.can_interrupt) {
    issues.push({ path: "commands.can_interrupt", message: "hard interrupt is already requested" });
  }
  const branchable = new Set<string>();
  for (const [index, revisionId] of commands.branchable_step_revision_ids.entries()) {
    const path = `commands.branchable_step_revision_ids.${index}`;
    if (branchable.has(revisionId)) issues.push({ path, message: `duplicate revision id ${revisionId}` });
    branchable.add(revisionId);
    const step = revisions.get(revisionId);
    if (!step) issues.push({ path, message: `unknown revision ${revisionId}` });
    else if (step.status !== "sealed") issues.push({ path, message: `revision ${revisionId} is not sealed` });
  }

  if (issues.length > 0) throw new ContractMismatchError(endpoint, "RunView semantics", issues, context);
  return run;
}

export function assertRunEventSemantics(
  event: RunEvent,
  expectedRunId: string,
  endpoint: string,
  context: ContractContext = {},
): RunEvent {
  if (event.run_id !== expectedRunId) {
    throw new ContractMismatchError(
      endpoint,
      "RunEvent semantics",
      [{ path: "run_id", message: `expected run ${expectedRunId}, received ${event.run_id}` }],
      context,
    );
  }
  if (event.run) assertRunSemantics(event.run, endpoint, { ...context, runId: expectedRunId });
  return event;
}

export function assertReportSemantics(
  report: ReportBundleView,
  expectedRunId: string,
  endpoint: string,
): ReportBundleView {
  if (report.run_id !== expectedRunId) {
    throw new ContractMismatchError(
      endpoint,
      "ReportBundleView semantics",
      [{ path: "run_id", message: `expected run ${expectedRunId}, received ${report.run_id}` }],
      { runId: expectedRunId },
    );
  }
  return report;
}

export function assertDeviceLoginSemantics(
  login: DeviceLoginStartView,
  endpoint: string,
): DeviceLoginStartView {
  const issues: ContractIssue[] = [];
  if (!allowedOpenAiHttpsUrl(login.verification_url)) {
    issues.push({ path: "verification_url", message: "expected an allowlisted OpenAI HTTPS URL" });
  }
  if (!Number.isFinite(Date.parse(login.expires_at))) {
    issues.push({ path: "expires_at", message: "expected an ISO-8601 timestamp" });
  }
  if ([login.login_id, login.user_code].some((value) => Array.from(value).some((character) => {
    const code = character.codePointAt(0) ?? 0;
    return code < 0x20 || code === 0x7f;
  }))) {
    issues.push({ path: "user_code", message: "control characters are not allowed" });
  }
  if (issues.length > 0) throw new ContractMismatchError(endpoint, "DeviceLoginStartView semantics", issues);
  return login;
}
