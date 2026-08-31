"""
脚本生成语料库
==============

- `few_shot/`  精品示例片段，生成时按标签检索注入 prompt（类 RAG）
- `paradigm/`  与业务无关的最小结构范式（审阅说明 + 对应 few_shot）
- `templates`  生产级 v2 脚本 + assets 介绍；每条带 annotation（summary / when_to_use / copy / states / images / keywords），检索和注入 prompt 都会用
- `golden/`    回归用例：explanation + 期望指纹 / 参考手写脚本
- `sessions/`  生成 / 试跑 / 修订自动归档（见 sessions/README.md）
- `index.json` 索引

离线回归（不调 LLM）::

    python -m backend.script_generator.regression

完整生成回归（需 API，较慢）::

    python -m backend.script_generator.regression --live
"""
