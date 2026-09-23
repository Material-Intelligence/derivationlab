# 推导运行记录 1.1

日期：2026-09-07。状态：本批实现合同；明确版本扩展，不替换冻结 V1。

## 版本与兼容

`RunConfig.record_version` 默认为 `1.0`。1.0 继续使用原事件、canonical 标签及
配置形状；历史 run 的字节、hash 与读法不变。1.1 必须显式选择，一个 run 内
禁止混合版本，不把新行为静默塞入旧日志。

1.1 事件标签为 `derivation-agent-event-v1.1`，投影标签为
`derivation-agent-canonical-v1.1`。机器 schema 位于
`src/derivation_agent_record/schemas/event-v1.1.schema.json` 与
`src/derivation_agent_record/schemas/canonical-v1.1.schema.json`。
JSON Schema 检查形状，replay 另检查来源、hash、顺序与跨事件不变量。

1.1 配置增加 `checker_enabled` 与 `max_local_repairs`（默认 true、2）。
`max_model_calls=null` 表示无全局调用数预算；旧默认 100 不因此改变。
关闭 Checker 不发起其调用，不制造回执，也不表示科学检查通过。

1.1 生成循环不自动调用旧的题面内 terminal Judge。候选形成即结束生成，
review_ready 只表示可送独立终审，不是 pass。答案知情的终审在隔离的
post-run 阶段冻结输入、配置与输出；不把它写作生成侧的旧 judgement，不反馈
给 Writer。1.0 的旧自动 Judge 行为保持。

## 自主输出与修订

五字段正文 `claim/why/source/derivation/scope` 保留。Writer 输出控制包括
`continue/fork/complete/revise/blocked`，以及 `alternatives`、
`revise_step_revision_id`、`reason`。revise 必须指向当前路线的准确步骤 ID，
给出理由，正文是完整替换；blocked 必须给出卡点，不形成完成候选。
两种 Checker 模式有同样的自主修订权限。

新事件 `model_revision_applied` 由 model actor 发出，载荷为
parent_branch_id、branch_id、target_step_revision_id、step_revision_id、
model_call_id、reason、content。一次事件原子执行：

1. 核验已完成 Writer 调用的目标为父分支下一槽位，raw control 明确要求本修订；
2. 核验正文 hash 与不可变调用输出一致，新 ID 未用过，旧目标在父路线中；
3. 以旧目标之前的精确前缀建立 replace 子分支，正文占用旧槽位，revision 加一；
4. 父分支以 `model_superseded` 停放，保留全部旧后缀；旧后缀不进入新候选。

没有 HumanAction，不伪造人工同意；来源仍为 model_only。修订后重新建立有效
前缀的模型会话，不复用包含旧后缀的提供方历史。调用结束后、原子修订前崩溃，
恢复从同一持久化输出补齐一次修订，不重调模型。

连续修订按被替换步骤的 lineage 计数，不随 Checker 的措辞或 verdict 重置。
达到局部上限后，该目标暂从允许修订 ID 中移除，要求换子目标、带条件探索
或声明 blocked；一次新的普通推导段使其可以重新处理。模型若仍返回被禁修订，
`model_revision_deferred` 记录 system 来源的策略处置，保留原始调用，不把该正文
装成有效步骤。这个确定性边界不能判断新推导段是否具有实质科学进步；此判断仍
由科学检查与最终验收负责。局部边界不是全局调用或时间预算。

## 检查、条件候选与证据

Writer 的下一轮获得带准确 step ID 的 Checker 意见、引用、问题 lineage、
局部修订次数。1.1 不因模型的 hard_defect 自动杀死整个分支。它表示需回答的
有证据指控，不能被提升为机器已经证实的反例。实际确定性证伪必须来自可解释
验证器；此版不将普通计算输出或 LLM 布尔判断自动升级为科学真值。
目前尚未实现通用的确定性反证 hard gate；计算结果保留在工具审计中，AI 仍须
判断工具结果的适用性，并遵守不采用已知反例作为有效前提的指令。不能把这项
指令约束或计算工具可用性声称为已完成机器级数学反证识别。

