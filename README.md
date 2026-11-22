# 模型汇聚调用的成本归因与降级边界

本仓库在本地合成数据上实现**模型用量结算与团队额度受理**应用：财务核对真实
费用、平台按团队预算受理后续用量额度、争议与更正有明确处置去向。应用不向
真实模型发起任何调用，仅依赖 Python 3.14 标准库。

## 目录

```
settlement/          应用包（存储/对账/分配/额度/争议/报表/CLI）
tests/               45 个单元与业务测试（含多进程并发测试）
demo/
  walkthrough.py     中文端到端演练脚本（带断言，可复现完整流程）
  inputs2/           紧预算/来源冲突场景的第二套合成输入
docs/
  USAGE.md           中文使用手册（角色、命令、业务口径）
  ARCHITECTURE.md    架构、事件模型与规则落点
inputs/              只读来源契约数据（保持不变，附 manifest 哈希）
run.py               -I 隔离模式下的统一运行入口
```

## 环境与资料检查

使用 Python 3.14，仅依赖标准库。先确认来源资料本身完整：

```bash
python3 -I -m unittest discover -s tests -v
```

该命令同时运行来源契约检查与全部应用测试。容器环境以 Dockerfile 中的公开
基础镜像摘要锁定，可 `docker build -t local-analytics-inputs .` 后离线运行。

## 最快上手

```bash
python3 -I run.py --workdir workdir init
python3 -I run.py --workdir workdir import inputs
python3 -I run.py --workdir workdir reconcile     # 尝试级对账
python3 -I run.py --workdir workdir quota-list    # 团队预算台账与额度申请
python3 -I run.py --workdir workdir worklist      # 财务待办
python3 -I run.py --workdir workdir integrity     # 不变量自检

# 带中文断言的完整业务演练（导入→对账→退款→分配→额度→争议→关账→更正→重建）
python3 -I demo/walkthrough.py
```

`-I` 隔离模式会忽略环境变量与用户 site-packages，防止来源目录中被植入的
`json.py` 等模块被解释器加载；`run.py` 会显式把仓库根加入模块搜索路径。

## 核心能力一览

* **口径分离**：成功业务量按请求统计，费用按实际可计费尝试（含失败重试）
  核算；生效价目、账期汇率、逐笔舍入差全部留痕，调价与外币差额可解释。
* **金额桶不混同**：预估 / 已确认 / 待核实 / 未分配严格区分；未确认退款不
  释放额度，已确认退款先补费用缺口再返还；无法唯一归属的金额保留在未分配池。
* **共享费分配**：确认前可比较用量、请求数、显式权重三种口径及对团队额度的
  影响，确认后口径永久留痕，关账后不可改。
* **额度受理**：按能力筛选模型，禁止因低价降级；给成本区间与超支风险；文件锁
  + 快照重读保证并发申请不双重占用、重复提交幂等；支持等待、预算追加重评估、
  消耗原子入账、过期释放，预留与批次全程可追溯。
* **争议与关账**：争议接受/驳回分别反转或维持记账；关账有前置守卫；已关账
  月份不改写，更正进入开放账期并保留与原账的联系；额度被更正冲减时在执行业务
  不被抹掉、后续请求受限并生成财务差额单。
* **可审计**：事件溯源 + 只追加日志，快照可随时重建，`integrity` 自检预算
  恒等式、分配校验和与“无半次处置”配对。

详细命令与业务口径见 [docs/USAGE.md](docs/USAGE.md)，设计细节见
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，字段契约见
[INPUT_CONTRACT.md](INPUT_CONTRACT.md)。
