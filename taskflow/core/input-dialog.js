let dialogEl = null;
let propertyDialogEl = null;

function ensureDialog() {
  if (dialogEl) return dialogEl;

  const wrap = document.createElement("div");
  wrap.id = "tf-text-input-dialog";
  wrap.style.cssText = [
    "position:fixed",
    "inset:0",
    "display:none",
    "align-items:center",
    "justify-content:center",
    "background:rgba(0,0,0,0.45)",
    "z-index:50000",
  ].join(";");

  wrap.innerHTML = `
    <div style="width:420px;max-width:92vw;background:#1f1f1f;color:#eee;border-radius:10px;padding:14px;box-shadow:0 10px 30px rgba(0,0,0,0.5);">
      <div id="tf-text-input-title" style="font-size:14px;margin-bottom:10px;">输入</div>
      <input id="tf-text-input-field" type="text" style="width:100%;height:34px;border:1px solid #555;border-radius:6px;background:#111;color:#fff;padding:0 10px;outline:none;" />
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px;">
        <button id="tf-text-input-cancel" style="height:30px;padding:0 12px;border:0;border-radius:6px;background:#444;color:#ddd;cursor:pointer;">取消</button>
        <button id="tf-text-input-ok" style="height:30px;padding:0 12px;border:0;border-radius:6px;background:#2d6cdf;color:#fff;cursor:pointer;">确定</button>
      </div>
    </div>
  `;

  document.body.appendChild(wrap);
  dialogEl = wrap;
  return dialogEl;
}

function ensurePropertyDialog() {
  if (propertyDialogEl) return propertyDialogEl;

  const wrap = document.createElement("div");
  wrap.id = "tf-property-dialog";
  wrap.style.cssText = [
    "position:fixed",
    "inset:0",
    "display:none",
    "align-items:center",
    "justify-content:center",
    "background:rgba(0,0,0,0.45)",
    "z-index:50000",
  ].join(";");

  wrap.innerHTML = `
    <div style="width:500px;max-width:92vw;background:#1f1f1f;color:#eee;border-radius:10px;padding:16px;box-shadow:0 10px 30px rgba(0,0,0,0.5);max-height:80vh;overflow-y:auto;">
      <div id="tf-property-title" style="font-size:16px;font-weight:bold;margin-bottom:12px;border-bottom:1px solid #444;padding-bottom:8px;">节点属性</div>
      <div id="tf-property-list" style="display:flex;flex-direction:column;gap:10px;"></div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px;border-top:1px solid #444;padding-top:12px;">
        <button id="tf-property-cancel" style="height:32px;padding:0 16px;border:0;border-radius:6px;background:#444;color:#ddd;cursor:pointer;">取消</button>
        <button id="tf-property-ok" style="height:32px;padding:0 16px;border:0;border-radius:6px;background:#2d6cdf;color:#fff;cursor:pointer;">确定</button>
      </div>
    </div>
  `;

  document.body.appendChild(wrap);
  propertyDialogEl = wrap;
  return propertyDialogEl;
}

export function openTextInputDialog({ title, value = "" }) {
  const el = ensureDialog();
  const titleEl = el.querySelector("#tf-text-input-title");
  const inputEl = el.querySelector("#tf-text-input-field");
  const okBtn = el.querySelector("#tf-text-input-ok");
  const cancelBtn = el.querySelector("#tf-text-input-cancel");

  return new Promise(resolve => {
    const close = result => {
      el.style.display = "none";
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      el.onclick = null;
      inputEl.onkeydown = null;
      resolve(result);
    };

    titleEl.textContent = title || "输入";
    inputEl.value = value ?? "";
    el.style.display = "flex";

    setTimeout(() => {
      inputEl.focus();
      inputEl.select();
    }, 0);

    okBtn.onclick = () => close(inputEl.value);
    cancelBtn.onclick = () => close(null);
    el.onclick = evt => {
      if (evt.target === el) close(null);
    };
    inputEl.onkeydown = evt => {
      if (evt.key === "Enter") close(inputEl.value);
      if (evt.key === "Escape") close(null);
    };
  });
}

