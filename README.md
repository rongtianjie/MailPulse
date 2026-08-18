# MailPulse

MailPulse 是一个多用户邮件归纳整理工具。系统通过 IMAP 只读同步邮件，使用确定性规则筛选内容，再调用可配置的 AI 模型生成结构化报告，并通过网页和 SMTP 提供结果。

## 功能概览

- 多用户账号与角色权限管理。
- IMAP 只读同步、邮件去重和本地全文搜索。
- 任务为中心的组织方式：每个任务包含一个收件邮箱、一系列筛选规则和若干报告投递渠道。
- 任务支持手动运行与每日、每周、自定义 Cron 定时运行；邮件页覆盖所有任务邮箱同步的全部邮件。
- 支持多模态主模型，或文本主模型搭配视觉模型处理图片附件。
- 附件通过内置 MarkItDown 转换为 Markdown 和图片资源后进入模型流程。
- 报告网页查看（始终开启）、SMTP 投递、失败重试和运行记录。
- 用户账号设置支持修改密码与显示名称。
- 本地 SQLite 数据库、CSRF 防护、登录限流和凭据加密。
- Loguru 统一日志输出、文件轮转和保留策略。

## 功能界面

系统根据账号角色提供对应的功能界面：

| 角色     | 主要功能                                     |
| -------- | -------------------------------------------- |
| 管理员   | 用户管理、模型管理、任务监控和平台运行概览   |
| 普通用户 | 任务管理（收件邮箱、筛选规则、投递渠道）、邮件搜索、报告和账号设置 |

管理员登录后进入管理控制台，普通用户登录后进入邮件工作台。两类页面使用独立导航，并通过服务端权限校验控制访问范围。

## 快速开始

项目使用 `uv` 管理 Python 环境和依赖：

```bash
uv sync
uv run python -m mailpulse --help
uv run pytest
```

### 不使用 `uv`：Conda 环境 + `pip` 安装

也可以使用 Conda 创建 Python 3.11–3.14 环境，但项目依赖和开发工具统一通过 `pip` 安装，不使用 `conda install`：

```bash
conda create -n mailpulse python=3.13
conda activate mailpulse
python -m pip install --upgrade pip
python -m pip install -e .
```

激活 Conda 环境后，直接使用 `python -m mailpulse` 启动命令即可，不需要 `uv run`。需要运行测试和代码检查时，额外安装开发工具：

```bash
python -m pip install pytest pytest-asyncio ruff
```

复制配置模板并修改密钥：

```bash
cp .env.example .env
```

首次启动服务时会自动创建数据库表结构，并在数据库中没有管理员账号时创建默认管理员：

```text
登录用户名：admin
登录密码：admin123
```

以上凭据仅用于首次开发环境启动。生产环境应在 `.env` 中设置独立的管理员账号和强密码，并在首次登录后立即完成密码变更。

启动网页服务：

```bash
uv run mailpulse serve
```

使用 Conda 环境时执行：

```bash
python -m mailpulse serve
```

服务仅在首次创建默认管理员时打印上述登录信息。首次登录后会进入密码设置页面，可以立即修改，也可以暂时跳过。

启动日志会输出实际使用的监听地址，以及 host/port 的配置来源（命令行、`.env`、环境变量或代码默认值）。

如需临时覆盖 `.env` 中的监听配置，可使用 `--host` 和 `--port` 参数。

如需显式执行初始化，可使用幂等命令：

```bash
uv run mailpulse init-db
```

