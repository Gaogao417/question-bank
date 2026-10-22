# Question Bank RDS 与 OSS 录入验收报告

**修复复验结论：F01–F03 已修复，严格契约和真实云端功能验收通过；持续负载性能仍未验证。** 历史失败证据完整保留在本文后半部分，不能再用其代表修复后状态。

后续性能优化已完成：同轮小图负载 P95 从 1052 ms 降到 477 ms，完整严格回归 190 passed，保留全部摘要校验。详见 [录入性能优化与隔离报告](ingestion-performance-2026-10-22.md)。下方 1730 ms 为优化前历史测量。

验收依据为 [录入不变量与性能规范](ingestion-invariants-and-performance.md)。初验只新增规范、验收用例与证据；随后修复业务实现并完成下述复验。

## 修复后复验

F01：证据页通过存储适配器读取并校验 SHA-256，OSS 无本地副本可正常录入，真正缺失仍拒绝。

F02：现有对象复用、PUT 后读回、图片/音频读取及裁图源引用均校验内容寻址摘要。禁用字节缓存命中捷径，每次重新读取存储，避免对象替换或删除被热缓存隐藏。代价是更多 OSS GET；尚无对象版本感知缓存。

F03：拒绝回执使用与成功路径一致的完整请求语义及资源摘要，在事务内先检查旧回执。同键同载荷重放，同键异载荷冲突，原回执保持不变。历史使用旧 `rejected:` 摘要的拒绝回执不改写；由于旧摘要未保存完整原载荷，无法安全推定其与新请求等价，此类旧键仍返回冲突，调用方应使用新键。

原 strict xfail 均已移除，新增热缓存替换/删除、音频摘要、拒绝同错误异载荷和缺失 OSS 证据断言。人工修订、历史、审核、发布数据结构与写入逻辑保持现有约束；没有迁移业务数据、提交 git 或部署。

| 复验项 | 结果 | 证据 |
| --- | --- | --- |
| 完整回归 `--runxfail` | 通过：188 passed，69.23 秒，退出码 0，无 xfail | [日志](acceptance/2026-10-22-ingestion-fixed/pytest.txt)、[JUnit](acceptance/2026-10-22-ingestion-fixed/pytest.xml)、[隔离与清理](acceptance/2026-10-22-ingestion-fixed/environment.json) |
| 两组严格契约 `--runxfail` | 通过：15 passed，3.08 秒，退出码 0，无 xfail | [日志](acceptance/2026-10-22-ingestion-fixed/rds-oss-contract.txt)、[JUnit](acceptance/2026-10-22-ingestion-fixed/rds-oss-contract.xml)、[隔离与清理](acceptance/2026-10-22-ingestion-fixed/rds-oss-environment.json) |
| 真实 RDS + OSS | 通过：30/30 accepted；丢响应重放、提交前回滚/重试、21 张表恢复及 4 个对象 SHA-256 核对通过；P95 1730.34 ms、P99 1922.67 ms；数据库及对象清理均核对成功 | [JSON 含请求级样本](acceptance/2026-10-22-ingestion-fixed/cloud.json)、[日志](acceptance/2026-10-22-ingestion-fixed/cloud.txt)、[探针](acceptance/2026-10-22-ingestion-fixed/cloud_probe.py) |
| 持续负载与 SLA | 未验证 | 30 次顺序短时 smoke 不能替代预热 5 分钟、持续 30 分钟及多并发/资源大小档验收。纯文本门槛不适用于本次每题 4 个图片资源。 |

W：C=1，顺序闭环无固定到达率，30 个新请求，计时阶段重放比例 0%；每题 4 张小 PNG，精确字节数及每请求耗时见 JSON。调用方在当前主机，RDS 为 PostgreSQL 18.6；未获取实例、连接池与网络区域规格，不能泛化性能。首请求创建 4 个对象，后续复用相同 key 并逐次读回校验；计时包含服务内 OSS/RDS，不含包生成或模型。响应耗时并非数据库提交时刻的独立 L_store 测量。

剩余未验证范围：30 分钟性能、真实进程强杀、跨租户/题目级授权、业务 GC/租约/宽限期、OSS 防覆盖权限、对象版本或独立媒体副本保留。真实恢复核对当前 OSS 引用与字节，未模拟原对象丢失后从独立副本恢复，不能视作独立媒体灾备通过。直接预签名 URL 的消费由 OSS 提供字节，不经过应用 SHA 检查；持续完整性仍依赖对象不可变策略。

