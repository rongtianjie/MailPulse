# MailPulse 架构说明

## 1. 组件关系

MailPulse 是一个 Python 单体应用，由网页服务、后台 worker、SQLite 数据库和外部邮箱/模型服务组成。

```text
Web UI ────────┐
               ├─ SQLAlchemy / SQLite
Worker ────────┘
    │
    ├─ IMAP：只读同步邮件
    ├─ MarkItDown：转换附件
    ├─ AI Provider：生成结构化内容
    └─ SMTP：投递报告
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

应用使用本地账号和 Session 认证。管理员和普通用户使用不同的网页布局与路由组：

- 管理员路由：`/admin`、`/admin/users`、`/admin/models`、`/admin/jobs`。
- 普通用户路由：邮箱、邮件、规则、任务和报告相关页面。

权限检查位于服务端依赖层。需要读取用户数据的查询必须附带当前账号范围，不能依赖模板或前端导航隐藏。

## 5. 运行数据

`var/` 用于 SQLite、附件、Markdown 转换结果、缓存、日志和截图等运行产物。它们不是源代码，删除后应能够通过重新同步或重新运行任务生成。

数据库结构以 SQLAlchemy 模型和 `migrations/` 中的 Alembic 迁移为准，不通过手工修改运行数据库改变结构。
