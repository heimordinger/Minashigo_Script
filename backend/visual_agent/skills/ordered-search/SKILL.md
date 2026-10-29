---
name: ordered-search
description: Enumerate a selector by advancing one direction until a seen candidate comes back, then deliver the extremum from that complete set. Stop on repeat, not on a name.
---

# 极值搜索：同向推进，回到已见项就停

## 何时适用
任务要从一组候选里取「最」（最高/最低/最新/最末/最便宜/性价比最高…），
而候选一屏放不下，需要左右切换、翻页或滚动才能看全。
不适用：全部候选已同屏可见且不超过 3 个；或任务不涉及极值。不要为了用而用。

## 主路径
1. 选一个方向（右箭头 / 下一页 / 向下滚），从当前这一项开始推进。
2. 每推一步只做一件事：报出这一项的名字（observed_option）。
3. **看到已经见过的项就停**。这就是闭环，集合已完整，不要再点第四次第五次。
4. 在完整集合上比较，选目标项，交付。

## 兜底：这个方向点不动
推进后画面没变化、箭头变灰或消失（连续两次）→ 这一侧到底了。
记下端点，反向走到另一侧端点；两侧端点都拿到才算完整（stop=unavailable）。
点不动 ≠ 看全了：别把「点不动」当闭环。

## 不要做的
- 不要因为名字像最高（Heaven / 極 / Ultra / Final）就停。名字不是依据，**回到已见项**才是。
- 不要在已见项之间来回点：来回点本身就是闭环信号，此时该交付，不该继续枚举。
- 不要假设「还有第 7 项」。账本里有几项就是几项；真觉得漏了，就点过去把它的名字读出来。
- 读不到的数值填 null，不得填 0、不得估数、不得凭记忆补。
- 不得用询问用户代替搜索；除非搜索本身触犯禁止清单。

## 报告（交付时必填，运行时按它核对）
search = {
  method,        // 右箭头 / 左箭头 / 下一页 …
  start_item,    // 这轮从哪一项开始
  visited[],     // 实际见过的项，按见到顺序
  stop,          // "wrap"（回到已见项）| "unavailable"（两侧端点）| "single"
  stop_evidence, // 一句话：在画面上看到什么
  pager_total    // 画面上能读到的总数；读不到填 null
}
