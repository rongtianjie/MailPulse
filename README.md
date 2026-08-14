# MailPulse

MailPulse 是一个面向公司内网的多用户邮件归纳整理工具：通过 IMAP 只读拉取邮件，使用规则筛选，再使用可配置的主模型和可选视觉副模型生成结构化报告，并通过网页和 SMTP 提供结果。

当前版本是可初期交付的 V1 垂直切片，适合在内网单机或单 worker 环境试运行。真实邮箱和 SMTP 仍需要在目标环境单独验收。

## 快速开始

项目使用 `uv` 管理 Python 环境和依赖：

```bash
uv sync
uv run mailpulse --help
uv run pytest
```

复制配置模板并修改密钥（不要提交 `.env`）：

```bash
cp .env.example .env
```

初始化数据库和管理员账号：

```bash
uv run mailpulse init --admin-email admin@example.com --admin-password 'change-this-password'
```

启动网页控制台：

```bash
uv run mailpulse serve --host 127.0.0.1 --port 8080
```

打开 <http://127.0.0.1:8080> 登录。应用启动和 `init` 会自动执行 Alembic 迁移；也可以显式执行 `uv run alembic upgrade head`。

## 演示模式

不连接真实邮箱时，可以先创建演示数据：

```bash
uv run mailpulse seed-demo --user-email admin@example.com
uv run mailpulse run-once --user-email admin@example.com --demo-ai
```

网页首页也提供“载入演示数据”和“生成演示报告”按钮。演示流程覆盖 Fake IMAP → MarkItDown → 规则/报告 → 网页查看。

## 本地 MLX 配置

管理员登录“管理控制台”，新增一个 `Primary` 模型目录。例如本地 MLX OpenAI-compatible 服务：

- Base URL：`http://127.0.0.1:8000/v1`
- API Key：按服务实际配置填写，不要写入版本库
- Model：`Qwen3.8-27B-MLX-4bit`
- 图片输入：只有确认该模型支持图片时才勾选

模型输入只包含邮件正文、MarkItDown 生成的 Markdown 和受限图片资源，不上传原生 PDF 或 Office 文件。若主模型只支持文本，可再新增一个 `Vision` profile；视觉模型先输出可校验的 `VisualEvidence`，主模型再生成最终报告。每个 profile 都可独立设置输入字符数、输出 token、超时、重试次数、图片数量和单图片大小。

配置模型后，可在首页点击“使用已配置模型”，或执行：

```bash
uv run mailpulse run-once --user-email admin@example.com
```

## 定时任务与投递

网页“定时任务”页面支持每日、每周、自定义五段 cron、IANA 时区和回看时间。单 worker 运行：

```bash
uv run mailpulse worker
```

报告详情页可手动通过 SMTP 发送；worker 会复用同一投递记录，并保存失败状态和重试次数。当前没有真实 SMTP 验收时，不要把演示邮箱主机当作可用 SMTP 服务。

## 数据与安全边界

管理员登录后可在“管理控制台”创建普通用户，并在同一页面维护 OpenAI-compatible 主模型或视觉模型目录。

- 运行数据默认写入 `var/`，包括 SQLite、附件、Markdown 转换结果、缓存和日志，不会提交到版本库。
- 生产环境必须设置独立的 `MAILPULSE_SECRET_KEY` 和 `MAILPULSE_CREDENTIAL_KEY`；密码和邮箱/模型凭据不会以明文写入数据库。
- 邮箱同步使用 IMAP 只读模式；标签、星标和已处理状态只存在于 MailPulse。
- 所有用户查询带用户范围；管理员页面默认只显示账号、任务和运行元数据，不提供普通用户邮件正文浏览入口。
- 邮件正文和附件属于不可信输入，不会触发系统工具调用。

## 验证命令

提交前建议执行：

```bash
uv sync
uv run pytest
uv run ruff check .
uv run alembic check
uv run python -m mailpulse --help
```

当前已验证：Fake Provider 的同步、附件转换、规则、调度、投递、权限和网页流程，以及本地 MLX OpenAI-compatible 文本模型的真实报告生成。尚未替代目标环境验收的部分包括真实 IMAP 邮箱、真实 SMTP 投递、多实例 worker 锁、用户停用/密码重置、报告模板编辑器和附件配额回收。
