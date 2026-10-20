结论：独立题库已在本机真实 PostgreSQL 18.6 上通过完整 Python 测试、真实浏览器回归、备份恢复与持久化验证；阿里云 RDS PostgreSQL 18 尚未验证。

# PostgreSQL 18 验证 — 2026-10-20

## 范围与实际环境

- 项目 `/Users/gaochong/develop/question-bank`，起始提交 `dd18555`；本轮开始工作树干净。
- 真实 `SELECT version()`：`PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit`。
- 官方镜像 `postgres:18`，本次拉取 digest `sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336`；浮动标签后续可能更新，版本结论覆盖此次实际镜像。
- 独立容器 `question-bank-pg18-test-20261020`，端口仅 `127.0.0.1:55438`，测试数据库 `question_bank`；Python 3.12.14、Node.js v25.9.0、npm 11.12.1。
- 新建独立卷 `question-bank-pg18-test-data-20261020:/var/lib/postgresql`；服务端 `SHOW data_directory` 为 `/var/lib/postgresql/18/docker`。
- 所有内容均为本地固定测试材料；未导入正式题库，未调用付费模型，未修改原教学仓库、正式媒体或之前停止的 PostgreSQL 17 测试容器。

## 配置改动

Compose 和 GitHub Actions 数据库镜像改为 `postgres:18`。按 [Docker 官方镜像说明](https://hub.docker.com/_/postgres#pgdata)，18 开始默认 PGDATA 使用主版本子目录，持久卷挂载点改为 `/var/lib/postgresql`；[Docker 官方 PostgreSQL 指南](https://docs.docker.com/guides/postgresql/)也采用该挂载路径。

Compose 使用新的 `postgres18-data` 卷名，防止将已有 17 数据目录误用于 18；应用端口仍只绑定 `127.0.0.1:8890`。README 说明旧卷需要由原版本导出并恢复，不能仅换镜像自动升级；补充 UI 测试和本报告入口，更新环境变量示例的目标版本。没有改动数据库业务实现和原管理界面。

新增 `test_backup_restore_resets_all_identity_sequences`：对恢复后的 checks、reviews、publications、exports、teaching_note_reviews、blueprint_batches 六组 identity，检查已用序列下一值为原最大 ID 加一、空表下一值为 1；同时确认审核、发布和媒体完整性仍有效。

## 本轮命令与结果

连接串通过 `QB_DATABASE_URL` 指向 `127.0.0.1:55438/question_bank`；其密码只用于本轮本地测试。Python 测试自动为每个测试创建并删除独立 schema；UI 自动建立独立 `qb_e2e_...` schema。

| 验证 | 实际命令或动作 | 结果 | 本轮证据文件 |
| --- | --- | --- | --- |
| 真实服务版本 | `SELECT version(); SHOW server_version; SHOW data_directory;` | PostgreSQL 18.6，真实服务端目录符合 18 约定 | `postgresql-version.txt` |
| 完整 Python 测试 | `.venv/bin/python -m pytest` | **167 passed, 44 warnings, 89.39s**；退出码 0 | `pytest.txt` |
| 真实 Chromium UI | `npm run test:ui` | **35 个检查全部通过**；退出码 0，32.70s | `ui.txt`、`screenshots/` |
| 容器持久化 | 固定材料入库 → `docker restart` → 全量比对 → `docker stop` / `docker rm` → 原卷重新 `docker run` → 再比对 | 重启和不同容器 ID 重建均通过；21 张业务表与原快照相同，4 个文件 SHA-256 相同；r2、审核、发布指针保留 | `persistence-lifecycle.txt`、`persistence_probe.py`、`persistence/` |
| Compose 配置 | `POSTGRES_PASSWORD=... docker compose config --quiet`，再读取 JSON 作断言 | 语法、镜像 18、新卷挂载路径与本机端口绑定均通过 | `compose-config.txt`、`compose-assertions.txt` |
| Python 编译 | `.venv/bin/python -m compileall -q question_bank tests` | 退出码 0 | `compileall.txt` |
| 包构建 | `uv build --offline --out-dir artifacts/pg18-20261020/round-01/dist` | wheel 与 sdist 构建成功；退出码 0 | `build.txt`、`dist/` |
| 差异格式 | `git diff --check` | 退出码 0 | `diff-check.txt` |
| 清理 | 独立测试 schema 检查、停止本轮容器并核对状态 | Python/UI schema 均已删除；本轮容器 exited | `schema-cleanup.txt`、`cleanup.txt`、`docker-inspect-stopped.json` |

上述文件统一保存在 `/Users/gaochong/develop/question-bank/artifacts/pg18-20261020/round-01/`。镜像拉取、初次容器配置、重建后配置及服务日志亦在该目录。截图复制到该轮 `screenshots/`，原始 UI 截图路径保留在 `ui.txt`。`artifacts/` 按项目约定不入 Git。

完整 Python 测试实际覆盖：SQLite 只读导入前后源文件 hash 不变，修订/审核/发布事实保留，导入后的 review 序列继续增长，非空目标拒绝覆盖，原生备份恢复包含题图与录音并检测缺失/损坏媒体，迁移失败事务回滚，并发人工编辑只有一个保存成功、另一个获基线冲突。新测试补足原生恢复的全部 identity 序列。浏览器验证包含人工文字和图片修订、刷新、应用服务重启、新浏览器上下文、审核、导出、发布，以及 409 冲突后本地修改保留。

Python 的 44 个 warnings 涉及已有测试字符串转义、Starlette/httpx 和 FastAPI 生命周期弃用提示；未出现 PostgreSQL 18 兼容失败。首轮完整 Python 与 UI 测试均成功，未为状态检查重复执行。最终清理辅助脚本曾误用系统 Python，因缺少 psycopg 在执行任何操作前退出；改用项目 `.venv/bin/python` 后完成，原失败记录保留于 `cleanup-attempt01.txt`，不影响测试结果。

## 清理与未验证项

本轮创建的 PostgreSQL 18 测试容器已停止并验证 `Running=false`、`Status=exited`，保留容器、专用数据卷和唯一的 `qb_pg18_persistence_20261020` 合成数据 schema 供复核；没有删除其他卷、改变其他服务或启动之前的 17 容器。

本报告仅覆盖本机 Docker PostgreSQL 18。阿里云 RDS PostgreSQL 18 的创建、网络/SSL、账号权限、参数、备份策略、费用和实际连接未在本轮验证；不能将本机结论当成 RDS 通过。亦未执行云部署、正式存量数据迁移、GitHub Actions 远程运行、Docker 应用镜像完整构建、真实付费模型或课堂消费导出的端到端验证。UI 使用固定候选并拦截外部 MathJax 请求，不证明外部资源联网渲染。原 PostgreSQL 17 的 166 测试历史证据仍在提取记录，本报告所有通过数量均来自此次 18 的新日志。