候选保存 `unresolved_check_ids`，由其有效路线中 objection/hard_defect 检查
精确导出；存在这些记录时 status 为 `conditional`。条件候选可以送独立 Judge，
并携带未决检查；不能作为无条件已通过结果被自动 selection。旧修订目标的
意见保留在历史中，但不混入已被替换的新路线。工具或平台失败不是科学 verdict。

`source_evidence_registered` 只由 system 在第一次模型调用前注册方法来源，
包括 source_id、kind=literature_quote、完整快照正文、sha256。不允许同 ID 重绑。
Checker 的 literature_quote 必须来自该 registry，引用必须是快照的精确子串；
记录可独立复核原文。给模型的目录只含 ID/hash/读取指引，全文通过只读资料工具
按需读取，不在每轮直接注入全部论文。引用存在只证明出处，不证明物理推论成立。
未提供资料的运行 registry 为空，不能接受其他条件或答案侧的文献引用。

1.1 的 `ancestor_quote` 来源为可读五字段原文，按 claim、why、source、derivation、
scope 顺序，以 `字段名:\n原文` 和空行分隔。字段内的引号、反斜杠、换行与 Unicode
保持原值，与 `transcript_read` 返回的解码字段一致。目录 sha256 对此可读文本计算；
不得以 canonical JSON 的转义字符要求模型引用正文。runtime 与 replay 共用同一
来源构造函数；1.0 保持历史 canonical-JSON 来源表示。正文内容 hash 和事件 hash
继续按原 canonical JSON 规则计算，证据可读表示的改变不改变这些原始正文。

## 宿主格式规范化（2026-09-17 增补）

`writer_output_normalized` 由 system actor 发出，只用于 1.1，位于 Writer 调用的
`model_call_finished` 之后、由它产生的封存（`step_revision_sealed`、
`model_revision_applied` 或 `writer_route_completion`）之前；只有替换清单非空时才写。
载荷为 model_call_id、raw_output_sha256、content（规范化后的五字段）、
output_sha256（其 canonical JSON 的 hash）、policy、normalizer_version、replacements。

原始调用输出永不改写。replay 逐条核验：调用是已完成、未被消费、未物化的 Writer
调用；raw_output_sha256 等于调用输出 hash，原始输出是五字段 canonical JSON；
按清单顺序把每条替换应用到当时文本（`start/end` 指应用该条时的偏移，
`original` 必须逐字匹配），结果必须逐字等于 content，output_sha256 必须匹配且与原始
hash 不同。每条替换的 kind 与结构受限：

- `control_char_backslash`：original 是 U+0000–U+0008、U+000B、U+000E–U+001F 中的
  单个字符，replacement 为一个反斜杠，其后紧跟 ASCII 字母；
