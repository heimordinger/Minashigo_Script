# scripts/converter/renderer.py
"""IR -> LiteGraph JSON 渲染器。

职责：
1. 将 ScriptIR 转换为 LiteGraph JSON 格式
2. 生成节点坐标（占位布局 -> 后续可替换为 dagre）
3. 生成节点间的连线
"""

from __future__ import annotations

from typing import Optional

from .ir import (
    ActionNode, FsmBody, GuardRef, Handler, Loop, ScriptIR, SetVariable,
)


# -- IR 类型 -> LiteGraph 节点类型 --

_TYPE_MAP = {
    "click":            "action/click",
    "click_image":      "action/click_image",
    "click_text":       "action/click_text",
    "click_until_gone": "action/click_until_gone",
    "match_image":      "action/match_image",
    "wait_image":       "action/match_image",
    "scroll":           "action/scroll",
    "scroll_to_bottom": "action/scroll",
    "scroll_to_top":    "action/scroll",
    "dmm_login":        "action/dmm_login",
    "url_goto":         "action/url_goto",
    "b_sleep":          "flow/sleep",
}

_TYPE_TITLES = {
    "click":            "点击坐标",
    "click_image":      "点击图片",
    "click_text":       "点击文本",
    "click_until_gone": "点到消失",
    "match_image":      "匹配图片",
    "wait_image":       "等待图片",
    "scroll":           "滚动",
    "scroll_to_bottom": "滚动到底部",
    "scroll_to_top":    "滚动到顶部",
    "dmm_login":        "DMM登录",
    "url_goto":         "跳转网址",
    "b_sleep":          "等待",
}


# -- 布局常量 --

START_POS = (50, 50)
NODE_SPACING_Y = 80
NODE_BASE_W = 210
NODE_BASE_H = 80


