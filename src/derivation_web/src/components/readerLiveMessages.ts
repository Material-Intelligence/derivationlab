/**
 * Copy owned by the reading desk's live-run surfaces only.
 *
 * The shared catalog in `src/i18n.tsx` is kept separate from these strings, so
 * the desk's strings live here and are selected with the same
 * locale value (`useLocale().locale`), exactly as `treeCanvasMessages.ts` does
 * for the map.
 */
import type { CallRole, CheckState } from "../live";

export type ReaderLocale = "en" | "zh-CN";

export interface ReaderLiveMessages {
  /** Heading of the end-of-document activity block. */
  activity: string;
  /** Role badge for one in-flight model call. */
  role: Record<CallRole, string>;
  /** Shown when nothing has been sealed yet and no call is visible. */
  awaitingFirstStep: string;
  /** Accessible name of the `m:ss` elapsed readout. */
  elapsed: (value: string) => string;
  /** Accessible name of the `STEP NN` anchor button. */
  stepAnchor: (index: string) => string;
  /** Anchor text for a call whose origin step is not on the route being read. */
  offRouteStep: string;
  /** Anchor text for a call that starts from the problem statement (no sealed step yet). */
  fromStart: string;
  jumpToLatest: string;
  /** Polite announcement made once a step is sealed. */
  sealedAnnouncement: (index: string, title: string) => string;
  /** Check states, mirroring the labels the details drawer uses. */
  check: Record<CheckState, string>;
  /** Accessible name of the per-step check mark. */
  checkSummary: (state: string) => string;
}

const catalog: Record<ReaderLocale, ReaderLiveMessages> = {
  en: {
    activity: "In progress",
    role: { writer: "Deriving", checker: "Checking", judge: "Reviewing", other: "Model call" },
    awaitingFirstStep: "The first step has not been sealed yet. Steps appear here once they pass their checks.",
    fromStart: "From the problem statement",
    elapsed: (value) => `Running for ${value}`,
    stepAnchor: (index) => `Go to step ${index}`,
    offRouteStep: "Off this route",
    jumpToLatest: "Jump to latest",
    sealedAnnouncement: (index, title) => `New step sealed: step ${index} — ${title}`,
    check: { passed: "Passed", failed: "Failed", pending: "Pending", not_requested: "Unchecked" },
    checkSummary: (state) => `Checks: ${state}`,
  },
  "zh-CN": {
    activity: "推导进行中",
    role: { writer: "推导中", checker: "检查中", judge: "评审中", other: "模型调用" },
    awaitingFirstStep: "第一步尚未封存。步骤通过检查后会出现在这里。",
    fromStart: "从题面出发",
    elapsed: (value) => `已用时 ${value}`,
    stepAnchor: (index) => `跳到步骤 ${index}`,
    offRouteStep: "不在当前路线",
    jumpToLatest: "跳到最新",
    sealedAnnouncement: (index, title) => `新步骤已封存：步骤 ${index} — ${title}`,
    check: { passed: "已通过", failed: "未通过", pending: "待定", not_requested: "未检查" },
    checkSummary: (state) => `检查：${state}`,
  },
};

export function readerLiveMessages(locale: ReaderLocale): ReaderLiveMessages {
  return catalog[locale] ?? catalog.en;
}
