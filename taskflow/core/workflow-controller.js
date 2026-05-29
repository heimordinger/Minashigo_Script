// core/workflow-controller.js

export class WorkflowController {
  constructor(getGraph) {
    this.getGraph = getGraph;
    this.state = STATE.IDLE;
    this.currentNode = null;
    this.executionSequence = []; // 执行序列数组
    this.currentLink = null; // 当前执行的连接线
    this.lastBackendDelay = 0; // 最后一次后端响应延迟（毫秒）
    this.requestStartTime = null; // 请求开始时间
    this.errorNode = null; // 错误节点（红色高亮）
    this.pingTimeout = 10000; // ping超时时间（毫秒）- 增加到10秒
    this.nodeTimeout = 60000; // 节点执行超时时间（毫秒）- 增加到60秒
    this.lastPingTime = null; // 最后一次ping时间
    this.nodeExecutionStartTime = null; // 节点执行开始时间
    this.timeoutCheckInterval = null; // 超时检查定时器
    this._startTimeoutChecker();
  }

  setState(state) {
    this.state = state;
    window.updateControlUI?.(state);
    window.updateRuntimeState?.(state);
  }

  start() {
    if (this.state === STATE.RUNNING) return false;
    const startNode = this._findStartNode();
    if (!startNode) return false;

    this.currentNode = null;
    this.executionSequence = [];
    this._buildExecutionSequence(startNode);
    this.setState(STATE.RUNNING);
    this._executeNext();
    return true;
  }

  pause() {
    if (this.state !== STATE.RUNNING) return;
    this.setState(STATE.PAUSED);
  }

  resume() {
    if (this.state !== STATE.PAUSED) return;
    this.setState(STATE.RUNNING);
    this._executeNext();
  }

  stop() {
    if (this.state === STATE.STOPPED || this.state === STATE.IDLE) return;
    this.setState(STATE.STOPPED);
    this.currentNode = null;
    this.executionSequence = [];
    this.currentLink = null;
    this.errorNode = null; // 清除错误节点
    window.updateCurrentNode?.(null);
    window.highlightLink?.(null);
    window.highlightErrorNode?.(null); // 清除错误高亮
  }

  finish() {
    // 防止重复调用
    if (this.state === STATE.FINISHED || this.state === STATE.STOPPED) {
      return;
    }
    this.setState(STATE.FINISHED);
    this.currentNode = null;
    this.executionSequence = [];
    this.currentLink = null;
    this.errorNode = null; // 清除错误节点
    window.updateCurrentNode?.(null);
    window.highlightLink?.(null);
    window.highlightErrorNode?.(null); // 清除错误高亮
  }

  onNodeStart(node) {
    this.currentNode = node;
    window.updateCurrentNode?.(node);
  }

  onNodeFinish(node) {
    if (this.currentNode === node) {
      this.currentNode = null;
      window.updateCurrentNode?.(null);
    }
  }

  async waitUntilRunnable() {
    while (this.state === STATE.PAUSED) {
      await new Promise(resolve => setTimeout(resolve, 80));
    }
    return this.state === STATE.RUNNING;
  }

  // 构建执行序列
  _buildExecutionSequence(startNode) {
    const graph = this.getGraph?.();
    if (!graph) return;

    this.executionSequence = [startNode];
    this._appendNextNodes(startNode);
  }

  // 递归添加后续节点
  _appendNextNodes(node) {
    console.log(`[WorkflowController] _appendNextNodes: 处理节点 ${node.title}, 当前序列: ${this.executionSequence.map(n => n.title).join(", ")}`);

    if (!node.outputs) return;

    const graph = this.getGraph?.();
    if (!graph) return;

    for (const output of node.outputs) {
      if (!output.links || output.links.length === 0) continue;

      for (const linkId of output.links) {
        const link = graph.links[linkId];
        if (!link) continue;

        const targetNode = graph._nodes.find(n => n.id === link.target_id);
        console.log(`[WorkflowController] _appendNextNodes: 找到连接 ${node.title} -> ${targetNode?.title || 'unknown'}`);
        if (targetNode && !this.executionSequence.includes(targetNode)) {
          this.executionSequence.push(targetNode);
          console.log(`[WorkflowController] _appendNextNodes: 添加节点 ${targetNode.title}, 序列: ${this.executionSequence.map(n => n.title).join(", ")}`);
          this._appendNextNodes(targetNode);
        } else if (targetNode) {
          console.log(`[WorkflowController] _appendNextNodes: 节点 ${targetNode.title} 已在序列中，跳过`);
        }
      }
    }
  }

