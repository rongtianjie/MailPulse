// MailPulse 前端增强：表单防重复提交。
// 给 <form data-disable-on-submit> 添加该行为：点击提交按钮后立即禁用并显示处理中提示，
// 避免长时间请求（同步、报告生成、投递等）被重复触发。
document.addEventListener(
  "click",
  (event) => {
    const button = event.target.closest("button[type='submit']");
    if (!button || button.disabled) return;
    const form = button.form;
    if (!form || !form.dataset.disableOnSubmit) return;
    button.disabled = true;
    if (!button.dataset.originalText) {
      button.dataset.originalText = button.textContent;
    }
    button.textContent = button.dataset.processingLabel || "处理中…";
  },
  true,
);
