# Question Bank 录入不变量与性能验收规范

本规范约束题目从候选包进入 PostgreSQL RDS 与 OSS 的过程，并覆盖重试、人工修订、审核发布和恢复。验收分为业务不变量、故障恢复与性能三部分；任一强制不变量失败，不能判定整体通过。验收结果见 [本轮报告](ingestion-acceptance-2026-10-22.md)。

## 领域状态与程序代数

令系统状态 `S = (D, O)`，D 为 RDS 业务事实，O 为 OSS 对象。D 包含题目 Q、不可变修订 V、资产 A、引用 R、请求回执 K、审核 H 与发布指针 P。

```text
Request = (bank, submission_id, base_revision, payload, assets)
Digest(r) = SHA256(Canonicalize(完整语义载荷与资源摘要))
Submit : State × Request → State × Receipt
Receipt.status ∈ {accepted, accepted_with_issues, noop, rejected, conflict}
Success(r) ≡ status(r) ∈ {accepted, accepted_with_issues, noop}
Stored(q,v) ≡ 修订、引用、校验记录及成功回执均已提交
Publishable(q,v) ≡ 结构校验通过 ∧ 无阻断疑点 ∧ 有效人工批准
Published(q,v) ≡ 当前发布指针指向该修订且未撤回
```

**录入成功不等于审核通过或发布。** 当前实现允许明确声明缺答案、缺解析等疑点的候选进入审核。管理端可读待审修订，学生消费应取已发布修订。此前抽象的 READY 在录入计时中映射为 Stored，不能将它作为当前数据库已有枚举；`PENDING → READY/FAILED` 仅适用于未来引入持久化异步录入任务的设计。

bank 当前映射为部署数据库/schema 边界；当前 schema 没有 tenant_id，不能据此声称支持同一 schema 内多租户隔离。对象身份应包含 `(bucket, key, version或不可变内容摘要)`，仅 key 不能区分不同 bucket。

## 强制不变量

| 编号 | 代数约束 | 验收断言 |
| --- | --- | --- |
| INV-01 有效性 | `Stored(q,v) ⇒ StructurallyValid(q,v)`；`Published(q,v) ⇒ Publishable(q,v)` 在发布提交时成立 | 空题干、非法选择题答案、未绑定图片拒绝；缺答案须显式记录疑点；有阻断疑点或无有效批准不得发布。程序不能证明数学答案正确。 |
| INV-02 资源完整性 | `Stored(q,v) ⇒ ∀a∈Refs(q,v): Exists(O,a) ∧ SHA256(Bytes(a))=a.sha256` | 检查题图、答案图、解析图、证据原页、source_asset 与讲解音频；注入同 key 异内容、缺对象，必须拒绝成功或阻断受影响内容消费。覆盖缓存命中路径。 |
| INV-03 提交原子性 | `Read(q)∈{完整旧修订, 完整新修订, 不存在}` | 修订、引用、current_revision、校验与回执在 RDS 同一事务内提交；任意提交点故障不能出现半题。上传后失败允许留下未引用资产，不能留下引用缺失资产的成功修订。 |
| INV-04 幂等性 | `Same(bank,id) ∧ SameDigest ⇒ SameCommittedResult`；`Same(bank,id) ∧ DifferentDigest ⇒ Conflict` | 顺序/并发重复提交只产生一个语义结果；重放允许 replayed 标志变化。成功、拒绝、冲突等持久化回执均按同一规则处理。唯一约束是最终保障。 |
| INV-05 成功回执 | `ReturnSuccess ⇒ Committed(D) ∧ AssetsVerified(O)`；`Timeout ⇏ Failure` | 在提交后丢弃响应，再查询回执或同键重试，应恢复原结果；不能因为响应丢失重复建题。异步 ACCEPTED 若引入，只表示任务持久化，不表示题目完成。 |
| INV-06 并发与历史 | `Save(q,base) succeeds ⇒ base=CurrentRevision(q)`，检查与写入不可分割 | 并发人工修改只有一个获胜；旧修订、人工修改、审核事实不被新模型输出覆盖；新编辑不能自动移动已发布指针。 |
| INV-07 授权隔离 | `ReadOrReference(actor,a) ⇒ Authorized(actor,a)` | 部署边界、数据库权限、对象权限与应用认证必须同时明确。HTTP Basic Auth 只能证明入口认证，不能证明题目级或租户级授权。多租户启用前必须验证跨租户引用和幂等隔离。 |
| INV-08 安全回收 | `Delete(a) ⇒ NoLiveRef(a) ∧ NoActiveLease(a) ∧ PastGrace(a)` | 活跃引用包括历史修订、证据、音频及备份保留范围。检查与删除须并发协调；共享资源不得因一次录入失败而删除。没有 GC 时，不能宣称已实现最终回收。 |
| INV-09 恢复完整性 | `RestoreSuccess ⇒ SameBusinessFacts ∧ SameReferencedBytes` | 恢复到空 schema/目录，核对历史、回执、审核、发布与资产 SHA-256。OSS 引用清单不是独立媒体备份；还需可恢复的对象版本/副本及保留策略。 |

INV-02 的持续成立还依赖对象权限与删除策略。内容寻址只规定命名，不能单独防止对象被覆盖或删除；必须用写入/读取校验和保护策略落实。RDS 外键只能证明资产行存在。

## 活性与故障恢复

令 `◇` 表示最终发生，活性成立的前提是依赖恢复、调用方或任务执行器持续重试且调度公平：

```text
DurablyAccepted(r) ⇒ ◇(Stored(r) ∨ TerminalFailed(r))
EligibleOrphan(a)  ⇒ ◇Deleted(a)
```