class LiteGraphRenderer:
    """将 ScriptIR 渲染为 LiteGraph JSON。"""

    def __init__(self, ir: ScriptIR):
        self.ir = ir
        self._node_id: int = 1
        self._link_id: int = 1
        self._nodes: list[dict] = []
        self._links: list[list] = []
        self._node_refs: dict[str, int] = {}

    def render(self) -> dict:
        """主入口：IR -> LiteGraph JSON。"""
        self._nodes.clear()
        self._links.clear()
        self._node_id = 1
        self._link_id = 1
        self._node_refs.clear()

        start_id = self._add_start_node()
        prev_id = start_id
        prev_slot = 0

        for item in self.ir.body:
            if isinstance(item, ActionNode):
                prev_id = self._add_action_node(item, prev_id, prev_slot)
                prev_slot = 0
            elif isinstance(item, Loop):
                _, prev_id = self._render_loop(item, prev_id, prev_slot)
                prev_slot = 0
            elif isinstance(item, GuardRef):
                prev_id = self._render_guard(item, prev_id, prev_slot)
                prev_slot = 0
            elif isinstance(item, FsmBody):
                self._render_fsm(item, prev_id, prev_slot)
                # FSM 自己管理生命周期（handler 内 _exit → end，其余 goto 回头）
                # render() 不再追加 end 节点
                prev_id = None
                break
            elif isinstance(item, SetVariable):
                prev_id = self._add_variable_node(item, prev_id, prev_slot)
                prev_slot = 1  # 变量节点输出 1 = 当前值

        if prev_id is not None:
            self._add_end_node(prev_id, prev_slot)
        return self._build_graph()

    # -- 节点创建 --

    def _add_start_node(self) -> int:
        nid = self._next_node_id()
        self._nodes.append({
            "id": nid,
            "type": "flow/start",
            "pos": list(START_POS),
            "size": {"0": 140, "1": 26},
            "flags": {},
            "order": 0,
            "mode": 0,
            "inputs": [],
            "outputs": [
                {"name": "next", "type": -1, "links": [], "slot_index": 0}
            ],
            "title": chr(36215)+chr(28857),
            "properties": {},
        })
        return nid

    def _add_end_node(self, from_id: int, from_slot: int = 0) -> int:
        nid = self._next_node_id()
        link_id = self._next_link_id()
        self._nodes.append({
            "id": nid,
            "type": "flow/end",
            "pos": [0, 0],
            "size": {"0": 140, "1": 26},
            "flags": {},
            "order": 0,
            "mode": 0,
            "inputs": [
                {"name": chr(32467)+chr(26463), "type": -1, "link": link_id}
            ],
            "outputs": [],
            "title": chr(32467)+chr(26463),
            "properties": {},
        })
        self._links.append([link_id, from_id, from_slot, nid, 0, -1])
        return nid

    def _add_action_node(
        self, action: ActionNode, from_id: int, from_slot: int,
        pos: Optional[tuple[int, int]] = None,
    ) -> int:
        nid = self._next_node_id()
        link_id = self._next_link_id()
        lite_type = _TYPE_MAP.get(action.type, action.type)
        title = action.properties.get("title", _TYPE_TITLES.get(action.type, action.type))

        node = {
            "id": nid,
            "type": lite_type,
            "pos": [pos[0] if pos else 0, pos[1] if pos else 0],
            "size": {"0": NODE_BASE_W, "1": NODE_BASE_H},
            "flags": {},
            "order": 0,
            "mode": 0,
            "inputs": [{"name": chr(35302)+chr(21457), "type": -1, "link": link_id}],
            "outputs": [
                {"name": chr(19979)+chr(19968)+chr(27493), "type": -1, "links": [], "slot_index": 0},
                {"name": chr(38169)+chr(35823), "type": -1, "links": [], "slot_index": 1},
            ],
            "properties": {},
            "title": title,
        }

        for k, v in action.properties.items():
            if k in ("title",):
                continue
            node["properties"][k] = v

        if action.type in ("click_image", "match_image", "click_text"):
            node["outputs"].append(
                {"name": chr(25104)+chr(21151), "type": -1, "links": [], "slot_index": 2}
            )

        self._nodes.append(node)
        self._links.append([link_id, from_id, from_slot, nid, 0, -1])
        return nid

    def _render_loop(self, loop: Loop, from_id: int, from_slot: int) -> tuple[int, int]:
        label_name = f"loop_{self._node_id}"
        label_id = self._add_label_node(label_name, from_id, from_slot)
        prev_id = label_id
        prev_slot = 0

        for act in loop.header_actions:
            if isinstance(act, ActionNode):
                prev_id = self._add_action_node(act, prev_id, prev_slot)
                prev_slot = 0

        for item in loop.body:
            if isinstance(item, ActionNode):
                prev_id = self._add_action_node(item, prev_id, prev_slot)
                prev_slot = 0

        goto_id = self._add_goto_node(label_name, prev_id, prev_slot)
        return label_id, goto_id

    def _render_guard(self, guard: GuardRef, from_id: int, from_slot: int) -> int:
        nid = self._next_node_id()
        link_id = self._next_link_id()
        self._nodes.append({
            "id": nid,
            "type": "flow/sleep",
            "pos": [0, 0],
            "size": {"0": NODE_BASE_W, "1": NODE_BASE_H},
            "flags": {},
            "order": 0,
            "mode": 0,
            "inputs": [{"name": chr(35302)+chr(21457), "type": -1, "link": link_id}],
            "outputs": [
                {"name": chr(19979)+chr(19968)+chr(27493), "type": -1, "links": [], "slot_index": 0},
            ],
            "properties": {
                "seconds": 0,
                "_guard_images": guard.images,
                "_prefab_source": "guard_v1",
            },
            "title": "Guard"+chr(26657)+chr(39564),
        })
        self._links.append([link_id, from_id, from_slot, nid, 0, -1])
        return nid

    def _add_label_node(self, name: str, from_id: int, from_slot: int) -> int:
        nid = self._next_node_id()
        link_id = self._next_link_id()
        self._nodes.append({
            "id": nid,
            "type": "flow/label",
            "pos": [0, 0],
            "size": [200, 80],
            "flags": {},
            "order": 0,
            "mode": 0,
            "inputs": [{"name": chr(35302)+chr(21457), "type": -1, "link": link_id}],
            "outputs": [
                {"name": chr(26631)+chr(35760), "type": -1, "links": [], "slot_index": 0},
            ],
            "properties": {"label": name},
            "title": chr(24490)+chr(29615)+": "+name,
        })
        self._links.append([link_id, from_id, from_slot, nid, 0, -1])
        self._node_refs[name] = nid
        return nid

    def _add_goto_node(self, target: str, from_id: int, from_slot: int) -> int:
        nid = self._next_node_id()
        link_id = self._next_link_id()
        self._nodes.append({
            "id": nid,
            "type": "flow/goto",
            "pos": [0, 0],
            "size": {"0": 210, "1": 58},
            "flags": {},
            "order": 0,
            "mode": 0,
            "inputs": [{"name": chr(35302)+chr(21457), "type": -1, "link": link_id}],
            "outputs": [],
            "properties": {"target": target},
            "title": chr(36339)+chr(36716),
        })
        self._links.append([link_id, from_id, from_slot, nid, 0, -1])
        return nid

    # -- FSM 渲染 --

    def _render_fsm(self, fsm: FsmBody, from_id: int, from_slot: int) -> int:
        """FSM -> 循环 + 状态变量 + state_dispatch + handler 子图 + goto 回头。"""
        LOOP_LABEL = f"fsm_loop_{self._node_id}"

        # 1. label（循环头）
        label_id = self._add_label_node(LOOP_LABEL, from_id, from_slot)

        # 2. 变量声明：current_state，初始值 = initial_state
        var_id = self._add_variable_node(
            SetVariable(name=fsm.dispatch_var, value=fsm.initial_state),
            label_id, 0,
        )

        # 3. state_dispatch：读取变量值，触发对应分支
        dispatch_id = self._add_state_dispatch_node(fsm, var_id, 1)

        # 4. 每个 handler → 子图 + 状态转移控制
        for i, handler in enumerate(fsm.handlers):
            prev_id = dispatch_id
            prev_slot = i

            for node in handler.body:
                if not isinstance(node, ActionNode):
                    continue
                if node.type == "_exit":
                    self._add_end_node(prev_id, prev_slot)
                    break  # 终止符，后续节点不连接
                elif node.type == "_goto_state":
                    target = node.properties.get("target", "")
                    var_id = self._add_variable_node(
                        SetVariable(name=fsm.dispatch_var, value=target),
                        prev_id, prev_slot,
                    )
                    self._add_goto_node(LOOP_LABEL, var_id, 1)
                    break  # goto 是终止符
                elif node.type == "_keep_state":
                    self._add_goto_node(LOOP_LABEL, prev_id, prev_slot)
                    break  # goto 是终止符
                else:
                    prev_id = self._add_action_node(node, prev_id, prev_slot)
                    prev_slot = 0

        return dispatch_id

    def _add_state_dispatch_node(self, fsm: FsmBody, from_id: int, from_slot: int) -> int:
        """添加 flow/state_dispatch 节点。"""
        nid = self._next_node_id()
        link_id = self._next_link_id()
        states_str = ",".join(h.name for h in fsm.handlers)
        total_slots = len(fsm.handlers) + 2  # N state slots + 未匹配 + 错误

        outputs = []
        for i, h in enumerate(fsm.handlers):
            outputs.append({"name": h.name, "type": -1, "links": [], "slot_index": i})
        outputs.append({"name": "未匹配", "type": -1, "links": [], "slot_index": len(fsm.handlers)})
        outputs.append({"name": "错误", "type": -1, "links": [], "slot_index": len(fsm.handlers) + 1})

        self._nodes.append({
            "id": nid,
            "type": "flow/state_dispatch",
            "pos": [0, 0],
            "size": {"0": max(220, 160 + total_slots * 8), "1": max(80, 50 + total_slots * 24)},
            "flags": {},
            "order": 0,
            "mode": 0,
            "inputs": [
                {"name": "触发", "type": -1, "link": link_id},
                {"name": "当前状态", "type": "string", "link": None},
            ],
            "outputs": outputs,
            "title": "状态派发",
            "properties": {"states": states_str},
        })
        self._links.append([link_id, from_id, from_slot, nid, 0, -1])
        return nid

    def _add_variable_node(self, var: SetVariable, from_id: int, from_slot: int) -> int:
        """添加 flow/variable 节点（声明并设置变量）。"""
        nid = self._next_node_id()
        link_id = self._next_link_id()

        self._nodes.append({
            "id": nid,
            "type": "flow/variable",
            "pos": [0, 0],
            "size": {"0": 240, "1": 140},
            "flags": {},
            "order": 0,
            "mode": 0,
            "inputs": [
                {"name": "触发", "type": -1, "link": link_id},
            ],
            "outputs": [
                {"name": "下一步", "type": -1, "links": [], "slot_index": 0},
                {"name": "当前值", "type": "", "links": [], "slot_index": 1},
            ],
            "title": "声明变量",
            "properties": {
                "name": var.name,
                "type": var.type_hint,
                "default": var.value,
            },
        })
        self._links.append([link_id, from_id, from_slot, nid, 0, -1])
        return nid

    # -- 图构建 --

    def _build_graph(self) -> dict:
        self._apply_layout()
        return {
            "last_node_id": self._node_id - 1,
            "last_link_id": self._link_id - 1,
            "nodes": self._nodes,
            "links": self._links,
            "groups": [],
            "config": {},
            "extra": {
                "converted_from": self.ir.source_path,
                "entry_point": self.ir.entry_point,
            },
            "version": 0.4,
        }

    def _apply_layout(self):
        x = START_POS[0]
        y = START_POS[1]
        for node in self._nodes:
            node["pos"][0] = x
            node["pos"][1] = y
            sz = node.get("size", NODE_BASE_H)
            h = (sz.get("1", NODE_BASE_H) if isinstance(sz, dict)
                 else (sz[1] if isinstance(sz, list) else NODE_BASE_H))
            y += h + NODE_SPACING_Y

    # -- ID 生成 --

    def _next_node_id(self) -> int:
        nid = self._node_id
        self._node_id += 1
        return nid

    def _next_link_id(self) -> int:
        lid = self._link_id
        self._link_id += 1
        return lid
