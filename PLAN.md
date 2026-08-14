# MailPulse：内网多用户邮件智能整理工具

## Summary

项目已从规划阶段进入 V1 垂直切片实现阶段。当前代码已经建立模块化 Python 单体应用和可操作网页控制台，后续工作以补齐生产化边界、配置体验和运维能力为主。系统目标支持：

- 多用户独立邮箱、权限隔离和本地账号登录（V1：每用户单账号、仅 INBOX，接口预留扩展）。
- IMAP 增量拉取（UIDVALIDITY 感知）、SMTP 报告发送（投递接口插件化）。
- 可视化规则 + 高级表达式（自定义 DSL，AST 沙箱求值）。
- 主模型与副模型角色化配置。
- 管理员控制台和用户报告页面。
- 邮件全文搜索（FTS5）、标签/星标/已处理标记。
- 统计仪表盘、演示模式。
- 默认只读邮箱、可追溯来源、失败可重试。

> V1 范围决策：聚焦功能性。本期不做高级安全能力（复杂提示注入检测、密钥轮换、集中式安全监控等）、IM 通知（企业微信/钉钉/飞书）与完整 AI 成本核算；但凭据保护、认证授权、HTML 安全处理、附件资源限制、外部 AI 发送策略和审计属于必做基础能力。

### 当前实现状态（2026-08-15）

已落地的基础能力：

- FastAPI + Jinja2 网页应用、Session 登录、CSRF、跨用户查询隔离、管理员账号 provisioning，以及管理员模型目录页面。
- SQLite/WAL、SQLAlchemy 数据模型、Alembic 初始迁移（应用启动和 CLI 初始化统一执行迁移）、CLI 的 `init`、`serve`、`run-once`、`worker` 和演示数据流程。
- IMAP 只读增量同步、UIDVALIDITY 变化处理、规范化邮件与邮箱出现记录分离、CanonicalMessage 去重。
- JSON 规则 DSL、AST 沙箱求值、规则页面、规则命中前的报告过滤；附件大小条件读取 `size_bytes`。
- 内置 MarkItDown 附件转换：原始附件先落盘并转换为 Markdown/图片资源，模型输入不使用原生 PDF 或 Office 上传能力；转换失败和资源缺失会进入报告附件处理状态。
- 附件单文件、单邮件数量、用户和全局存储配额，受控文件名和图片资源大小/数量限制；超限附件不会进入模型。
- 主模型/视觉副模型路由：支持多模态主模型直接处理，也支持纯文本主模型搭配视觉副模型输出 `VisualEvidence` 后再归纳。
- OpenAI-compatible Provider、全局模型绑定解析、结构化报告、来源校验、模型重试和模型处理审计；模型 profile 可独立配置输入/输出上限、超时、重试和图片资源限制。
- 定时任务页面和 worker：每日、每周、自定义 cron、IANA 时区、回看时间、启停、唯一 `run_key` 和失败 `JobRun` 记录。
- 报告详情页 SMTP 手动投递、投递状态/尝试次数、失败重试，以及 worker 中的自动投递复用同一投递记录。
- Fake Mail Provider、演示 AI Provider、MarkItDown、调度、投递和网页流程测试。
- 登录失败限流、IMAP 连接测试、FTS 语法失败降级和友好化的报告生成错误页面。
- FTS5 不可用时同步仍可继续，搜索自动降级到规范化字段的 `LIKE` 查询。
- 已验证全新运行目录可完成 `uv sync`、数据库初始化、Alembic 升级/检查和演示报告生成。

### 初期交付验收记录（2026-08-15）