修复首轮回归因新增测试调用了错误方法名而 1 failed、186 passed；修正测试后重新执行完整回归。首轮云端成功证据与回归失败证据保留于 [attempt01](acceptance/2026-10-22-ingestion-fixed/attempt01/)。以下为初验历史，失败状态只适用于修复前代码。

## 初验历史记录

## 环境与隔离

- 日期：2026-10-22；证据 JSON 时间使用 UTC，北京时间为 UTC+8。
- 仓库：`/home/admin/question-bank`；检查时 HEAD 为 `20cbf07d35b44c1aa3a8ac30f9b3e2753b27d08b`，另有本轮新增测试和文档。
- 数据库：真实阿里云 RDS，服务端返回 `PostgreSQL 18.6`。每轮创建独立 `qb_acceptance_<随机值>` 数据库；pytest 再按测试创建隔离 schema。
- OSS：Python 回归使用 FakeOSS；云端探测使用现有服务配置中的真实 bucket，所有写入均经适配器限定到独立 `acceptance/<随机值>/` 前缀。该适配器仅改变测试对象命名空间，未替代真实 PUT/GET/存在性操作。
- 8890：匿名请求 HTTP 401；使用已有配置认证后 `/healthz` 返回 HTTP 200、`ok=true`。未执行浏览器 UI 全流程。
- 所有本轮临时数据库及 OSS 前缀对象均已清理并核对。没有修改正式题目、审核、发布或原有媒体；未调用模型。

## 实际执行结果

| 验收项 | 结果 | 证据与边界 |
| --- | --- | --- |
| 完整 Python 回归，真实 RDS 专用测试库 | 179 passed、2 xfailed，62.99 秒，退出码 0 | [日志](acceptance/2026-10-22-ingestion/pytest.txt)、[JUnit](acceptance/2026-10-22-ingestion/pytest.xml)、[环境与清理](acceptance/2026-10-22-ingestion/environment.json)。此轮包含原有 173 项及新增离线存储 8 项；下方两个 RDS/OSS 契约测试随后单独执行。xfail 不计为验收通过。 |
| 离线存储严格契约 | 6 通过、2 失败，退出码 1 | [严格模式日志](acceptance/2026-10-22-ingestion/strict.txt)；已使用 `--runxfail` 实际触发断言失败。 |
| RDS/OSS 新增严格契约 | 2 失败，1.20 秒，退出码 1 | [日志](acceptance/2026-10-22-ingestion/rds-oss-contract.txt)、[JUnit](acceptance/2026-10-22-ingestion/rds-oss-contract.xml)、[隔离清理](acceptance/2026-10-22-ingestion/rds-oss-environment.json)；真实 RDS + FakeOSS。 |
| 真实 OSS 上传与逐对象 SHA-256 读回 | 通过，4 个对象 | [云端 JSON](acceptance/2026-10-22-ingestion/cloud.json) 的 `real_oss_hashes_verified=4`；这是探针主动校验，不表示业务读取路径已实现摘要校验。 |
| 真实 RDS + OSS 候选提交 | 失败 | 同一 JSON 中 `status=rejected`，两个 `EVIDENCE_ASSET_UNAVAILABLE`；实际 1 次请求，0 次成功。 |
| 云端成功提交后的响应丢失、事务故障与备份恢复 | 未验证 | 云端探针在首个提交失败后停止；脚本中后续断言未执行。已有回归的本地媒体/FakeOSS 结果不能替代这一项。 |
| 持续负载性能 | 未验证 | 原计划短时 30 次 smoke，首次失败即停止。唯一失败请求约 900.26 ms；它不是成功录入延迟，也不足以计算成功 P95/P99 或 SLA。 |

离线 6 项通过覆盖：重复资产身份及避免重复上传、上传失败不返回成功、内容变化保留原对象、缺失对象读取报错、预检不写 OSS、完整备份校验识别同 key 损坏内容。完整 Python 回归还覆盖原有成功回执重放、并发人工修改冲突、半题防护、审核发布门槛和恢复等用例。

## 失败项与修复验收条件

### F01 OSS 证据页被当作本地文件校验

对应 INV-01 的合法输入可录入要求及端到端功能门槛。`QuestionBankService.validate_content` 查询证据资产行后，用 `(self.root / row["path"]).is_file()` 判断可用性；OSS 模式不在本地保存字节，合法证据因此被拒绝。真实探针确认 4 个对象可读且摘要正确，排除了这次失败由上传缺失或字节损坏引起。

