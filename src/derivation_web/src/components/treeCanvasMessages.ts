/**
 * Copy owned by the tree map only.
 *
 * The shared catalog in `src/i18n.tsx` is kept separate from these strings, so the map's
 * strings live here and are selected with the same locale value (`useLocale().locale`).
 */
export type TreeLocale = "en" | "zh-CN";

/** Roles a live model call can play, as far as the map needs to distinguish them. */
export type LiveRole = "writer" | "checker" | "judge" | "other";


export interface TreeCanvasMessages {
  expandNode: (title: string, hidden: number) => string;
  collapseNode: (title: string) => string;
  hiddenSteps: (hidden: number) => string;
  directChildren: (count: number) => string;
  containsCurrentStep: string;
  expandAll: string;
  collapseBranches: string;
  expandAllShort: string;
  collapseBranchesShort: string;
  zoomFloorHint: string;
  connection: (from: string, to: string, kinds: string) => string;
  edgeKind: (kind: string) => string;
  role: (role: LiveRole) => string;
  runningStep: string;
  /** Parent label for a ghost that hangs off the task itself (no sealed step yet). */
  rootTask: string;
  /** Accessible name of a ghost placeholder: which role is working under which sealed step. */
  ghostCall: (role: string, parent: string) => string;
}

const kindLabels: Record<TreeLocale, Record<string, string>> = {
  en: {
    continuation: "continuation",
    model_fork: "model alternative",
    human_direction: "human direction",
    human_revision: "human revision",
    proposed: "proposed",
  },
  "zh-CN": {
    continuation: "顺延",
    model_fork: "模型备选",
    human_direction: "人工指向",
    human_revision: "人工修订",
    proposed: "待展开",
  },
};

const roleLabels: Record<TreeLocale, Record<LiveRole, string>> = {
  en: { writer: "Deriving", checker: "Checking", judge: "Judging", other: "Model call" },
  "zh-CN": { writer: "推导中", checker: "检查中", judge: "评审中", other: "模型调用" },
};

function plural(count: number, noun: string): string {
  return count === 1 ? noun : `${noun}s`;
}

const catalog: Record<TreeLocale, TreeCanvasMessages> = {
  en: {
    expandNode: (title, hidden) => `Expand: ${title} (${hidden} following ${plural(hidden, "step")})`,
    collapseNode: (title) => `Collapse: ${title}`,
    hiddenSteps: (hidden) => `${hidden} following ${plural(hidden, "step")} hidden`,
    directChildren: (count) => `${count} direct next ${plural(count, "step")}`,
    containsCurrentStep: "Has current step",
    expandAll: "Expand all branches",
    collapseBranches: "Collapse side branches",
    expandAllShort: "Expand all",
    collapseBranchesShort: "Collapse",
    zoomFloorHint: "Large map · zoom kept at the readable floor; collapse or focus a branch to see more",
    connection: (from, to, kinds) => `${from} → ${to} · ${kinds}`,
    edgeKind: (kind) => kindLabels.en[kind] ?? kind,
    role: (role) => roleLabels.en[role],
    runningStep: "Running",
    rootTask: "the problem statement",
    ghostCall: (role, parent) => `In progress: ${role} · under ${parent}`,
  },
  "zh-CN": {
    expandNode: (title, hidden) => `展开：${title}（${hidden} 个后续步骤）`,
    collapseNode: (title) => `收起：${title}`,
    hiddenSteps: (hidden) => `已收起 ${hidden} 个后续步骤`,
    directChildren: (count) => `${count} 个直接后继`,
    containsCurrentStep: "内含当前步骤",
    expandAll: "展开全部分支",
    collapseBranches: "收起旁支",
    expandAllShort: "展开全部",
    collapseBranchesShort: "收起分支",
    zoomFloorHint: "地图较大，已保留当前缩放；用折叠或聚焦查看",
    connection: (from, to, kinds) => `${from} → ${to} · ${kinds}`,
    edgeKind: (kind) => kindLabels["zh-CN"][kind] ?? kind,
    role: (role) => roleLabels["zh-CN"][role],
    runningStep: "进行中",
    rootTask: "题面",
    ghostCall: (role, parent) => `进行中：${role} · 挂在「${parent}」下`,
  },
};

export function treeMessages(locale: TreeLocale): TreeCanvasMessages {
  return catalog[locale] ?? catalog.en;
}
