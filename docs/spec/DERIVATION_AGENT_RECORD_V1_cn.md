# Derivation Agent Record V1 — 推导 Agent 运行记录契约

> 状态：**冻结候选规范**。版本 id：`derivation-agent-record-v1`。
>
> 本规范只定义记录与重放语义，不选择 writer、checker、judge、effort、步长或活枝帽，也不实现真实模型 backend。任何一次运行都必须把这些配置显式写入 Run；V1 没有隐藏默认值。

## 0. 目的与边界

一次推导 Agent 运行必须留下这样一份记录：任何后来的人只读事件日志，就能回答“发生了什么、谁造成的、检查的是哪一版文字、候选为何合格或失格、最终选择到底指向哪份正文”。

V1 的唯一事实源是 append-only JSONL 事件流。Branch、StepRevision、Candidate、Judgement、Selection、canonical JSON 和 HTML 页面全是同一事件流的重放结果；它们不得在数据库或 UI 中另有一份可变真相。

本规范处理的是审计与状态语义，不证明物理推导正确。步级 checker 只能否证明确硬伤；最终科学判断属于独立 Judgement。

## 1. 八类正式对象

| 对象 | 不可变身份 | 作用 |
|---|---|---|
| Run | `run_id` | 冻结代码、规范、题面、论文包、模型、backend、粒度、预算与输入边界 |
| Branch | `branch_id` | 一条推导 lineage；保存父枝、fork 点、继承前缀、工作假设和状态历史 |
| StepRevision | `step_revision_id` | 一次不可变五栏步骤；人工修改产生新的 replacement branch/revision，绝不覆盖旧对象 |
| ModelCall | `model_call_id` | 保存 writer/checker/judge 调用的开始、流片段、完成/失败/中止与部分输出 |
| Check | `check_id` | 绑定精确 StepRevision 与 `output_sha256` 的步级检查 |
| Candidate | `candidate_id` | 某一时刻整条正文的不可变快照，不是 Branch 的别名 |
| Judgement | `judgement_id` | 对精确 Candidate 哈希的终稿裁判结果 |
| Selection | `selection_id` | 指向合格 Candidate + 通过 Judgement 的最终选中稿 |

HumanAction 作为 provenance 事件单独保存。它不是第九种科学对象，但所有人工暂停、恢复、砍枝、改步、给方向、改假设、中止调用和选择候选都必须先有明确的 HumanAction，再执行对应状态变化。

## 2. 事件包络与哈希链

每行是一条完整 JSON 对象，必须只有以下字段：

```json
{
  "schema_version": "derivation-agent-event-v1",
  "run_id": "run_fixture_001",
  "seq": 1,
  "event_id": "evt_0001",
  "recorded_at": "2026-08-25T18:00:00Z",
  "type": "run_created",
  "actor": {"kind": "system", "id": "record-runtime"},
  "prev_event_sha256": null,
  "event_sha256": "...",
  "payload": {}
}
```

约束：

1. `seq` 从 1 连续递增；`event_id` 在 Run 内唯一。
2. 第一条必须是 `run_created`，且 `prev_event_sha256 = null`。
3. 后续事件的 `prev_event_sha256` 必须等于上一事件的 `event_sha256`。
4. `event_sha256` 是删除本字段后，对事件对象做 canonical JSON 再取 SHA-256。
5. canonical JSON：UTF-8、键字典序、无多余空格、非 ASCII 字符不转义。
6. 更改事件正文、顺序、前链或哈希都会使 `verify` 失败。
7. 哈希链用于发现篡改，不等同数字签名。正式科学冻结仍应由 git commit/tag 或外部签名锚定日志头哈希。

V1 事件类型：

- `run_created`
- `human_action_recorded`
- `branch_created`、`branch_status_changed`
- `model_call_started`、`model_call_chunk`、`model_call_finished`、`model_call_failed`、`model_call_aborted`
- `step_revision_sealed`
- `check_requested`、`check_completed`
- `candidate_declared`
- `judgement_requested`、`judgement_completed`
- `selection_recorded`

单事件结构由 `docs/spec/derivation_agent_event_v1.schema.json` 约束；跨事件状态机由 `src/derivation_agent_record/` 重放器约束。

## 3. Run：所有实验条件都显式记录

`run_created` 必须冻结：

- 本规范、event schema、canonical schema 的路径与 SHA-256；
- 40 位代码 commit；
- task 和 pack 的 id + SHA-256；
- 本次运行的 `granularity`；
- 本次运行的 `max_active_branches` 与 `max_model_calls`；
- writer/checker/judge 的 provider、model、effort；
- backend 名称与版本；
- `reference_allowed` 与允许输入路径。

