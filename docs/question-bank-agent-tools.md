> 独立仓库入口：安装后可用 `question-bank-agent`。数据库通过 QB_DATABASE_URL/QB_DATABASE_SCHEMA 配置；`--root` 是媒体目录。下面 Windows 命令示例在 macOS 使用 `.venv/bin/python`。预检需要 PostgreSQL，并使用自动清理的独立 schema。

# 客户端 agent 录入工具：可直接使用

2026-09-27。入口：`python -m question_bank.agent_tools`。这是材料/候选工具，不调用模型、不启动 agent、不裁图；转写和来源核对由 Codex/Claude Code 中的 agent 完成。

在 teaching_skills 仓根运行，Windows 使用 `.venv/Scripts/python.exe`。本工具调用现有 A/B `QuestionBankService`，不复制数据库、审核或发布实现。

## 1. 准备材料

已有提取结果时：

```powershell
& ./.venv/Scripts/python.exe -m question_bank.agent_tools prepare --word-dir "documents/初三/<归档>/word" --paper-id <卷号> --out "build/agent-ingestion/<卷号>/materials"
```

答案分卷再次传 `--word-dir ".../word-answers"`，顺序为试卷、答案。尚未提取的 DOC/DOCX 复用现有工具，不写逐卷脚本：

```powershell
& ./.venv/Scripts/python.exe .codex/skills/math-docx-question-bank-ingestion/scripts/extract_docx_source.py "<原卷.doc>" "<归档>/word" --soffice "C:/Program Files/LibreOffice/program/soffice.exe"
```

输出 `README.md`（页图和插图路径索引）、`paragraphs.md`（只作定位辅助）、`materials.json`（来源指纹、页图 ID、可用原图 ID）。不覆盖已有目录；需重建时用新目录。

页图 ID 如 `src1-p001`、`src2-p001`，保留分卷身份；提交到 A/B 时用全局连续页号避免两份第一页混淆，原始分卷映射保留在提交快照中。

插图筛选采取保守策略：排除 OLE/formula、不可读或不支持的格式、短边小于 64px 的碎片/窄条、非完整归属结果，以及非 high 的归属提示。只保留可直接读取的 PNG/JPEG/WebP，不做转换或图片修复。短边阈值比旧 harness 的最长边阈值严格，是为排除真实 DOC 中失去 OLE 标记的宽公式条；会漏掉部分小插图，符合宁缺勿滥。

尺寸和归属仍是保守启发式，不是图像语义证明。agent 须对照原页确认所选图确为本题插图；无法确认就不引用，标待人工补图。不要求使用白名单中的所有图。

## 2. 为一道题创建工作目录

```powershell
& ./.venv/Scripts/python.exe -m question_bank.agent_tools new --out "build/agent-ingestion/<卷号>/Q001" --number 1 --type problem --points 4
```

创建后先读取 `stem.md`、`answer.md`、`solution.md`、`meta.json` 四个文件（包括空文件），再编辑。可执行 `agent_tools read --task <题目录>`，一次返回四份内容及各自的 SHA-256。若使用客户端 Read/Write 工具，仍需遵守该客户端的先读后写要求。

agent 编辑 `stem.md`、`answer.md`、`solution.md` 三个普通文本文件。小问、选项和解答步骤自然写在正文里，不拆数组。题型支持 choice/fillin/short_answer/problem。

`meta.json` 只编辑少量管理信息，例如：

```json
{
  "question_number": 1,
  "question_type": "problem",
  "points": 4,
  "section_title": "",
  "base_revision": null,
  "question_pages": ["src1-p001"],
  "solution_pages": ["src1-p010", "src1-p011"],
  "images": [
    {"key": "fig1", "illustration": "src1-fig023", "role": "stem"}
  ],
  "issues": []
}
```

这些页号/插图 ID 仅是示例，必须替换为材料索引里真实存在且对应本题的 ID。跨页列齐全部中间页。正文用 `![题图](asset:fig1)` 引用所选完整原图；工具检查原图 high 归属的题号和 prompt/solution 角色与当前题一致。提示不符时不强配，留给 UI。

没有可用图：`images` 留空，正文不写虚假图片引用，在 issues 中声明：

