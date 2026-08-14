# MailPulse：内网多用户邮件智能整理工具

## Summary

当前项目目录为空，第一步建立项目规范和模块化 Python 单体应用。系统支持：

- 多用户独立邮箱、权限隔离和本地账号登录。
- IMAP 增量拉取、SMTP 报告发送。
- 可视化规则 + 高级表达式。
- 主模型与副模型角色化配置。
- 管理员控制台和用户报告页面。
- 默认只读邮箱、可追溯来源、失败可重试。

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
- SQLite 默认，保留 PostgreSQL 兼容能力
- APScheduler 或等价轻量调度器
- 支持 `web`、`worker`、`run-once` 运行模式
- 支持 systemd，不依赖 Docker

### 主要模块

- `auth`：本地账号、Session、密码哈希、权限。
- `mail`：IMAP/SMTP、增量同步、线程归并、去重。
- `filtering`：规则树、高级表达式、预览和回放。
- `attachments`：PDF、DOCX、XLSX、HTML、文本解析。
- `ai`：模型目录、能力检测、路由、结构化输出。
- `reports`：摘要模板、报告渲染、来源引用。
- `delivery`：SMTP 发送、重试和发送状态。
- `jobs`：调度、运行记录、锁和失败恢复。
- `audit`：审计日志、敏感访问授权和操作追踪。

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
```

核心类型：

- `ModelProfile`
- `ModelBinding`
- `ModelCapability`
- `RoutingDecision`
- `VisualEvidence`
- `StructuredSummary`
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
- 第一版使用本地账号、IMAP/SMTP，不接入 SSO 或 OAuth2。
- 外部 AI 默认关闭，必须经过策略授权。
- 邮箱默认只读，不自动标记、移动或删除邮件。
- 主模型和副模型都支持 OpenAI-compatible HTTP 与内部模型服务适配。
- 图片 OCR 不是独立硬编码模块，而是视觉模型能力的一种实现。
- 自动数据清理默认关闭，启用后必须支持预览、审计和可恢复处理。
- 本轮只更新设计方案，尚未修改项目文件。