  // 执行下一个节点
  async _executeNext() {
    if (this.state !== STATE.RUNNING || this.executionSequence.length === 0) {
      if (this.executionSequence.length === 0 && this.state === STATE.RUNNING) {
        this.finish();
      }
      return;
    }

    // 检查点1：onAction前检查暂停状态
    await this.waitUntilRunnable();

    const node = this.executionSequence[0];
    console.log(`[WorkflowController] 执行节点: ${node.title} (id: ${node.id}), 剩余节点: ${this.executionSequence.length}`);

    // 先高亮节点
    this.onNodeStart(node);
    window.highlightNode?.(node);

    // 高亮连接线（从上一个节点到当前节点）
    if (this.executionSequence.length > 1) {
      const prevNode = this.executionSequence[1];
      const link = this._findLinkBetweenNodes(prevNode, node);
      if (link) {
        this.currentLink = link;
        window.highlightLink?.(link);
      }
    }

    // 节点延迟
    const delay = window.getNodeDelay?.() || 0;
    if (delay > 0) {
      console.log(`[WorkflowController] 节点延迟: ${delay}ms`);
      await new Promise(resolve => setTimeout(resolve, delay));
    }

    // 记录节点执行开始时间
    this.nodeExecutionStartTime = Date.now();

    try {
      await node.onAction?.("flow");
    } catch (err) {
      console.error(`[WorkflowController] 节点执行错误:`, err);
      // 取消正常高亮，设置错误节点（红色高亮）
      window.unhighlightNode?.(node);
      this.setErrorNode(node);
      // 停止流程
      this.stop();
      // 显示错误信息
      window.showToast?.(`节点 ${node.title} 执行失败: ${err.message}`, "error");
      return;
    }

    // 检查点2：onAction后检查暂停状态
    await this.waitUntilRunnable();

    // 移除已执行的节点（在onAction完成后）
    // 注意：如果节点在onAction中修改了executionSequence，需要确保移除的是正确的节点
    const index = this.executionSequence.indexOf(node);
    if (index !== -1) {
      this.executionSequence.splice(index, 1);
    } else {
      console.warn(`[WorkflowController] 节点 ${node.title} 不在序列中，当前序列: ${this.executionSequence.map(n => n.title).join(", ")}`);
    }

    // 取消高亮
    window.unhighlightNode?.(node);
    window.highlightLink?.(null);
    this.currentLink = null;
    this.onNodeFinish(node);

    // 清除节点执行开始时间
    this.nodeExecutionStartTime = null;

    console.log(`[WorkflowController] 节点 ${node.title} 执行完成，剩余节点: ${this.executionSequence.length}, 序列: ${this.executionSequence.map(n => n.title).join(", ")}`);

    // 继续执行下一个
    if (this.state === STATE.RUNNING) {
      this._executeNext();
    }
  }

  // 查找两个节点之间的连接线
  _findLinkBetweenNodes(sourceNode, targetNode) {
    const graph = this.getGraph?.();
    if (!graph) return null;

    for (const linkId in graph.links) {
      const link = graph.links[linkId];
      if (link.source_id === sourceNode.id && link.target_id === targetNode.id) {
        return link;
      }
    }
    return null;
  }

  // 开始后端请求计时
  startRequest() {
    this.requestStartTime = Date.now();
    this.lastPingTime = Date.now();
  }

  // 收到后端ping响应，计算网络延迟
  onPingResponse() {
    if (this.requestStartTime) {
      this.lastBackendDelay = Date.now() - this.requestStartTime;
      this.requestStartTime = null;
    }
    this.lastPingTime = Date.now();
  }

  // 结束后端请求计时（保留用于兼容，实际延迟由onPingResponse计算）
  endRequest() {
    // 如果没有收到ping响应，则使用当前时间计算（兼容旧逻辑）
    if (this.requestStartTime) {
      this.lastBackendDelay = Date.now() - this.requestStartTime;
      this.requestStartTime = null;
    }
  }

