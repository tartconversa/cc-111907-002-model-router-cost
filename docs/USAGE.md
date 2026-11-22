# 模型用量结算与团队额度受理 · 使用手册

本应用只处理**本地合成**的调用、账单与额度请求，不向任何真实模型发起调用。
全部代码仅依赖 Python 3.14 标准库，所有状态保存在本地工作目录。

## 1. 角色与职责

| 角色 | 能做的事 | 对应命令 |
|---|---|---|
| 财务 | 导入、对账、发票核实、争议处置、共享费分配审核、关账、更正与差额处置、预算追加 | `import / reconcile / verify / dispute / propose / confirm-allocation / close / correction / shortfall / budget / worklist / integrity` |
| 业务负责人 | 查询成本与请求链、评估并申请/确认/取消额度、登记实际消耗、获知受限原因 | `team-cost / request / evaluate / reserve / cancel / consume / quota-list` |
| 系统（可定时） | 到期预留释放 | `expire` |

## 2. 快速开始

```bash
# 仓库根目录执行；-I 为隔离模式，防止来源目录中被植入的 json.py 等被加载
python3 -I run.py --workdir workdir init
python3 -I run.py --workdir workdir import inputs
python3 -I run.py --workdir workdir reconcile      # 尝试级对账
python3 -I run.py --workdir workdir quota-list     # 团队台账与额度申请
python3 -I run.py --workdir workdir worklist       # 财务待办
python3 -I run.py --workdir workdir integrity      # 不变量自检
```

* 加 `--json` 输出机器可读 JSON；默认输出中文表格。
* 加 `--as-of 2026-11-05T23:59:59Z` 可只看该时刻前 `received` 的事实，做时点回溯。
* 每个 `--workdir` 是相互隔离的独立状态目录，删除目录即清空全部本地状态。

一键端到端演练（覆盖本手册全部业务环节，含断言）：

```bash
python3 -I demo/walkthrough.py
```

## 3. 工作目录结构

```
<workdir>/
├── source/             # 每次导入的来源文件只读副本（按事件序号命名）
└── state/
    ├── events.jsonl    # 只追加事件日志：全部人工处置的唯一事实来源
    ├── snapshot.json   # 派生快照（可由 events.jsonl 随时 rebuild）
    └── store.lock      # 文件锁，串行化所有写事务
```

设计要点：**事件溯源**。预留、释放、消耗、争议、关账、更正等每一步都是一条
只追加事件；快照只是缓存。`rebuild` 命令可从事件日志完整重建状态，便于审计
与排障。所有写操作在文件锁内执行，并且拿到锁后会重新加载磁盘快照，因此两个
进程同时提交额度申请也不会花掉同一余额。

## 4. 金额与口径约定

* 内部记账一律使用**人民币整数分**；来源原币金额是十进制字符串，用 `Decimal`
  计算，按账期 `fx` 表汇率（不用实时汇率）折算后四舍五入到分。
* 每笔折算的舍入差记录在成本明细 `rounding_fen` 中，差额可解释，禁止用 float 承载金额。
* 金额桶严格区分，绝不混同：
  * `estimated_*` 按生效价目算出的预估（未到票成本）；
  * `confirmed_*` 供应商已确认或内部已记账；
  * `pending_*` 未确认/待核实承诺，只备忘，**不释放也不占用实际额度**；
  * 未分配池 `unallocated` 保留无法唯一归属的金额，**不强行均摊**。
* 共享费确认分配时用最大余数法做整数拆分，各份之和恒等于源行金额，数学差异为零。

## 5. 从调用导入到对账（财务核对真实费用）

### 5.1 导入与来源问题

```bash
python3 -I run.py --workdir workdir import inputs
```

* 同一字节文件重复导入是幂等的：不产生新事实、不重复占用额度（按 sha256 去重）。
* 发票行按 `id + revision` 识别，同 id 的高版本替代低版本；同键同版本内容冲突
  时保留先到版本、挂起新版本并登记到 `worklist` 的“来源冲突”，不静默覆盖。
* 拆分文件按文件名首段归并实体，如 `invoices_extra.json` 视为 invoices。
* 带 `manifest.json` 的目录会校验文件哈希，不符则拒绝导入；自扩充目录可用
  `--no-manifest-check`。

