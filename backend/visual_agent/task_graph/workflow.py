"""Thin LangGraph control plane over the persistent dynamic task graph.

LangGraph topology is stable.  User-created tasks are data in VisualTaskStore,
so inserting a task never requires recompiling or migrating graph topology.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .store import VisualTaskStore


class TaskWorkflowState(TypedDict, total=False):
    run_id: str
    graph_node: str
    route: str
    active_task_id: str
    active_plan_id: str
    tree_revision: int
    inbox_processed: int
    terminal_status: str
    reason: str


def build_task_workflow(store: VisualTaskStore):
    def commit(node_name: str, state: TaskWorkflowState,
               update: TaskWorkflowState) -> TaskWorkflowState:
        value: TaskWorkflowState = {**state, **update, "graph_node": node_name}
        store.save_checkpoint(state["run_id"], node_name, dict(value))
        return update

    def load_session(state: TaskWorkflowState) -> TaskWorkflowState:
        run = store.get_run(state["run_id"])
        checkpoint = store.get_checkpoint(run.id)
        update: TaskWorkflowState = {"tree_revision": run.tree_revision}
        # Resume control references, never task facts: SQLite task tables remain
        # the sole source of truth for the dynamic goal graph.
        if checkpoint:
            saved = checkpoint.get("state") or {}
            update.setdefault("active_task_id", str(saved.get("active_task_id") or ""))
            update.setdefault("active_plan_id", str(saved.get("active_plan_id") or ""))
        return commit("load_session", state, update)

    def drain_task_inbox(state: TaskWorkflowState) -> TaskWorkflowState:
        affected = store.drain_inbox(state["run_id"])
        run = store.get_run(state["run_id"])
        return commit("drain_task_inbox", state, {
            "inbox_processed": len(affected), "tree_revision": run.tree_revision,
            "active_plan_id": "" if affected else str(state.get("active_plan_id") or ""),
        })

    def reconcile_task_graph(state: TaskWorkflowState) -> TaskWorkflowState:
        status = store.recompute(state["run_id"])
        run = store.get_run(state["run_id"])
        return commit("reconcile_task_graph", state, {
            "tree_revision": run.tree_revision, "terminal_status": status,
        })

    def schedule_next_node(state: TaskWorkflowState) -> TaskWorkflowState:
        run = store.get_run(state["run_id"])
        root = store.get_node(run.root_node_id)
        candidates = store.ready_nodes(run.id)
        if root.status == "succeeded":
            route, active, reason = "final", "", "all required top-level tasks succeeded"
        elif candidates and candidates[0].status == "unexpanded":
            route, active, reason = "expand", candidates[0].id, "task requires discovery"
        elif candidates:
            route, active, reason = "plan", candidates[0].id, "ready task selected"
        else:
            route, active, reason = "blocked", "", "no ready task"
        return commit("schedule_next_node", state, {
            "route": route, "active_task_id": active, "reason": reason,
            "tree_revision": run.tree_revision,
        })

    def expand_task(state: TaskWorkflowState) -> TaskWorkflowState:
        # Actual LLM/page discovery is deliberately a later integration.  This
        # checkpoint makes the missing work explicit and resumable today.
        return commit("expand_task", state, {
            "terminal_status": "needs_expansion",
            "reason": "active task must be expanded from authoritative observations",
        })

    def build_short_plan(state: TaskWorkflowState) -> TaskWorkflowState:
        task_id = str(state.get("active_task_id") or "")
        plan_id = store.create_plan(state["run_id"], {
            "steps": [{"task_id": task_id, "action": "resolve_task_capability"}],
            "reconcile_after": True,
        })
        return commit("build_short_plan", state, {
            "active_plan_id": plan_id, "terminal_status": "plan_ready",
            "reason": "short plan created for the selected task",
        })

    def resolve_blockers(state: TaskWorkflowState) -> TaskWorkflowState:
        return commit("resolve_blockers", state, {
            "terminal_status": "blocked", "reason": "no executable or expandable task",
        })

    def final_consistency_check(state: TaskWorkflowState) -> TaskWorkflowState:
        before = store.get_run(state["run_id"]).tree_revision
        affected = store.drain_inbox(state["run_id"])
        store.recompute(state["run_id"])
        run = store.get_run(state["run_id"])
        root = store.get_node(run.root_node_id)
        stable = not affected and store.pending_inbox_count(run.id) == 0 and run.tree_revision == before
        if stable and root.status == "succeeded":
            update: TaskWorkflowState = {
                "route": "done", "terminal_status": "succeeded",
                "tree_revision": run.tree_revision, "reason": "final revision and inbox are stable",
            }
        else:
            update = {
                "route": "continue", "terminal_status": "pending",
                "tree_revision": run.tree_revision, "active_plan_id": "",
                "reason": "task graph changed during finalization",
            }
        return commit("final_consistency_check", state, update)

    graph = StateGraph(TaskWorkflowState)
    graph.add_node("load_session", load_session)
    graph.add_node("drain_task_inbox", drain_task_inbox)
    graph.add_node("reconcile_task_graph", reconcile_task_graph)
    graph.add_node("schedule_next_node", schedule_next_node)
    graph.add_node("expand_task", expand_task)
    graph.add_node("build_short_plan", build_short_plan)
    graph.add_node("resolve_blockers", resolve_blockers)
    graph.add_node("final_consistency_check", final_consistency_check)

    graph.add_edge(START, "load_session")
    graph.add_edge("load_session", "drain_task_inbox")
    graph.add_edge("drain_task_inbox", "reconcile_task_graph")
    graph.add_edge("reconcile_task_graph", "schedule_next_node")
    graph.add_conditional_edges(
        "schedule_next_node", lambda state: state["route"],
        {"expand": "expand_task", "plan": "build_short_plan",
         "blocked": "resolve_blockers", "final": "final_consistency_check"},
    )
    graph.add_edge("expand_task", END)
    graph.add_edge("build_short_plan", END)
    graph.add_edge("resolve_blockers", END)
    graph.add_conditional_edges(
        "final_consistency_check", lambda state: state["route"],
        {"done": END, "continue": "drain_task_inbox"},
    )
    return graph.compile()
