# `click_until_gone` 只读专项核对与建议（Phase 2.1-2）

- **性质**：只读核对 + 建议。**本阶段不拍板**（人工决定 一.2）
- **未做**：没有批准它、没有改契约、没有改历史脚本
- **证据**：`tools\_scratch\_click_until_gone_audit.py`（可复现）→ `tools\_scratch\_click_until_gone_audit.md`

---

## 1. 契约现状

| 项 | desktop_user_window | browser_user_browser |
| --- | --- | --- |
| 是否登记 | 是（`desktop_user_window.click_until_gone`） | **完全未登记** |
| `approved` | **False** | — |
| `proxied` | False | — |
| 登记签名 | `browser.click_until_gone()`（**参数表为空**） | — |
| 登记返回 | `bool`，`truthiness` 为空 | — |
| 语义卡 | "需要时请先在 registry 登记并补语义卡，不要绕过契约调用。" | — |
| tags | `click` | — |

结论：它现在是一张**占位拒绝卡**（参数、真假语义、副作用、错误都没登记），不是"已批准但被限制"的能力。

## 2. 真实签名与实现（两端都存在）

两端同名同签名：`async def click_until_gone(self, img_path, timeout=10)` → **协程方法**。

```python
# desktop: backend/automation/user_window.py（owner=UserWindow）
async def click_until_gone(self, img_path, timeout=10):
    """点击直到图片消失。"""
    deadline = datetime.now() + timedelta(seconds=timeout)
    while datetime.now() < deadline:
        await self._check()                       # ← 任务取消检查
        await self._observer.ensure_frame(force=True)
        if await self.click_image(img_path=img_path):
            continue
        else:
            return True
    return False

# browser: backend/browser/user_browser.py（owner=UserBrowser）
async def click_until_gone(self, img_path, timeout=10):
    deadline = datetime.now() + timedelta(seconds=timeout)
    while datetime.now() < deadline:
        await self._task_ctrl.check()             # ← 任务取消检查
        await self._observer.ensure_frame(force=True)
        if await self.click_image(img_path=img_path):
            continue
        else:
            return True
    return False
```

逐项核对（人工决定 一.2 要求的八项）：

| 核对项 | 结论 |
| --- | --- |
| desktop / browser 真实签名 | 两端一致：`(img_path, timeout=10)`，async |
| 返回值语义 | `bool`：图消失 → `True`；超时 → `False`。**没有**真假语义卡（`truthiness` 空） |
| 是否都经过任务取消检查 | **两端都经过**（desktop `self._check()`；browser `self._task_ctrl.check()`），每轮循环开头各一次 |
| timeout 边界 | `timeout=10` 秒，墙钟 `deadline`；超时返回 `False`；**没有下限/上限校验**（`timeout=0` → 立即返回 `False`；负数同样立即失败），循环体内**没有 sleep** |
| 内部调用的 `click_image` 语义 | 每轮 `click_image(img_path=img_path)`，用默认 `expect='none'`（不确认转场）→ 命中即点击并 `continue`，未命中即 `return True`。**每轮都是一次真实点击**，且每轮都 `ensure_frame(force=True)` 强制抓帧 |
| 是否属于 portable 交集 | **不属于**（未批准侧被安全交集排除） |
| 是否存在等价批准能力 | 有，但**不等价**：`click_image(expect='gone')` 是"点一次 + 等转场确认"；`click_until_gone` 是"反复点，直到图消失"（副作用可放大 N 次点击） |
| 历史调用的 profile 与用途 | 唯一调用点 `scripts\孤儿\孤儿推本.py:151`，脚本注解 profile = **browser_user_browser**；用途：点"2_出击"直到它消失（`await browser.click_until_gone(ttk_path / "2_出击")`，随后 `return True`）。另有 2 个文件只是出现名字（`scripts\converter\*.py`，无调用点） |

## 3. 关键事实（决定建议的部分）

1. **唯一历史调用点落在 browser profile**，而 browser 侧**根本没有登记**这个方法（desktop 侧那张未批准卡在 browser 上不生效）⇒ 即使把 desktop 侧批准了，这个历史脚本依然对不上契约。
2. 它是**循环点击**语义：`click_image` 每轮都是真实点击，且每轮强制抓帧（`ensure_frame(force=True)`），无 sleep、无迭代上限；只有墙钟 deadline。审批"能力面"等于把"可能点很多次"的副作用纳入批准范围。
3. 现在这张卡**信息不完整**（参数为空、无 truthiness、无 side_effects、无 errors），批准它必须先把契约补全并让 `verify_contract` 通过漂移核验。
4. 存在**已批准的近似替代**（`click_image(expect='gone')`），且历史脚本只有 1 处，迁移成本极低。

## 4. 建议（**不是决定**，请人工拍板）

**建议：保留未批准，并迁移旧脚本**（不要本阶段顺手批准）。理由按权重：

1. 语义不是 `click_image` 的等价物（反复点击 vs 单次点击 + 等转场），批准它等于批准一种"可放大副作用"的循环行为，应该单独设计迭代上限/退避/证据要求；
2. 唯一历史调用点在 browser 侧，而 browser 侧未登记 ⇒ 批准 desktop 侧解决不了实际问题，两边都批准才需要同时补两套契约与语义卡；
3. 迁移成本极低（1 处调用点，可用 `click_image(expect='gone')` 或 `match_image` + `click` 循环重写），且不涉及改脚本行为以外的风险；
4. 契约本身不完整，批准前需要补参数表/真假语义/副作用/错误码 —— 这属于"重新登记"而不是"把 approved 改成 True"。

**如果将来决定批准**，最小完整登记清单（供参考，本阶段不做）：

- 参数：`req("img_path", "str | Path")` + `opt("timeout", "10", "float", doc=...)`，并注明"必须是正数、墙钟截止、无 sleep"；
- 返回：`ReturnContract(type="bool", truthiness="True = 图已消失；False = 到 deadline 仍在（没有异常）", identity_check_unsafe=True)`；
- 副作用：`("每次命中都会真实点击；未命中即判定消失", "每轮强制抓帧")`；
- 语义：与 `click_image(expect='gone')` 的区别（反复点击 vs 单次确认）；
- 两端各登记一条（desktop 侧把现有未批准卡升级，browser 侧新增）；
- 漂移核验：`verify_contract` 必须能按真实签名核验通过（现在参数表为空，无法核验）。

**反对"现在就批准"的理由总结**：它是**循环点击**语义、两端只有一端有卡、契约不完整、只有 1 个历史用户且有近似替代 —— 现在批准既不解决历史调用点，也把未设计的循环副作用纳入了能力面。
