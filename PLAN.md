# MailPulse：内网多用户邮件智能整理工具

## Summary

当前项目目录为空，第一步建立项目规范和模块化 Python 单体应用。系统支持：

- 多用户独立邮箱、权限隔离和本地账号登录（V1：每用户单账号、仅 INBOX，接口预留扩展）。
- IMAP 增量拉取（UIDVALIDITY 感知）、SMTP 报告发送（投递接口插件化）。
- 可视化规则 + 高级表达式（自定义 DSL，AST 沙箱求值）。
- 主模型与副模型角色化配置。
- 管理员控制台和用户报告页面。
- 邮件全文搜索（FTS5）、标签/星标/已处理标记。
- 统计仪表盘、演示模式。
- 默认只读邮箱、可追溯来源、失败可重试。

> V1 范围决策：聚焦功能性。本期明确不做安全加固（提示注入防护、HTML 消毒、密钥轮换等）、IM 通知（企业微信/钉钉/飞书）与 AI 成本控制（摘要缓存、预算限流），详见下文「V1 范围决策」。

## AI 模型编排

### 模型角色

`ModelProfile` 统一描述模型：

- Provider 类型和 API Endpoint。
- 模型名称。
- 加密后的 API Key。
- 能力声明：`text`、`image`、`pdf`、`structured_output`。
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

### 自动路由流程

```text
邮件正文和附件
        ↓
附件能力分析
        ↓
主模型支持图片/媒体？
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
- 图像型 PDF 先尝试文本提取，缺少有效文本时再转页面图片交给视觉模型。

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
  confidence
  warnings
```

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
- SQLite FTS5 提供邮件全文搜索（trigram tokenizer 支持中文子串匹配）
- APScheduler 或等价轻量调度器
- 支持 `web`、`worker`、`run-once` 运行模式
- 支持 systemd，不依赖 Docker

### 主要模块

- `auth`：本地账号、Session、密码哈希、权限。
- `mail`：IMAP/SMTP、增量同步（UIDVALIDITY 感知）、线程归并、去重、标签/星标/已处理标记。
- `filtering`：规则树、高级表达式（DSL + AST 沙箱求值）、预览和回放。
- `attachments`：PDF、DOCX、XLSX、HTML、文本解析。
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
- 增量同步：跟踪 UIDVALIDITY，检测到变化时触发全量重同步；去重键为 `Message-ID + folder + UIDVALIDITY`。
- 附件：同步时拉取元数据与正文，附件按需下载，设置大小上限与配额显示。
- 高级表达式：自定义 DSL，编译为 AST 后沙箱求值，禁止 `eval` / 任意代码执行；表达式可引用邮件元数据（标签、已处理状态）；规则按优先级求值、支持短路。
- 调度：报告任务按用户时区执行，支持每日 / 每周 / 自定义 cron，定义错过窗口的处理策略。
- 投递：V1 实现 SMTP；`DeliveryProvider` 按插件设计，为 IM Webhook 等渠道预留。
- 明确不做：安全加固（提示注入防护、HTML 消毒、密钥轮换等）、IM 通知、AI 成本控制（摘要缓存、预算限流）。

### 权限

- `user`：只能访问自己的邮箱、规则、报告和附件。
- `admin`：管理账号、配置、模型、任务和运行元数据，默认不能查看正文。
- 敏感正文访问必须通过临时审计授权，填写原因、设置有效期并记录日志。

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
  generate(request)

ModelRouter
  choose_primary(context)
  choose_vision_processor(context)
  build_execution_plan(context)

VisionProcessor
  extract_evidence(message, attachments)

ReportGenerator
  generate_structured_summary(messages, evidence, template)

DeliveryProvider
  send(report, destination)

SearchService
  index_message(message)
  search(query, filters)      # 全文 + 发件人/日期/标签/处理状态组合筛选

MailStore
  set_label(message_id, label)
  set_processed(message_id, processed)

DashboardStats
  collect(scope)              # 处理量、规则命中、待办等聚合

DemoSeeder
  seed(user_id)               # 生成示例邮箱与规则数据
```

核心类型：

- `ModelProfile`
- `ModelBinding`
- `ModelCapability`
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
- 页码、附件编号和原始邮件引用准确性。
- 用户无法访问其他用户的模型配置、邮件和报告。
- 外部模型禁止策略确实阻止数据发送。
- 重复执行不会重复同步、总结和发送。
- UIDVALIDITY 变化时触发全量重同步，不丢邮件、不重复。
- 表达式 DSL：非法表达式报错、沙箱禁止任意代码、规则优先级与短路求值正确。
- 全文搜索：中文子串匹配、发件人/日期/标签/处理状态组合筛选。
- 标签/星标/已处理标记的读写，以及规则引用处理状态。
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
- V1 聚焦功能性：不引入安全加固、IM 通知与 AI 成本控制；SMTP 投递接口插件化预留。
- 高级表达式使用自定义 DSL + AST 沙箱求值，禁止 `eval`。
- 第一版使用本地账号、IMAP/SMTP，不接入 SSO 或 OAuth2。
- 外部 AI 默认关闭，必须经过策略授权。
- 邮箱默认只读，不自动标记、移动或删除邮件。
- 主模型和副模型都支持 OpenAI-compatible HTTP 与内部模型服务适配。
- 图片 OCR 不是独立硬编码模块，而是视觉模型能力的一种实现。
- 自动数据清理默认关闭，启用后必须支持预览、审计和可恢复处理。
- 设计方案已更新（V1 功能范围确定），尚未开始实现。
