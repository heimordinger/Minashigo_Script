// taskflow/core/account-pool-widget.js
// 账号池浮窗 - 在每个tab上显示当前所有账号

// 全局账号池
const accountPool = [];

// 所有浮窗实例
const widgets = [];

// ===== 账号池管理 =====

export function getAccountPool() {
    return [...accountPool];
}

export function addAccountToPool(accountInfo) {
    const email = accountInfo.email;
    if (!email) return;
    if (accountPool.find(a => a.email === email)) return;
    accountPool.push({ ...accountInfo });
    refreshAllWidgets();
}

export function removeAccountFromPool(email) {
    const idx = accountPool.findIndex(a => a.email === email);
    if (idx !== -1) {
        accountPool.splice(idx, 1);
        refreshAllWidgets();
    }
}

// ===== 浮窗组件 =====

export function createPoolWidget(tabObj) {
    const container = document.createElement('div');
    container.className = 'account-pool-widget';

    // 标题栏（可拖拽）
    const header = document.createElement('div');
    header.className = 'apw-header';
    header.innerHTML = '<span class="apw-drag">☰ 账号池</span><button class="apw-toggle">–</button>';

    // 内容区
    const body = document.createElement('div');
    body.className = 'apw-body';

    container.appendChild(header);
    container.appendChild(body);

    // 挂载到tab的canvas容器
    const canvasContainer = tabObj.canvas?.parentElement || document.getElementById('canvas-container');
    if (canvasContainer) {
        canvasContainer.appendChild(container);
    }

    // 折叠/展开
    const toggleBtn = header.querySelector('.apw-toggle');
    let collapsed = false;
    toggleBtn.onclick = () => {
        collapsed = !collapsed;
        body.style.display = collapsed ? 'none' : '';
        toggleBtn.textContent = collapsed ? '+' : '–';
    };

    // 拖拽
    let dragging = false, dragX, dragY, startX, startY;
    header.querySelector('.apw-drag').onmousedown = (e) => {
        dragging = true;
        dragX = e.clientX - container.offsetLeft;
        dragY = e.clientY - container.offsetTop;
        container.style.cursor = 'grabbing';
    };
    document.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        container.style.left = (e.clientX - dragX) + 'px';
        container.style.top = (e.clientY - dragY) + 'px';
        container.style.right = 'auto';
        container.style.bottom = 'auto';
    });
    document.addEventListener('mouseup', () => {
        dragging = false;
        container.style.cursor = '';
    });

    const widget = { container, body, tabObj };
    widgets.push(widget);
    refreshWidget(widget);
    return widget;
}

export function removePoolWidget(tabObj) {
    const idx = widgets.findIndex(w => w.tabObj === tabObj);
    if (idx !== -1) {
        const widget = widgets[idx];
        widget.container.remove();
        widgets.splice(idx, 1);
    }
}

// ===== 刷新 =====

function refreshWidget(widget) {
    const { body } = widget;
    body.innerHTML = '';

    if (accountPool.length === 0) {
        body.innerHTML = '<div class="apw-empty">暂无账号</div>';
        return;
    }

    const list = document.createElement('div');
    list.className = 'apw-list';

    accountPool.forEach(acc => {
        const item = document.createElement('div');
        item.className = 'apw-item';

        // 状态点：检查此账号是否有对应tab
        const hasTab = widgets.some(w =>
            w.tabObj.account && w.tabObj.account.email === acc.email
        );
        const dot = document.createElement('span');
        dot.className = 'apw-dot ' + (hasTab ? 'apw-dot-on' : 'apw-dot-off');
        item.appendChild(dot);

        const label = document.createElement('span');
        label.className = 'apw-label';
        label.textContent = acc.name || acc.email;
        item.appendChild(label);

        list.appendChild(item);
    });

    body.appendChild(list);
}

function refreshAllWidgets() {
    widgets.forEach(refreshWidget);
}
