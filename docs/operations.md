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

生产环境必须替换：

- `MAILPULSE_SECRET_KEY`
- `MAILPULSE_CREDENTIAL_KEY`

两个密钥应使用不同的随机值。`.env` 不得提交到版本库。

## 3. 初始化与启动

初始化数据库和管理员账号：

```bash
uv run mailpulse init \
  --admin-email admin@example.com \
  --admin-password 'change-this-password'
```

启动网页服务：

```bash
uv run mailpulse serve --host 127.0.0.1 --port 8080
```

启动后台 worker：

```bash
uv run mailpulse worker
```

应用启动和 `init` 命令会执行数据库迁移。需要显式迁移时使用：

```bash
uv run alembic upgrade head
```

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
uv run alembic check
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
- 数据库结构不一致：执行 `uv run alembic check`，按迁移流程处理，不要直接修改 SQLite 表结构。

测试使用 Fake Provider，不依赖真实公司邮箱、SMTP 或模型服务。目标环境的外部连接必须单独验收。