export async function editTextWidgetProperty(node, propertyKey, { title, widget } = {}) {
  const value = await openTextInputDialog({
    title,
    value: node.properties?.[propertyKey],
  });
  if (value == null) return null;

  node.properties = node.properties || {};
  node.properties[propertyKey] = String(value).trim();
  if (widget) widget.value = node.properties[propertyKey];
  node.setDirtyCanvas?.(true, true);
  return node.properties[propertyKey];
}

// 创建属性输入控件
function createPropertyInput(propName, propValue, propConfig = {}) {
  const container = document.createElement("div");
  container.style.cssText = "display:flex;flex-direction:column;gap:4px;";

  const label = document.createElement("label");
  label.textContent = propConfig.label || propName;
  label.style.cssText = "font-size:12px;color:#aaa;";
  container.appendChild(label);

  let input;
  const type = propConfig.type || typeof propValue;

  switch (type) {
    case "number":
      input = document.createElement("input");
      input.type = "number";
      input.value = propValue ?? 0;
      if (propConfig.min !== undefined) input.min = propConfig.min;
      if (propConfig.max !== undefined) input.max = propConfig.max;
      if (propConfig.step) input.step = propConfig.step;
      break;

    case "select":
      input = document.createElement("select");
      const options = propConfig.options || propConfig.values || [];
      options.forEach(opt => {
        const option = document.createElement("option");
        option.value = opt.value || opt;
        option.textContent = opt.label || opt;
        if (String(opt.value || opt) === String(propValue)) {
          option.selected = true;
        }
        input.appendChild(option);
      });
      break;

    case "boolean":
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = Boolean(propValue);
      break;

    default:
      input = document.createElement("input");
      input.type = "text";
      input.value = propValue ?? "";
  }

  input.style.cssText = [
    "width:100%",
    "height:32px",
    "border:1px solid #555",
    "border-radius:4px",
    "background:#111",
    "color:#fff",
    "padding:0 8px",
    "outline:none",
  ].join(";");

  if (type === "boolean") {
    input.style.width = "auto";
    input.style.height = "20px";
  }

  container.appendChild(input);

  return { container, input, type };
}

