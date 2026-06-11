export function pickImageFile() {
    return new Promise(resolve => {
        const input = document.createElement("input");

        input.type = "file";
        input.accept = "image/*";

        input.onchange = e => {
            const file = e.target.files[0];
            resolve(file || null);
        };

        input.click();
    });
}

/**
 * 从 assets/images 库中选择图片
 * @param {function} onSelect - 选中后的回调 (dataUrl, fileName) => void
 */
export function pickFromAssets(onSelect) {
    fetch("/api/list_images")
        .then(r => r.json())
        .then(result => {
            if (!result.success || !result.files.length) {
                window.showToast?.("assets/images/ 目录下没有图片", "warn");
                return;
            }
            showAssetPicker(result.files, onSelect);
        })
        .catch(e => {
            window.showToast?.("获取图片列表失败: " + e.message, "error");
        });
}

// 记忆上次在图片库中打开的位置
let _lastPickerPath = [];

function showAssetPicker(files, onSelect) {
    // 移除已有面板
    const old = document.getElementById("asset-picker");
    if (old) old.remove();

    const overlay = document.createElement("div");
    overlay.id = "asset-picker";
    overlay.style.cssText =
        "position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);" +
        "z-index:9999;display:flex;align-items:center;justify-content:center;";

    const panel = document.createElement("div");
    panel.style.cssText =
        "background:#2a2a2a;border:1px solid #555;border-radius:8px;padding:16px;" +
        "min-width:520px;max-width:660px;max-height:85vh;display:flex;flex-direction:column;" +
        "box-shadow:0 4px 20px rgba(0,0,0,0.5);";

    // ─── 构建目录树 ───
    // Windows 路径用 \，其他用 /，统一按 [/\] 切分
    const tree = { $files: [] };
    for (const f of files) {
        const parts = f.split(/[/\\]/);
        let node = tree;
        for (let i = 0; i < parts.length - 1; i++) {
            if (!node[parts[i]]) node[parts[i]] = { $files: [] };
            node = node[parts[i]];
        }
        node.$files.push({ full: f, name: parts[parts.length - 1] });
    }

    // 扁平化叶子节点（只有一张图的单层目录不折叠）
    function flattenSingles(node, path) {
        const keys = Object.keys(node).filter(k => k !== "$files");
        if (keys.length === 1 && node.$files.length === 0 && node[keys[0]]) {
            const sub = node[keys[0]];
            const merged = { $files: sub.$files };
            const subKeys = Object.keys(sub).filter(k => k !== "$files");
            for (const sk of subKeys) merged[path ? `${keys[0]}/${sk}` : sk] = sub[sk];
            return merged;
        }
        return node;
    }
    // 只对根层做一次展开
    const flatTree = flattenSingles(tree, "");

    // ─── 缩略图 ───
    const loaders = [];
    function loadThumb(imgEl, fullPath) {
        const ctrl = new AbortController();
        imgEl._abort = ctrl;
        fetch(`/api/get_thumbnail?name=${encodeURIComponent(fullPath)}&size=100`, { signal: ctrl.signal })
            .then(r => r.json())
            .then(result => { if (result.success) { imgEl.src = result.data_url; imgEl.style.display = "block"; } })
            .catch(() => {});
        return ctrl;
    }

    // ─── 导航状态（从记忆恢复，并验证路径仍有效） ───
    let currentPath = [];
    if (_lastPickerPath.length > 0) {
        let node = flatTree;
        let valid = true;
        for (const seg of _lastPickerPath) {
            if (node[seg] && typeof node[seg] === "object") {
                node = node[seg];
                currentPath.push(seg);
            } else {
                valid = false;
                break;
            }
        }
        if (!valid) currentPath = [];
    }
    const body = document.createElement("div");
    body.style.cssText = "flex:1;overflow-y:auto;min-height:200px;";

    function getNode(path) {
        let node = flatTree;
        for (const seg of path) node = node[seg] || {};
        return node;
    }

    function getPathLabel(path) {
        return path.length ? path.join(" / ") : "assets/images/";
    }

    // ─── 渲染当前目录 ───
    function render() {
        body.innerHTML = "";
        const node = getNode(currentPath);
        const dirs = Object.keys(node).filter(k => k !== "$files").sort((a, b) => a.localeCompare(b));
        const files = (node.$files || []);

        // 面包屑
        const bread = document.createElement("div");
        bread.style.cssText = "font-size:12px;color:#888;padding:6px 4px 10px;border-bottom:1px solid #444;margin-bottom:8px;display:flex;flex-wrap:wrap;gap:4px;align-items:center;";
        const rootLink = document.createElement("a");
        rootLink.textContent = "📁 图片库";
        rootLink.style.cssText = "color:#aaa;cursor:pointer;text-decoration:none;";
        rootLink.onclick = () => { currentPath = []; render(); };
        bread.appendChild(rootLink);

        for (let i = 0; i < currentPath.length; i++) {
            const sep = document.createElement("span");
            sep.textContent = " / ";
            sep.style.cssText = "color:#666;";
            bread.appendChild(sep);
            const crumb = document.createElement("a");
            crumb.textContent = currentPath[i];
            crumb.style.cssText = "color:#aaa;cursor:pointer;text-decoration:none;";
            const idx = i;
            crumb.onclick = () => { currentPath = currentPath.slice(0, idx + 1); render(); };
            bread.appendChild(crumb);
        }

        const countSpan = document.createElement("span");
        countSpan.style.cssText = "color:#666;margin-left:auto;font-size:11px;";
        countSpan.textContent = `${dirs.length}个文件夹 · ${files.length}张图`;
        bread.appendChild(countSpan);
        body.appendChild(bread);

        // 返回上级
        if (currentPath.length > 0) {
            const up = document.createElement("div");
            up.style.cssText = "padding:6px 8px;border-radius:4px;cursor:pointer;color:#999;font-size:13px;display:flex;align-items:center;gap:6px;transition:background .12s;";
            up.innerHTML = "<span style='font-size:14px;'>📂</span> ..";
            up.onmouseenter = () => up.style.background = "#3a3a3a";
            up.onmouseleave = () => up.style.background = "transparent";
            up.onclick = () => { currentPath.pop(); render(); };
            body.appendChild(up);
        }

        // 文件夹列表
        for (const dir of dirs) {
            const subNode = node[dir];
            const subDirs = Object.keys(subNode).filter(k => k !== "$files").length;
            const subFiles = (subNode.$files || []).length;

            const item = document.createElement("div");
            item.style.cssText = "padding:8px 10px;border-radius:4px;cursor:pointer;color:#ccc;font-size:13px;display:flex;align-items:center;gap:8px;transition:background .12s;margin:2px 0;";
            item.innerHTML = `<span style="font-size:16px;">📁</span> <span style="flex:1;">${dir}</span> <span style="font-size:11px;color:#666;">${subFiles}张</span>`;
            item.onmouseenter = () => item.style.background = "#3a3a3a";
            item.onmouseleave = () => item.style.background = "transparent";
            item.onclick = () => { currentPath.push(dir); render(); };
            body.appendChild(item);
        }

        // 文件列表
        for (const { full, name } of files) {
            const item = document.createElement("div");
            item.style.cssText = "padding:5px 8px;background:#3a3a3a;border-radius:4px;cursor:pointer;color:#ddd;display:flex;align-items:center;gap:10px;transition:background .12s;margin:2px 0;";

            const thumbBox = document.createElement("div");
            thumbBox.style.cssText = "width:44px;height:44px;flex-shrink:0;border-radius:3px;overflow:hidden;background:#555;display:flex;align-items:center;justify-content:center;";
            const img = document.createElement("img");
            img.style.cssText = "width:100%;height:100%;object-fit:cover;display:none;";
            const spinner = document.createElement("div");
            spinner.textContent = "...";
            spinner.style.cssText = "color:#888;font-size:10px;";
            thumbBox.appendChild(spinner);
            thumbBox.appendChild(img);
            item.appendChild(thumbBox);

            const label = document.createElement("span");
            label.style.cssText = "font-size:13px;word-break:break-all;flex:1;";
            label.textContent = name;
            item.appendChild(label);

            item.onmouseenter = () => item.style.background = "#4a4a4a";
            item.onmouseleave = () => item.style.background = "#3a3a3a";
            item.onclick = async () => {
                if (img._abort) img._abort.abort();
                try {
                    const resp = await fetch(`/api/get_image?name=${encodeURIComponent(full)}`);
                    const result = await resp.json();
                    if (result.success) {
                        onSelect(result.data_url, full);
                    } else {
                        window.showToast?.("加载图片失败: " + result.error, "error");
                    }
                } catch (e) {
                    window.showToast?.("加载图片失败: " + e.message, "error");
                }
                overlay.remove();
            };

            const loader = loadThumb(img, full);
            loaders.push(loader);
            body.appendChild(item);
        }

        if (dirs.length === 0 && files.length === 0) {
            const empty = document.createElement("div");
            empty.style.cssText = "color:#666;text-align:center;padding:40px 0;font-size:13px;";
            empty.textContent = "此文件夹为空";
            body.appendChild(empty);
        }

        _lastPickerPath = [...currentPath]; // 记忆当前位置
    }

    // ─── 标题 ───
    const header = document.createElement("div");
    header.style.cssText = "display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;";
    const title = document.createElement("h3");
    title.style.cssText = "margin:0;color:#eee;font-size:15px;";
    title.textContent = "选择图片";
    header.appendChild(title);
    panel.appendChild(header);

    panel.appendChild(body);

    const closeBtn = document.createElement("button");
    closeBtn.textContent = "取消";
    closeBtn.style.cssText = "margin-top:10px;padding:6px 16px;background:#555;color:#eee;border:none;border-radius:4px;cursor:pointer;align-self:flex-end;";
    closeBtn.onclick = () => { loaders.forEach(c => c.abort()); overlay.remove(); };
    panel.appendChild(closeBtn);

    overlay.appendChild(panel);
    document.body.appendChild(overlay);

    overlay.addEventListener("click", e => {
        if (e.target === overlay) { loaders.forEach(c => c.abort()); overlay.remove(); }
    });

    render();
}