- `control_char_removed`：同一字符集中的单个字符删除，其后紧跟反斜杠加字母；
- `del_removed`：U+007F 删除，其后紧跟反斜杠；
- `ansi_escape_removed`：完整 ANSI CSI 序列删除；
- `macro_expansion`：original 以反斜杠加控制词开头，不在 `source` 字段，
  source_id 是已登记来源，source_line 在该来源行数内，且该行确实定义了这个宏名
  （`\newcommand` / `\renewcommand` / `\providecommand` / `\def` / `\gdef` /
  `\DeclareMathOperator`），original 恰好是「宏名 + 它的参数」（多一个字符都不行），
  replacement 括号配平、不含 `$ % # `` ` `` \( \) \[ \]`，并且**等于该定义对该 original
  的展开**——replay 用记录包内独立实现（`derivation_agent_record/macro_expansion.py`）
  自己算一遍，算不出同一结果就拒收。

控制类替换的 source_id 与 source_line 必须为 null。事件之后，该调用在
canonical 中带 `normalized_output`（事件 ID、原始与规范化 hash、policy、版本、
替换条数），模型步骤封存、模型修订与原路线完成都以规范化 hash 代替原始调用 hash
比对。没有该事件的调用与历史记录的比对规则、canonical 字节都不变。

替换是否"可证明无歧义"由运行时规则（`formula-normalization-v1`，见
`src/derivation_runtime/formula_normalization.py`）决定；replay 证明替换可逆、
结构合法、与封存内容一致，并且每条 `macro_expansion` 的 replacement 就是它所引
定义算出来的结果。

**replay 不能机械核验的部分（2026-09-17）**：

1. **出处选择**。运行时按「逐字归属 / 本步引用的来源 / 全部来源一致」三条规则挑
   一个定义；replay 只核验「你引的这一行确实这样定义」，不重算该不该引这一行。
   同名宏在两篇文献里定义不同、而宿主引了其中一篇时，replay 不会反对。
2. **该不该展开**。引擎白名单不在记录里，所以 replay 无法判断某个名字本该由引擎
   直接排版、因而根本不该被展开；也无法重建 `evidence_source_documents` 提供的
   「同一文档的多个部分」分组。
3. **嵌套展开**。运行时对定义体和参数递归展开。当单层代入的结果里已经没有任何
   已登记宏名时，replay 的单层核验是精确的；否则 replay 用「凡是已登记来源里唯一
   定义的名字都展开」这一无白名单近似再算一遍，两者都对不上才拒收。因此一个既是
   引擎命令又被手稿重定义的名字，理论上可能让合法记录被拒（fail closed），而不是
   让伪造记录通过。
4. **引文判定**。「这段公式是不是在引用文献原文」由运行时判断（见
   `formula_normalization` 模块文档）；replay 只强制 `source` 字段永不展开。

## 停止与上下文

`writer_blocked` 状态事件必须由模型 actor 引用本分支已完成且明确 blocked 的
Writer 调用，分支停放，运行显示 `paused / model_blocked`，不形成完成候选。
模型完成、模型不能继续、全局旧预算耗尽、工具失败分别表示。

正文原件全部保存在 append-only Record。长历史用近期完整段组成有界可见上下文，
并提供首段及近期段的 ID/claim/scope 目录；目录是导航，不替代证据。更早内容通过
`transcript_catalog` 分页查询，`transcript_read` 按本条件当前有效路线的步骤 ID、
字段与 offset 分段读取。模型会话定期重新建立，不能把上下文满当成永久等待。
revision 后仅新有效路线能被这些工具读取，历史原件仍留在审计记录中。

每条新检查都回馈，不能因较早步骤已有争议而跳过当前意见。收到当前 tip 意见后，
若模型决定保留该内容并交付条件候选，可完整重复 tip 正文并声明 complete。
`writer_route_completion` 核验当前 tip、已完成 Writer 调用、control 与正文 hash，
原子完成原路线；它是明确的完成决定，不产生重复科学节点或新检查。改变正文则
必须产生新的步骤并接受检查。

1.1 Writer/Checker 的已知调用失败以 `runtime_failure` 暂停，保留原始部分输出，
不能被显示成科学完成。显式用户 resume 后允许新尝试；Checker 原 instrument_failure
通过 `check_retry_authorized` 引用真实 resume HumanAction，退出当前 required 集合，
原回执与该授权事件均保留，然后创建新的 check。该事件不是把失败改写为 ok。
重建时提供方状态为 MISSING 的在途调用保持 started/RECOVERING，不能凭缺失句柄
推断远端已失败并盲重试。

Writer 检查意见上下文同样有界：显示近期意见摘要和目录，完整意见通过
`feedback_read(check_id, offset)` 回读；`check_id=catalog` 分页列出更早意见。
完整反馈与证据保存在本运行记录中，截短导航不改变原始意见。

## 验证边界

`test_reproduction_loop.py` 用可核对 fake 输出验证多轮、开关、自查修订、Checker
反馈、条件候选、模型卡点、恢复与版本隔离。旧 runtime/Record selftests 保持。
这些测试证明运行和记录行为，不能据此声称任何特定已发表结果已经复现。
