export default class WSClient{
    constructor(url) {
        this.url = url;
        this.ws = null;
        this.callbacks = new Map();
    }

    connect() {
        this.ws = new WebSocket(this.url);

        this.ws.onopen = () => {
            console.log("[WS] Connected");
        };

        this.ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            this.handleMessage(data);
        };

        this.ws.onclose = () => {
            console.log("[WS] Disconnected");
        };
    }

    send(data) {
        this.ws.send(JSON.stringify(data));
    }

    request(action, params) {
        const id = crypto.randomUUID();

        return new Promise((resolve, reject) => {
            this.callbacks.set(id, { resolve, reject });

            this.send({
                type: "task",
                id,
                action,
                params
            });
        });
    }

    handleMessage(data) {
        const cb = this.callbacks.get(data.id);
        if (cb) {
            cb.resolve(data);
            this.callbacks.delete(data.id);
        }
    }
}