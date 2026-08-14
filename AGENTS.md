# MailPulse 项目约定

## 项目目标

MailPulse 是一个供公司内网使用的多用户邮件归纳整理工具。系统通过 IMAP 只读拉取邮件，使用确定性规则筛选，再通过可配置的主模型和可选视觉副模型生成结构化报告，并通过网页和 SMTP 提供结果。

## 目录约定

- `src/mailpulse/`：正式 Python 源代码，按领域模块组织。
- `src/mailpulse/templates/`：服务端网页模板，是网页结构的 source of truth。
- `src/mailpulse/static/`：本地 CSS、JavaScript 和图标资源，不依赖外部 CDN。
- `tests/`：单元测试、集成测试和网页测试；测试不得依赖真实公司邮箱或真实 AI 服务。
- `docs/`：架构、配置、部署和运维说明；不存放运行日志或临时调试文件。
- `migrations/`：数据库迁移文件，是数据库结构变更的 source of truth。
- `var/`：运行时数据库、日志、缓存、附件和 MarkItDown 转换结果；属于 output/cache，不提交版本库。
- `scripts/`：可复用的开发、测试和运维辅助脚本；不得存放一次性个人实验代码。

不要把密钥、邮箱密码、AI API Key、真实邮件、真实附件、运行日志或生成报告提交到版本库。临时输出放入 `var/`，完成验证后清理或保留在明确命名的测试 fixture 中。

## 命名与实现原则

- Python 模块、变量、配置键和 CLI 命令使用英文 `snake_case`。
- 面向用户的网页文案默认使用中文；领域类型和接口名称使用英文。
- 邮件原始身份与邮箱文件夹中的出现记录分离：`CanonicalMessage` 表示规范化邮件，`MessageOccurrence` 表示同步游标记录。
- 邮箱默认只读；标签、星标和已处理状态是 MailPulse 应用内状态，不修改 IMAP 服务端状态。
- 附件先经过项目内置 MarkItDown 转换为 Markdown 和图片资源，再进入模型编排；模型不上传原生 PDF 或 Office 文件。
- 邮件正文、HTML、附件和模型输出都属于不可信输入；禁止任意代码执行，禁止把邮件内容当作工具指令。
- 所有用户数据查询必须带权限范围；不能只依赖网页层隐藏数据。
- 修改前先阅读相关模块和测试；改动应紧贴当前目标，不做无关重构。

## Source of truth、缓存与生成物

- Python 领域逻辑、Pydantic schema、数据库模型和迁移是行为与数据结构的 source of truth。
- `var/` 中的 SQLite、索引、附件、转换后的 Markdown、缓存和日志均为运行输出，删除后必须可以重新生成。
- 测试 fixture 可以提交，但必须是明确的脱敏或合成数据，并放在 `tests/fixtures/`。
- 修改数据库结构时必须同步迁移和测试；不直接手工修改运行数据库作为结构变更。

## 验证命令

优先使用项目虚拟环境执行：

```bash
uv sync
uv run pytest
uv run ruff check .
uv run python -m mailpulse --help
```

长时间运行或网页验证使用日志重定向，避免把运行输出写入源代码目录。无法执行真实邮箱或 AI 验证时，必须使用 Fake Provider 并明确未验证范围。