// 打开节点属性编辑器
export function openNodePropertyEditor(node) {
  const el = ensurePropertyDialog();
  const titleEl = el.querySelector("#tf-property-title");
  const listEl = el.querySelector("#tf-property-list");
  const okBtn = el.querySelector("#tf-property-ok");
  const cancelBtn = el.querySelector("#tf-property-cancel");

  // 清空现有属性列表
  listEl.innerHTML = "";

  // 设置标题
  titleEl.textContent = `${node.title || "节点"} 属性`;

  // 获取属性配置
  const propertiesInfo = node.properties_info || [];
  const properties = node.properties || {};

  // 存储输入控件引用
  const propertyInputs = new Map();

  // 创建属性名到widget的映射
  const propToWidgetMap = new Map();
  if (node.widgets) {
    // 优先使用 properties_info 中的 label 与 widget.name 进行匹配
    if (node.properties_info) {
      node.properties_info.forEach(propConfig => {
        const propName = propConfig.name;
        const label = propConfig.label || propName;
        node.widgets.forEach(widget => {
          // 尝试通过 widget.name 匹配 label
          if (widget.name === label) {
            propToWidgetMap.set(propName, widget);
          }
          // 尝试通过 widget.label 匹配 label
          if (widget.label === label) {
            propToWidgetMap.set(propName, widget);
          }
        });
      });
    }

    // 如果没有匹配到，尝试通过 widget.name 直接匹配属性名
    if (propToWidgetMap.size === 0) {
      node.widgets.forEach(widget => {
        if (widget.name && properties[widget.name] !== undefined) {
          propToWidgetMap.set(widget.name, widget);
        }
      });
    }

    // 最后尝试通过 widget.label 匹配属性名
    if (propToWidgetMap.size === 0) {
      node.widgets.forEach(widget => {
        if (widget.label) {
          const cleanLabel = widget.label.replace(/\([^)]*\)/g, '').trim();
          Object.keys(properties).forEach(propName => {
            if (propName === cleanLabel || propName.includes(cleanLabel) || cleanLabel.includes(propName)) {
              propToWidgetMap.set(propName, widget);
            }
          });
        }
      });
    }
  }

  // 如果没有 properties_info，从 properties 生成
  if (propertiesInfo.length === 0 && Object.keys(properties).length > 0) {
    Object.keys(properties).forEach(propName => {
      // 跳过路径类型
      if (propName.toLowerCase().includes("path")) return;

      const propValue = properties[propName];
      const { container, input, type } = createPropertyInput(propName, propValue);
      listEl.appendChild(container);
      propertyInputs.set(propName, { input, type });
    });
  } else {
    // 使用 properties_info 生成
    propertiesInfo.forEach(propConfig => {
      // 跳过路径类型
      if (propConfig.type === "path" || propConfig.name.toLowerCase().includes("path")) return;

      const propName = propConfig.name;
      const propValue = properties[propName];
      const { container, input, type } = createPropertyInput(propName, propValue, propConfig);
      listEl.appendChild(container);
      propertyInputs.set(propName, { input, type });
    });
  }

  // 如果没有可编辑的属性
  if (propertyInputs.size === 0) {
    const noProps = document.createElement("div");
    noProps.textContent = "此节点没有可编辑的属性";
    noProps.style.cssText = "text-align:center;color:#666;padding:20px;";
    listEl.appendChild(noProps);
  }

  return new Promise(resolve => {
    let close = (save = false) => {
      el.style.display = "none";
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      el.onclick = null;

      if (save) {
        // 保存属性值
        propertyInputs.forEach(({ input, type }, propName) => {
          let value;
          switch (type) {
            case "number":
              value = parseFloat(input.value);
              break;
            case "boolean":
              value = input.checked;
              break;
            default:
              value = input.value;
          }
          node.properties[propName] = value;
        });

        // 更新节点状态
        node.setDirtyCanvas?.(true, true);

        // 更新 widgets - 使用预构建的映射
        if (node.widgets && propToWidgetMap) {
          propertyInputs.forEach(({ input, type }, propName) => {
            const widget = propToWidgetMap.get(propName);
            if (widget) {
              widget.value = node.properties[propName];
              // 触发widget的回调以更新内部状态
              if (widget.callback) {
                try {
                  widget.callback(node.properties[propName]);
                } catch (e) {
                  console.warn('Widget callback error:', e);
                }
              }
            }
          });
        }

        // 强制重绘节点和画布
        if (node.graph) {
          node.graph.setDirtyCanvas(true, true);
        }

        // 额外强制节点重绘
        if (node.setDirtyCanvas) {
          node.setDirtyCanvas(true, true);
        }

        // 强制整个画布重绘
        if (node.graph && node.graph.canvas) {
          node.graph.canvas.draw(true, true);
        }

        resolve(true);
      } else {
        resolve(false);
      }
    };

    el.style.display = "flex";

    okBtn.onclick = () => close(true);
    cancelBtn.onclick = () => close(false);
    el.onclick = evt => {
      if (evt.target === el) close(false);
    };

    // ESC 键取消
    const handleKeyDown = evt => {
      if (evt.key === "Escape") close(false);
      if (evt.key === "Enter" && evt.ctrlKey) close(true);
    };
    document.addEventListener("keydown", handleKeyDown);

    // 清理事件监听器
    const originalClose = close;
    close = (save = false) => {
      document.removeEventListener("keydown", handleKeyDown);
      return originalClose(save);
    };
  });
}
