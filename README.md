# Question Bank

独立题库仓库：PostgreSQL 数据库、Review UI、媒体文件、题目与讲解修订、审核发布、agent 录入工具及课堂导出。

运行时只使用 PostgreSQL。SQLite 只供显式、只读的旧数据导入，不作为自动回退。原教学仓库及现有数据没有删除或改写。

## 本机启动

需要 Docker Desktop。复制 `.env.example` 为 `.env`，将示例密码换成长随机密码（字母和数字等 URL 安全字符），然后：

```sh
docker compose up --build -d
```

打开 http://localhost:8890 。数据库和媒体分别保存在持久化卷中；停止容器不会删除数据。不要执行 `docker compose down -v`，它会删除卷。

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
- 媒体暂用本地持久化目录。接对象存储属于下一步，不因数据库改为 PostgreSQL 而自动完成。

## 测试

设置指向专用测试数据库的 `QB_DATABASE_URL`，运行 `.venv/bin/python -m pytest`。测试使用真实 PostgreSQL，每个测试建立独立 schema，结束后删除；不调用真实付费模型。迁移前的 SQLite 内部 DDL 测试由 PostgreSQL 迁移、并发及只读导入测试替代。
