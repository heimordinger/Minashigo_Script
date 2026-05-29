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
        "min-width:500px;max-width:620px;max-height:85vh;overflow-y:auto;" +
        "box-shadow:0 4px 20px rgba(0,0,0,0.5);";

    panel.innerHTML = `<h3 style="margin:0 0 12px;color:#eee;font-size:16px;">选择图片 (assets/images/)</h3>`;

    // 按目录分组
    const groups = {};
    files.forEach(f => {
        const idx = f.lastIndexOf("/");
        const dir = idx > 0 ? f.slice(0, idx) : ".";
        const name = idx > 0 ? f.slice(idx + 1) : f;
        if (!groups[dir]) groups[dir] = [];
        groups[dir].push({ full: f, name });
    });

    // 缩略图懒加载：每个 Item 创建后自动加载
    function loadThumb(imgEl, fullPath) {
        const ctrl = new AbortController();
        imgEl._abort = ctrl;
        fetch(`/api/get_thumbnail?name=${encodeURIComponent(fullPath)}&size=100`, { signal: ctrl.signal })
            .then(r => r.json())
            .then(result => {
                if (result.success) {
                    imgEl.src = result.data_url;
                    imgEl.style.display = "block";
                }
            })
            .catch(() => {});
        return ctrl;
    }

    const list = document.createElement("div");
    list.style.cssText = "display:flex;flex-direction:column;gap:3px;";

    const allLoaders = [];

    for (const [dir, entries] of Object.entries(groups)) {
        if (dir !== ".") {
            const dirLabel = document.createElement("div");
            dirLabel.style.cssText =
                "font-size:11px;color:#888;padding:4px 4px 2px 0;font-weight:500;margin-top:4px;";
            dirLabel.textContent = dir + "/";
            list.appendChild(dirLabel);
        }

        entries.forEach(({ full, name }) => {
            const item = document.createElement("div");
            item.style.cssText =
                "padding:4px 8px;background:#3a3a3a;border-radius:4px;cursor:pointer;color:#ddd;" +
                "display:flex;align-items:center;gap:10px;transition:background .12s;";

            // 缩略图容器
            const thumbBox = document.createElement("div");
            thumbBox.style.cssText =
                "width:50px;height:50px;flex-shrink:0;border-radius:3px;overflow:hidden;" +
                "background:#555;display:flex;align-items:center;justify-content:center;";

            const img = document.createElement("img");
            img.style.cssText = "width:100%;height:100%;object-fit:cover;display:none;";

            // 加载中占位
            const spinner = document.createElement("div");
            spinner.textContent = "...";
            spinner.style.cssText = "color:#888;font-size:10px;";

            thumbBox.appendChild(spinner);
            thumbBox.appendChild(img);
            item.appendChild(thumbBox);

            // 文件名
            const label = document.createElement("span");
            label.style.cssText = "font-size:13px;word-break:break-all;";
            label.textContent = name;
            item.appendChild(label);

            // 鼠标悬停
            item.onmouseenter = () => { item.style.background = "#4a4a4a"; };
            item.onmouseleave = () => { item.style.background = "#3a3a3a"; };

            // 点击选中
            item.onclick = async () => {
                // 取消正在进行的缩略图请求（如果有）
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

            list.appendChild(item);

            // 延迟加载缩略图（IntersectionObserver 懒加载）
            const loader = loadThumb(img, full);
            allLoaders.push(loader);
        });
    }
    panel.appendChild(list);

    const closeBtn = document.createElement("button");
    closeBtn.textContent = "取消";
    closeBtn.style.cssText =
        "margin-top:12px;padding:6px 16px;background:#555;color:#eee;" +
        "border:none;border-radius:4px;cursor:pointer;";
    closeBtn.onclick = () => {
        allLoaders.forEach(c => c.abort());
        overlay.remove();
    };
    panel.appendChild(closeBtn);

    overlay.appendChild(panel);
    document.body.appendChild(overlay);

    // 点击遮罩关闭
    overlay.addEventListener("click", e => {
        if (e.target === overlay) {
            allLoaders.forEach(c => c.abort());
            overlay.remove();
        }
    });
}
