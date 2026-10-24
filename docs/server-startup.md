# Question Bank 服务器开机启动

Review UI 和远程录入 API 运行在同一个 8890 进程，由 `question-bank.service` 管理。UI、`POST /api/submissions` 和回执查询共用认证、题库业务逻辑、RDS 与 OSS 配置；不需要为录入再开启一个服务端口。

## 本机配置

服务单元模板位于 [deploy/systemd/question-bank.service](../deploy/systemd/question-bank.service)，本机安装位置为 `/etc/systemd/system/question-bank.service`。该模板明确针对当前主机：用户 admin、工作目录 `/home/admin/question-bank`、项目 `.venv`、监听 `0.0.0.0:8890`。

凭证位于 `/home/admin/question-bank/.qb.env`，权限为 0600，已被 Git 忽略。systemd 使用 EnvironmentFile 直接加载它，不会继承终端里的环境变量，也不读取 shell 启动脚本。文件须提供：

- `QB_DATABASE_URL`、`QB_DATABASE_SCHEMA`：正式题库连接。
- `QB_UI_USER`、`QB_UI_PASSWORD`：管理 UI 和远程录入共用的访问凭证；公网入口缺少密码会拒绝启动。
- `QB_OSS_BUCKET`、`QB_OSS_ENDPOINT`、`QB_OSS_PUBLIC_ENDPOINT`：存储配置。
- `QB_OSS_ACCESS_KEY_ID`、`QB_OSS_ACCESS_KEY_SECRET`：服务访问 OSS 的凭证；也支持原有 ALIBABA_CLOUD 同名后缀回退变量。
- 可选 `QB_ROOT`：材料与归档目录，默认 `/home/admin/question-bank/data`。不要用改变 root 的方式切换题库，数据库由连接配置决定。

跨电脑客户端只配置服务地址和访问凭证，不分发上述数据库或 OSS 密钥。裸 8890 为 HTTP，生产跨公网访问应使用 HTTPS 反代入口（见下文），或先通过 SSH 隧道访问，不把 Basic Auth 密码直接通过公网 HTTP 发送。

## HTTPS 反代入口（8443）

2026-10-24 起，本机 Caddy 在 `/etc/caddy/Caddyfile` 增加 `https://139.224.234.74:8443`，反代 `127.0.0.1:8890`，复用同一张 IP 证书；认证仍由服务的 Basic Auth 处理。云安全组放行 8443/TCP 后，浏览器与远程录入客户端统一走该入口，Basic Auth 密码不再经公网明文传输。

不挂在 443 子路径：题库前端写死 `/api/*`、`/static/*` 绝对路径，与同 IP 的 teaching-tools 站点冲突。部署当日已本机验证 healthz 未认证 401、认证后 200、UI 首页与静态资源正常；安全组放行前公网不可达。配置快照另存于服务器 `~/deploy/`（该目录不入库）。

## 安装与管理

以下操作会更新并重启本机服务。首次执行前，需准备好项目虚拟环境和上述私密配置。

```sh
cd /home/admin/question-bank
sudo install -m 0644 deploy/systemd/question-bank.service /etc/systemd/system/question-bank.service
sudo systemctl daemon-reload
sudo systemctl enable question-bank.service
sudo systemctl restart question-bank.service

systemctl is-enabled question-bank.service
systemctl is-active question-bank.service
systemctl status question-bank.service --no-pager
journalctl -u question-bank.service -n 50 --no-pager
```

`enabled` 表示启动项已加入 multi-user.target；`active` 表示当前进程运行。服务等待网络启动，运行前执行不访问数据库的配置检查；异常退出每隔 3 秒重试。正常停止不会被自动拉起。停止最多等待 120 秒，uvicorn 给在途请求 90 秒退出窗口。客户端遇到中断应查询提交回执，不应假设超时意味着未入库。

服务使用 UMask 0077 创建材料归档，禁止获取额外进程权限，并隔离临时目录。数据库及持久材料仍在原配置位置，升级服务不迁移或删除它们。

## 验证口径

部署验证检查 unit 模板、配置预检、enabled/active、重启后进程、认证前后 UI/healthz 与 OpenAPI 中的远程提交和回执路由。正式服务只进行无业务写入的请求验证；真实上传验收使用专用临时数据库和隔离 OSS 前缀。

启用启动项和重启服务不等于已做整机重启实验。本轮不重启整台主机，以免影响同机其他任务；启动挂接与服务实际重启均单独记录。

## 本次实际部署

2026-10-22 已安装仓库 unit、执行 daemon-reload、enable 和 restart。服务状态 enabled/active/running，重启后进程 PID 5474，自动失败重启计数为 0。实际 systemd 环境已验证 OSS 凭证加载；UI 与 healthz 认证后 200、未认证提交 401、远程路由存在、缺失回执查询 404。没有提交测试题到正式服务，也没有重启整台主机。[部署证据](acceptance/2026-10-22-remote/deployment.json)。
