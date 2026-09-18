"""
脚本生成语料库
==============

- `few_shot/`  精品示例片段；**类 RAG**：首轮生成只注入目录卡（id/title/when），校验失败修码或修订评价困惑时再按错误检索全文
- `paradigm/`  与业务无关的最小结构范式（审阅说明 + 对应 few_shot）；默认不塞全文，fix/evaluate 命中 `run_task`/未知/过场 时再打开
- `templates`  生产级脚本 + assets 介绍；首轮只给 annotation 卡，全文同样按需检索
- `golden/`    回归用例：explanation + 期望指纹 / 参考手写脚本
- `sessions/`  生成 / 试跑 / 修订自动归档（见 sessions/README.md）
- `promoted/`  会话高频校验错自动升格的 prompt 规则（只进不出；`corpus_promote.py` / `analyze_gen_sessions --promote`）
- `../skills/` 生成后优化流程（伪录制 effective 循环；GUI「脚本优化」按钮，见 skills/README.md）
- `index.json` 索引

配置（`config.json` → `defaults`）：

- `few_shot_inject`: `rag`（默认）| `eager`（旧全文硬塞）
- `few_shot_retrieve_max`: 困惑时最多取几条全文（默认 2）

API：`format_corpus_catalog` / `retrieve_corpus_snippets` / `build_retrieve_block` / `build_few_shot_block`

离线回归（不调 LLM）::

    python -m backend.script_generator.regression

完整生成回归（需 API，较慢）::

    python -m backend.script_generator.regression --live
"""
