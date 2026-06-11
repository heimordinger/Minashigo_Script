import { reportNodeEvent } from "../../core/node-reporter.js";
import { openNodePropertyEditor } from "../../core/input-dialog.js";

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
    if (this._targetWidget) this._targetWidget.value = this.properties.target;
  }

  async onAction(action, param, options) {
    const target = String(this.properties.target || "").trim();
    if (!target) {
      window.taskflowLog?.("warn", "[跳转] 目标标签为空");
      window.workflowController?.stop();
      return;
    }

    const graph = this.graph;
    if (!graph) return;

    const nodes = graph._nodes || [];
    const normalize = v => String(v ?? "").trim();
    const knownLabelTypes = new Set(["flow/label", "label"]);

    console.log(`[GotoNode] 查找label节点, target=${target}, 总节点数=${nodes.length}`);
    console.log(`[GotoNode] 所有节点:`, nodes.map(n => ({id: n.id, type: n.type, title: n.title})));

    for (const node of nodes) {
      const type = normalize(node.type).toLowerCase();
      const title = normalize(node.title);
      const isLabelLike = knownLabelTypes.has(type) || title === "Label" || title === "标签";
      console.log(`[GotoNode] 节点: type="${type}" title="${title}" isLabel=${isLabelLike} props=${JSON.stringify(node.properties)}`);
      if (!isLabelLike) continue;

      const candidates = [node.properties?.label, node.properties?.target, node.properties?.name,
                          node.properties?.id, node.properties?.tag, node.title, node.constructor?.title];
      if (node.widgets) {
        for (const w of node.widgets) candidates.push(w?.value);
      }
      const vals = candidates.map(normalize).filter(Boolean);
      console.log(`[GotoNode] 候选值: ${JSON.stringify(vals)} 目标="${target}"`);
      if (vals.includes(target)) {
        window.taskflowLog?.("info", `[跳转] 找到标签节点: ${target}`);

        // 通过 _runNode 执行标签（带延迟），然后驱动其下游
        window.taskflowLog?.("info", `[跳转] 跳转到标签节点: ${target}`);
        const ctrl = window.workflowController;
        if (ctrl && typeof ctrl._runNode === "function") {
          await ctrl._runNode(node, "flow");
        }
        if (typeof node.execOutput === "function") {
          await node.execOutput(0);
        }
        return;
      }
    }

    window.taskflowLog?.("error", `[跳转] 未找到目标标签: ${target}`);
    window.workflowController?.stop();
  }
}

LiteGraph.registerNodeType("flow/goto", GotoNode);