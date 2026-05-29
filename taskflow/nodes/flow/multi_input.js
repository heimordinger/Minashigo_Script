import { reportNodeEvent } from "../../core/node-reporter.js";

class MultiInputNode {
  static title = "多输入汇合";

  constructor() {
    this.addInput("输入_1", LiteGraph.EVENT);
    this.addOutput("下一步", LiteGraph.EVENT);

    this.input_count = 1;

    this.addWidget(
      "button",
      "新增输入",
      null,
      () => {
        this.addNewInput();
      }
    );
  }

  addNewInput() {
    this.input_count++;

    this.addInput("输入_" + this.input_count, LiteGraph.EVENT);
    reportNodeEvent(this, "add_input", { input_count: this.input_count });

    this.setDirtyCanvas(true, true);
  }
}

LiteGraph.registerNodeType("flow/multi_input", MultiInputNode);