"""共享平台费用的分配方案与确认。

契约要求：共享费用可按用量、业务请求数或显式权重提出分摊方案；未确认部分
维持未分配；正式确认前必须能比较不同方案的公平性与对团队额度的影响；正式
结算后保留所使用的分配口径。

因此本模块提供：
* :func:`propose_allocation` 一次给出三种口径的整数分拆分（最大余数法，
  各份之和恒等于源行金额，数学差异为零，不强行制造均摊）。
* :func:`confirm_allocation` 由财务确认其中一种口径，写入事件日志；开放账期
  内可以重新确认（新口径替代旧口径，历史完整保留），关账后拒绝修改。
"""

from __future__ import annotations

from typing import Any

from .costing import all_attempt_costs, success_units
from .ledger import (effective_allocations, team_ledger,
                     unallocated_pool)
from .money import allocate_integer
from .store import Store, StoreError

BASES = ("usage", "requests", "weight")
BASE_LABELS = {
    "usage": "按可计费尝试预估成本占比",
    "requests": "按成功业务请求数占比",
    "weight": "按显式权重",
}


def _usage_weights(store: Store, period: str, teams: list[str]) -> list[int]:
    totals = {t: 0 for t in teams}
    for a in all_attempt_costs(store):
        if a["occurred"][:7] == period and a["team"] in totals:
            totals[a["team"]] += a["estimated_fen"]
    return [totals[t] for t in teams]


def _request_weights(store: Store, teams: list[str]) -> list[int]:
    units = success_units(store)
    return [units.get(t, 0) for t in teams]


def propose_allocation(store: Store, period: str, line_id: str,
                       explicit_weights: dict[str, int] | None = None
                       ) -> dict[str, Any]:
    """对一条未分配的共享/悬空费用行生成三种口径方案及额度影响比较。"""
    pool = unallocated_pool(store, period)
    target = next((ln for ln in pool["lines"] if ln["line_id"] == line_id), None)
    if target is None:
        raise StoreError(f"{line_id} 不在账期 {period} 的未分配池中")
    if store.is_closed(period):
        raise StoreError(f"账期 {period} 已关账，不能再提出分配方案")

    teams = sorted(r["team"] for r in store.table("teams") if r["period"] == period)
    if not teams:
        raise StoreError("没有团队预算记录，无法分配")
    total_fen = target["fen"]

    weight_sets = {
        "usage": _usage_weights(store, period, teams),
        "requests": _request_weights(store, teams),
        "weight": [int(explicit_weights.get(t, 0)) if explicit_weights else 0 for t in teams],
    }
    ledgers = {t: team_ledger(store, t, period) for t in teams}
    plans = []
    for basis in BASES:
        weights = weight_sets[basis]
        if basis == "weight" and sum(weights) <= 0:
            plans.append({"basis": basis, "feasible": False,
                          "reason": "未提供有效显式权重"})
            continue
        parts_fen = allocate_integer(total_fen, weights)
        parts = []
        for team, weight, fen in zip(teams, weights, parts_fen):
            lg = ledgers[team]
            parts.append({
                "team": team,
                "weight": weight,
                "fen": fen,
                "available_before_fen": lg["available_fen"],
                "available_after_fen": lg["available_fen"] - fen,
                "headroom_pct_after": round(
                    (lg["available_fen"] - fen) / lg["budget_fen"] * 100, 2
                ) if lg["budget_fen"] else None,
            })
        plans.append({
            "basis": basis,
            "label": BASE_LABELS[basis],
            "feasible": True,
            "sum_check_fen": sum(parts_fen),
            "parts": parts,
            "max_team_hit_fen": max(abs(p["fen"]) for p in parts),
            "min_headroom_pct_after": min(
                p["headroom_pct_after"] for p in parts
                if p["headroom_pct_after"] is not None
            ),
        })

    current = next((a for a in effective_allocations(store, period)
                    if a["source_line_id"] == line_id), None)
    return {
        "period": period,
        "source_line_id": line_id,
        "source_line": target,
        "total_fen": total_fen,
        "plans": plans,
        "current_confirmed": current,
    }


def confirm_allocation(store: Store, period: str, line_id: str, basis: str,
                       explicit_weights: dict[str, int] | None = None,
                       operator: str = "finance") -> dict[str, Any]:
    """财务确认分配口径；事件留痕，开放账期内可重新确认。"""
    if basis not in BASES:
        raise StoreError(f"未知分配口径 {basis}，可选 {BASES}")
    if store.is_closed(period):
        raise StoreError(f"账期 {period} 已关账，分配口径不可修改")
    proposal = propose_allocation(store, period, line_id, explicit_weights)
    plan = next(p for p in proposal["plans"] if p["basis"] == basis)
    if not plan["feasible"]:
        raise StoreError(f"口径 {basis} 不可行：{plan.get('reason')}")

    payload = {
        "period": period,
        "source_line_id": line_id,
        "basis": basis,
        "basis_label": BASE_LABELS[basis],
        "total_fen": proposal["total_fen"],
        "parts": [{"team": p["team"], "weight": p["weight"], "fen": p["fen"]}
                  for p in plan["parts"]],
        "operator": operator,
        "supersedes": next((a["seq"] for a in effective_allocations(store, period)
                            if a["source_line_id"] == line_id), None),
    }
    return store.append_event("allocation.confirmed", payload)["payload"]
