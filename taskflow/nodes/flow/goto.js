import { reportNodeEvent } from "../../core/node-reporter.js";
import {openNodePropertyEditor} from "../../core/input-dialog.js";

class GotoNode extends LiteGraph.LGraphNode {
  static title = "跳转";

  constructor() {
    super();
    this.title = "跳转";
    this.category = "Flow";

    this.addInput("触发", LiteGraph.EVENT);

    this.properties = {
      target: "label_1"
    };

    this._targetWidget = this.addWidget("text", "目标标签", this.properties.target, (value) => {
      this.properties.target = value;
    });

    // 禁用widget的键盘输入，强制使用属性编辑器
    this._targetWidget.onKeyDown = (e) => {
      e.stopPropagation();
      e.preventDefault();
      return false;
    };

    // 添加属性配置
    this.properties_info = [
      {
        name: "target",
        type: "text",
        label: "目标标签"
      }
    ];
  }


  onDblClick(e, pos, canvas) {
    openNodePropertyEditor(this);
    return true;
  }

  onConfigure(info) {
    // 同步widget值到properties
    if (this._targetWidget) {
      this._targetWidget.value = this.properties.target;
    }
  }

  onAction(action, param, options) {
    window.taskflowLog?.("info", `[跳转] 目标标签: ${this.properties.target}`);
    reportNodeEvent(this, "trigger", { action, target: this.properties.target });

    const graph = this.graph;
    const target = String(this.properties.target || "").trim();

    if (!graph || !target) {
      window.taskflowLog?.("warn", "[跳转] 目标标签为空");
      return;
    }

    const nodes = graph._nodes || [];
    const normalize = value => String(value ?? "").trim();
    const knownLabelTypes = new Set(["flow/label", "label"]);

    const getNodeLabelCandidates = node => {
      const candidates = [];
      const props = node.properties || {};
      candidates.push(props.label, props.target, props.name, props.id, props.tag);
      candidates.push(node.title, node.constructor?.title);
      if (Array.isArray(node.widgets)) {
        for (const widget of node.widgets) {
          candidates.push(widget?.value);
        }
      }
      return candidates.map(normalize).filter(Boolean);
    };

    console.log(`[GotoNode] 查找label节点, target=${target}, 总节点数=${nodes.length}`);
    console.log(`[GotoNode] 所有节点:`, nodes.map(n => ({id: n.id, type: n.type, title: n.title})));

    for (const node of nodes) {
      const type = normalize(node.type).toLowerCase();
      const title = normalize(node.title);
      const isLabelLike = knownLabelTypes.has(type) || title === "Label" || title === "标签";
      if (!isLabelLike) continue;

      const candidates = getNodeLabelCandidates(node);
      if (candidates.includes(target)) {
        window.taskflowLog?.("info", `[跳转] 找到标签节点: ${target}`);
        reportNodeEvent(this, "goto_hit", { target, matched_node_id: node.id });

        // 将label节点插入到执行序列中goto的下一位
        const controller = window.workflowController;
        if (controller) {
          window.taskflowLog?.("info", `[跳转] 跳转到标签节点: ${target}`);
          controller._insertNodeAfter(node, this);
        }

        return;
      }
    }

    reportNodeEvent(this, "goto_miss", { target });
    window.taskflowLog?.("error", `[跳转] 未找到目标标签: ${target}`);

    // 弹窗提醒并暂停流程
    window.showToast?.(`未找到目标标签: ${target}`, "error");
    window.workflowController?.stop();
  }
}

LiteGraph.registerNodeType("flow/goto", GotoNode);