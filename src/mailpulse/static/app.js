// MailPulse 前端增强：
// 1. 表单防重复提交（data-disable-on-submit）。
// 2. 新建任务四步向导（data-wizard）：步骤切换、规则/条件动态行、投递地址收集。
// 3. 规则编辑器（data-rule-editor）：简单条件 <-> JSON 高级模式切换。

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

const RULE_FIELDS = ["subject", "sender", "recipients", "cc", "body_text", "attachment_name", "local_labels"];
const RULE_OPERATORS = ["contains", "not_contains", "equals", "starts_with", "ends_with", "regex"];
const FIELD_LABELS = {
  subject: "邮件标题", sender: "发件人", recipients: "收件人", cc: "抄送",
  body_text: "邮件正文", attachment_name: "附件名称", local_labels: "标签",
};
const OPERATOR_LABELS = {
  contains: "包含", not_contains: "不包含", equals: "等于",
  starts_with: "开头是", ends_with: "结尾是", regex: "正则匹配",
};

function makeConditionRow(field, operator, value) {
  const row = document.createElement("div");
  row.className = "condition-row";
  const fieldSelect = document.createElement("select");
  fieldSelect.className = "condition-field";
  RULE_FIELDS.forEach((f) => {
    const option = document.createElement("option");
    option.value = f;
    option.textContent = FIELD_LABELS[f] || f;
    if (f === field) option.selected = true;
    fieldSelect.appendChild(option);
  });
  const opSelect = document.createElement("select");
  opSelect.className = "condition-op";
  RULE_OPERATORS.forEach((op) => {
    const option = document.createElement("option");
    option.value = op;
    option.textContent = OPERATOR_LABELS[op] || op;
    if (op === operator) option.selected = true;
    opSelect.appendChild(option);
  });
  const valueInput = document.createElement("input");
  valueInput.className = "condition-value";
  valueInput.placeholder = "匹配内容";
  valueInput.value = value || "";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "link-button danger condition-remove";
  remove.title = "删除条件";
  remove.textContent = "×";
  remove.addEventListener("click", () => {
    if (row.parentElement.querySelectorAll(".condition-row").length > 1) row.remove();
  });
  row.append(fieldSelect, opSelect, valueInput, remove);
  return row;
}

function makeRuleRow(name, conditions) {
  const row = document.createElement("div");
  row.className = "rule-row";
  row.dataset.ruleRow = "";

  const head = document.createElement("div");
  head.className = "rule-row-head";
  const nameInput = document.createElement("input");
  nameInput.className = "rule-name";
  nameInput.placeholder = "规则名称，如：项目相关邮件";
  nameInput.value = name || "";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "link-button danger";
  remove.dataset.removeRule = "";
  remove.textContent = "删除规则";
  head.append(nameInput, remove);

  const conditionsBox = document.createElement("div");
  conditionsBox.className = "rule-conditions";
  const rows = (conditions && conditions.length ? conditions : [{ field: "subject", operator: "contains", value: "" }]);
  rows.forEach((c) => conditionsBox.appendChild(makeConditionRow(c.field, c.operator, c.value)));

  const addCondition = document.createElement("button");
  addCondition.type = "button";
  addCondition.className = "link-button";
  addCondition.dataset.addCondition = "";
  addCondition.textContent = "+ 添加条件（条件之间为「且」关系）";

  row.append(head, conditionsBox, addCondition);
  bindRuleRow(row);
  return row;
}

function bindRuleRow(row) {
  const removeRule = row.querySelector("[data-remove-rule]");
  if (removeRule) removeRule.addEventListener("click", () => row.remove());
  const addCondition = row.querySelector("[data-add-condition]");
  if (addCondition) {
    addCondition.addEventListener("click", () => {
      const conditionsBox = row.querySelector(".rule-conditions");
      conditionsBox.appendChild(makeConditionRow("subject", "contains", ""));
    });
  }
  row.querySelectorAll(".condition-remove").forEach((button) => {
    button.addEventListener("click", () => {
      if (row.querySelectorAll(".condition-row").length > 1) {
        button.closest(".condition-row").remove();
      }
    });
  });
}

function collectRules(form) {
  const list = form.querySelector("[data-rule-list]");
  const rules = [];
  if (!list) return rules;
  const rows = list.querySelectorAll("[data-rule-row]");
  const topName = (form.querySelector("[name='name']") || {}).value?.trim() || "";
  rows.forEach((row, index) => {
    const conditions = [];
    row.querySelectorAll(".condition-row").forEach((condition) => {
      conditions.push({
        field: condition.querySelector(".condition-field").value,
        operator: condition.querySelector(".condition-op").value,
        value: condition.querySelector(".condition-value").value,
      });
    });
    if (!conditions.length) return;
    let name = (row.querySelector(".rule-name") || {}).value?.trim() || "";
    if (rows.length === 1 && topName) name = topName;
    rules.push({ name: name || `规则 ${index + 1}`, conditions });
  });
  return rules;
}

function conditionsToDefinition(conditions) {
  const nodes = conditions.map((c) => ({
    kind: "condition",
    field: c.field,
    operator: c.operator,
    value: c.value,
  }));
  if (nodes.length === 1) return nodes[0];
  return { kind: "group", operator: "and", children: nodes };
}