- 自动化验证：`uv sync` 成功，25 个测试通过，Ruff、Alembic check 和 CLI 帮助通过。
- 本地 AI：已用 `http://127.0.0.1:8000/v1` 的 `Qwen3.8-27B-MLX-4bit` 完成真实报告生成；报告状态为 `success`，模型 trace、MarkItDown 附件状态和来源引用均已写入。
- 网页验收：管理员和普通用户登录成功；普通用户不显示管理导航，不能读取管理员报告；报告详情、邮箱设置、规则、定时任务、模型目录和投递页面可加载；桌面和 390px 移动视口均无横向溢出。
- 路由验收：多模态主模型直连、文本主模型搭配视觉副模型、视觉失败降级和无图片资源路径均有 Fake Provider 测试覆盖。

当前仍不是生产就绪版本：真实邮箱/SMTP 尚未在目标环境验收，复杂后台任务队列、高级审计授权、完整用户生命周期管理、报告模板编辑器、完整附件配额回收和多实例 worker 锁仍未实现。部署前必须配置真实密钥、运行迁移并完成目标邮箱和 SMTP 的连接测试。

## AI 模型编排

### 模型角色

`ModelProfile` 统一描述模型：

- Provider 类型和 API Endpoint。
- 模型名称。
- 加密后的 API Key。
- 能力声明：`text_input`、`image_input`、`structured_output`、`strict_json_schema`。
- 上下文长度、最大图片数量、图片大小限制。
- 超时、重试、成本和限流配置。

模型绑定分为：

- `Primary Model`：负责最终摘要、分类、行动项和报告生成。
- `Vision/Attachment Model`：可选，负责图片、扫描 PDF 和图像型附件识别。
- 后续可扩展 OCR、Embedding、分类模型等角色。

配置层级采用：

1. 全局默认配置。
2. 用户覆盖配置。
3. 邮箱覆盖配置。
4. 报告任务可选择是否允许覆盖。

管理员维护模型目录和安全策略，用户只能选择被授权的模型。

### 附件 Markdown 化流程

项目内置 MarkItDown 作为统一附件转换工具。模型不接收原生 PDF 或 Office 文档上传；所有纳入处理范围的附件先转换为 Markdown 和关联资源，再进入模型编排流程。

```text
原始附件
   ↓
MarkItDown 转换
   ↓
Markdown 文本 + 图片资源 + 表格 + 转换警告
   ↓
模型能力分析与路由
```

`ConvertedAttachment` 至少包含：

- `attachment_id`
- `markdown_content` 或 Markdown 文件路径
- 图片资源列表及资源标识
- 表格和文本提取结果
- 原始 MIME 类型和文件哈希
- MarkItDown 版本
- 转换状态和警告

处理原则：

- 纯文本主模型接收 Markdown 文本、表格和视觉副模型生成的 `VisualEvidence`。
- 多模态主模型接收 Markdown 文本和转换结果中的图片资源，不接收原生 PDF。
- 图片型 PDF 如果转换后缺少有效文字，则将可提取的页面/图片资源交给视觉模型。
- MarkItDown 转换失败、资源缺失或附件超限时，报告标记具体失败原因，不猜测内容。
- Markdown 中的图片引用必须映射到内部资源 ID，不能把服务器本地路径直接暴露给模型或用户。

### 自动路由流程

```text
邮件正文和 Markdown 化附件
        ↓
附件能力分析
        ↓
主模型支持图片输入？
   ┌────┴────┐
   是        否
   ↓         ↓
主模型直接   调用视觉副模型
处理媒体     提取视觉证据
       └────┬────┘
            ↓
主模型生成统一结构化报告
```

具体行为：

- 多模态主模型：默认由主模型直接处理正文和图片。
- 纯文本主模型 + 多模态副模型：副模型先提取视觉信息，再交给主模型归纳。
- 主模型和副模型都支持多模态：默认优先主模型，副模型可作为 fallback 或强制附件解析器。
- 没有可用视觉模型时，报告明确标记附件未处理，不猜测内容。
- 同一封邮件可以同时包含正文、文本附件和图片附件。
- 所有模型输入均来自正文、Markdown 和图片资源；不使用模型的原生 PDF 上传能力。

