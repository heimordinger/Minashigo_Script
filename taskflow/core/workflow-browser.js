// core/workflow-browser.js
// 统一文件浏览器对话框 — 供 save/load/从库选择/删除 共用

/**
 * 打开文件浏览器对话框
 * @param {Object} options
 * @param {"save"|"load"|"select"} options.mode  — save: 输入文件名保存; load: 点击文件加载; select: 点击选中路径
 * @param {string} [options.title]               — 对话框标题，默认自动
 * @param {string} [options.jsonContent]         — mode=save 时的 JSON 内容
 * @param {(path: string) => void} [options.onSelect]  — mode=load/select 时选中回调
 * @param {(name: string) => Promise<boolean>} [options.onSave] — mode=save 时保存回调
 * @param {(path: string) => Promise<boolean>} [options.onDelete] — 删除文件回调（默认调用 API）
 */
export async function showWorkflowBrowser(options = {}) {
  const { mode = "load", title, jsonContent, onSelect, onSave, onDelete } = options;

  // 获取文件列表
  let files = [];
  try {
    const resp = await fetch("/api/list_workflows");
    const result = await resp.json();
    if (result.success) files = result.files || [];
  } catch (_) {}

  // 构建树
  const tree = { $files: [] };
  for (const f of files) {
    const parts = f.split("/");
    let node = tree;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!node[parts[i]]) node[parts[i]] = { $files: [] };
      node = node[parts[i]];
    }
    node.$files.push({ full: f, name: parts[parts.length - 1] });
  }

  // 创建 DOM
  const picker = document.createElement("div");
  picker.id = "workflow-browser";
  picker.style.cssText =
    "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);" +
    "background:#2a2a2a;border:1px solid #555;border-radius:8px;padding:16px;" +
    "z-index:10000;min-width:380px;max-width:520px;max-height:80vh;display:flex;flex-direction:column;" +
    "box-shadow:0 4px 20px rgba(0,0,0,0.5);";

  const dialogTitle = title || (mode === "save" ? "保存工作流" : mode === "select" ? "选择脚本" : "加载工作流");
  picker.innerHTML = `<h3 style="margin:0 0 12px;color:#eee;font-size:16px;">${dialogTitle}</h3>`;

  // 文件名输入（仅 save 模式）
  let nameInput = null;
  if (mode === "save") {
    nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.value = "workflow";
    nameInput.style.cssText =
      "width:calc(100% - 16px);padding:8px;margin-bottom:4px;border:1px solid #555;" +
      "border-radius:4px;background:#3a3a3a;color:#eee;font-size:14px;outline:none;";
    nameInput.placeholder = "输入文件名（可含子路径，如: 测试/文件名）";
    picker.appendChild(nameInput);

    const hint = document.createElement("div");
    hint.style.cssText = "color:#777;font-size:11px;margin-bottom:10px;";
    hint.textContent = "可用子路径: 文件夹/文件名";
    picker.appendChild(hint);
  }

  // 面包屑导航 + 文件列表
  let _browsePath = [];
  const body = document.createElement("div");
  body.style.cssText = "flex:1;overflow-y:auto;min-height:160px;max-height:260px;margin-bottom:12px;";

  function getNode(path) {
    let node = tree;
    for (const seg of path) node = node[seg] || {};
    return node;
  }

  function showConfirmDialog(msg, confirmText, danger) {
    if (!confirmText) confirmText = "确定";
    return new Promise(resolve => {
      const overlay = document.createElement("div");
      overlay.style.cssText =
        "position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);" +
        "z-index:10001;display:flex;align-items:center;justify-content:center;";

      const box = document.createElement("div");
      box.style.cssText =
        "background:#2a2a2a;border:1px solid #555;border-radius:8px;padding:20px;" +
        "min-width:300px;max-width:420px;box-shadow:0 10px 30px rgba(0,0,0,0.5);";

      const text = document.createElement("div");
      text.style.cssText = "color:#eee;font-size:14px;margin-bottom:18px;line-height:1.5;";
      text.style.whiteSpace = "pre-wrap";
      text.textContent = msg;
      box.appendChild(text);

      const btnRow = document.createElement("div");
      btnRow.style.cssText = "display:flex;gap:10px;justify-content:flex-end;";

      const cancelBtn = document.createElement("button");
      cancelBtn.textContent = "取消";
      cancelBtn.style.cssText = "padding:7px 18px;background:#555;color:#eee;border:none;border-radius:5px;cursor:pointer;";
      cancelBtn.onclick = () => { overlay.remove(); resolve(false); };
      btnRow.appendChild(cancelBtn);

      const confirmBtn = document.createElement("button");
      confirmBtn.textContent = confirmText;
      confirmBtn.style.cssText = `padding:7px 18px;background:${danger ? "#e53935" : "#4a9eff"};color:#fff;border:none;border-radius:5px;cursor:pointer;font-weight:bold;`;
      confirmBtn.onclick = () => { overlay.remove(); resolve(true); };
      btnRow.appendChild(confirmBtn);

      box.appendChild(btnRow);
      overlay.appendChild(box);
      document.body.appendChild(overlay);
      cancelBtn.focus();
    });
  }

  async function deleteFile(fullPath, is_dir) {
    const label = is_dir ? `文件夹「${fullPath}」` : `「${fullPath}」`;
    const ok = await showConfirmDialog(`确定删除${label}？\n${is_dir ? "文件夹内的所有文件将被一并删除。" : ""}`, "确定删除", true);
    if (!ok) return;
    try {
      const resp = await fetch("/api/delete_workflow", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: fullPath, is_dir: !!is_dir }),
      });
      const result = await resp.json();
      if (result.success) {
        showToast(`已删除: ${fullPath}`, "success");
        // 从树中移除
        if (is_dir) {
          // 删除目录：整个子树消失，强制重新渲染
          renderBrowser();
        } else {
          const parts = fullPath.split("/");
          const fileName = parts.pop();
          let node = tree;
          for (const seg of parts) node = node[seg] || {};
          node.$files = node.$files.filter(f => f.full !== fullPath);
          renderBrowser();
        }
        if (typeof onDelete === "function") onDelete(fullPath);
      } else {
        showToast(`删除失败: ${result.error}`, "error");
      }
    } catch (e) {
      showToast(`删除失败: ${e.message}`, "error");
    }
  }

  function renderBrowser() {
    body.innerHTML = "";
    const node = getNode(_browsePath);
    const dirs = Object.keys(node).filter(k => k !== "$files").sort();
    const fs = node.$files || [];

    // 面包屑
    const bread = document.createElement("div");
    bread.style.cssText = "font-size:12px;color:#888;padding:4px 4px 8px;border-bottom:1px solid #444;margin-bottom:6px;display:flex;flex-wrap:wrap;gap:4px;align-items:center;";
    const root = document.createElement("a");
    root.textContent = "📁 scripts";
    root.style.cssText = "color:#aaa;cursor:pointer;text-decoration:none;";
    root.onclick = () => { _browsePath = []; renderBrowser(); };
    bread.appendChild(root);
    for (let i = 0; i < _browsePath.length; i++) {
      bread.appendChild(Object.assign(document.createElement("span"), { textContent: " / ", style: "color:#666;" }));
      const cr = document.createElement("a");
      cr.textContent = _browsePath[i];
      cr.style.cssText = "color:#aaa;cursor:pointer;text-decoration:none;";
      const idx = i;
      cr.onclick = () => { _browsePath = _browsePath.slice(0, idx + 1); renderBrowser(); };
      bread.appendChild(cr);
    }
    body.appendChild(bread);

    // 返回上级
    if (_browsePath.length > 0) {
      const up = document.createElement("div");
      up.style.cssText = "padding:5px 8px;border-radius:4px;cursor:pointer;color:#999;font-size:12px;display:flex;align-items:center;gap:6px;";
      up.innerHTML = "<span>📂</span> ..";
      up.onmouseenter = () => up.style.background = "#3a3a3a";
      up.onmouseleave = () => up.style.background = "transparent";
      up.onclick = () => { _browsePath.pop(); renderBrowser(); };
      body.appendChild(up);
    }

    // 目录（每行带删除按钮）
    for (const d of dirs) {
      const dirPath = _browsePath.length > 0 ? _browsePath.join("/") + "/" + d : d;
      const row = document.createElement("div");
      row.style.cssText = "display:flex;align-items:center;gap:4px;margin:2px 0;border-radius:4px;cursor:pointer;";
      row.onmouseenter = () => { row.style.background = "#3a3a3a"; };
      row.onmouseleave = () => { row.style.background = "transparent"; };

      const folderItem = document.createElement("div");
      folderItem.style.cssText = "flex:1;padding:6px 8px;color:#ccc;font-size:12px;display:flex;align-items:center;gap:6px;";
      folderItem.innerHTML = `<span>📁</span> ${d}`;
      folderItem.onclick = () => { _browsePath.push(d); renderBrowser(); };
      row.appendChild(folderItem);

      // 删除按钮
      const delBtn = document.createElement("span");
      delBtn.textContent = "🗑";
      delBtn.title = "删除文件夹";
      delBtn.style.cssText = "font-size:13px;cursor:pointer;padding:4px 6px;border-radius:4px;display:none;color:#e57373;";
      delBtn.onmouseenter = () => { delBtn.style.background = "rgba(229,115,115,0.15)"; };
      delBtn.onmouseleave = () => { delBtn.style.background = "transparent"; };
      delBtn.onclick = (e) => {
        e.stopPropagation();
        deleteFile(dirPath, true);
      };
      row.onmouseenter = () => { row.style.background = "#3a3a3a"; delBtn.style.display = "inline"; };
      row.onmouseleave = () => { row.style.background = "transparent"; delBtn.style.display = "none"; };
      row.appendChild(delBtn);

      body.appendChild(row);
    }

    // 文件（每行带删除按钮）
    for (const { full, name } of fs) {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;align-items:center;gap:4px;margin:2px 0;border-radius:4px;cursor:pointer;";
      row.onmouseenter = () => { row.style.background = "#3a3a3a"; };
      row.onmouseleave = () => { row.style.background = "transparent"; };

      const fileItem = document.createElement("div");
      fileItem.style.cssText = "flex:1;padding:6px 8px;color:#ddd;font-size:12px;display:flex;align-items:center;gap:6px;";
      fileItem.innerHTML = `<span>📄</span> ${name}`;

      if (mode === "load" || mode === "select") {
        fileItem.onclick = () => {
          if (typeof onSelect === "function") onSelect(full, picker.dataset.addMode === "true");
          picker.remove();
        };
      } else if (mode === "save") {
        fileItem.onclick = async () => {
          const ok = await showConfirmDialog(`文件「${full}」已存在，确定要覆盖？`, "确认覆盖");
          if (!ok) return;
          const name = full.replace(/\.json$/, "");
          if (nameInput) nameInput.value = name;
          if (typeof onSave === "function") {
            const saved = await onSave(name);
            if (saved) picker.remove();
          }
        };
      }
      row.appendChild(fileItem);

      // 删除按钮（所有模式都有）
      const delBtn = document.createElement("span");
      delBtn.textContent = "🗑";
      delBtn.title = "删除";
      delBtn.style.cssText = "font-size:13px;cursor:pointer;padding:4px 6px;border-radius:4px;display:none;color:#e57373;";
      delBtn.onmouseenter = () => { delBtn.style.background = "rgba(229,115,115,0.15)"; };
      delBtn.onmouseleave = () => { delBtn.style.background = "transparent"; };
      delBtn.onclick = (e) => {
        e.stopPropagation();
        deleteFile(full);
      };
      row.onmouseenter = () => { row.style.background = "#3a3a3a"; delBtn.style.display = "inline"; };
      row.onmouseleave = () => { row.style.background = "transparent"; delBtn.style.display = "none"; };
      row.appendChild(delBtn);

      body.appendChild(row);
    }

    if (dirs.length === 0 && fs.length === 0) {
      const empty = document.createElement("div");
      empty.style.cssText = "color:#666;text-align:center;padding:20px;font-size:12px;";
      empty.textContent = "此文件夹为空";
      body.appendChild(empty);
    }
  }

  picker.appendChild(body);
  renderBrowser();

  // 加载模式专用：替换/添加切换
  let _addMode = localStorage.getItem("wf_addMode") === "true";
  picker.dataset.addMode = String(_addMode);
  if (mode === "load") {
    const modeRow = document.createElement("div");
    modeRow.style.cssText = "display:flex;align-items:center;gap:8px;margin-bottom:10px;padding:4px 0;font-size:12px;color:#aaa;";
    const label = document.createElement("span");
    label.textContent = "加载方式：";
    modeRow.appendChild(label);

    const replaceBtn = document.createElement("button");
    replaceBtn.textContent = "替换";
    replaceBtn.style.cssText = "padding:4px 14px;border:1px solid #4a9eff;border-radius:4px;cursor:pointer;font-size:12px;";
    modeRow.appendChild(replaceBtn);

    const addBtn = document.createElement("button");
    addBtn.textContent = "添加";
    addBtn.style.cssText = "padding:4px 14px;border:1px solid #555;border-radius:4px;cursor:pointer;font-size:12px;background:transparent;color:#aaa;";
    modeRow.appendChild(addBtn);

    function applyUI(add) {
      if (add) {
        addBtn.style.background = "#4a9eff"; addBtn.style.color = "#fff"; addBtn.style.borderColor = "#4a9eff";
        replaceBtn.style.background = "transparent"; replaceBtn.style.color = "#aaa"; replaceBtn.style.borderColor = "#555";
      } else {
        replaceBtn.style.background = "#4a9eff"; replaceBtn.style.color = "#fff"; replaceBtn.style.borderColor = "#4a9eff";
        addBtn.style.background = "transparent"; addBtn.style.color = "#aaa"; addBtn.style.borderColor = "#555";
      }
    }
    applyUI(_addMode);

    replaceBtn.onclick = () => {
      _addMode = false; picker.dataset.addMode = "false";
      localStorage.setItem("wf_addMode", "false");
      applyUI(false);
    };
    addBtn.onclick = () => {
      _addMode = true; picker.dataset.addMode = "true";
      localStorage.setItem("wf_addMode", "true");
      applyUI(true);
    };
    picker.appendChild(modeRow);
  }

  // 按钮行
  const btnRow = document.createElement("div");
  btnRow.style.cssText = "display:flex;gap:8px;justify-content:flex-end;";

  const cancelBtn = document.createElement("button");
  cancelBtn.textContent = "取消";
  cancelBtn.style.cssText = "padding:6px 16px;background:#555;color:#eee;border:none;border-radius:4px;cursor:pointer;";
  cancelBtn.onclick = () => picker.remove();
  btnRow.appendChild(cancelBtn);

  if (mode === "save") {
    const saveBtn = document.createElement("button");
    saveBtn.textContent = "保存";
    saveBtn.style.cssText = "padding:6px 16px;background:#4a9eff;color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:bold;";
    let _saving = false;
    saveBtn.onclick = async () => {
      if (_saving) return;
      const rawName = nameInput ? nameInput.value.trim() : "";
      if (!rawName) { showToast("请输入文件名", "warn"); return; }
      const folderPath = _browsePath.length > 0 ? _browsePath.join("/") + "/" : "";
      const name = folderPath + rawName;
      _saving = true;
      saveBtn.disabled = true;
      saveBtn.style.background = "#888";
      saveBtn.textContent = "保存中…";
      try {
        if (typeof onSave === "function") {
          const ok = await onSave(name);
          if (ok) picker.remove();
        } else {
          picker.remove();
        }
      } finally {
        if (picker.isConnected) {
          _saving = false;
          saveBtn.disabled = false;
          saveBtn.style.background = "#4a9eff";
          saveBtn.textContent = "保存";
        }
      }
    };
    btnRow.appendChild(saveBtn);
  }

  picker.appendChild(btnRow);
  document.body.appendChild(picker);
}

// 确保 showToast 可用
function showToast(msg, type) {
  window.showToast?.(msg, type);
}
