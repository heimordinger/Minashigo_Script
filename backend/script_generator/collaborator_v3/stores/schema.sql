PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    status TEXT NOT NULL,
    current_step_id TEXT,
    plan_revision INTEGER NOT NULL CHECK(plan_revision >= 1),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS resources (
    id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    display_name TEXT NOT NULL,
    content_path TEXT,
    content_hash TEXT,
    source_kind TEXT NOT NULL,
    source_ref TEXT,
    metadata_json TEXT NOT NULL,
    reusable_scope TEXT NOT NULL,
    task_id TEXT,
    created_in_turn TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    PRIMARY KEY(id, version),
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS resource_parents (
    resource_id TEXT NOT NULL,
    resource_version INTEGER NOT NULL,
    parent_id TEXT NOT NULL,
    parent_version INTEGER NOT NULL,
    PRIMARY KEY(resource_id, resource_version, parent_id, parent_version),
    FOREIGN KEY(resource_id, resource_version) REFERENCES resources(id, version),
    FOREIGN KEY(parent_id, parent_version) REFERENCES resources(id, version)
);

CREATE TABLE IF NOT EXISTS domain_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    task_id TEXT,
    turn_id TEXT,
    aggregate_id TEXT,
    -- 层级 ID（设计 §14.3）：老记录这三列可以为 NULL（legacy），
    -- 缺字段的行不得被当成新式可信凭证使用；代码里不做任何伪造补齐。
    run_id TEXT,
    subturn_id TEXT,
    action_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS main_turns (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    user_event_id TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    active_snapshot_id TEXT NOT NULL,
    checkpoint_id TEXT,
    commit_id TEXT,
    model_id TEXT,
    model_version TEXT,
    -- 最近一次执行的 run_id（同一主轮恢复时会被新的 run_id 覆盖，设计 §14.2）。
    run_id TEXT,
    -- 任务定向（设计 §3，migration 7）：任务类型是**主轮状态**，不属 workspace。
    -- intent_json 存 TurnIntent（task_type/goal/quotes/revision/source 等），
    -- 唯一的意图事实源；缺列/空值 = 本轮还没通过定向闸（不得当成"默认脚本"）。
    task_type TEXT,
    intent_json TEXT NOT NULL DEFAULT '{}',
    intent_revision INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

-- 副轮身份表：层级里 Subturn 这一层（设计 §14.1）。
-- 位置：domain_events 只记"发生了什么"，副轮自身的身份/序号/所属 run 记在这里，
-- 这样 (main_turn_id, seq) 的唯一性由数据库保证，而不是靠调用方自觉。
-- 一次副轮会写多条事件，所以事件表上的 subturn_id 只是索引、不能加唯一约束。
CREATE TABLE IF NOT EXISTS subturns (
    subturn_id TEXT PRIMARY KEY,
    main_turn_id TEXT NOT NULL,
    run_id TEXT,
    --: 整个主轮内单调递增的序号（跨 resume 不归零）。
    seq INTEGER NOT NULL,
    --: 本 run 内的序号（仅用于展示进度，可随 resume 重置）。
    index_in_run INTEGER,
    action_type TEXT,
    action_id TEXT,
    outcome TEXT,
    -- Runtime 自动生成的副轮决策留痕；旧记录保持空对象。
    trace_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    FOREIGN KEY(main_turn_id) REFERENCES main_turns(id)
);

CREATE TABLE IF NOT EXISTS viewport_snapshots (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    base_id TEXT,
    base_version INTEGER,
    overlays_json TEXT NOT NULL,
    zoom REAL NOT NULL,
    pan_x REAL NOT NULL,
    pan_y REAL NOT NULL,
    selections_json TEXT NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    question TEXT NOT NULL,
    reason TEXT NOT NULL,
    options_json TEXT NOT NULL,
    allow_free_text INTEGER NOT NULL,
    blocking INTEGER NOT NULL,
    resume_token_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY(turn_id) REFERENCES main_turns(id)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    task_id TEXT,
    phase TEXT NOT NULL,
    viewport_snapshot_id TEXT NOT NULL,
    completed_steps_json TEXT NOT NULL,
    acquired_refs_json TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    runtime_json TEXT NOT NULL DEFAULT '{}',
    unresolved_interaction_id TEXT,
    budget_json TEXT NOT NULL,
    resume_phase TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(turn_id) REFERENCES main_turns(id),
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(unresolved_interaction_id) REFERENCES interactions(id)
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    subject_id TEXT,
    subject_version INTEGER,
    payload_json TEXT NOT NULL,
    producer_capability_id TEXT,
    producer_version TEXT,
    confidence REAL,
    status TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    -- 视觉证据指纹（migration 8，设计 §5.5）：可空 —— 老行没有就是 NULL，不伪造。
    -- 跨主轮按 (kind, fingerprint, status) 查（见 ix_evidence_fingerprint）。
    fingerprint TEXT
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    subject_id TEXT,
    subject_version INTEGER,
    predicate TEXT NOT NULL,
    value_json TEXT NOT NULL,
    confidence REAL,
    status TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_in_turn TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    claim_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    PRIMARY KEY(claim_id, evidence_id),
    FOREIGN KEY(claim_id) REFERENCES claims(id),
    FOREIGN KEY(evidence_id) REFERENCES evidence(id)
);

CREATE TABLE IF NOT EXISTS capabilities (
    id TEXT NOT NULL,
    version TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    input_schema_json TEXT NOT NULL,
    output_schema_json TEXT NOT NULL,
    permission_actions_json TEXT NOT NULL,
    cost_class TEXT NOT NULL,
    availability TEXT NOT NULL,
    scope TEXT NOT NULL,
    task_id TEXT,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY(id, version),
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS permission_grants (
    id TEXT PRIMARY KEY,
    capability_id TEXT NOT NULL,
    capability_version TEXT,
    actions_json TEXT NOT NULL,
    task_id TEXT NOT NULL,
    resource_refs_json TEXT NOT NULL,
    stage_id TEXT,
    batch_id TEXT,
    expires_kind TEXT NOT NULL,
    expires_value TEXT,
    environment_fingerprint TEXT NOT NULL,
    target_fingerprint TEXT NOT NULL,
    revision INTEGER NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS commits (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    task_id TEXT,
    turn_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_facts (
    task_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_turn_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(task_id, key),
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

-- 契约凭据（migration 6，设计 §6/§11）：ticket 是**跨副轮**查询/失效/审计的结构化事实，
-- 所以进独立表而不是文件。它是签发时刻的契约快照 + contracts_revision 绑定；
-- 版本一变就失效（不维护自己的方法表，事实源仍是 api_contracts registry）。
-- 时间线事件只引用 ticket_id（见 domain_events），不在这里复制事件。
CREATE TABLE IF NOT EXISTS contract_tickets (
    ticket_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    main_turn_id TEXT,
    view_event_id TEXT,
    contract_revision TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    receiver_type TEXT NOT NULL,
    requested_method TEXT NOT NULL,
    resolved_owner TEXT NOT NULL,
    resolution_chain_json TEXT NOT NULL,
    runtime_profile TEXT NOT NULL,
    profile_source TEXT NOT NULL,
    call_kind TEXT NOT NULL,
    parameters_required_json TEXT NOT NULL,
    parameters_allowed_json TEXT NOT NULL,
    return_model_json TEXT NOT NULL,
    -- 权限**只存引用与作用域摘要**：事实本体在 permission_grants（设计 §12，不合并）。
    permission_id TEXT NOT NULL DEFAULT '',
    permission_scope_json TEXT NOT NULL DEFAULT '[]',
    resource_scope_json TEXT NOT NULL DEFAULT '[]',
    environment_revision TEXT NOT NULL DEFAULT '',
    issued_at TEXT NOT NULL,
    integrity TEXT NOT NULL,
    revoked_at TEXT,
    revoked_reason TEXT
);

CREATE INDEX IF NOT EXISTS ix_resources_task_status
    ON resources(task_id, status, type);
CREATE INDEX IF NOT EXISTS ix_events_task_sequence
    ON domain_events(task_id, sequence);
-- 注意：domain_events.run_id / subturn_id 是 migration 5 给老库补的列，
-- 所以引用它们的索引不能在 executescript 里建（老库此刻还没有这些列），
-- 由 SqliteDomainStore.initialize() 在 ALTER 之后补建。
-- 副轮身份的唯一约束：同一主轮内序号不得重复（resume 后还要继续递增）。
CREATE UNIQUE INDEX IF NOT EXISTS ux_subturns_turn_seq
    ON subturns(main_turn_id, seq);
CREATE INDEX IF NOT EXISTS ix_subturns_run
    ON subturns(run_id, seq);
-- 凭据索引：跨副轮/跨主轮查"这一轮签过哪些票"、"这个方法签过哪些票"。
CREATE INDEX IF NOT EXISTS ix_tickets_turn
    ON contract_tickets(main_turn_id, issued_at);
CREATE INDEX IF NOT EXISTS ix_tickets_capability
    ON contract_tickets(capability_id, runtime_profile);
