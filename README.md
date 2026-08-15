# MailPulse

MailPulse 是面向公司内网的多用户邮件归纳整理工具。系统通过 IMAP 只读同步邮件，使用确定性规则筛选内容，再调用可配置的 AI 模型生成结构化报告，并通过网页和 SMTP 提供结果。

## 功能概览

- 多用户账号与角色权限管理。
- IMAP 只读同步、邮件去重和本地全文搜索。
- 可配置筛选规则、标签、星标和已处理状态。
- 每日、每周和自定义 Cron 定时任务。
- 支持多模态主模型，或文本主模型搭配视觉模型处理图片附件。
- 附件通过内置 MarkItDown 转换为 Markdown 和图片资源后进入模型流程。
- 报告网页查看、SMTP 投递、失败重试和运行记录。
- 本地 SQLite 数据库、CSRF 防护、登录限流和凭据加密。
- Loguru 统一日志输出、文件轮转和保留策略。

## 功能界面

系统根据账号角色提供对应的功能界面：

| 角色     | 主要功能                                     |
| -------- | -------------------------------------------- |
| 管理员   | 用户管理、模型管理、任务监控和平台运行概览   |
| 普通用户 | 邮箱设置、邮件搜索、筛选规则、定时任务和报告 |

管理员登录后进入管理控制台，普通用户登录后进入邮件工作台。两类页面使用独立导航，并通过服务端权限校验控制访问范围。

## 快速开始

项目使用 `uv` 管理 Python 环境和依赖：

```bash
uv sync
uv run python -m mailpulse --help
uv run pytest
```

复制配置模板并修改密钥：

```bash
cp .env.example .env
```

首次启动服务时会自动执行数据库迁移，并在数据库中没有管理员账号时创建默认管理员：

```text
登录邮箱：admin@mailpulse.local
登录密码：admin123
```

启动网页服务：

```bash
uv run mailpulse serve
```

服务仅在首次创建默认管理员时打印上述登录信息。首次登录后会进入密码设置页面，可以立即修改，也可以暂时跳过。

启动日志会输出实际使用的监听地址，以及 host/port 的配置来源（命令行、`.env`、环境变量或代码默认值）。

如需临时覆盖 `.env` 中的监听配置，可使用 `--host` 和 `--port` 参数。

如需显式执行初始化，可使用幂等命令：

```bash
uv run mailpulse init-db
```

打开启动日志中显示的地址（默认 [http://127.0.0.1:8080](http://127.0.0.1:8080)）登录。`init-db` 和应用启动都会自动执行数据库迁移，也可以手动执行：

```bash
uv run alembic upgrade head
```

`init` 命令仍用于显式创建一个自定义管理员账号：

```bash
uv run mailpulse init \
  --admin-email admin@example.com \
  --admin-password 'change-this-password'
```

需要清空本地 SQLite 数据库并重新创建默认管理员时，必须显式确认：

```bash
uv run mailpulse reset-db --confirm
```

执行前应先停止网页服务和后台 worker。该命令只删除数据库及其 SQLite `-wal`、`-shm` 文件，不删除附件、MarkItDown 转换结果和其他运行目录内容。

## 配置说明

配置项位于 `.env`，模板见 `.env.example`。生产环境至少需要设置：

- `MAILPULSE_SECRET_KEY`：Session 加密密钥。
- `MAILPULSE_CREDENTIAL_KEY`：邮箱和模型凭据加密密钥。
- `MAILPULSE_DATA_DIR`：SQLite、附件、转换结果和日志的存储目录。
- `MAILPULSE_LOG_LEVEL`：控制台和文件日志级别，默认 `INFO`。
- `MAILPULSE_LOG_ROTATION` / `MAILPULSE_LOG_RETENTION`：日志轮转时间和保留时间，默认每天零点轮转。

生产环境应使用独立的随机密钥，并确保运行目录仅对应用账号可读写。密钥、邮箱密码和 AI API Key 不得提交到版本库。
日志默认输出到控制台并写入 `var/logs/mailpulse.log`，按配置自动轮转和清理。默认管理员密码仅输出到控制台，不写入日志文件。

## AI 模型配置

管理员在“模型管理”页面维护 OpenAI-compatible 模型服务。模型可按以下方式配置：

1. 配置一个同时支持文本和图片输入的 Primary 模型。
2. 配置一个仅支持文本的 Primary 模型，再配置一个支持图片输入的 Vision 模型。

附件处理流程如下：

```text
原始附件 → MarkItDown → Markdown 和图片资源 → 模型编排 → 结构化报告
```

模型流程使用转换后的内容，不使用原生 PDF 或 Office 文件上传。每个模型配置均可独立设置输入长度、输出 token、超时、重试次数、图片数量和图片大小限制。

本地 MLX 等 OpenAI-compatible 服务示例：

```text
Base URL: http://127.0.0.1:8000/v1
Model:    按服务实际提供的模型名称填写
```

## 定时任务与报告投递

普通用户可以在“定时任务”页面配置同步、筛选和报告生成任务：

- 每日、每周或五段 Cron 表达式。
- IANA 时区。
- 邮件回看时间。
- 任务启用、停用和编辑。

启动后台 worker：

```bash
uv run mailpulse worker
```

报告详情页支持通过 SMTP 手动发送。定时任务也可以复用投递记录，并保存状态、尝试次数和错误信息。

## 数据与安全

- 邮箱使用 IMAP 只读模式，不修改服务器端已读、星标、文件夹或删除状态。
- 本地标签、星标和已处理状态由 MailPulse 独立维护。
- 用户查询均带账号范围，服务端不会仅依赖前端隐藏导航实现权限控制。
- 邮件正文、HTML、附件和模型输出均按不可信输入处理。
- 附件解析受文件类型、大小、数量、处理时间和资源数量限制。
- 运行数据默认写入 `var/`，该目录属于缓存和输出，不纳入版本控制。

更详细的系统结构、运行方式和维护边界见：

- [架构说明](docs/architecture.md)
- [运行与维护](docs/operations.md)
- [项目计划](PLAN.md)

## 开发验证

演示数据命令仅用于本地开发和自动化测试，不属于正式网页功能：

```bash
uv run mailpulse seed-demo --user-email user@example.com
uv run mailpulse run-once --user-email user@example.com --demo-ai
```

提交前执行：

```bash
uv run pytest
uv run ruff check .
uv run alembic check
uv run python -m mailpulse --help
```

测试使用 Fake IMAP、Fake SMTP 和 Fake AI Provider，不依赖真实公司邮箱或真实 AI 服务。真实 IMAP、SMTP、目标模型服务和生产部署仍需在目标环境单独验收。