V1 支持记录 `one_claim` 和 `one_task` 两种粒度，但**不为任何一次运行自动选择**。`max_active_branches` 可是正整数或显式 `null`；`null` 表示该 Run 不由记录契约执行数量帽，不代表未来 runtime 的推荐值。

允许输入路径必须是安全的仓库相对路径；`reference_allowed = false` 时，`allowed_paths` 不得列入 `reference/`。这条规则防止只在说明文字里说“没用 reference”，实际运行却把它列进输入边界。

不同 task hash、pack hash、模型、effort、backend、输入边界或粒度意味着不同 Run。不得把两次运行拼成同一个事件流。

## 4. Branch：lineage 而非可变文本容器

### 4.1 创建

根枝：

- `parent_branch_id = null`
- `fork_mode = root`
- 不继承步骤
- `created_reason = root`

子枝：

- `fork_mode = after`：继承父枝到 anchor **含 anchor** 的精确前缀；
- `fork_mode = replace`：继承父枝到 anchor **不含 anchor** 的精确前缀；
- 必须保存父枝、anchor、继承的 StepRevision ID 列表和新的工作假设；
- 不得通过摘要代替父枝正文，也不得重新复制祖先步骤为新 ID。

允许来源：`model_alternative`、`instrument_retry`、`human_direction`、`human_hypothesis`、`human_revision`。后三类必须引用先前 HumanAction；HumanAction 的 target 形状由 action 决定，方向 / 假设动作必须指向父枝，人工假设文本必须等于动作中已哈希的内容。

### 4.2 人工修改的唯一合法语义

规定：人工修改历史步骤不得原地覆盖。

合法流程：

1. `human_action_recorded(action=revise_step)` 指向旧 `step_revision_id`，保存新五栏内容的 canonical 文本与 SHA-256；
2. 创建 `fork_mode=replace`、`created_reason=human_revision` 的子枝；
3. 子枝精确继承旧步骤之前的前缀；
4. 在同一 `step_slot` 写入 `revision = old.revision + 1` 的新 StepRevision；
5. 旧枝、旧 revision、旧检查、旧候选永久保留。

重放器拒绝重复 `step_revision_id`、原地重写、错误前缀、错误 revision 号或人改步骤没有对应 HumanAction。

### 4.3 状态

状态：`active`、`paused`、`parked`、`completed`、`killed`。

- `paused`：可恢复的人为/调度暂停；
- `parked`：仪器、预算或配置条件未满足；不构成科学失败；
- `completed`：writer 声明达到题面目标，可以冻结 Candidate；
- `killed`：路线终止，正文仍保留在附录。

允许转换由 machine validator 固定。硬伤可使 `completed -> killed`；零正文仪器失败可使 `active -> parked`。暂停和砍枝不得共用同一 reason code。

若 Run 给出正整数 `max_active_branches`，任何创建/恢复使 active 数超帽都被拒绝；若为 `null`，契约不施加数量帽。V1 没有默填数字。

## 5. StepRevision：五栏、不可变、精确来源

每个已封存步骤必须有五个非空字段：

1. `claim`：这一步主张什么；
2. `why`：为什么现在走这一步；
3. `source`：依据是什么；
4. `derivation`：推导过程；
5. `scope`：适用范围与假设。

`output_sha256` 是五栏对象 canonical JSON 的 SHA-256。

来源：

- `origin.kind = model`：引用完成态 writer ModelCall；调用 target 必须是同一 branch/slot，输出哈希必须等于步骤哈希；
- `origin.kind = human`：只能用于 replacement revision，引用 `revise_step` HumanAction，动作内容哈希必须等于步骤哈希。

五栏缺失、空字段或哈希错误属于确定性格式故障，由程序拒绝；不能交给 checker 自由裁量为“可能有硬伤”。

输出被截断但已有正文时，后续 runtime 可用多个 ModelCall 完成同一尚未封存步骤；只有五栏完整后才能产生 StepRevision。正文为零时不产生 StepRevision。

## 6. ModelCall：仪器读数与科学判断分开

生命周期：

```text
started -> finished
        -> failed
        -> aborted
```

`model_call_chunk` 可保存 `analysis`、`body`、`raw` 通道，index 连续，文本自带 SHA-256。完成事件保存完整输出文本、输出哈希、字符数、finish reason 和 usage；失败/中止保存部分文本、哈希、字符数和失败类别。

