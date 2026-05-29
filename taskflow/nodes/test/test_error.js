// taskflow/nodes/test/test_error.js
import { openNodePropertyEditor } from "../../core/input-dialog.js";

class TestErrorNode extends LiteGraph.LGraphNode {
  static title = "测试错误";

  constructor() {
    super();
    this.title = "测试错误";
    this.category = "Test";

    this.addInput("触发", LiteGraph.EVENT);
    this.addOutput("下一步", LiteGraph.EVENT);

    this.properties = {
      exception_type: "generic_error", // 异常类型
      error_message: "测试错误",
      delay_ms: 5000
    };

    this.addWidget("text", "异常类型", this.properties.exception_type, v => (this.properties.exception_type = v));

    this.addWidget("text", "错误信息", this.properties.error_message, v => (this.properties.error_message = v));
    this.addWidget("number", "延迟(ms)", this.properties.delay_ms, v => (this.properties.delay_ms = v));

    // 禁用widget的键盘输入，强制使用属性编辑器
    if (this.widgets) {
      this.widgets.forEach(widget => {
        widget.onKeyDown = (e) => {
          e.stopPropagation();
          e.preventDefault();
          return false;
        };
      });
    }

    this.properties_info = [
      {
        name: "exception_type",
        type: "select",
        label: "异常类型",
        values: [
          { value: "generic_error", label: "通用错误" },
          { value: "network_error", label: "网络错误" },
          { value: "timeout_error", label: "超时错误" },
          { value: "validation_error", label: "验证错误" },
          { value: "permission_error", label: "权限错误" },
          { value: "not_found_error", label: "未找到错误" },
          { value: "internal_error", label: "内部错误" },
          { value: "database_error", label: "数据库错误" },
          { value: "api_error", label: "API错误" },
          { value: "file_error", label: "文件错误" },
          { value: "memory_error", label: "内存错误" },
          { value: "concurrency_error", label: "并发错误" }
        ]
      },
      {
        name: "error_message",
        type: "text",
        label: "错误信息"
      },
      {
        name: "delay_ms",
        type: "number",
        label: "延迟(ms)",
        min: 0,
        step: 1000
      }
    ];
  }

  onDblClick(e, pos, canvas) {
    openNodePropertyEditor(this);
    return true;
  }

  onConfigure(info) {
    // 同步widget值到properties
    if (this.widgets) {
      this.widgets.forEach(widget => {
        if (widget.name === "异常类型") {
          widget.value = this.properties.exception_type;
        } else if (widget.name === "错误信息") {
          widget.value = this.properties.error_message;
        } else if (widget.name === "延迟(ms)") {
          widget.value = this.properties.delay_ms;
        }
      });
    }
  }

  // 获取异常类型的显示名称
  _getExceptionTypeLabel(exceptionType) {
    const labels = {
      "generic_error": "通用错误",
      "network_error": "网络错误",
      "timeout_error": "超时错误",
      "validation_error": "验证错误",
      "permission_error": "权限错误",
      "not_found_error": "未找到错误",
      "internal_error": "内部错误",
      "database_error": "数据库错误",
      "api_error": "API错误",
      "file_error": "文件错误",
      "memory_error": "内存错误",
      "concurrency_error": "并发错误"
    };
    return labels[exceptionType] || exceptionType;
  }

  async onAction(action, param, options) {
    console.log(`[TestErrorNode] ========== 节点开始执行 ==========`);
    console.log(`[TestErrorNode] 异常类型: ${this.properties.exception_type}`);
    console.log(`[TestErrorNode] 错误信息: ${this.properties.error_message}`);
    console.log(`[TestErrorNode] 延迟: ${this.properties.delay_ms}ms`);

    const delay = this.properties.delay_ms || 5000;
    const errorClass = this._getErrorClass(this.properties.exception_type);

    console.log(`[TestErrorNode] 抛出异常: ${this.properties.error_message}`);
    throw new errorClass(this.properties.error_message);
  }

  _getErrorClass(exceptionType) {
    switch (exceptionType) {
      case "network_error":
        return class NetworkError extends Error {
          constructor(message) {
            super(message);
            this.name = "NetworkError";
          }
        };
      case "timeout_error":
        return class TimeoutError extends Error {
          constructor(message) {
            super(message);
            this.name = "TimeoutError";
          }
        };
      case "validation_error":
        return class ValidationError extends Error {
          constructor(message) {
            super(message);
            this.name = "ValidationError";
          }
        };
      case "permission_error":
        return class PermissionError extends Error {
          constructor(message) {
            super(message);
            this.name = "PermissionError";
          }
        };
      case "not_found_error":
        return class NotFoundError extends Error {
          constructor(message) {
            super(message);
            this.name = "NotFoundError";
          }
        };
      case "internal_error":
        return class InternalError extends Error {
          constructor(message) {
            super(message);
            this.name = "InternalError";
          }
        };
      case "database_error":
        return class DatabaseError extends Error {
          constructor(message) {
            super(message);
            this.name = "DatabaseError";
          }
        };
      case "api_error":
        return class APIError extends Error {
          constructor(message) {
            super(message);
            this.name = "APIError";
          }
        };
      case "file_error":
        return class FileError extends Error {
          constructor(message) {
            super(message);
            this.name = "FileError";
          }
        };
      case "memory_error":
        return class MemoryError extends Error {
          constructor(message) {
            super(message);
            this.name = "MemoryError";
          }
        };
      case "concurrency_error":
        return class ConcurrencyError extends Error {
          constructor(message) {
            super(message);
            this.name = "ConcurrencyError";
          }
        };
      default:
        return Error;
    }
  }
}

LiteGraph.registerNodeType("test/test_error", TestErrorNode);
