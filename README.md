# Question Bank

独立题库仓库：PostgreSQL 18 数据库、Review UI、媒体文件、题目与讲解修订、审核发布、agent 录入工具及课堂导出。

运行时只使用 PostgreSQL。SQLite 只供显式、只读的旧数据导入，不作为自动回退。原教学仓库及现有数据没有删除或改写。

## 本机启动

需要 Docker Desktop；Compose 与 CI 使用官方 `postgres:18` 镜像。复制 `.env.example` 为 `.env`，将示例密码换成长随机密码（字母和数字等 URL 安全字符），然后：

```sh
docker compose up --build -d
```

打开 http://localhost:8890 。数据库和媒体分别保存在持久化卷中；停止容器不会删除数据。不要执行 `docker compose down -v`，它会删除卷。

PostgreSQL 18 的数据卷挂载在 `/var/lib/postgresql`，镜像默认数据目录为 `/var/lib/postgresql/18/docker`（[官方镜像说明](https://hub.docker.com/_/postgres#pgdata)）。Compose 使用单独的 `postgres18-data` 卷；旧版 PostgreSQL 17 卷不能直接挂载给 18。已有数据应先用原版本和本工具备份，再恢复到新的空 PostgreSQL 18 schema 与媒体目录；此配置不会自动迁移旧卷。

本版本沿用原 Review UI 的访问模型，默认只绑定本机。公网部署前需要接入登录、权限和 HTTPS；不要直接将管理端口向公网开放。模型调用配置按需另行提供；不开启模型调用也可录入、编辑和审核。

## Python 开发

```sh
uv sync --frozen --extra test
export QB_DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/question_bank'
export QB_DATABASE_SCHEMA=question_bank
.venv/bin/question-bank init --root ./data
.venv/bin/question-bank serve --root ./data
```

`--root` 只决定媒体、输入和导出文件位置。数据库由 `QB_DATABASE_URL` 和 `QB_DATABASE_SCHEMA` 唯一决定；换一个 root 不会自动创建另一套数据库。一个运行中的题库使用一个固定的数据目录。测试和预检使用独立 PostgreSQL schema。

运行账号需要在指定数据库中创建 schema/table 的权限（启动迁移及隔离预检）；不需要超级用户权限。生产环境可后续拆分迁移账号和运行账号。

## 旧数据迁移

先停止旧库写入，做好原始备份。向一个全新的 PostgreSQL schema 和空媒体目录执行：

```sh
.venv/bin/question-bank import-sqlite --source /path/to/legacy-bank-root --root ./data
.venv/bin/question-bank verify --root ./data
```

导入保留题目 ID、修订、审核、发布、教学内容、回执与媒体 hash。源 SQLite 以只读方式打开；目标非空时拒绝覆盖；缺媒体或不完整旧 schema 会报错，不生成替代内容。支持现有完整管理库，不直接导入旧 YAML 题库。正式存量迁移仍需先确认实际使用的源库。

## 备份与恢复

```sh
.venv/bin/question-bank backup --root ./data --dest /path/to/new-backup
.venv/bin/question-bank verify --root /path/to/new-backup
# Restore requires a NEW, empty PostgreSQL schema and media directory.
QB_DATABASE_SCHEMA=restored_bank .venv/bin/question-bank restore --source /path/to/new-backup --root /path/to/restored-data
```

备份包含全部业务表的逻辑快照及 assets/inputs/exports 文件。不要只备份 PostgreSQL 而遗漏媒体。备份期间使用数据库事务锁协调应用写入；不要让外部脚本绕过应用直接修改业务表。迁移或恢复失败后保留文件作为诊断证据，数据库事务回滚；重试选择新的空目标目录。

## 边界

- 本仓库负责题库内容与审核，不包含学生课堂运行时、几何执行器或教学 agent 编排。
- `question-bank-agent prepare/new/read/fill/check/submit` 是 agent 的材料和录入工具；`check` 在一次性 PostgreSQL schema 中预检。
- DOCX 提取助手随包提供，原卷转页仍需 LibreOffice/Poppler 等系统工具。录音探测需要 ffmpeg/ffprobe（Docker 镜像已安装）。
- 课堂以导出内容包为接口；`QB_TEACHING_TOOLS_ROOT` 是可选的外部投递位置，未设置时仍可生成本地导出。
- 媒体支持本地持久化目录及 OSS；OSS 证据页录入、资产摘要校验与拒绝回执重放已修复并通过云端复验，详见下方验收报告。

## 测试

设置指向专用 PostgreSQL 18 测试数据库的 `QB_DATABASE_URL`，运行 `.venv/bin/python -m pytest`；安装 Node 依赖后运行 `npm ci && npm run test:ui` 验证实际管理页面。测试使用真实 PostgreSQL，每个测试建立独立 schema，结束后删除；不调用真实付费模型。迁移前的 SQLite 内部 DDL 测试由 PostgreSQL 迁移、并发及只读导入测试替代。

本机 PostgreSQL 18 的真实版本、测试数量、持久化重启和证据见 [PostgreSQL 18 验证报告](docs/postgresql18-validation.md)。本机验证不代表阿里云 RDS PostgreSQL 18 已验证。

录入到 RDS 与 OSS 的业务约束、程序代数及性能口径见 [录入不变量与性能验收规范](docs/ingestion-invariants-and-performance.md)。真实 RDS/OSS 的本轮结果见 [2026-10-22 录入验收报告](docs/ingestion-acceptance-2026-10-22.md)：F01–F03 已修复，严格契约及真实云端功能复验通过；30 分钟持续负载 SLA、独立媒体灾备等仍未验证，报告保留初验失败证据。

小图录入优化、分阶段耗时与测试数据库隔离说明见 [性能优化报告](docs/ingestion-performance-2026-10-22.md)：同轮 P95 从 1052 ms 降至 477 ms；该轮 190 项严格回归通过；相关优化已随下述远程录入服务部署。

进一步的大图与并发优化见 [第二轮优化报告](docs/ingestion-scale-optimization-2026-10-22.md)：每题约 4 MiB、4 并发下，短时吞吐从 0.53 提升至 2.08 题/s，保留事务锁和全部摘要校验。

外部电脑可通过同一服务远程录入：`question-bank-agent upload --package ./candidate --server https://your-server`，或现有 `submit --materials … --task … --server …`；仅需服务地址与 Basic Auth 凭证，无需数据库或 OSS 配置。查询使用 `question-bank-agent receipt --server … --submission-id …`。直连方式保留；包限制、超时后恢复和完整命令见 [远程录入指南](docs/remote-ingestion.md)。

当前主机 Review UI 和远程录入 API 共用 8890，`question-bank.service` 已启用开机启动并重启验证。配置、管理命令与验证边界见 [服务器启动说明](docs/server-startup.md)。最新远程功能完整回归 221 项通过；部署后认证、UI、健康检查及新接口只读验证通过。
