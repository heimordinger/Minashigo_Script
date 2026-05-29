export function reportNodeEvent(node, event, payload = {}) {
  const backend = window.taskflow?.backend;
  if (!backend) return;

  // Fire-and-forget: 不 await，避免日志上报阻塞任务执行
  backend.invoke("node_event", {
    node_id: node?.id ?? null,
    node_type: node?.type ?? null,
    node_title: node?.title ?? node?.constructor?.title ?? "unknown",
    event,
    payload,
  }).catch(() => {});
}