### 5.2 成本核算与请求链

* 成功业务量按**请求**统计（一个请求重试多次，成功只算一笔业务）。
* 费用按**实际可计费尝试**核算：失败尝试只要可计费同样入成本。
* 单价取调用 `occurred` 时刻已生效（`start <= occurred`）的最新价目版本，
  明细带 `price_id`，11 月 7 日调价前后的差额可直接解释。

```bash
python3 -I run.py --workdir workdir reconcile       # 全部尝试：预估/确认/待处理/差额
python3 -I run.py --workdir workdir request R0      # 单请求尝试链与四类费用
python3 -I run.py --workdir workdir team-cost       # 团队单位成本与日趋势
```

`request R0` 会展示失败尝试 A1 与成功尝试 A2，以及各自的已确认、预估、争议费用。

### 5.3 退款：先补缺口，剩余才返还

供应商“说已退款”但记录 `confirmed=false` 时，额度不会回来：

```bash
python3 -I run.py --workdir workdir worklist                 # 看到待核实发票 I4
python3 -I run.py --workdir workdir verify I4 confirmed      # 财务凭凭证核实确认
python3 -I run.py --workdir workdir verify I4 rejected       # 或驳回，永不释放
```

已确认退款的返还规则（人民币分，以尝试预估成本为封顶）：

1. 该尝试已确认账单高于预估时（供应商按更高金额计费），退款先把账单往真实
   成本拉回，**不返还额度**——避免“退款立刻返、失败重试账单后到”造成超支；
2. 该尝试确认账单低于预估（存在未结敞口）时，退款先补足缺口，超出部分才
   计入 `refund_credit` 返还团队可用额度；
3. 无法归属到具体尝试的悬空退款全额留存，不返还团队。

`reconcile` 输出的 `refunds` 段同时给出 `confirmed/pending/held/credit` 四个数。

## 6. 共享费用与未分配池

平台共享费（如 I3，100 元）无法直接归属到某次调用，先全额进未分配池：

```bash
python3 -I run.py --workdir workdir pool                 # 未分配池余额与明细
python3 -I run.py --workdir workdir propose I3           # 三种口径方案比较
python3 -I run.py --workdir workdir confirm-allocation I3 requests
```

* 三种口径：`usage`（按可计费尝试预估成本占比）、`requests`（按成功请求数占比）、
  `weight`（显式权重，`--weights '{"care":1,"ops":2}'`）。
* 方案比较直接给出每个团队确认前后的可用额度与额度充裕率，便于在确认前评估
  公平性与额度影响。
* 确认后口径写入事件日志永久留痕；开放账期内可重新确认（新口径替代旧口径，
  历史保留）；关账后不可修改。
* 若共享费行事后争议成立被反转，其历史分配自动失效，金额口径回退、团队额度
  回退，但分配事件仍可审计。

## 7. 团队额度受理

### 7.1 评估（只读，不占用）

```bash
python3 -I run.py --workdir workdir evaluate B2
```

输出：具备所需能力的全部合规模型、单位/合计成本、成本区间（low~high）、
当前可用额度、超支风险（`within_budget / partial_overrun / over_budget`）与建议。

### 7.2 能力边界（不可降级）

* 必须完成（`deferrable=false`）的业务只能选具备 `required_capability` 的模型，
  指定不具备能力的便宜模型会被直接拒绝（如 reasoning 请求指定 small）。
* 不可延迟业务不允许缩小批量；可延迟业务可以缩小批量或进入等待。

### 7.3 预留与占用口径

```bash
python3 -I run.py --workdir workdir reserve B1 --model small   # 指定合规模型
python3 -I run.py --workdir workdir reserve B3                 # 不指定则按最贵合规方案保守预留
```

* 重复提交同一请求返回既有预留（`idempotent_hit`），**不二次占用**。
* 预算不足时：可延迟业务进入等待清单；不可延迟业务仍获批（不能被报告成从未
  获批），同时生成财务差额单 `SF-<请求号>`，可用额度允许暂时为负并等待追加预算。