function definitionToConditions(definition) {
  if (!definition || typeof definition !== "object") return null;
  if (definition.kind === "condition") {
    return [{ field: definition.field, operator: definition.operator, value: String(definition.value || "") }];
  }
  if (definition.kind === "group" && definition.operator === "and") {
    const rows = [];
    for (const child of definition.children || []) {
      if (child.kind !== "condition") return null;
      rows.push({ field: child.field, operator: child.operator, value: String(child.value || "") });
    }
    return rows.length ? rows : null;
  }
  return null;
}

function setupWizard(form) {
  const steps = [...form.querySelectorAll(".wizard-step")];
  const indicators = [...form.querySelectorAll(".wizard-steps li")];
  let current = 0;
  const showStep = (index) => {
    current = Math.max(0, Math.min(steps.length - 1, index));
    steps.forEach((step, i) => step.classList.toggle("active", i === current));
    indicators.forEach((indicator, i) => {
      indicator.classList.toggle("active", i === current);
      indicator.classList.toggle("done", i < current);
    });
  };
  form.querySelectorAll("[data-next]").forEach((button) => {
    button.addEventListener("click", () => {
      const section = steps[current];
      const fields = section.querySelectorAll("input, select, textarea");
      for (const field of fields) {
        if (!field.checkValidity()) {
          field.reportValidity();
          return;
        }
      }
      showStep(current + 1);
    });
  });
  form.querySelectorAll("[data-prev]").forEach((button) => {
    button.addEventListener("click", () => showStep(current - 1));
  });
  showStep(0);

  const list = form.querySelector("[data-rule-list]");
  if (list) {
    list.querySelectorAll("[data-rule-row]").forEach(bindRuleRow);
    const addRule = form.querySelector("[data-add-rule]");
    if (addRule) {
      addRule.addEventListener("click", () => list.appendChild(makeRuleRow("", null)));
    }
  }

  const targetInput = form.querySelector("#target-input");
  const targetList = form.querySelector("[data-target-list]");
  const addTarget = form.querySelector("[data-add-target]");
  if (targetInput && targetList && addTarget) {
    const appendTarget = (email) => {
      const item = document.createElement("li");
      item.dataset.email = email;
      item.textContent = email;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "target-remove";
      remove.title = "移除";
      remove.textContent = "×";
      remove.addEventListener("click", () => item.remove());
      item.appendChild(remove);
      targetList.appendChild(item);
    };
    addTarget.addEventListener("click", () => {
      const email = targetInput.value.trim().toLowerCase();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
        targetInput.setCustomValidity("请输入有效的邮箱地址");
        targetInput.reportValidity();
        return;
      }
      targetInput.setCustomValidity("");
      const exists = [...targetList.querySelectorAll("[data-email]")].some(
        (item) => item.dataset.email === email,
      );
      if (exists) return;
      appendTarget(email);
      targetInput.value = "";
    });
  }

  form.addEventListener("submit", () => {
    const rulesJson = form.querySelector("#rules-json");
    if (rulesJson) rulesJson.value = JSON.stringify(collectRules(form));
    const targetsJson = form.querySelector("#targets-json");
    if (targetsJson) {
      const emails = [...(targetList ? targetList.querySelectorAll("[data-email]") : [])].map(
        (item) => item.dataset.email,
      );
      targetsJson.value = JSON.stringify(emails);
    }
  });
}

function setupRuleEditor(form) {
  const tabs = [...form.querySelectorAll("[data-editor-tab]")];
  const panes = [...form.querySelectorAll("[data-editor-pane]")];
  const modeInput = form.querySelector("[data-rule-mode]");
  const jsonPane = form.querySelector('[data-editor-pane="json"]');
  const definitionTextarea = form.querySelector(".rule-definition");
  const list = form.querySelector("[data-rule-list]");
  if (!tabs.length || !modeInput) return;

  const activate = (mode) => {
    tabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.editorTab === mode));
    panes.forEach((pane) => pane.classList.toggle("active", pane.dataset.editorPane === mode));
    modeInput.value = mode;
  };

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const target = tab.dataset.editorTab;
      if (target === "json" && definitionTextarea && list) {
        const rules = collectRules(form);
        const conditions = rules.length ? rules[0].conditions : [];
        if (conditions.length) {
          definitionTextarea.value = JSON.stringify(conditionsToDefinition(conditions), null, 2);
        }
      }
      if (target === "form" && definitionTextarea && list) {
        let parsed = null;
        try {
          parsed = JSON.parse(definitionTextarea.value || "{}");
        } catch {
          parsed = null;
        }
        const conditions = definitionToConditions(parsed);
        if (conditions) {
          list.replaceChildren(makeRuleRow("", conditions));
        } else {
          alert("该 JSON 结构无法用简单条件表达（如 or/not/match_all），请继续使用 JSON 模式编辑。");
          return;
        }
      }
      activate(target);
    });
  });

  if (list) {
    list.querySelectorAll("[data-rule-row]").forEach(bindRuleRow);
  }

  form.addEventListener("submit", () => {
    const rulesJson = form.querySelector("#rules-json");
    if (modeInput.value === "form" && rulesJson) {
      rulesJson.value = JSON.stringify(collectRules(form));
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-wizard]").forEach(setupWizard);
  document.querySelectorAll("[data-rule-editor]").forEach(setupRuleEditor);
});
