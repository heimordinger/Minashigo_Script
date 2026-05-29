// wsManager.js
export class WSManager {
    static instance = null;

    constructor(url) {
        if (WSManager.instance) {
            return WSManager.instance;
        }

        this.url = url || null;
        this.ws = null;
        this.callbacks = {};
        WSManager.instance = this;
        return this;
    }

    connect(url) {
        // 优先用传入 URL，其次用构造时保存的
        const targetUrl = url || this.url;
        if (!targetUrl) {
            throw new Error("WebSocket URL 未提供");
        }

        this.url = targetUrl;

        // 已连接或正在连接中，不重复创建
        if (this.ws) {
            if (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING) return;
        }

        this.ws = new WebSocket(targetUrl);

        this.ws.onopen = () => {
            console.log("[WS] Connected to", targetUrl);
            // 重连计数器重置
            this.reconnectAttempts = 0;
        };
        
        this.ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);

                // 全局ping消息 - 后端在长时间操作中发来的保活信号
                if (msg.type === "ping") {
                    window.workflowController?.onPingResponse?.();
                    console.debug("[WS] 收到ping保活信号");
                    return;
                }

                if (msg.meta?.id && this.callbacks[msg.meta.id]) {
                    this.callbacks[msg.meta.id](msg);
                    delete this.callbacks[msg.meta.id];
                }
            } catch (e) {
                console.error("[WS] Message parsing error:", e, event.data);
            }
        };
        
        this.ws.onclose = (event) => {
            console.log("[WS] Disconnected, code:", event.code, "reason:", event.reason);
            
            // 只有在非正常关闭时才重连
            if (event.code !== 1000 && event.code !== 1001) {
                // 自动重连，但增加延迟避免循环
                this.attemptReconnect();
            } else {
                console.log("[WS] 正常关闭，不重连");
            }
        };
        
        this.ws.onerror = (err) => {
            console.error("[WS] Error:", err);
            // 连接错误时也尝试重连，但增加延迟
            if (!this.isReconnecting) {
                setTimeout(() => this.attemptReconnect(), 3000);
            }
        };
    }

    sendTask(form) {
        return new Promise((resolve, reject) => {
            if (!form.validated) return reject(new Error("表单未验证"));
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                return reject(new Error("WebSocket 未连接"));
            }

            this.callbacks[form.meta.id] = resolve;
            this.ws.send(JSON.stringify(form));

            setTimeout(() => {
                if (this.callbacks[form.meta.id]) {
                    delete this.callbacks[form.meta.id];
                    reject(new Error("等待响应超时"));
                }
            }, form.task.timeout ?? 30*1000);
        });
    }
    
    attemptReconnect() {
        if (this.reconnectAttempts >= 5) {
            console.error("[WS] 重连次数已达上限，停止重连");
            return;
        }
        
        // 防止重复重连
        if (this.isReconnecting) {
            console.log("[WS] 已在重连中，跳过重复重连");
            return;
        }
        
        this.isReconnecting = true;
        this.reconnectAttempts = (this.reconnectAttempts || 0) + 1;
        console.log(`[WS] 尝试重连 (${this.reconnectAttempts}/5)...`);
        
        setTimeout(() => {
            this.isReconnecting = false;
            this.connect();
        }, 5000 * this.reconnectAttempts); // 增加延迟，避免快速循环
    }
}