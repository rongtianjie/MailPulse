# MailPulse 运行与维护

## 1. 环境准备

要求：

- Python 3.11–3.14
- `uv`
- 可访问的 IMAP/SMTP 服务
- 已部署的 OpenAI-compatible 模型服务，或其他受支持的模型 Provider

安装依赖：

```bash
uv sync
```

## 2. 配置环境变量

复制模板：

```bash
cp .env.example .env
```

`MAILPULSE_ENVIRONMENT` 用于标识运行环境。设置为 `development` 时使用开发环境规则；设置为 `production` 或 `prod` 时，应用会要求显式配置 `MAILPULSE_SECRET_KEY` 和 `MAILPULSE_CREDENTIAL_KEY`。该配置只影响环境标识和密钥校验，不会自动切换数据库、日志或其他服务实现。

默认配置使用 `MAILPULSE_DATA_DIR` 下的文件型 SQLite 数据库、附件目录、MarkItDown 转换目录和日志目录。可以通过 `MAILPULSE_DATABASE_URL` 提供其他 SQLAlchemy 数据库 URL，但当前 CLI 的 `reset-db` 只支持文件型 SQLite，部署与备份流程也按 SQLite 编写。

生产环境必须替换：

- `MAILPULSE_SECRET_KEY`
- `MAILPULSE_CREDENTIAL_KEY`

两个密钥应使用不同的随机值。`.env` 不得提交到版本库。

日志默认输出到控制台，并写入 `var/logs/mailpulse.log`，每天零点轮转。可通过 `MAILPULSE_LOG_LEVEL`、`MAILPULSE_LOG_ROTATION` 和 `MAILPULSE_LOG_RETENTION` 调整级别、轮转时间和保留策略。默认管理员密码仅输出到控制台，不写入日志文件。
登录页面的“记住登录状态”只延长签名 Session Cookie 的有效期，默认保留 30 天，不保存密码。“记住密码”会把账号和密码使用凭据密钥加密后存入独立的 HttpOnly Cookie（默认保留 30 天，由 `MAILPULSE_REMEMBER_PASSWORD_DAYS` 控制），仅用于下次打开登录页时预填账号与密码，并在取消勾选登录时清除；启用 `MAILPULSE_SESSION_HTTPS_ONLY` 后该 Cookie 同样只通过 HTTPS 发送。

配置读取优先级为：`serve` 的 `--host`/`--port` 命令行参数 > 环境变量 > `.env` > 代码默认值；其他配置为环境变量 > `.env` > 代码默认值。`MAILPULSE_SESSION_HTTPS_ONLY` 用于控制 Session Cookie 是否仅通过 HTTPS 发送，HTTPS 部署应设为 `true`。`MAILPULSE_EXTERNAL_AI_ALLOWED` 默认为 `false`，用于阻止未经明确许可的外部 AI 内容发送。

AI 的环境变量回退配置包括 Primary 与可选 Vision 服务地址、模型名称、API Key 和能力声明；`MAILPULSE_AI_TIMEOUT_SECONDS`、`MAILPULSE_AI_MAX_OUTPUT_TOKENS`、`MAILPULSE_AI_MAX_INPUT_CHARS`、`MAILPULSE_AI_MAX_RETRIES`、`MAILPULSE_MAX_MESSAGES_PER_REPORT` 控制默认运行限制。附件同步还受单文件大小、单封邮件附件数量、用户/全局存储配额以及图片资源限制控制，完整变量列表和默认值见 `.env.example`。

## 3. 初始化与启动

首次启动服务时会自动创建数据库表结构，并在数据库中没有管理员账号时创建默认管理员。默认登录信息为：

```text
登录用户名：admin
登录密码：admin123
```

启动网页服务：

```bash
uv run mailpulse serve
```

服务仅在首次创建默认管理员时打印登录信息。首次登录后会进入密码设置页面，可以修改密码，也可以暂时跳过。

登录页面提供“注册新账号”入口，普通员工可自助注册普通用户账号（角色固定为 `user`，注册接口不接受管理员角色）。用户名是账号的唯一登录标识（3-32 位字母、数字、下划线、连字符或点），邮箱仅是可选属性，注册和创建账号时都可以不填。管理员账号只能由管理员在“系统管理 → 用户管理”中创建，或通过 `init` 命令创建。

`serve` 默认读取 `.env` 中的 `MAILPULSE_HOST` 和 `MAILPULSE_PORT`；命令行提供的 `--host` 或 `--port` 会临时覆盖对应配置。
启动日志会输出实际使用的监听地址，以及 host/port 的配置来源（命令行、`.env`、环境变量或代码默认值）。

需要在服务启动前显式初始化时，使用幂等命令：

```bash
uv run mailpulse init-db
```

`init` 命令仍可用于显式创建自定义管理员账号：

```bash
uv run mailpulse init \
  --admin-username admin \
  --admin-email admin@example.com \
  --admin-password 'change-this-password'
```

邮箱是用户的可选属性，`--admin-email` 可以不提供；用户名默认取 `MAILPULSE_DEFAULT_ADMIN_USERNAME`。

需要重置本地 SQLite 数据库时，必须显式确认：

```bash
uv run mailpulse reset-db --confirm
```

执行前应先停止网页服务和后台 worker。该命令只删除数据库及其 `-wal`、`-shm` 文件，然后重新执行初始化；附件、MarkItDown 转换结果和其他运行目录内容会保留。

启动后台 worker：

```bash
uv run mailpulse worker
```

应用启动、`init-db` 和 `init` 命令会自动创建数据库表结构（`create_all`），不维护版本化迁移。开发阶段修改数据库模型后，用 `reset-db` 重置本地数据库（破坏性操作，会删除全部数据）。

## 4. 模型服务配置

管理员进入“模型管理”页面，填写 OpenAI-compatible 服务地址和模型名称。建议先保存配置，再使用小范围数据验证：

- Primary：负责最终结构化报告。
- Vision：为文本 Primary 模型提供图片理解能力。

所有附件先转换为 Markdown 和图片资源，再进入模型编排。模型配置中的超时、重试、输入长度、输出 token 和图片资源上限应结合实际服务容量设置。

## 5. 运行数据与备份

默认运行数据位于 `var/`，包括：

- SQLite 数据库。
- 邮件附件和 Markdown 转换结果。
- 缓存、日志和运行截图。

备份前应停止写入数据库的服务或使用一致性快照。备份文件必须按照公司数据保密要求存储，不得提交到版本库。

## 6. 验证与故障定位

提交前执行：

```bash
uv run pytest
uv run ruff check .
uv run python -m mailpulse --help
```

基础健康检查：

```bash
curl http://127.0.0.1:8080/healthz
```

常见问题：

- 页面无法访问：确认网页服务监听地址和端口，检查应用日志。
- 邮箱同步失败：在邮箱设置中验证 IMAP 主机、端口、用户名、密码和 TLS 配置。
- 报告生成失败：检查模型 Base URL、模型名称、能力声明、上下文限制和服务日志。
- 报告投递失败：验证 SMTP 主机、端口、TLS 和收件人配置。
- 数据库结构不一致：开发阶段模型变更后直接使用 `uv run mailpulse reset-db --confirm` 重置本地数据库，不要直接手工修改 SQLite 表结构。

测试使用 Fake Provider，不依赖真实公司邮箱、SMTP 或模型服务。目标环境的外部连接必须单独验收。
