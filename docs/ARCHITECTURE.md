# 架构与数据模型说明

面向另一位工程师：读完本文应能独立复现“调用导入 → 对账 → 分配 → 额度受理 →
争议/关账/更正”的完整过程，并知道每条业务规则落在哪个模块。

## 1. 模块总览

```
settlement/
├── money.py        金额：Decimal 折算、四舍五入留痕、最大余数法整数分摊
├── timeutil.py     UTC 时间解析、账期边界、as-of 可见性
├── store.py        事件日志 + 快照存储、文件锁、来源去重/冲突、事实修订
├── importer.py     契约字段校验、manifest 哈希校验、目录导入
├── costing.py      生效价目选择、账期汇率、逐尝试预估成本、请求链、趋势
├── ledger.py       发票生效行、尝试级对账、退款拆解、未分配池、团队预算台账
├── allocation.py   共享费三种分配口径的方案比较与确认
├── quota.py        合规模型筛选、成本区间、预留/等待/消耗/过期、预算重评估
├── disputes.py     发票核实、争议、关账守卫、关账后更正、差额单
├── reports.py      只读报表与不变量自检
└── cli.py / __main__.py   命令行入口
```

依赖方向是单向的：`cli → 业务模块 → store/costing/money`，业务模块之间只通过
`store` 的快照与事件协作，不互相持有可变状态。

## 2. 事件溯源模型

所有人工处置都是 `state/events.jsonl` 里的只追加事件，信封统一为
`{seq, at, type, payload}`。快照 `snapshot.json` 只是投影，`rebuild` 按 seq
重放即可还原。

| 事件类型 | 含义 | 关键负载 |
|---|---|---|
| `source.imported` | 来源文件导入（含行副本路径与 sha256） | file, sha256, rows |
| `fact.overridden` | 内部复核修订来源事实（原值保留） | entity, key, row, previous |
| `invoice.verified` | 未确认发票行人工核实 | line_id, revision, decision |
| `adjustment.posted` | 内部记账行（消耗配对、关账后更正） | line_id, kind, amount, origin_line |
| `allocation.confirmed` | 共享费分配口径确认（可被新确认取代） | basis, parts[], supersedes |
| `dispute.opened / dispute.resolved` | 争议开立与结论 | ref, verdict |
| `period.closed` | 账期关账 | period, warnings |
| `budget.increased` | 预算追加（触发等待重评估、差额自动结清） | team, delta_fen |
| `shortfall.opened / shortfall.resolved` | 财务差额单生命周期 | gap_fen |
| `reservation.created / waiting / cancelled / expired / consumed` | 额度预留全生命周期 | batch, model, reserved_fen |

## 3. 关键业务规则的落点

### 3.1 成功量按请求、费用按尝试

`costing.request_chains` 以 request 聚合尝试链；`costing.success_units` 对成功
请求去重计数；`reconcile_attempts` 对每个 `(request, attempt_id)` 独立核算，
失败尝试只要有 token 用量就产生预估成本。

### 3.2 生效价目与汇率

`costing.effective_price` 取 `start <= occurred` 的最新价目；汇率只按
`fx` 账期表，不使用实时汇率。每条预估保留 `price_id / currency / native_amount /
cny_rate / rounding_fen`，调价（P1→P2）与外币（USD 7.10）差额都可解释。

### 3.3 发票版本与状态机

`ledger.effective_invoice_lines` 合并来源发票（同 id 取最高 revision）与内部
调整行，并叠加三类状态：

* `verifications`：人工把未确认行提升为 confirmed，或驳回；
* `disputes`：open / accepted（反转，`rejected=true`）/ rejected（恢复记账）；
* 来源同键内容冲突：冲突行进不了事实表，只挂在 `conflicts` 待人工处理。

### 3.4 退款拆解（ledger.refund_breakdown）

对每条有效 refund 行：未确认 → 只记 `pending_refund_fen`；已确认 → 以尝试
预估成本为封顶，先抵消“已确认账单超出预估的部分”，剩余才进
`refund_credit_fen` 真正释放团队额度。悬空退款全额留存。

### 3.5 团队预算恒等式

```
预算（含追加）= 已承诺 + 持有预留 + 可用
已承诺 = 确认用量 + 未结敞口(max(预估-确认-退款补缺,0))
        + 确认退款(负) + 共享分摊
```

`reports.integrity_check` 对每个团队账期断言该恒等式；任何写操作后都可运行。

### 3.6 额度受理（quota.py）

* 能力过滤：`eligible_models` 只返回具备 `required_capability` 的模型，
  `reserve` 再次校验指定模型，杜绝“因为便宜换成不合适模型”。
* 保守预留：未指定模型时按最贵合规方案预留，给后续账单波动留缓冲。
* 超预算分支：可延迟 → `reservation.waiting`（重评估不重复造事件）；
  不可延迟 → 照常 `reservation.created` 并开 `shortfall.opened`。
* 消耗原子性：`reservation.consumed` 与 `adjustment.posted(ADJ-CONSUME-*)`
  在同一文件锁事务内先后落盘，自检会验证配对存在。

### 3.7 并发安全

`store.transaction` 用 `fcntl.flock` 互斥，并在**拿锁后重新读取磁盘快照**
（seq 较新则覆盖内存快照）。这保证后提交者基于先提交者的最新余额决策，
从根上避免两个进程双重占用同一余额。测试见 `tests/test_concurrency.py`。

### 3.8 关账与更正

* `close_period` 汇总未结争议、差额单、待核实发票、未分配余额四类阻塞项。
* 关账后所有写操作（分配、预算、预留、更正入账）都校验 `periods[period]`。
* `post_correction` 只允许写入开放账期，调整行携带 `origin_line/origin_period`，
  原账期数据保持不变；更正导致可用为负时自动开 `SF-CORR-*` 差额单。

## 4. 测试策略

`tests/` 下四组共 45 个用例（`python3 -I -m unittest discover -s tests`）：

* `test_money_and_pricing.py`：舍入、负金额、最大余数法、价目切换、USD 折算；
* `test_reconciliation.py`：版本替代、失败可计费、退款两种情形、未分配池、
  争议双向、分配校验和；
* `test_quota.py`：能力边界、幂等、等待/重评估、强制业务差额、消耗原子性、
  过期/取消、预算恒等式；
* `test_periods_and_import.py`：关账守卫、关账后更正联系、导入幂等、冲突保留、
  快照重建；
* `test_concurrency.py`：多进程并发预留与跨进程重复提交。

`demo/walkthrough.py` 是带中文断言的端到端剧本，可作为活文档运行。

## 5. 已知边界与设计取舍

1. 不做实时汇率与外部调用：汇率完全来自账期 `fx` 表，缺汇率时显式报错并在
   自检中提示，而不是猜测。
2. 未分配金额永不自动均摊：即使只有两个团队，也必须由财务确认分配口径后才
   离开未分配池。
3. 不可延迟业务超预算时允许可用额度暂时为负：业务连续性优先，差额交给财务
   追加预算处置，而不是拒绝业务或降级模型。
4. 快照可随时删除重建；直接修改快照文件不被视为合法操作，正规修订走
   `fact.overridden` 事件，保证重建结果一致。
