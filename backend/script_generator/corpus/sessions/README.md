# 脚本生成会话归档

每次「生成 / 试运行结束 / 根据反馈修订」会自动在此写入一份材料，便于事后分析生成质量、反复出现的问题。

## 目录结构（每次会话一个子文件夹）

```
20260819_213045_revise/
  meta.json                 # 提供商、模型、素材目录、账号等
  events.jsonl              # 时间线事件（追加）
  explanation.txt           # 当时的脚本描述
  code_generated.py         # 生成结果
  code_at_trial.py          # 试运行时的脚本副本
  code_pre_revise.py        # 修订前
  code_post_revise.py       # 修订后
  trial_log.txt             # 试运行日志
  feedback.txt              # 提交修订前的用户反馈
  feedback_writeback.txt    # 勾选写回介绍的约束
  revise_summary.txt        # 修订摘要 + 审查结论
  trajectory.json           # 生成轨迹步骤
  screenshot_trial_end.png  # 试跑结束时的游戏画面（若账号已连接）
  screenshot_stop.png       # 用户点「停止」时优先缓存的停帧（修订可识图）
  screenshot_pre_revise.png # 点「根据反馈修订」前的画面缓存
  diagnosis.json            # 修订前诊断（含是否识停帧 / 停帧描述）
  validation_*.txt        # 本地校验未通过项（若有）
```

## 用途

- 复盘「审查未覆盖 / 幼稚错误」时对照代码与日志
- 挑选典型失败案例加入 `corpus/golden/` 做回归
- 对比同一反馈多轮修订是否真改到了

默认不提交 git；需要共享时可手动复制单个会话文件夹。
