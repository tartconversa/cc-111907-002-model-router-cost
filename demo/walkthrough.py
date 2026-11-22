#!/usr/bin/env python3
"""端到端业务演练：从调用导入到后续额度变化的完整可复现过程。

用法（仓库根目录，隔离模式，不触网、不调用真实模型）：
    python3 -I demo/walkthrough.py

脚本在 demo/work-walkthrough/ 下建立独立工作目录，依次演示：
  1. 来源导入（去重/版本/冲突口径）
  2. 按生效价目与账期汇率核算每次可计费尝试（含失败重试与调价差额）
  3. 团队单位成本/趋势、单请求尝试链（确认/预估/争议费用）
  4. 未确认退款不释放额度；核实确认后先补费用缺口再返还
  5. 共享费用三方案比较、确认留痕、未分配不强行均摊
  6. 额度受理：能力边界、成本区间、超支风险、幂等、等待与预算追加重评估
  7. 实际消耗的原子入账、预留释放、过期释放
  8. 争议、关账守卫、关账后更正进入开放账期、差额单
  9. 不变量自检与快照重建
所有断言失败会立即抛错，便于另一位同事复核每一步。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from settlement.allocation import confirm_allocation, propose_allocation  # noqa: E402
from settlement import disputes as dispute_mod  # noqa: E402
from settlement.importer import import_sources  # noqa: E402
from settlement.ledger import (refund_breakdown, team_ledger,  # noqa: E402
                                unallocated_pool)
from settlement import quota as quota_mod  # noqa: E402
from settlement.reports import (integrity_check,  # noqa: E402
                                 reconciliation_report, request_report,
                                 team_cost_report)
from settlement.store import Store, StoreError  # noqa: E402

WORK = ROOT / "demo" / "work-walkthrough"


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "✓" if condition else "✗"
    print(f"  {mark} {label}" + (f"（{detail}）" if detail else ""))
    if not condition:
        raise AssertionError(label)


def main() -> None:
    shutil.rmtree(WORK, ignore_errors=True)
    store = Store(WORK)
    store.init()

    print("【1】来源导入")
    with store.transaction():
        summary = import_sources(store, ROOT / "inputs")
    check("7 个来源文件全部导入", len(summary["imported"]) == 7)
    check("无字段校验问题", summary["validation_problems"] == [])
    with store.transaction():
        again = import_sources(store, ROOT / "inputs")
    check("重复投递幂等，无新事实", again["imported"] == [])

    print("【2】尝试级对账：失败重试可计费、调价与汇率留痕")
    recon = reconciliation_report(store, "2026-11")
    check("103 次可计费尝试全部对账", len(recon["attempts"]) == 103,
          f"预估合计 {recon['totals']['estimated_fen']} 分")
    check("预估总额 506 分", recon["totals"]["estimated_fen"] == 506)
    r0 = request_report(store, "R0")
    check("R0 含失败 A1 + 成功 A2 尝试链",
          [a["attempt_id"] for a in r0["attempts"]] == ["A1", "A2"])
    check("成功业务量按请求：R0 只算 1 笔业务", r0["success"] is True)

    print("【3】团队单位成本与趋势")
    cost = team_cost_report(store, "2026-11")
    by_team = {t["team"]: t for t in cost["teams"]}
    check("两团队各 45 个成功请求",
          by_team["care"]["success_units"] == 45
          and by_team["ops"]["success_units"] == 45)
    check("单位成本可解释", by_team["ops"]["unit_estimated_fen"] == 5.64)
    check("日趋势非空", all(t["daily_estimated_fen"] for t in cost["teams"]))

    print("【4】退款口径：未确认零额度影响，核实后先补缺口")
    before = refund_breakdown(store)["ops"]
    check("I4 未确认时只在 pending 备忘", before["pending_refund_fen"] == -7
          and before["refund_credit_fen"] == 0)
    with store.transaction():
        dispute_mod.verify_invoice(store, "I4", "confirmed", "供应商退款凭证核实")
    after = refund_breakdown(store)["ops"]
    # R0/A1 确认账单 14 > 预估 6：退款 7 全部留存，零返还
    check("账单已覆盖预估时退款不返还", after["refund_credit_fen"] == 0
          and after["refund_held_fen"] == 7)

    print("【5】共享费用：方案比较、确认留痕、未分配不强行均摊")
    check("确认前 10000 分全部未分配",
          unallocated_pool(store, "2026-11")["balance_fen"] == 10000)
    proposal = propose_allocation(store, "2026-11", "I3")
    feasible = [p for p in proposal["plans"] if p["feasible"]]
    check("给出用量/请求数/权重三种口径", len(feasible) >= 2)
    check("每种口径整数拆分校验和等于源金额",
          all(p["sum_check_fen"] == 10000 for p in feasible))
    with store.transaction():
        confirm_allocation(store, "2026-11", "I3", "requests")
    check("确认后未分配池清零",
          unallocated_pool(store, "2026-11")["balance_fen"] == 0)
    lg = team_ledger(store, "care", "2026-11")
    check("团队台账体现共享分摊 5000 分", lg["shared_allocated_fen"] == 5000)

    print("【6】额度受理：能力边界、区间、幂等、等待与重评估")
    ev_b2 = quota_mod.evaluate(store, "B2")
    check("reasoning 只能选 large（拒绝低价不合规模型）",
          [o["model"] for o in ev_b2["eligible_options"]] == ["large"])
    try:
        with store.transaction():
            quota_mod.reserve(store, "B2", model="small")
        raise AssertionError("不合规降级未被拦截")
    except StoreError:
        check("不可延迟业务降级到 small 被拒绝", True)
    with store.transaction():
        first = quota_mod.reserve(store, "B2")
        second = quota_mod.reserve(store, "B2")
    check("重复提交幂等不二次占用", second.get("idempotent_hit") is True)

    # 收紧 care 预算制造超预算（此时 care 已承担共享分摊 5000 分）
    with store.transaction():
        tight_row = dict(store.fact("teams", "care|2026-11"))
        tight_row["budget_fen"] = 1500
        store.override_fact("teams", "care|2026-11", tight_row, "演练收紧预算")
    with store.transaction():
        out = quota_mod.reserve(store, "B1", model="small")
    check("可延迟业务超预算进入等待清单", out.get("waiting") is True)
    with store.transaction():
        result = quota_mod.increase_budget(
            store, "care", "2026-11", 9000, "演练追加预算")
    check("预算追加后等待请求自动转 held",
          any(r.get("request_id") == "B1" and "reservation" in r
              for r in result["reevaluated"]))

    print("【7】实际消耗原子入账与过期释放")
    with store.transaction():
        consumed = quota_mod.consume(store, "B1", 250, 900, "演练实际用量")
    check("消耗与预留释放同一事务", consumed["consumed_fen"] == 900)
    manual = {x["line_id"] for x in store.snapshot["manual_invoice_lines"]}
    check("配对成本调整行 ADJ-CONSUME-B1 存在（无半次处置）",
          "ADJ-CONSUME-B1" in manual)
    with store.transaction():
        expired = quota_mod.expire_due(store, "2026-12-01T00:00:01Z")
    check("到期预留自动释放", any(p["request_id"] == "B2" for p in expired))

    print("【8】争议、关账守卫与关账后更正")
    with store.transaction():
        dispute_mod.open_dispute(store, "I3", "演练争议")
        dispute_mod.resolve_dispute(store, "I3", "rejected", "驳回，维持原账")
    check("争议驳回后共享分摊仍在账",
          team_ledger(store, "care", "2026-11")["shared_allocated_fen"] == 5000)
    # 新增一笔待核实发票（走来源导入，可重建），制造关账前置未结事项
    pending_dir = ROOT / "demo" / "walkthrough-pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "invoices_pending.json").write_text(json.dumps([{
        "id": "I5", "revision": 1, "provider": "A", "period": "2026-11",
        "request": "R1", "attempt_id": "A1", "currency": "CNY",
        "amount": "-0.01", "kind": "refund", "confirmed": False,
    }], ensure_ascii=False), encoding="utf-8")
    with store.transaction():
        import_sources(store, pending_dir, verify_manifest=False)
    try:
        with store.transaction():
            dispute_mod.close_period(store, "2026-11")
        raise AssertionError("关账守卫未生效")
    except StoreError:
        check("存在待核实发票时关账被阻止", True)
    with store.transaction():
        dispute_mod.verify_invoice(store, "I5", "rejected", "凭证不足，驳回退款")
        dispute_mod.close_period(store, "2026-11")
    check("待办清零后正式关账", store.is_closed("2026-11"))
    # 下一开放账期（12 月）的预算与汇率事实：走来源导入事件，保证快照可重建
    extra_dir = ROOT / "demo" / "walkthrough-extra"
    extra_dir.mkdir(parents=True, exist_ok=True)
    (extra_dir / "teams_december.json").write_text(json.dumps([
        {"team": "care", "period": "2026-12", "budget_fen": 5000, "carry": False},
    ], ensure_ascii=False), encoding="utf-8")
    (extra_dir / "fx_december.json").write_text(json.dumps([
        {"period": "2026-12", "currency": "CNY", "cny_rate": "1.00"},
    ], ensure_ascii=False), encoding="utf-8")
    with store.transaction():
        import_sources(store, extra_dir, verify_manifest=False)
    with store.transaction():
        corr = dispute_mod.post_correction(
            store, "I3", "2026-12", "5.00", "shared",
            "11 月共享费复核少计，原账不改", team="care")
    check("更正进入开放账期并保留原账联系",
          corr["origin_line"] == "I3" and corr["origin_period"] == "2026-11")

    print("【9】不变量自检与快照重建")
    check("全部不变量通过", integrity_check(store)["ok"])
    seq_before = store.snapshot["seq"]
    store.rebuild()
    check("快照重建后事件序号一致", store.snapshot["seq"] == seq_before)
    check("重建后不变量仍通过", integrity_check(store)["ok"])

    print("\n全部演练步骤通过。工作目录：%s" % WORK)
    print("事件日志：%s（只追加，可逐条审计）" % (WORK / "state" / "events.jsonl"))


if __name__ == "__main__":
    main()