角色：`writer`、`checker`、`judge`；actor kind 必须分别为 `model`、`checker`、`judge`。每次调用的 provider、model、effort 必须等于 Run 中该角色的冻结配置；若要换模型或 effort，应开新 Run，而不是只改一次调用。writer 只能瞄准当前 active branch 的下一个 slot；checker / judge 只能瞄准仍处于 requested 状态的对象；同一角色与 target 不允许有两个同时在途的调用。

`body_chars = 0` 的失败调用是 `zero_body_instrument_failure` 一类仪器读数。它可以把 Branch park，但不得：

- 生成 StepRevision；
- 生成 Check hard defect；
- 计作物理路线失败；
- 被拿去“续写空正文”。

## 7. Check：绑定精确 revision/hash，权限只是否证

### 7.1 请求与完成

`check_requested` 保存：

- `check_id`
- `target_step_revision_id`
- `target_output_sha256`
- 是否为 Candidate 必需检查
- 请求原因

`check_completed` 必须重复同一 revision/hash，并引用精确 checker ModelCall。任一不一致都拒绝重放。人改出新 revision 后，旧检查只属于旧 revision，永远不会迁移到新步骤。

### 7.2 verdict

- `ok`：没有发现可执行异议，不是正确性证书；
- `objection`：理由或路线值得人审，但不足以自动砍枝；
- `hard_defect`：满足白名单，可自动终止包含该 revision 的路线；
- `instrument_failure`：checker 调用失败，候选被阻塞而非判错。

### 7.3 hard defect 白名单

至少一条带原文引用的 evidence，kind 只能是：

- `ancestor_quote`：与被检查路线前缀中的已封存正文直接矛盾；
- `hypothesis_quote`：与被检查路线 lineage 中的明示工作假设直接矛盾；
- `scope_quote`：违反本步明示适用范围；
- `task_constraint_quote`：违反题面硬约束。

“我不喜欢理由”“另一条路线更好”“可能少一步”“最终答案不像参考”只能是 objection。checker 不得代写下一步、改正文、发终稿通过证书或自行选 Candidate。

### 7.4 晚到结果

Candidate 保存必需 check ID 快照。声明时仍 pending 则为 `provisional`；晚到结果重算状态：

- 任一 pending：`provisional`
- 任一 instrument failure：`blocked`
- 任一 hard defect：`rejected`
- 全部完成且无 hard defect/instrument failure：`eligible`

旧 revision 的晚到结果只能影响包含该 revision 的 Candidate，不得污染 replacement Candidate。

Candidate 声明后不得再给其包含的 revision 追加“必需检查”；否则可以通过先声明候选逃避检查。

## 8. Candidate：不可变全文快照

writer 或人宣布完成时，创建 Candidate，保存：

- `branch_id`
- `tip_step_revision_id`
- 顺序固定的 `transcript_step_revision_ids`
- `transcript_sha256`
- `required_check_ids`
- 声明者与理由

Candidate 必须等于 Branch **当时完整正文**；后续 Branch 恢复、增长、被砍或派生子枝都不改变旧 Candidate。Selection 不能只指向 branch id。

`objection` 不自动剥夺 eligibility，但必须在终审视图显示；终稿 judge 可以据此给 fail/near_pass。

## 9. Judgement：终稿判断是独立对象

所有可评审完成稿都通过 `judgement_requested` 与 `judgement_completed` 形成独立 Judgement。它绑定：

- `candidate_id`
- `candidate_transcript_sha256`
- judge ModelCall
- verdict：`pass`、`near_pass`、`fail`、`instrument_failure`
- 原因与可选数值分数

只有 `eligible` Candidate 才能请求和完成 Judgement。judge 仪器失败不等于终稿 fail。步级 check 全部 ok 也不自动产生 pass Judgement。

## 10. Selection：哈希一致、资格仍有效、终审通过

V1 每个 Run 最多一份 Selected submission。`selection_recorded` 必须同时满足：

1. Candidate 存在，当前仍为 `eligible`；
2. 事件中的 Candidate transcript SHA-256 完全一致；
3. 引用的 Judgement 指向同一 Candidate/hash；
4. Judgement 已完成且 verdict 为 `pass`；
5. 人工选择时先有 `select_candidate` HumanAction。

若选中后又出现使 Candidate 失格的事件，完整日志验证失败；runtime 必须先撤销/重做选择，而不能保留一份已失格 Selected submission。

其余完成 Candidate、失败路线和被砍路线都保留，作为附录，而非静默删除。

## 11. 人工 provenance 与对外声称

HumanAction 至少保存 actor、动作、精确 target、理由、内容和内容哈希。内容为空的纯操作（暂停、恢复、砍枝）显式记 `null`。

