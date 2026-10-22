# 远程候选录入

外部电脑只安装本仓库 Python 包并配置服务地址及登录凭证，不需要数据库地址、数据库账号、OSS endpoint 或密钥。服务端接收候选包，复用现有 schema、媒体摘要、原页证据、版本保护及幂等校验，写入 RDS/OSS 并保留原包和回执。此入口不调用模型，不运行 agent 调度，不批准或发布内容。

## 客户端

准备好的目录根部必须有 `candidate.json`，并包含它引用的全部图片及证据页文件。格式与现有直连 `QuestionBankService.submit_candidate` 一致；远程包仅接受 JSON 信封和包内相对文件引用，不接受已有 `asset_id` 代替文件。原始卷文件、材料快照也可一并打包。

```sh
export QB_SERVER_URL='https://your-question-bank.example'
export QB_SERVER_USER='your-user'
# 通过本机安全环境配置提供 QB_SERVER_PASSWORD；不要把密码写入仓库。
question-bank-agent upload --package ./prepared-candidate --receipt-out ./receipt-001.json
question-bank-agent receipt --submission-id your-submission-id
```

也可显式传 `--server URL --user USER`，密码优先用 `QB_SERVER_PASSWORD`；`--password` 可用，但可能留在命令历史及进程参数中。客户端地址不得包含用户名、密码、query、fragment。客户端不跟随 HTTP 重定向，避免把认证凭证送到另一主机。通过公网传输使用 HTTPS；8890 原始 HTTP 地址适合可信网络或 TLS 反向代理后的服务。

`upload` 的 `--receipt-out` 使用独占创建，选择包目录以外的新文件；重试使用新的回执文件名。完整保存 JSON 和退出码，不依赖截尾文本。退出码 0 表示成功，1 表示服务拒绝或网络结果未知，2 表示本地输入/执行错误。

已有材料工作流继续使用 `prepare/new/read/fill`，随后：

```sh
question-bank-agent submit --materials ./materials/materials.json --task ./Q001 --server "$QB_SERVER_URL"
```

远程 `submit` 只在本地构建并保存不可覆盖的 `Q001/submissions/<submission_id>/` 包，直接交服务器完整校验；不会调用本地数据库预检。每次结果写入 `Q001/receipts/`。本地 `check` 仍需要专用 PostgreSQL，不能将它当作无数据库远程预检命令。直连 `submit --root ./data` 原样保留；使用直连前需取消 `QB_SERVER_URL`。`--root` 和显式或环境默认 `--server` 冲突时明确报错，防止静默写错目标。

## API 与恢复

`POST /api/submissions` 接收 `application/zip` 原始 ZIP 字节；`GET /api/submissions/{submission_id}` 查询权威数据库回执。两个接口与 Review UI 同用 `serve_public:app` 的 Basic Auth。服务端身份为 `remote-agent`，凭证授权覆盖整个管理端，不提供每人/每卷细分权限。

成功响应形如 `{"ok":true,"receipt":{...}}`。accepted_with_issues 只表示带疑点入库待审。相同 submission_id 和候选内容重放返回原回执；同键不同候选内容返回 409 / SUBMISSION_ID_REUSED。语义摘要以业务候选内容和引用资产为准，不以 ZIP 时间戳或未引用附件字节为准。

| HTTP | 语义 |
| --- | --- |
| 200 | 接收成功或查询到回执 |
| 401 | 凭证缺失或错误 |
| 404 | 该 submission_id 尚无回执 |
| 409 | 同一 submission_id 复用为不同候选内容 |
| 413 | 压缩请求体超过 64 MiB |
| 422 | 包格式、schema、资产或业务校验不通过；详见 errors 或 rejected 回执 |
| 503 | 服务/归档故障，结果可能未知，先查询回执 |

ZIP 最多 512 个成员，实际解压总量最多 128 MiB，压缩请求体最多 64 MiB；拒绝重复成员、路径穿越、绝对路径、反斜杠/盘符、符号链接和特殊文件。引用必须能在本次解压目录内找到常规文件。服务端不从客户端机器或任意 URL 下载引用。

每次已读入限制内的请求保存在服务数据目录 `inputs/remote/<随机attempt-id>/upload.zip`，正常解压后保留 `package/`，最终保留 `receipt.json` 或 `error.json`；超限请求在读取时拒绝，不保留超大包。ZIP 与回执写入会 flush/fsync。磁盘故障在业务提交前会阻断提交；若数据库已提交、最终回执归档失败，返回 503/结果未知，不能把该响应当作未写入。归档不自动清理，须纳入容量监控和既有 inputs 备份策略。

默认网络超时 120 秒，可用 `--timeout` 调整。超时或连接中断时，响应包含 `outcome_unknown: true`；服务端可能继续完成。先以原 submission_id 查询回执，404 可能仅表示仍在处理；稍后继续查询或原样重放归档包。不要为重试生成新 submission_id，不要改包后复用旧 ID。没有异步队列、任务轮询状态或自动模型重试。

真实 HTTP、首次 OSS 上传、认证、重放、冲突与超时恢复结果见 [远程录入验收报告](remote-ingestion-acceptance-2026-10-22.md)。

服务部署及开机启动见 [服务器启动说明](server-startup.md)：UI 和远程 API 由同一 `question-bank.service` 的 8890 进程提供，无需另外启动录入服务。