当前 submit 是同步工具调用，不持久化调度异步录入任务。不得为满足抽象状态机引入 agent 调度服务；重试由现成 agent/调用方负责。可重试错误需退避、上限和最终诊断；进程退出不能等价于请求未提交。

| 场景 | 必须观测的结果 |
| --- | --- |
| 上传前失败或 OSS PUT 失败 | 无成功题目回执、无新成功修订；调用方得到可诊断错误 |
| OSS PUT 完成后 RDS 写入失败 | RDS 事务回滚；对象可暂留；重试不重复修订 |
| RDS COMMIT 后响应丢失 | 原请求标识查询/重试返回原题号与修订 |
| 相同幂等键并发或相同基线并发编辑 | 单一结果或明确冲突；旧回执、人工修订保留 |
| 写入中 worker 退出并重启 | 不出现半题；按原请求重试可收敛 |
| 对象被删除或同 key 内容被替换 | 存在性/摘要检查阻断不完整内容；不能只验证图片仍可解码 |
| GC 与发布并发 | 发布引用对象不被清理；回收涵盖全部持久化引用 |
| 备份后原对象不可用 | 从受保留的版本/副本恢复原字节，不能以重新生成内容替代 |

## 性能口径与候选门槛

性能报告必须记录 `W=(λ,C,B,A,D,R)`：到达速率、并发数、每题资源大小分布、资源数量分布、持续时间和重放比例；另记客户端与 RDS/OSS 区域、服务实例与连接池规格、缓存冷热状态、数据规模和网络路径。未绑定这些条件的 QPS/P99 不作为验收证据。

`t_received` 为服务收到完整候选包开始处理；`t_stored` 为 RDS 完成提交；`t_response` 为调用方收到回执。材料准备、模型生成和人工审核时间不计入录入延迟。客户端资源上传若在独立阶段发生，必须单独记录并另报包含上传的端到端耗时。

| 指标 | 计算方式 | 候选门槛或判定 |
| --- | --- | --- |
| 完整录入延迟 | `L_store=t_stored-t_received`；客户端另报 `L_response` | 纯文本 P95 ≤ 300 ms，P99 ≤ 800 ms；含图/音频按大小档单独评估 |
| 异步接收延迟 | `L_ack=t_durable_accept-t_received` | 仅在异步接口存在时：P95 ≤ 200 ms；当前同步提交不适用 |
| 有效吞吐 | `μ=新成功录入题目数/观测秒数` | 排除 noop、重放、拒绝；达到指定 λ 且无持续积压 |
| 成功率 | `成功的有效逻辑请求/有效逻辑请求` | ≥ 99.9%；输入无效单列，系统超时/错误不能剔除 |
| 重试放大 | `实际尝试次数/逻辑请求数` | 正常无故障负载接近 1；故障期与正常期分开 |
| 排队与锁等待 | 排队时间 P99、最老任务年龄、RDS 锁等待 | 当前关注 schema 级写锁等待；未设业务阈值，不判通过 |
| 回收时间 | `t_deleted-t_eligible` 的 P99 | GC 未实现前未验收；上线前确定宽限期和期限 |
| 一致性违规 | 各 INV 的反例数 | 必须为 0，不能被高成功率抵消 |

上述数值继承讨论中的候选目标，不是既有 SLA。正式验收负载：预热 5 分钟，稳定观测至少 30 分钟；建议分别使用 C=1/4/16，λ=1/5/10 请求每秒，资源 0/1/4 个及 100 KiB/1 MiB/10 MiB 档。单独测 0%/10% 重放、冷热对象、网络故障与恢复，不能混合样本掩盖尾延迟。纯文本题仍须满足来源证据要求；无法构造合规无资源输入时将该档标为不适用并解释。

每次请求保留单调时钟起止、逻辑请求 ID、状态、字节数、重试次数；P95/P99 用排序后 nearest-rank 分位数，失败率以全部有效逻辑请求为分母；超时报告为超时并单独给出超时阈值，不能当作快速成功。性能工具不得调用付费模型或伪造人工批准。

## 验收执行与证据

1. 将 `QB_DATABASE_URL` 指向专用临时 PostgreSQL 测试数据库，不能仅更换数据 root。使用项目 `.venv/bin/python`。pytest 各测试再隔离 schema；真实 OSS 验收使用独立随机测试前缀。
2. 执行存储契约与完整 Python 回归，保存日志、退出码及 JUnit。已知缺口用 strict xfail 跟踪，正式契约验收使用 `--runxfail`，不得把 xfail 计为通过。
3. 执行上述故障场景和真实云端往返，核对媒体 SHA-256、回执与数据库记录。模拟 OSS 的通过不能替代真实 OSS。
4. 在约定 W 下压测，保存请求级样本。短时 smoke 仅用于诊断，不能替代 30 分钟负载验收。
5. 清理本轮数据库和独立对象前缀，验证清理结果；不删除业务库、历史资源或用户已有测试环境。

```sh
# QB_DATABASE_URL 必须已指向专用测试库；回归中的 OSS 使用 fake
QB_OSS_BUCKET= .venv/bin/python -m pytest -ra
QB_OSS_BUCKET= .venv/bin/python -m pytest tests/test_ingestion_storage_contract.py --runxfail -ra
```

报告每项必须使用“通过、失败、未验证、不适用”之一，并附实际执行环境与证据。代码审阅可支持风险判断，不能冒充并发/事务/故障运行证据；历史 PostgreSQL 本机验证也不能替代本轮 RDS 验收。
