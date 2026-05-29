export class Message {

    static createEmptyTask() {
        return {
            type: "task",

            meta: {
                id: crypto.randomUUID(),
                timestamp: Date.now(),
                account: null,
            },

            task: {
                task_name: null,
                properties: {},
                timeout: null,
            },

            // 每个表单独立的验证标记
            validated: false
        };
    }

    static validate(form) {

        if (!form.meta.account) {
            throw new Error("传递对象为空");
        }

        if (!form.task.task_name) {
            throw new Error("操作名称为空");
        }

        if (
            !form.task.properties ||
            typeof form.task.properties !== "object"
        ) {
            throw new Error("未传递操作参数");
        }

        // 验证通过
        form.validated = true;
    }
}