打开启动日志中显示的地址（默认 [http://127.0.0.1:8080](http://127.0.0.1:8080)）登录。`init-db` 和应用启动都会自动创建数据库表结构。

`init` 命令仍用于显式创建一个自定义管理员账号：

```bash
uv run mailpulse init \
  --admin-username admin \
  --admin-password 'change-this-password'
```

需要清空本地 SQLite 数据库并重新创建默认管理员时，必须显式确认：

```bash
uv run mailpulse reset-db --confirm
```

执行前应先停止网页服务和后台 worker。该命令只删除数据库及其 SQLite `-wal`、`-shm` 文件，不删除附件、MarkItDown 转换结果和其他运行目录内容。

该命令会删除全部本地数据库数据；开发阶段修改数据库模型后才使用。生产数据不得通过此命令迁移或修复。

常用命令：

| 命令 | 用途 | 适用范围 |
| --- | --- | --- |
| `uv run mailpulse serve` | 启动网页服务 | 开发与部署 |
| `uv run mailpulse worker` | 启动后台任务 worker | 开发与部署 |
| `uv run mailpulse init-db` | 创建缺失的表并初始化默认管理员 | 开发与部署 |
| `uv run mailpulse init --admin-password '…'` | 显式创建管理员账号 | 初始化或运维 |
| `uv run mailpulse reset-db --confirm` | 删除并重建本地 SQLite 数据库 | 仅开发 |
| `uv run mailpulse seed-demo --username <user>` | 写入演示邮件 | 仅开发与测试 |
| `uv run mailpulse run-once --username <user> --demo-ai` | 生成演示报告 | 仅开发与测试 |

网页服务和后台 worker 是两个独立进程。手动运行、立即同步和定时任务都会进入 `JobRun` 队列；需要执行这些任务时，必须同时运行 worker。

## 配置说明

配置项位于 `.env`，模板见 `.env.example`。生产环境至少需要设置：

- `MAILPULSE_ENVIRONMENT`：运行环境标识。`development` 为开发环境；`production` 或 `prod` 会启用生产密钥校验，但不会自动切换数据库、日志或其他服务实现。
- `MAILPULSE_SECRET_KEY`：Session 加密密钥。
- `MAILPULSE_CREDENTIAL_KEY`：邮箱和模型凭据加密密钥。
- `MAILPULSE_DATA_DIR`：SQLite、附件、转换结果和日志的存储目录。
- `MAILPULSE_LOG_LEVEL`：控制台和文件日志级别，默认 `INFO`。
- `MAILPULSE_LOG_ROTATION` / `MAILPULSE_LOG_RETENTION`：日志轮转时间和保留时间，默认每天零点轮转。
- `MAILPULSE_REMEMBER_ME_DAYS`：勾选记住登录状态时的会话有效期，默认 30 天。
- `MAILPULSE_SESSION_HTTPS_ONLY`：是否仅通过 HTTPS 发送 Session Cookie；生产 HTTPS 部署应设为 `true`。
- `MAILPULSE_EXTERNAL_AI_ALLOWED`：是否允许访问本机以外的 AI 服务，默认 `false`。
- `MAILPULSE_DATABASE_URL`：数据库 URL；当前版本只支持文件型 SQLite，未设置时使用 `var/mailpulse.sqlite3`，并依赖 SQLite FTS5 全文搜索。
- `MAILPULSE_REMEMBER_PASSWORD_DAYS`：启用“记住密码”时凭据 Cookie 的有效期，默认 30 天。

生产环境应使用独立的随机密钥，并确保运行目录仅对应用账号可读写。密钥、邮箱密码和 AI API Key 不得提交到版本库。
日志默认输出到控制台并写入 `var/logs/mailpulse.log`，按配置自动轮转和清理。默认管理员密码仅输出到控制台，不写入日志文件。
登录页面的“记住登录状态”只延长签名 Session Cookie 的有效期，不保存密码。
“记住密码”是独立选项，会将账号凭据加密后保存在浏览器 Cookie 中，仅用于登录页预填；生产环境建议只在受信任的浏览器中启用，并配合 HTTPS 使用。

配置优先级分两类：`MAILPULSE_HOST` 和 `MAILPULSE_PORT` 遵循“命令行参数 > 环境变量 > `.env` > 代码默认值”；其他配置由 Pydantic Settings 按“环境变量 > `.env` > 代码默认值”解析。服务启动日志会分别显示 host 和 port 的实际来源。

`.env.example` 还列出了 AI 超时、输入输出限制、附件大小/数量和本地存储配额等可选项。管理员在模型管理页面保存的模型配置可以按模型单独设置运行策略，并优先于环境变量中的模型回退配置。

## AI 模型配置

管理员在“模型管理”页面维护 OpenAI-compatible 模型服务。模型可按以下方式配置：

1. 配置一个同时支持文本和图片输入的 Primary 模型。
2. 配置一个仅支持文本的 Primary 模型，再配置一个支持图片输入的 Vision 模型。

如果没有在管理控制台绑定模型，也可以通过 `MAILPULSE_AI_BASE_URL`、`MAILPULSE_AI_MODEL` 等环境变量提供 Primary 回退配置；需要视觉副模型时，再配置对应的 `MAILPULSE_AI_VISION_*` 变量。默认策略只允许访问本机模型服务，访问其他地址前需要显式设置 `MAILPULSE_EXTERNAL_AI_ALLOWED=true`。

附件处理和归纳流程如下：

```text
原始附件 → MarkItDown → Markdown 和图片资源 → 事实卡片提取 → 总报告汇总 → 结构化报告
```

模型流程使用转换后的内容，不使用原生 PDF 或 Office 文件上传。每个模型配置均可独立设置输入长度、输出 token、超时、重试次数、图片数量和图片大小限制。`MAILPULSE_AI_MESSAGE_BATCH_SIZE` 控制事实卡片提取的每批邮件数量，默认 8；单封邮件使用直接归纳路径，多封邮件先提取逐封事实卡片，再生成总报告。若某批事实卡片或最终汇总模型失败，系统保留已提取的事实卡片生成降级报告，并在覆盖范围中标记 `degraded` 和处理警告。

本地 MLX 等 OpenAI-compatible 服务示例：

```text
Base URL: http://127.0.0.1:8000/v1
Model:    按服务实际提供的模型名称填写
```

## 任务与报告投递

普通用户通过“任务”组织全部配置：每个任务包含一个收件邮箱（IMAP 连接与 SMTP 发信身份，支持连接验证）、一系列筛选规则和若干投递渠道。

新建任务采用四步向导：基本信息 → 收件邮箱（IMAP 配置与即时连接验证；SMTP 为可选折叠项，用于邮件投递）→ 筛选规则（表单化编辑：字段/操作符/值，规则列表按优先级排列、可上移下移）→ 投递渠道（网页内直接查看始终开启；邮件投递地址支持添加、编辑、删除、启停与发送测试邮件）。

筛选规则语义：满足**任一**启用规则的邮件进入报告（OR 并集）；规则内多条件之间为“且”关系；不配置规则时处理全部邮件。高级用户可在任务详情中使用 JSON DSL 模式编辑。

任务支持两种执行方式：

- 手动运行：在任务详情页点击「立即运行」，执行同步 → 筛选 → AI 归纳 → 生成报告 → 按渠道投递的完整流水线。
- 定时运行：每日、每周或五段 Cron 表达式，配合 IANA 时区和邮件回看时间，由后台 worker 自动触发。

启动后台 worker：

```bash
uv run mailpulse worker
```

使用 Conda 环境时执行：

```bash
python -m mailpulse worker
```

报告详情页支持通过 SMTP 手动发送和失败重试，并保存投递状态、尝试次数和错误信息。

## 数据与安全

- 邮箱使用 IMAP 只读模式，不修改服务器端已读、星标、文件夹或删除状态。
- 本地标签、星标和已处理状态由 MailPulse 独立维护。
- 用户查询均带账号范围，服务端不会仅依赖前端隐藏导航实现权限控制。
- 邮件正文、HTML、附件和模型输出均按不可信输入处理。
- 附件同步和转换受单文件大小、单封邮件附件数量、用户/全局存储配额、图片资源数量与大小等限制。
- 运行数据默认写入 `var/`，该目录属于缓存和输出，不纳入版本控制。

更详细的系统结构、运行方式和维护边界见：

- [架构说明](docs/architecture.md)
- [运行与维护](docs/operations.md)

## 开发验证

演示数据命令仅用于本地开发和自动化测试，不属于正式网页功能：

```bash
uv run mailpulse seed-demo --username user
uv run mailpulse run-once --username user --demo-ai
```

提交前执行：

```bash
uv run pytest
uv run ruff check .
uv run python -m mailpulse --help
git diff --check
```

测试使用 Fake IMAP、Fake SMTP 和 Fake AI Provider，不依赖真实公司邮箱或真实 AI 服务。真实 IMAP、SMTP、目标模型服务和生产部署仍需在目标环境单独验收。