  // 检查ping超时
  checkPingTimeout() {
    // 有节点正在执行时不检查ping（goto等操作可能耗时较长）
    if (!this.lastPingTime || this.state !== STATE.RUNNING || this.currentNode) return;

    const elapsed = Date.now() - this.lastPingTime;
    if (elapsed > this.pingTimeout) {
      throw new Error(`Ping超时: ${elapsed}ms > ${this.pingTimeout}ms`);
    }
  }

  // 检查节点执行超时
  checkNodeTimeout() {
    if (this.nodeExecutionStartTime && this.state === STATE.RUNNING) {
      const elapsed = Date.now() - this.nodeExecutionStartTime;
      if (elapsed > this.nodeTimeout) {
        throw new Error(`节点执行超时: ${elapsed}ms > ${this.nodeTimeout}ms`);
      }
    }
  }

  // 设置错误节点（红色高亮）
  setErrorNode(node) {
    this.errorNode = node;
    window.highlightErrorNode?.(node);
  }

  // 清除错误节点
  clearErrorNode() {
    this.errorNode = null;
    window.highlightErrorNode?.(null);
  }

  // 启动超时检查器
  _startTimeoutChecker() {
    this.timeoutCheckInterval = setInterval(() => {
      if (this.state === STATE.RUNNING) {
        try {
          this.checkPingTimeout();
          this.checkNodeTimeout();
        } catch (err) {
          console.error(`[WorkflowController] 超时检查失败:`, err);
          if (this.currentNode) {
            this.setErrorNode(this.currentNode);
          }
          this.stop();
          window.showToast?.(`超时错误: ${err.message}`, "error");
        }
      }
    }, 1000); // 每秒检查一次
  }

  // 停止超时检查器
  _stopTimeoutChecker() {
    if (this.timeoutCheckInterval) {
      clearInterval(this.timeoutCheckInterval);
      this.timeoutCheckInterval = null;
    }
  }

  // 在指定位置插入节点到执行序列
  _insertNodeAfter(node, insertAfterNode) {
    const index = this.executionSequence.indexOf(insertAfterNode);
    console.log(`[WorkflowController] _insertNodeAfter: 插入节点 ${node.title} 到 ${insertAfterNode.title} 之后, index=${index}, 当前序列: ${this.executionSequence.map(n => n.title).join(", ")}`);

    if (index !== -1) {
      // 移除insertAfterNode之后的所有节点
      this.executionSequence = this.executionSequence.slice(0, index + 1);
      // 添加新节点
      this.executionSequence.push(node);
      console.log(`[WorkflowController] _insertNodeAfter: 插入后序列: ${this.executionSequence.map(n => n.title).join(", ")}`);
      // 重新构建后续节点序列
      this._appendNextNodes(node);
      console.log(`[WorkflowController] _insertNodeAfter: _appendNextNodes后序列: ${this.executionSequence.map(n => n.title).join(", ")}`);
    } else {
      console.warn(`[WorkflowController] _insertNodeAfter: 未找到insertAfterNode ${insertAfterNode.title}, 将直接添加到序列末尾`);
      // 如果找不到insertAfterNode，直接添加到序列末尾
      this.executionSequence.push(node);
      console.log(`[WorkflowController] _insertNodeAfter: 添加后序列: ${this.executionSequence.map(n => n.title).join(", ")}`);
      // 重新构建后续节点序列
      this._appendNextNodes(node);
      console.log(`[WorkflowController] _insertNodeAfter: _appendNextNodes后序列: ${this.executionSequence.map(n => n.title).join(", ")}`);
    }
  }

  _findStartNode() {
    const graph = this.getGraph?.();
    if (!graph) return null;

    const startNode = graph._nodes.find(node => node.type === "flow/start");
    if (!startNode) {
      window.showToast?.("未找到 Start 节点，请添加流程起点", "error");
      this.setState(STATE.IDLE);
      return null;
    }
    return startNode;
  }
}

export const STATE = {
  IDLE: "idle",
  RUNNING: "running",
  PAUSED: "paused",
  STOPPED: "stopped",
  FINISHED: "finished",
};