### 视觉证据接口

副模型不直接生成最终报告，而是输出可校验的 `VisualEvidence`：

```text
VisualEvidence
  message_id
  attachment_id
  page_number
  image_index
  extracted_text
  tables
  detected_objects
  key_fields
  confidence_advisory
  evidence_status
  warnings
```

`confidence_advisory` 仅作为模型提供的参考信息，不能单独作为业务判断依据；关键结论和行动项必须绑定来源定位，没有来源时标记为“未验证”。

主模型最终接收：

- 邮件正文。
- 文本附件提取结果。
- 视觉结构化证据。
- 来源定位信息。
- 报告模板和筛选上下文。

最终报告至少包含：

- 分类和优先级。
- 邮件摘要。
- 行动项、负责人和截止时间。
- 决策、风险和待确认问题。
- 原始邮件、附件、页码或图片编号引用。
- 附件处理状态。

## 核心系统设计

### 技术形态

- Python 3.11+
- FastAPI
- Jinja2 + HTMX
- SQLAlchemy + Alembic
- SQLite 默认（WAL 模式），保留 PostgreSQL 兼容能力
- SQLite FTS5 提供邮件全文搜索；启动时检测 FTS5/trigram 能力，不支持时降级到规范化字段和 `LIKE` 查询，短中文查询也保留 `LIKE` fallback
- APScheduler 或等价轻量调度器
- 支持 `web`、`worker`、`run-once` 运行模式
- 支持 systemd，不依赖 Docker

### 主要模块

- `auth`：本地账号、Session、密码哈希、权限。
- `mail`：IMAP/SMTP、增量同步（UIDVALIDITY 感知）、线程归并、规范化去重、应用内标签/星标/已处理标记。
- `filtering`：规则树、高级表达式（DSL + AST 沙箱求值）、预览和回放。
- `attachments`：附件大小/类型限制、MarkItDown 转换、Markdown 和图片资源管理。
- `ai`：模型目录、能力检测、路由、结构化输出。
- `reports`：摘要模板、报告渲染、来源引用。
- `delivery`：SMTP 发送、重试和发送状态（接口插件化，预留 IM Webhook 等渠道）。
- `search`：FTS5 全文索引与组合筛选查询。
- `dashboard`：处理量、规则命中、待办等只读统计聚合。
- `demo`：演示模式，内置 Fake Mail Provider 与示例数据。
- `jobs`：调度、运行记录、锁和失败恢复。
- `audit`：审计日志、敏感访问授权和操作追踪。

### V1 范围决策

- 邮箱：每用户单账号、仅同步 INBOX；`MailConnector` 接口预留多账号与多文件夹扩展。
- 增量同步：`UIDVALIDITY` 只用于游标和重同步判断；使用 `MessageOccurrence` 保存 `folder + UIDVALIDITY + UID`，使用规范化 `Message-ID` 或正文/头部哈希维护 `CanonicalMessage` 去重。
- 附件：同步时拉取元数据，规则命中或报告需要时按需下载；通过内置 MarkItDown 转换为 Markdown 和图片资源，设置单附件、单用户和全局容量上限。
- 附件筛选：V1 只按附件名称、类型、大小等元数据筛选；附件正文筛选留给 MarkItDown 索引扩展。
- 高级表达式：自定义 DSL，编译为 AST 后沙箱求值，禁止 `eval` / 任意代码执行；表达式可引用邮件元数据（标签、已处理状态）；规则按优先级求值、支持短路。
- 调度：报告任务使用 IANA 时区，支持每日 / 每周 / 自定义 cron；服务重启后的默认策略是合并为一次补跑，时间范围从上次成功游标到当前时间，并通过唯一 `run_key` 防止重复报告。
- 投递：V1 实现 SMTP；`DeliveryProvider` 按插件设计，为 IM Webhook 等渠道预留。
- AI 限制：V1 不做预算报表、费用预测和复杂摘要缓存；但必须限制单封邮件大小、附件数量、上下文长度、输出 token、并发、超时和重试次数。
- 明确不做：高级提示注入检测、密钥轮换、集中式安全监控、IM 通知和完整 AI 成本核算。