定位：[service.py](../question_bank/service.py) 的 `validate_content`；复现：[test_candidate_with_oss_evidence_is_accepted](../tests/test_ingestion_rds_oss_contract.py)。修复后须在无本地资源副本时成功录入，并保留真实缺失证据的拒绝行为；再执行真实云端探针后续事务和恢复检查。

### F02 OSS 内容寻址缺少读取与复用时的摘要校验

对应 INV-02。`AssetStore._persist` 遇到已存在 key 直接返回；`_load_bytes` 从 OSS 取得字节后直接返回。将 key 对应对象替换成另一张有效 PNG 后，重复登记和读取均未报错，两条严格测试失败。能解码和 key 命名正确都不等价于内容摘要正确。

定位：[assets.py](../question_bank/assets.py)；复现：[存储契约测试](../tests/test_ingestion_storage_contract.py) 末尾两项。修复后错误摘要必须被拒绝，校验应覆盖缓存入口与所有资源种类；对对象覆盖和删除的存储权限保护仍需单独验证。

### F03 完全相同的拒绝请求不能重放原回执

对应 INV-04。空答案且未声明疑点的候选首次返回 `rejected`；原包、原 submission_id 重试时，`_persist_rejected_submission` 再次 INSERT 导致唯一键冲突，转为 `SUBMISSION_ID_REUSED`，没有返回原拒绝结果。

定位：[service.py](../question_bank/service.py) 的 `_persist_rejected_submission`；复现：[test_identical_rejected_submission_replays_original_receipt](../tests/test_ingestion_rds_oss_contract.py)。修复后同键同载荷重放、同键异载荷冲突；拒绝请求摘要须覆盖请求语义，不能只比较错误消息。

## 不变量判定与剩余验收

| 规范项 | 本轮判定 |
| --- | --- |
| INV-01 校验与审核 | 已有校验/发布门槛用例通过；合法 OSS 候选被拒，端到端功能失败 F01 |
| INV-02 完整性 | 失败 F02；真实 OSS 本轮上传字节正确不消除该反例 |
| INV-03 原子性 | 原有回归通过；真实 OSS 与 RDS 组合故障注入未验证 |
| INV-04 幂等 | 成功路径回归通过；拒绝路径失败 F03 |
| INV-05 成功语义 | 原有回执恢复用例通过；真实云端丢响应情形未验证 |
| INV-06 并发和历史 | 原有真实 RDS 并发编辑、历史与发布指针用例通过；进程强杀/过期 worker 未验证 |
| INV-07 授权 | 8890 入口认证通过；同 schema 多租户未实现，跨租户与题目级授权未验证 |
| INV-08 回收及活性 | 未验证；没有得到业务 GC、租约与宽限期机制证据；探针清理不是业务 GC |
| INV-09 恢复 | 本地/FakeOSS 回归通过；真实 OSS 恢复及对象版本/独立副本保留未验证。默认 verify 仅查存在，full 才校验摘要。 |
| 性能门槛 | 未验证；先修复 F01，再按规范固定负载、大小分档和持续时间测量 |

## 复现方式

先将 `QB_DATABASE_URL` 指向专用测试库，再执行严格验收；不得直接在业务库跑整套 pytest。

```sh
QB_OSS_BUCKET= .venv/bin/python -m pytest \
  tests/test_ingestion_storage_contract.py \
  tests/test_ingestion_rds_oss_contract.py --runxfail -ra
```

[真实云端探针](acceptance/2026-10-22-ingestion/cloud_probe.py) 要求已加载部署的 RDS/OSS/8890 认证环境变量，账号具备建库权限。它自动建立并清理临时测试数据库与独立对象前缀；失败时保存具体回执。脚本重跑会覆盖同目录 `cloud.json` 和调用方重定向的日志，复验前应归档本轮证据。

```sh
.venv/bin/python docs/acceptance/2026-10-22-ingestion/cloud_probe.py
```

云端前两次运行同样触发 F01；分别保留 `cloud-attempt01.*` 与 `cloud-attempt02.*`。最终一次增加了真实对象 SHA-256 核对。脚本自身在失败时的 AssertionError 是验收断言，业务错误以回执中的 `EVIDENCE_ASSET_UNAVAILABLE` 为准。