canonical replay 对每条 Branch、Candidate、Selection 计算：

- `human_touched`
- `content_class = model_only | human_steered | human_edited`
- `steering_action_ids`
- `edit_action_ids`
- `operational_action_ids`

归因按 lineage 计算：

- 人给方向/工作假设：`human_steered`；
- 人直接写过步骤：`human_edited`，优先级高于 steered；
- 只有模型内容：`model_only`。

暂停、恢复、砍枝等操作另外列出，不自动把正文归为 human-edited。Selection 自身由人执行时，Selection 的 `human_touched` 为 true，但 Candidate 的正文 provenance 不被倒写。

从旧枝派生 human revision 不污染旧枝：旧 Branch/Candidate 仍按自己的祖先与步骤计算 provenance。

对外报告至少分开：模型独立完成、人给方向完成、人直接改文完成；不得用一个模糊 `human_touched` 抹平差异。

## 12. Canonical state 与 HTML

重放器输出 `derivation-agent-canonical-v1`，包含：Run、Branches、StepRevisions、ModelCalls、Checks、Candidates、Judgements、HumanActions、Selections、summary 和 event-log head hash。

机器结构由 `docs/spec/derivation_agent_canonical_v1.schema.json` 约束。canonical JSON 必须按排序规则 byte-stable；同一 event log 在同一规范版本下重放，输出逐字节相同。

HTML 是同一次 verified replay 产生的只读审计页，必须显示：

- Branch 状态与内容 provenance；
- Candidate 资格、精确哈希和必需检查；
- Judgement 与 Selection；
- 完整事件哈希链和 payload。

HTML 不是控制面板，不得向事件流外写隐藏状态。

## 13. Golden fixture 最低覆盖

本规范的 golden record 必须覆盖：

1. 根枝正常写两步；
2. 模型提出替代枝；
3. 人指定方向派生枝；
4. writer 零正文失败，Branch parked，未生成 StepRevision；
5. 人修改旧步骤，建立 replace 子枝与新 revision，旧路线永久保留；
6. 根 Candidate 在检查 pending 时 provisional；
7. 晚到 hard defect 只使根 Candidate rejected；
8. replacement Candidate 检查完成后 eligible；
9. 独立 Judgement pass；
10. 只有 replacement Candidate 被合法选择；
11. body、seq、`prev_event_sha256` 或 event hash 被篡改时 verify 失败；
12. JSON、canonical JSON、HTML 能 byte-for-byte 重建。

fixture 中的模型调用全部是 `mock:*` 记录，不发网络请求，不构成物理实验数据。

## 14. 文件与命令接口

稳定文件：

- 本规范：`docs/spec/DERIVATION_AGENT_RECORD_V1_cn.md`
- event schema：`docs/spec/derivation_agent_event_v1.schema.json`
- canonical schema：`docs/spec/derivation_agent_canonical_v1.schema.json`
- stdlib package：`src/derivation_agent_record/`

离线命令：

```sh
PYTHONPATH=src python3 -m derivation_agent_record verify EVENT_LOG.jsonl

PYTHONPATH=src python3 -m derivation_agent_record build EVENT_LOG.jsonl \
  --canonical CANONICAL.json \
  --html AUDIT.html

PYTHONPATH=src python3 -m derivation_agent_record.selftest
```

所有命令不需要 key，不调用 provider，不读取 `reference/`。

## 15. V1 明确不做

- 不选择模型、effort、粒度、步长或活枝帽；
- 不实现 streaming provider、并发锁、durable database 或运行 UI；
- 不建设 RAG、知识库、本体、符号证明或自动合枝；
- 不把 Check 当终审；
- 不把仪器失败当科学失败；
- 不运行任何特定科学题目；
- 不修改此前已冻结的题面、裁判配置或历史 tag；
- 不读取 `reference/` 作为 fixture 或 prompt 输入。

## 16. 变更控制

任何改变以下语义的修改必须升级规范版本，不得静默兼容：

- StepRevision 是否不可变；
- human revision 是否必须派生 Branch；
- Check 的 revision/hash 绑定；
- event hash-chain 算法；
- Candidate 的 transcript snapshot；
- eligibility 与晚到 hard defect；
- Judgement 的 Candidate/hash 绑定；
- Selection 的 pass 前置条件；
- provenance 分类。

新增可选字段通常也需要 `v1.1` 或明确迁移说明，因为 V1 schema 默认拒绝未知字段。旧事件永远按它声明的 schema version 重放，不自动升级或覆盖。