### 基础安全边界

以下能力属于 V1 必做，不得因为“功能优先”而排除：

- Argon2id 密码哈希、安全 Session Cookie、CSRF 防护和登录限流。
- 邮箱凭据、AI API Key 使用外部主密钥加密，不写入日志。
- 所有查询强制执行用户数据隔离；管理员默认不能读取邮件正文。
- HTML 邮件默认以纯文本展示，或经过严格消毒后展示。
- MarkItDown 和附件解析设置文件类型、大小、解压深度、处理时间和资源数量限制，不执行宏或任意附件程序。
- 邮件内容作为不可信数据处理，不允许邮件正文触发工具调用或系统状态修改。
- 外部 AI 发送必须经过 Provider 策略授权，并记录模型、邮件范围和发送结果；日志不得包含正文、附件内容或凭据。

### 权限

- `user`：只能访问自己的邮箱、规则、报告和附件。
- `admin`：管理账号、配置、模型、任务和运行元数据，默认不能查看正文。
- 敏感正文访问必须通过临时审计授权，填写原因、设置有效期并记录日志。

标签、星标和已处理状态在 V1 均为 MailPulse 应用内状态，不修改 IMAP 服务器上的已读、星标、移动或删除状态。

### 初学者体验

- 首次使用向导。
- 邮箱和 AI 连接测试。
- 模型能力标签，例如“支持图片”“支持结构化输出”。
- 基础模式隐藏复杂参数。
- 高级模式开放模型、提示词、路由和限流配置。
- 规则预览、历史邮件试运行和报告预览。
- 内置报告模板和规则模板。
- 配置导入导出。

## Public Interfaces

```text
MailConnector
  test_connection()
  sync_messages(cursor, time_window)
  fetch_message(uid)

ModelProvider
  test_connection()
  get_capabilities()
  generate(generation_request)

GenerationRequest
  role
  content_parts          # text / markdown / image / visual evidence
  response_schema
  max_input_tokens
  max_output_tokens
  timeout

ModelRouter
  choose_primary(context)
  choose_vision_processor(context)
  build_execution_plan(context)

AttachmentConversionService
  convert(attachment) -> ConvertedAttachment

VisionProcessor
  extract_evidence(converted_attachment, image_assets)

ReportGenerator
  generate_structured_summary(messages, evidence, template)

DeliveryProvider
  send(report, destination)

SearchService
  index_message(message)
  search(query, filters)      # 全文 + 发件人/日期/标签/处理状态组合筛选

MailStore
  set_local_label(message_id, label)
  set_local_starred(message_id, starred)
  set_local_processed(message_id, processed)

DashboardStats
  collect(scope)              # 处理量、规则命中、待办等聚合

DemoSeeder
  seed(user_id)               # 生成示例邮箱与规则数据
```

核心类型：

- `ModelProfile`
- `ModelBinding`
- `ModelCapability`
- `GenerationRequest`
- `ConvertedAttachment`
- `CanonicalMessage`
- `MessageOccurrence`
- `RoutingDecision`
- `VisualEvidence`
- `StructuredSummary`
- `SearchResult`
- `JobRun`
- `AuditLog`

## Test Plan

重点测试：

