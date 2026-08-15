# MailPulse 架构说明

## 1. 组件关系

MailPulse 是一个 Python 单体应用，由网页服务、后台 worker、SQLite 数据库、Loguru 日志系统和外部邮箱/模型服务组成。

```text
Web UI ────────┐
               ├─ SQLAlchemy / SQLite
Worker ────────┘
    │
    ├─ IMAP：只读同步邮件
    ├─ MarkItDown：转换附件
    ├─ AI Provider：生成结构化内容
    ├─ SMTP：投递报告
    └─ Loguru：控制台与文件日志
```

正式源代码位于 `src/mailpulse/`，网页模板位于 `src/mailpulse/templates/`，本地样式和脚本位于 `src/mailpulse/static/`。

## 2. 邮件数据模型

- `CanonicalMessage`：规范化邮件，负责跨文件夹和重复同步时的邮件身份。
- `MessageOccurrence`：邮箱文件夹中的出现记录，保存文件夹、UIDVALIDITY 和 UID 等同步信息。
- `Mailbox`：用户的邮箱连接配置和同步游标。
- `Attachment`：附件元数据、转换状态和资源引用。

同步默认使用 IMAP 只读模式。应用内的标签、星标和已处理状态不会写回邮箱服务器。

## 3. 附件转换与模型编排

附件处理分为四个阶段：

1. 按策略下载附件并执行大小、类型和数量限制。
2. 使用 MarkItDown 将 PDF、Office、HTML 和文本等附件转换为 Markdown。
3. 保存 Markdown 和关联图片资源，记录转换状态与警告。
4. 根据模型能力选择直接处理或先调用 Vision 模型提取视觉证据。

模型输入由邮件正文、Markdown、图片资源和结构化视觉证据组成。模型服务不使用原生 PDF 或 Office 上传接口。

## 4. 权限与数据范围

应用使用本地账号和 Session 认证。账号以用户名（`users.username`，全局唯一、大小写不敏感）作为登录标识，账号不绑定邮箱。收件邮箱（IMAP 账户）与报告投递邮箱分离：任务通过 `mailboxes` 关联收件邮箱，报告投递目标由 `task_delivery_targets` 表按任务配置（网页查看渠道始终开启，SMTP 渠道支持每个任务配置多个收件人，未来可扩展更多投递方式）。管理员和普通用户使用不同的网页布局与路由组：

- 登录入口：`/login`；健康检查：`/healthz`。
- 管理员路由：`/admin`、`/admin/users`、`/admin/models`、`/admin/jobs`、`/admin/account/password`。
- 普通用户路由：`/`、`/tasks`（含 `/tasks/new` 与 `/tasks/{id}` 详情）、`/messages`、`/reports` 和 `/account`。

未登录或 Session 中的账号无效时，受保护页面返回 `303` 并跳转到 `/login`。登录后，管理员进入 `/admin`，普通用户进入 `/`；使用默认管理员首次登录时先进入账号密码设置页，该步骤可以跳过。

Session 使用签名 Cookie。未勾选“记住登录状态”时，Cookie 为浏览器会话级别；勾选后按 `MAILPULSE_REMEMBER_ME_DAYS` 设置有效期，默认 30 天。该选项不保存密码。

权限检查位于服务端依赖层。需要读取用户数据的查询必须附带当前账号范围，不能依赖模板或前端导航隐藏。

## 5. 配置与日志

应用默认使用启用 WAL 的 SQLite。当前开发阶段不维护版本化迁移：启动或 CLI 初始化时按 SQLAlchemy 模型直接创建缺失的表结构（`create_all`），模型变更后通过 `reset-db` 重置本地数据库。监听地址支持命令行参数、环境变量、`.env` 和代码默认值，实际生效值及来源会在 `serve` 启动日志中输出。

Loguru 接管应用及 Uvicorn、APScheduler 的日志，默认同时输出到控制台和 `var/logs/mailpulse.log`。文件日志按 `MAILPULSE_LOG_ROTATION` 轮转，并按 `MAILPULSE_LOG_RETENTION` 清理；只输出到控制台的默认管理员密码不会进入文件日志。

## 6. 运行数据

`var/` 用于 SQLite、附件、Markdown 转换结果、缓存、日志和截图等运行产物。它们不是源代码，删除后应能够通过重新同步或重新运行任务生成。

数据库结构以 SQLAlchemy 模型定义为 source of truth；开发阶段不维护迁移文件，结构变更通过 `reset-db` 重置本地数据库，不通过手工修改运行数据库改变结构。
