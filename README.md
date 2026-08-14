# MailPulse

MailPulse 是一个面向公司内网的多用户邮件归纳整理工具：通过 IMAP 只读拉取邮件，使用规则筛选，再使用可配置的主模型和可选视觉副模型生成结构化报告，并通过网页和 SMTP 提供结果。

## 开发环境

```bash
uv sync
uv run mailpulse --help
uv run pytest
```

## 本地启动

```bash
uv run mailpulse serve --reload
```

默认地址：<http://127.0.0.1:8080>

运行数据默认写入 `var/`，不会提交到版本库。真实邮箱凭据和 AI Key 不应写入代码或提交文件。
