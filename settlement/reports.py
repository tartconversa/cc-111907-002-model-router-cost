"""只读分析报表：财务核对与业务查询的统一出口。

所有报表同时提供结构化数据（供 CLI ``--json`` 输出）与中文表格（默认）。
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .costing import success_units, team_daily_estimated
from .ledger import (effective_invoice_lines, reconcile_attempts,
                     refund_breakdown, request_cost_view, team_ledger,
                     unallocated_pool, all_team_ledgers, _line_fen)
from .quota import evaluate
from .store import Store


# ------------------------------------------------------------------ 团队成本
def team_cost_report(store: Store, period: str,
                     as_of: str | None = None) -> dict[str, Any]:
    """团队单位成本与趋势：成功业务量按请求，费用按全部可计费尝试。"""
    units = success_units(store, as_of)
    daily = team_daily_estimated(store, as_of)
    rows = []
    for ledger in all_team_ledgers(store, period, as_of):
        team = ledger["team"]
        n_units = units.get(team, 0)
        rows.append({
            "team": team,
            "success_units": n_units,
            "estimated_total_fen": ledger["estimated_fen"],
            "confirmed_usage_fen": ledger["confirmed_usage_fen"],
            "exposure_fen": ledger["exposure_fen"],
            "shared_allocated_fen": ledger["shared_allocated_fen"],
            "refund_credit_fen": ledger["refund_credit_fen"],
            "unit_estimated_fen": (
                round(ledger["estimated_fen"] / n_units, 2) if n_units else None
            ),
            "daily_estimated_fen": dict(sorted(daily.get(team, {}).items())),
        })
    return {"period": period, "as_of": as_of, "teams": rows}


# ------------------------------------------------------------------ 请求尝试链
def request_report(store: Store, request_id: str,
                   as_of: str | None = None) -> dict[str, Any]:
    return request_cost_view(store, request_id, as_of)


# ------------------------------------------------------------------ 对账
def reconciliation_report(store: Store, period: str,
                          as_of: str | None = None) -> dict[str, Any]:
    rows = [r for r in reconcile_attempts(store, as_of) if r["period"] == period]
    totals = defaultdict(int)
    for r in rows:
        totals["estimated_fen"] += r["estimated_fen"]
        totals["confirmed_fen"] += r["confirmed_fen"]
        totals["pending_fen"] += r["pending_fen"]
        totals["variance_fen"] += r["variance_fen"]
    pool = unallocated_pool(store, period)
    refunds = refund_breakdown(store)
    return {
        "period": period,
        "as_of": as_of,
        "attempts": rows,
        "totals": dict(totals),
        "unallocated_balance_fen": pool["balance_fen"],
        "unallocated_lines": pool["lines"],
        "refunds": refunds,
    }


# ------------------------------------------------------------------ 额度受理
def quota_report(store: Store, period: str,
                 as_of: str | None = None) -> dict[str, Any]:
    items = []
    for req in store.table("requests"):
        if req["period"] != period:
            continue
        item = {"request": req, "evaluation": evaluate(store, req["id"], as_of)}
        item["reservation"] = store.snapshot["reservations"].get(req["id"])
        item["waiting"] = store.snapshot["waiting"].get(req["id"])
        items.append(item)
    ledgers = {l["team"]: l for l in all_team_ledgers(store, period, as_of)}
    return {"period": period, "as_of": as_of,
            "team_ledgers": list(ledgers.values()), "requests": items}


# ------------------------------------------------------------------ 财务待办
def finance_worklist(store: Store) -> dict[str, Any]:
    pending_invoices = [
        _line_view_safe(store, ln) for ln in effective_invoice_lines(store)
        if not ln["confirmed"] and not ln["rejected"]
    ]
    open_disputes = [
        {"ref": k, **v} for k, v in store.snapshot["disputes"].items()
        if v["status"] == "open"
    ]
    open_shortfalls = [s for s in store.snapshot["shortfalls"].values()
                       if s["status"] == "open"]
    pools = {}
    for period in {ln["period"] for ln in effective_invoice_lines(store)}:
        try:
            pool = unallocated_pool(store, period)
        except KeyError:
            continue  # 缺汇率账期在自检中报问题，待办列表不因此崩溃
        if pool["balance_fen"]:
            pools[period] = pool
    return {
        "pending_invoice_lines": pending_invoices,
        "open_disputes": open_disputes,
        "open_shortfalls": open_shortfalls,
        "unallocated_pools": pools,
        "source_conflicts": store.snapshot["conflicts"],
        "periods": store.snapshot["periods"],
    }


def _line_view_safe(store: Store, line: dict[str, Any]) -> dict[str, Any]:
    return {
        "line_id": line["line_id"], "revision": line["revision"],
        "provider": line["provider"], "period": line["period"],
        "request": line["request"], "attempt_id": line["attempt_id"],
        "kind": line["kind"], "currency": line["currency"],
        "amount": str(line["amount"]), "fen": _line_fen(store, line),
        "source": line["source"],
    }


# ------------------------------------------------------------------ 不变量自检
def integrity_check(store: Store) -> dict[str, Any]:
    """全量不变量检查；任何一条失败都说明存在“半次处置”或账实不符。"""
    problems: list[str] = []

    # 1. 分配各份之和必须等于源行金额。
    for alloc in store.snapshot["allocations"]:
        if sum(p["fen"] for p in alloc["parts"]) != alloc["total_fen"]:
            problems.append(f"分配 {alloc['source_line_id']} 各份之和不等于源金额")

    # 2. 已关账账期内不允许出现关账时点之后的可变事件（分配/预算/预留写操作）。
    mutable_after_close = {
        "allocation.confirmed", "budget.increased",
        "reservation.created", "reservation.cancelled",
        "reservation.consumed", "adjustment.posted",
    }
    closed = {p: m["closed_at"] for p, m in store.snapshot["periods"].items()
              if m["status"] == "closed"}
    if closed and store.events_path.exists():
        for line in store.events_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["type"] not in mutable_after_close:
                continue
            period = event["payload"].get("period")
            if period in closed and event["at"] > closed[period]:
                problems.append(
                    f"关账后仍发生 {event['type']}（账期 {period}，事件 {event['seq']}）")

    # 3. 每条 consumed 预留都必须有配对的内部调整行（费用与额度同生共死）。
    manual_ids = {x["line_id"] for x in store.snapshot["manual_invoice_lines"]}
    for rid, r in store.snapshot["reservations"].items():
        if r["status"] == "consumed" and f"ADJ-CONSUME-{rid}" not in manual_ids:
            problems.append(f"预留 {rid} 已消耗但缺少配对成本调整行（半次处置）")

    # 4. 预算恒等式：预算 = 已承诺 + 持有预留 + 可用（逐团队账期）。
    for team_row in store.table("teams"):
        lg = team_ledger(store, team_row["team"], team_row["period"])
        identity = lg["committed_fen"] + lg["reserved_held_fen"] + lg["available_fen"]
        if identity != lg["budget_fen"]:
            problems.append(
                f"{team_row['team']}/{team_row['period']} 预算恒等式不成立: "
                f"{identity} != {lg['budget_fen']}")

    # 5. 未分配池余额 = 未归属已确认行 - 已生效分配（缺汇率账期跳过并提示）。
    for period in {ln["period"] for ln in effective_invoice_lines(store)}:
        try:
            pool = unallocated_pool(store, period)
        except KeyError as exc:
            problems.append(f"{period} 未分配池无法折算：{exc}")
            continue
        if sum(ln["remaining_fen"] for ln in pool["lines"]) != pool["balance_fen"]:
            problems.append(f"{period} 未分配池明细与余额不一致")

    return {"ok": not problems, "problems": problems}
