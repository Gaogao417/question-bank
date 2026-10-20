# 独立题库提取记录 — 2026-10-20

用户要求：数据库使用 PostgreSQL；将题库相关代码抽成独立 Git 仓库。用户确认目录 `/Users/gaochong/develop/question-bank`。

## 已提取

- 原 `scripts/question_bank` → 可安装的 `question_bank` 包；包含现有管理 UI、媒体、内容模型、修订、审核、发布、教学笔记、导出、CLI 与 agent 材料工具。
- 原 DOCX 提取/图片归属助手一并纳入，不再访问原仓库 `.codex/skills`。
- 原题库业务测试、浏览器回归脚本及业务设计资料。
- Python 依赖锁、Docker/Compose 启动配置、CI 测试配置。

## PostgreSQL 改造

- Psycopg 连接、事务性 SQL 迁移、迁移内容校验；唯一运行后端。
- 使用 PostgreSQL 占位参数、identity/RETURNING 和 ON CONFLICT。
- 写事务采用数据库级 advisory lock，保留旧业务的串行写和乐观版本检查；适合当前管理服务，不宣称完成高并发多租户架构。
- agent 预检与测试使用独立 schema，避免写入正式题库。
- 逻辑备份包含全部业务表及 assets/inputs/exports；恢复到空目标。
- SQLite 显式只读导入，保留 ID、修订、审核、发布、媒体与回执；不提供 SQLite 运行回退。

## 验证

- 真实本机 PostgreSQL 17，完整 Python 测试：166 passed。
- 新增并发编辑冲突、迁移事务回滚、只读 SQLite 导入、序列恢复和禁止隐式 SQLite 回退测试。
- 真实 Chromium 管理 UI 回归通过：改文、换图、删除图、刷新、服务重启、新浏览器上下文、审核、导出、发布、409 冲突与本地草稿保留。
- Compose 配置校验、Python 编译检查通过。
- 在最终目录重新安装锁定的 Python 依赖和独立 Node 依赖后，真实浏览器再次全部通过；日志为 artifacts/extraction-20261020/ui-standalone.txt。
- 测试使用固定候选和模型替身；没有真实付费模型或语音生成验证。浏览器测试验证功能流程，不代表所有外部 MathJax 资源的联网渲染已验证。
- 日志和浏览器截图保存在 gitignored `artifacts/`，保留此前失败轮次。

## 当前边界

- 原仓库代码及实际数据均保留；新仓库是独立开发入口。没有在原仓库删除模块或切换正在运行的服务，避免破坏旧调用者。
- 尚未选择/迁移正式存量题库，未部署到云端，未创建远程 Git 仓库。
- 媒体仍用持久化本地目录，未实现对象存储适配。
- 原管理界面没有用户登录系统，Compose 仅绑定本机；公网发布需要单独落实访问控制。
- Docker 镜像构建/云端部署和 CI 远程执行尚未验证；本次实际验证使用新包与本机 PostgreSQL 容器。
- 课堂导出格式保持现状；本次未启动教学课堂消费导出，未宣称课堂端到端通过。
