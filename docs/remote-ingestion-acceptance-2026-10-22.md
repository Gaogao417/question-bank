# 远程录入验收（2026-10-22）

远程客户端只用地址与 Basic Auth 凭证完成真实 HTTP 上传；服务端复用 RDS/OSS 录入逻辑，首次创建媒体、重放、冲突、超时查回执和错误包处理均通过。首轮 pytest 发生 OSS 环境隔离失误，留下未删除的普通 assets 对象，详见下文；不能宣称本轮全部 OSS 测试资产已清理。

## 实现与结果

接口为 `POST /api/submissions` 原始 ZIP 和 `GET /api/submissions/{submission_id}` 回执查询。部署入口 `serve_public:app` 同时保护 UI 与 API，未另建服务或调度层。客户端有 `upload --package`、远程 `submit --materials --task`、`receipt --submission-id`；保留直连提交。用法及边界见 [远程指南](remote-ingestion.md)。

远程传输拒绝超过 64 MiB 的压缩体、超过 128 MiB 的实际解压总量、超过 512 个成员、重复成员、路径穿越、链接及包外引用。schema 先校验，非法顶层/字段结构返回稳定 JSON。原始请求 ZIP、解压包、最终回执/错误归档于 inputs/remote；归档写入失败不能返回成功，提交后归档失败仍可查询数据库权威回执。网络结果未知不会被解释为未提交。

| 验收项 | 结果 |
| --- | --- |
| 完整严格回归 | 通过：221 passed，64.893 秒（runner 65.79 秒），0 failures/errors/skips，专用随机数据库，OSS SDK 默认禁止真实构造 |
| 未认证真实 HTTP | 401，认证中间件在业务处理前拒绝 |
| 无 DB/OSS 配置的 CLI 首次上传 | 通过：4 个真实 PUT，4 个对象 SHA-256 读回一致 |
| 相同包重放 | 通过：replayed=true，最终仍只有 1 题 |
| 同键异候选内容 | 通过：409 / SUBMISSION_ID_REUSED |
| 实际网络超时后查询 | 通过：CLI --timeout 0.01 返回结果未知，随后原 ID 查询成功 |
| 非法 schema | 通过：422 / SCHEMA_INVALID，JSON 错误 |
| 原始包与回执 | 通过：5 个请求各保留 ZIP，3 个回执、2 个错误 |
| 专用 HTTP 测试数据库及前缀对象清理 | 通过：数据库删除，4 个 acceptance 随机前缀对象删除后核对不存在 |

真实端到端在本机独立随机 loopback 端口运行 uvicorn，使用实际 `serve_public.BasicAuthMiddleware`、临时 root、独立随机 RDS 数据库及真实 OSS 的随机 acceptance 前缀。客户端以子进程运行，清除了全部 QB_* 与 ALIBABA_*，再仅设置 QB_SERVER_URL/USER/PASSWORD；没有客户端数据库或对象存储凭证。服务端路径代理只增加测试前缀，读写仍为真实 SDK 网络请求。

W：C=1，顺序闭环，1 个首次新题请求及重放/冲突/错误恢复请求；4 张 512×512 噪声 PNG 共 2,276,811 字节，ZIP 2,049,092 字节，首次资源为冷对象。首上传 CLI 端到端 **1694.48 ms**，其中服务端接收完整请求后的录入及归档 **974.64 ms**；CLI 计时还含 Python 启动、压缩包生成和 loopback 传输。不是跨公网时延测量，只有 1 个冷上传样本，不能计算可靠 P95/P99 或持续负载 SLA；无模型或人工审核耗时。

证据：[真实 HTTP 完整结果](acceptance/2026-10-22-remote/http.json)、[探针](acceptance/2026-10-22-remote/http_probe.py)、[最终回归日志](acceptance/2026-10-22-remote/pytest.txt)、[JUnit](acceptance/2026-10-22-remote/pytest.xml)、[回归隔离与清理](acceptance/2026-10-22-remote/environment.json)、[安全 runner](acceptance/2026-10-22-remote/test_runner.py)。此前 HTTP 探针因前缀适配器漏了 NoSuchKey 的业务错误翻译而返回 503，尚未产生对象；修正探针后通过，保留 [失败证据](acceptance/2026-10-22-remote/http-initial-prefix-wrapper-error.json)。

## 首轮 OSS 隔离失误与未清理对象

首轮 pytest 使用独立随机数据库，但错误继承了真实 QB_OSS_BUCKET，未关闭未被 FakeOSS fixture 覆盖的 SDK 调用。该轮 6 项失败，其中 5 项为原本预期本地媒体的测试意外走 OSS，1 项为新增测试的错误码预期，均保留 [原始日志](acceptance/2026-10-22-remote/pytest-initial.txt)、[JUnit](acceptance/2026-10-22-remote/pytest-initial.xml)、[环境记录](acceptance/2026-10-22-remote/environment-initial.json)。环境记录的初始 isolation 文本曾错误声称 fake OSS，已更正并在此说明。专用测试数据库已删除，正式题库表没有用于该轮测试写入。

只读审核在 06:29:50–06:32:15 UTC 时间窗内发现普通 `assets/` 下 12 个 LastModified 命中对象，内容 SHA-256 均匹配 key，当前正式配置 schema 的 assets 表均未引用。缺少每次 PUT 日志和测试前对象快照，不能证明它们全是新建、完全属于该轮或其他数据库没有引用，因此 **没有删除这 12 个对象**，不能宣称全量清理。具体 key、字节、时间与核查限制见 [只读 OSS 影响审核](acceptance/2026-10-22-remote/oss-initial-audit.json)。

后续 runner 清除 QB_OSS_* 和 ALIBABA_CLOUD_* 环境，pytest 的 autouse fixture 另外禁止真实 `_bucket` / `_public_bucket` 构造，防止再次遗漏环境隔离。真实 HTTP probe 不经过 pytest fixture，所有读写均强制随机 acceptance 前缀并在 finally 中清理核对。第二轮隔离回归 214 项通过，见 [中间日志](acceptance/2026-10-22-remote/pytest-intermediate.txt)；随后新增网络非 JSON/异常 JSON、提交后回执归档失败等回归并执行最终完整测试。

## 边界

Basic Auth 是部署入口级授权，不提供每题/每卷/租户隔离。归档持续增长，需要现有备份和容量管理；没有 GC。未验证 30 分钟持续负载、真实跨公网吞吐、强制断电后的归档恢复、独立 OSS 灾备或对象防覆盖策略。此次功能验收不替代已有录入不变量及性能规范中的这些项目。未调用付费模型；HTTP 测试使用独立端口，未修改生产 8890 题库内容。

部署与开机启动由同一 [systemd 服务](server-startup.md) 管理，UI/API 共用 8890；本报告的隔离 HTTP 测试不能代替实际部署后的只读验证。

## 实际部署验证

主代理已将 UI/API 共用的 question-bank.service 安装并启用开机启动，重启后 enabled/active/running。认证后 UI 和 healthz 返回 200，匿名提交返回完整 401，OpenAPI 包含提交和回执路由，随机不存在的回执返回 404。已确认正式服务进程具有 OSS 凭证；没有通过正式服务提交测试题。详见 [部署记录](acceptance/2026-10-22-remote/deployment.json)；未执行整机重启。