* 团队台账恒等式：`预算 = 已承诺 + 持有预留 + 可用`，已承诺含确认用量、未结
  敞口（预估未到票部分）、共享分摊与确认退款。

### 7.4 等待、预算追加与重评估

```bash
python3 -I run.py --workdir workdir budget care 5000 --reason "月度追加"
```

预算追加事件会自动重评估等待清单：能受理的按等待时登记的首选模型/批量转
held，仍不足的继续等待且不制造重复事件；该团队开放的差额单自动结清。

### 7.5 消耗、取消、过期

```bash
python3 -I run.py --workdir workdir consume B1 250 900 --note "实际用量"
python3 -I run.py --workdir workdir cancel B1 --reason "业务取消"
python3 -I run.py --workdir workdir expire --now 2026-12-01T00:00:01Z
```

* `consume` 在**同一事务**里登记实际成本（内部调整行 `ADJ-CONSUME-*`）并释放
  预留余量，杜绝“费用入账了却没有额度变化”的半次处置；实际成本超出预留导致
  可用为负时自动开差额单。
* 预留生命周期 `held → consumed / released / expired` 与批次号全程可互相追溯。

## 8. 争议、关账与更正

```bash
python3 -I run.py --workdir workdir dispute open I3 --reason "口径存疑"
python3 -I run.py --workdir workdir dispute resolve I3 accepted   # 成立：反转该行
python3 -I run.py --workdir workdir dispute resolve I4 rejected   # 驳回：维持记账
```

* 争议**接受**：usage 费用冲回、refund 不再返还、shared 退回未分配口径，并影响
  团队后续可用额度；争议**驳回**：行恢复正常记账。两种结论都进入未分配费用与
  团队额度的后续计算。
* 关账前置检查：未结争议、未处置差额单、待核实发票、未分配池有余额都会阻止
  关账（`close --force` 可带警告强制关账）。
* **已关账月份不改写**。认可的更正以内部调整行进入指定开放账期，并保留
  `origin_line / origin_period` 与原账的双向联系：

```bash
python3 -I run.py --workdir workdir correction I3 2026-12 5.00 shared \
    --reason "11月共享费复核少计" --team care
```

* 更正使已确认额度不再充足时：在执行的预留保持获批状态（不篡改历史），后续
  可延迟请求在额度评估中自然受限，同时生成 `SF-CORR-*` 差额单交财务处理。

## 9. 财务待办与自检

```bash
python3 -I run.py --workdir workdir worklist     # 待核实发票/未结争议/差额单/未分配池/来源冲突
python3 -I run.py --workdir workdir integrity    # 不变量自检，失败退出码 1
python3 -I run.py --workdir workdir rebuild      # 从事件日志重建快照
```

`integrity` 校验：分配各份之和等于源金额、关账后无可变写事件、每条消耗都有
配对成本行（无半次处置）、逐团队预算恒等式、未分配池明细与余额一致。

## 10. 扩充新数据

新记录沿用 `INPUT_CONTRACT.md` 的字段契约，放入一个新目录（可含任意多个
`*.json`，`manifest/schema` 可选），再次执行 `import` 即可：

* 新调用/发票/价目/汇率/请求会与既有事实合并；
* 新账期记得同时提供该账期的 `fx` 记录，否则该账期金额无法折算（自检会报问题）；
* 来源文件保持只读，应用只在工作目录内保存副本与状态。

## 11. 命令速查

| 命令 | 说明 |
|---|---|
| `init / import / rebuild` | 初始化、导入来源、重建快照 |
| `reconcile / request / team-cost / pool` | 对账、请求链、团队成本趋势、未分配池 |
| `propose / confirm-allocation` | 共享费方案比较与确认 |
| `evaluate / reserve / cancel / consume / expire` | 额度评估、预留、取消、消耗、过期 |
| `budget / shortfall / quota-list` | 预算追加、差额处置、额度总览 |
| `verify / dispute / close / correction` | 发票核实、争议、关账、关账后更正 |
| `worklist / integrity` | 财务待办、不变量自检 |

业务拒绝（预算不足、账期已关、重复占用、能力不符等）退出码为 2；输入错误
退出码为 3；自检失败退出码为 1。