```json
{"issue_id":"missing-figure","type":"missing_image","target":"stem","reason":"原页有图，请人工截图补入","evidence":"src1-p001","blocking":true,"state":"open"}
```

答案/解析确实缺失可留空，同时声明 `missing_answer` / `missing_solution`。不得用自己的解答冒充原答案。agent 只声明 open 问题，不能写人工解决/批准记录。

## 3. 预检与提交

```powershell
& ./.venv/Scripts/python.exe -m question_bank.agent_tools check --materials "build/agent-ingestion/<卷号>/materials/materials.json" --task "build/agent-ingestion/<卷号>/Q001"
& ./.venv/Scripts/python.exe -m question_bank.agent_tools submit --materials "build/agent-ingestion/<卷号>/materials/materials.json" --task "build/agent-ingestion/<卷号>/Q001" --root "<A/B 管理根>" --actor codex-agent
```

`check` 在临时数据库复用 A/B 完整接收校验，不写目标库、不批准题目。内容忠实与否仍需 agent 对照真实页图核对。

`submit` 要求 QB_DATABASE_URL/QB_DATABASE_SCHEMA 指向已初始化的 PostgreSQL 题库，不自动新建业务库。先预检，再保存不可覆盖的 `submissions/<提交ID>/` 原包（含所用媒体、页图和 materials 快照），最后调用现有 service；每次回执单独保存到 `receipts/`。相同内容生成稳定提交 ID，响应丢失可安全查询/重放；修改内容生成新提交 ID。已存在题目的不同内容仍受 A/B 的版本/防覆盖规则保护，不自动更新人工修订。

禁止裁框、旋转、任意图片路径、页图当插图和外部图片标记；未知元信息字段也拒绝。程序只能复制白名单完整原图，模型不截图、不输出坐标代裁。原始材料变化会拒绝提交，要求重新准备。

工具约束作用于此 agent 入口；底层 A/B 接口仍保留人工 UI 裁图能力。不能把这个本地工具当权限隔离系统，agent 必须遵守 AGENTS.md，不绕过入口直接写库或编辑白名单。

## 4. 查询与接续：复用 A/B 命令

```powershell
& ./.venv/Scripts/python.exe -m question_bank.cli status --root "<管理根>" --paper-id "<卷号>"
& ./.venv/Scripts/python.exe -m question_bank.cli receipt --root "<管理根>" --submission-id "<回执中的ID>"
```

新 agent 先看已有候选和回执，继续缺题/待修题；不用模型运行时或后台任务服务。需要修改已有人工作业时走现有局部建议/人工 UI 流程，不用首次录入工具强行覆盖。

## 验证范围

测试文件 `tests/test_qb_agent_tools.py` 覆盖材料筛选、禁止裁图/外链、跨页缺页、错题图片、原文件变更、缺图待审、分卷页号、幂等接收及保护人工修改。真实 DOC 提取产物另做材料准备冒烟。

本次工具交付不等于整卷转写或真实 UI E2E 已通过；后续按 C/D/E 计划由客户端 agent 实际录入，并操作 Review UI 验收。


### 插图归属修复（2026-09-27）

`prepare` 现在从原始段落重新计算归属，不再信任旧 `word-source.yaml` 中缓存的归属。
“参考答案与试题解析”等独立标题重置题号；答案区重复题干仍属于 prompt，分析/解答后的图属于 solution。
带分值的题号与普通解答步骤分别处理；没有明确章节边界的带分值重编号拒绝自动归属。
同一媒体被不同题复用时不进入高置信度白名单。

旧 materials 缺少 `attribution_version: section-aware-v2`，check/submit 会要求重新 prepare。
请输出到新的材料目录，检查每题的图片引用与角色后再使用；不要覆盖原材料或批量改写已入库记录。
`src1-figNNN` 是媒体列表序号，不等于 Word 的 `imageNNN` 文件名；以完整路径和段落归属核对。
算法提供结构归属，不能代替对原页的语义核对。缺少段落或题号断裂时保留原页，图片待人工确认。


## 受保护填写与明确反馈

逐文件写入示例（入口仍为 `python -m question_bank.agent_tools`）：

