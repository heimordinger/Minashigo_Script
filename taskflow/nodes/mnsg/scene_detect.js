// taskflow/nodes/mnsg/scene_detect.js
import { reportNodeEvent } from "../../core/node-reporter.js";

class SceneDetectNode extends LiteGraph.LGraphNode {
  constructor() {
    super();
    this.title = "场景识别";
    this.category = "MNSG";
    this.addInput("触发", LiteGraph.EVENT);
    this.addOutput("识别完成", LiteGraph.EVENT);
    this.addOutput("场景名", "string");
  }
  onAction() {
    reportNodeEvent(this, "trigger");
    this.triggerSlot(0);
  }
}
LiteGraph.registerNodeType("mnsg/scene_detect", SceneDetectNode);

// taskflow/nodes/mnsg/ap_recovery.js
class APRecoveryNode extends LiteGraph.LGraphNode {
  constructor() {
    super();
    this.title = "AP 恢复";
    this.category = "MNSG";
    this.addInput("触发", LiteGraph.EVENT);
    this.addOutput("需要恢复", LiteGraph.EVENT);
    this.addOutput("AP值", "number");
  }
  onAction() {
    reportNodeEvent(this, "trigger");
    this.triggerSlot(0);
  }
}
LiteGraph.registerNodeType("mnsg/ap_recovery", APRecoveryNode);

// taskflow/nodes/mnsg/select_battle.js
class SelectBattleNode extends LiteGraph.LGraphNode {
  constructor() {
    super();
    this.title = "选择战斗";
    this.category = "MNSG";
    this.addInput("触发", LiteGraph.EVENT);
    this.addInput("战斗ID", "number");
    this.addOutput("已选择", LiteGraph.EVENT);
    this.addOutput("成功", "boolean");
  }
  onAction() {
    reportNodeEvent(this, "trigger");
    this.triggerSlot(0);
  }
}
LiteGraph.registerNodeType("mnsg/select_battle", SelectBattleNode);