- 多模态主模型直接处理图片。
- 纯文本主模型自动调用视觉副模型。
- 主模型和副模型均支持图片时的默认路由。
- 视觉模型不可用、超时、返回非法结构时的降级。
- 图片、扫描 PDF、文本 PDF、混合附件。
- MarkItDown：PDF、DOCX、XLSX、HTML、文本等附件转换为 Markdown，图片资源和转换警告可追溯。
- 页码、附件编号和原始邮件引用准确性。
- 用户无法访问其他用户的模型配置、邮件和报告。
- 外部模型禁止策略确实阻止数据发送。
- 重复执行不会重复同步、总结和发送。
- UIDVALIDITY 变化时触发全量重同步，不丢邮件、不重复；同一封邮件可正确关联到 `CanonicalMessage`。
- 表达式 DSL：非法表达式报错、沙箱禁止任意代码、规则优先级与短路求值正确。
- 全文搜索：中文子串匹配、发件人/日期/标签/处理状态组合筛选。
- FTS5 不可用或短中文查询时正确降级到 `LIKE` 查询。
- 应用内标签/星标/已处理标记的读写，以及规则引用处理状态，不能修改 IMAP 状态。
- 凭据加密、密码哈希、Session/CSRF、用户隔离、HTML 安全展示和附件资源限制。
- 邮件提示注入不能触发工具调用或系统状态修改，日志不包含正文、附件和凭据。
- 模型能力不匹配、MarkItDown 转换失败和视觉证据缺失时的明确降级。
- 演示模式：Fake Provider 种子数据可完整走通 同步 → 总结 → 报告 流程。
- 仪表盘统计与底层数据一致。
- SMTP 投递插件化：替换为假投递渠道不影响上层流程。
- 使用 Fake IMAP、Fake SMTP、Fake AI Provider 端到端测试。

验收标准：

1. 用户可独立配置自己的邮箱和报告任务。
2. 管理员可配置模型目录及其能力，但不能默认查看邮件正文。
3. 系统能自动判断是否需要副模型。
4. 主模型可以是纯文本模型，也可以单独承担多模态处理。
5. 每个视觉结论都能定位到邮件、附件、页码或图片。
6. 模型失败时不会生成无法验证的假摘要。
7. 系统可在内网 Python 环境中长期运行。

## Assumptions and Defaults

- 初期规模按 1–50 用户设计。
- V1 每用户单账号、仅同步 INBOX，`MailConnector` 接口预留多账号与多文件夹扩展。
- V1 聚焦功能性：不引入高级安全能力、IM 通知、完整 AI 成本核算；基础安全边界和运行限制必须实现，SMTP 投递接口插件化预留。
- 高级表达式使用自定义 DSL + AST 沙箱求值，禁止 `eval`。
- 第一版使用本地账号、IMAP/SMTP，不接入 SSO 或 OAuth2。
- 外部 AI 默认关闭，必须经过策略授权。
- 邮箱默认只读，不自动标记、移动或删除邮件。
- 标签、星标和已处理状态默认为 MailPulse 应用内状态，不修改 IMAP 服务器状态。
- 所有附件先通过项目内置 MarkItDown 转换为 Markdown 和关联图片资源；模型不使用原生 PDF 上传能力。
- 主模型和副模型都支持 OpenAI-compatible HTTP 与内部模型服务适配，模型能力以 `text_input`、`image_input`、`structured_output` 和 `strict_json_schema` 独立声明。
- 图片 OCR 不是独立硬编码模块，而是视觉模型处理 Markdown 转换结果中图片资源的一种实现。
- UIDVALIDITY 只用于同步游标和重同步判断，邮件身份由 `CanonicalMessage` 与 `MessageOccurrence` 分离维护。
- FTS5/trigram 能力在启动时检测，不可用或不适合短查询时降级到规范化字段和 `LIKE`。
- 调度使用 IANA 时区、单 worker 和唯一 `run_key`；错过窗口默认合并为一次从上次成功游标到当前时间的补跑。
- 自动数据清理默认关闭，启用后必须支持预览、审计和可恢复处理。
- 根目录 `AGENTS.md` 已建立，定义源代码、运行数据、测试和生成物边界。
- 设计方案已更新（V1 功能范围确定），核心垂直切片已实现；剩余工作以生产化加固和功能扩展为主。