```powershell
& ./.venv/Scripts/python.exe -m question_bank.agent_tools read --task "<题目录>"
& ./.venv/Scripts/python.exe -m question_bank.agent_tools fill --task "<题目录>" --file stem.md --input "<正文.txt>"
& ./.venv/Scripts/python.exe -m question_bank.agent_tools fill --task "<题目录>" --file meta.json --input "<新元信息.json>" --expected-sha256 "<read 返回的该文件哈希>"
```

`fill` 默认只写空文件；替换非空文件必须携带读取时的哈希。哈希冲突后重新读取、合并，不自动重试覆盖。
每次只替换一个文件；同一题的 fill 调用串行并使用原子替换。meta.json 写入前做字段验证。
不要并行填写与提交同一道题。此保护覆盖使用 fill 的调用者，不拦截外部编辑器或任意脚本直接写文件。
进程异常退出可能留下 `.fill.lock`；确认没有写入者后再清理该锁，不自动抢锁。

`check` 输出 `preflight_only: true`、`submitted: false`、`content_approved: false`、`preflight_status`、`open_blocking_issues` 以及本题引用图的原文件名和 SHA-256。
`content_status` 为兼容旧调用保留，是临时库接收状态，不表示正文正确或目标库接收。
所有白名单图的文件名和哈希也在 materials.json / README.md 中提供。
`submit` 以目标库回执为准；accepted_with_issues 允许入库待审，阻塞疑点不因提交而解决，不能据此批准或发布。

批量调用保存完整 JSON，不用 grep/截尾提取 accepted。退出码 0 表示本次操作成功，1 表示业务拒绝，2 表示输入/执行错误。
预检非零就停止该题提交；预检为零仍需读取疑点数、确认来源内容和提交条件。预检不保证目标库版本检查通过。
以下示例保留完整输出，并让有阻塞疑点的题显式留待处理；需要带疑点入库待审时可单独执行 submit：

```powershell
& ./.venv/Scripts/python.exe -m question_bank.agent_tools check --materials "<materials.json>" --task "<题目录>" > "<本次check.json>"
$checkExit = $LASTEXITCODE
if ($checkExit -eq 0) {
    $checkResult = Get-Content -Raw "<本次check.json>" | ConvertFrom-Json
    if ($checkResult.ok -and $checkResult.open_blocking_issues -eq 0) {
        & ./.venv/Scripts/python.exe -m question_bank.agent_tools submit --materials "<materials.json>" --task "<题目录>" --root "<管理根>" > "<本次submit.json>"
        $submitExit = $LASTEXITCODE
        # 读取完整 submit JSON 和 submitExit；失败保留记录，不覆盖历史。
    }
}
```

`status --paper-id <卷号>` 仅列目标卷已入库题目；省略参数仍列全库。空列表表示没有已入库题，不代表整卷完成；待写候选仍需查看本地目录。

## 远程服务入口（2026-10-22）

外部 agent 可将上述 submit 的 `--root` 换为 `--server https://your-server`，配置 `QB_SERVER_USER` / `QB_SERVER_PASSWORD`。也可用 `QB_SERVER_URL` 默认地址。此路径不初始化/连接本地 PostgreSQL，不需要 OSS 密钥；全部业务校验和上传由服务器执行。`--root` 与 `--server`（含环境默认值）不能同时使用，切换直连时先取消 `QB_SERVER_URL`。

已经准备好 `candidate.json` 与全部引用资产的目录，可直接执行：

```sh
question-bank-agent upload --package ./candidate --server https://your-server --receipt-out ./receipt-001.json
question-bank-agent receipt --server https://your-server --submission-id your-submission-id
```

`upload` 不修改输入包，`--receipt-out` 必须是新的、位于包外的文件。远程材料 submit 仍在任务内保存不可覆盖的原包和各次回执。远程 submit 不执行依赖本地 PostgreSQL 的 check；服务器执行同一套完整校验。超时先查询原 submission_id，再原样重放；带疑点接收仍不能批准或发布。详细 API、格式、64/128 MiB 限制及错误码见 [远程录入指南](remote-ingestion.md)。
