# 生成失败回放报告（补丁链覆盖率）

- 回放会话：14；其中可编译 10
- 补丁链把错误清空：**8**；部分减少：0；仍有残差：0
- 残差类别：无

| 会话 | last_event | 代码 | 编译 | 错误(前) | 错误(补丁后/常规) | 错误(补丁后/自由) | 残差类别 |
|---|---|---|---|---|---|---|---|
| 20260910_183959_generate | trial_end | code_generated.py | OK | 11 | 0 | 0 | - |
| 20260910_160020_generate | revise_done | code_generated.py | OK | 0 | 0 | 0 | - |
| 20260910_155654_generate | generate_done | code_generated.py | OK | 8 | 0 | 0 | - |
| 20260910_154241_generate | generate_done | code_generated.py | OK | 11 | 0 | 0 | - |
| 20260910_153949_generate | generate_failed | code_failed.py | FAIL | 0 | - | - | - |
| 20260910_153658_generate | generate_done | code_generated.py | OK | 8 | 0 | 0 | - |
| 20260910_145339_generate | generate_done | code_generated.py | OK | 16 | 0 | 0 | - |
| 20260910_144001_generate | generate_failed | code_failed.py | FAIL | 0 | - | - | - |
| 20260906_161105_generate | generate_done | code_generated.py | OK | 3 | 0 | 0 | - |
| 20260906_145230_generate | generate_done | code_generated.py | OK | 0 | 0 | 0 | - |
| 20260906_142110_generate | generate_failed | code_failed.py | FAIL | 0 | - | - | - |
| 20260906_140307_generate | generate_failed | code_failed.py | FAIL | 0 | - | - | - |
| 20260906_134829_generate | generate_done | code_generated.py | OK | 7 | 0 | 0 | - |
| 20260906_033205_generate | generate_done | code_generated.py | OK | 7 | 0 | 0 | - |

> 口径：归档无 plan_struct，回放按 plan=None 校验；真实管线带 plan，
> 表名/键集类补丁（patch_task_table_contract 等）命中率更高。
