"""对账与预算台账。

金额桶严格区分，绝不混同
=========================
* ``estimated_*``：按生效价目+账期汇率算出的预估（未到票成本）。
* ``confirmed_*``：供应商已确认或内部已记账的人民币分。
* ``pending_*``：未确认/待核实承诺，只做备忘，不释放也不占用实际额度。
* ``unallocated``：无法唯一归属的金额（平台共享费、悬空引用等），保留不摊。

退款口径（契约：先补足费用缺口，剩余才返还；未确认不释放额度）
================================================================
对某尝试的已确认退款，可释放部分以该尝试的 **预估成本** 为封顶——即退款最多
把这次调用本身的成本降到零，超出部分属于多退，在仍有未结费用时不能返还：
* 该尝试已确认用量低于预估（未结敞口）时，退款先补足缺口（``refund_held``）；
* 已确认用量已覆盖预估时，退款全部留存（供应商已按更高金额计费，退款只是
  把账单往真实成本方向拉回），``refund_credit`` 为 0。
待核实（未确认）退款只在 ``pending_refund`` 备忘，对额度零影响。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .costing import all_attempt_costs
from .money import to_decimal, to_fen
from .store import Store


# ---------------------------------------------------------------- 生效发票行
def effective_invoice_lines(store: Store) -> list[dict[str, Any]]:
    """合并来源发票（同 id 取最高 revision）与内部调整行。

    每行带归一化字段与状态标记：``rejected``（人工驳回）、``dispute``
    （open/accepted/rejected）。冲突挂起的来源版本不会进入事实表，因此不会
    出现在这里。
    """
    latest: dict[str, dict[str, Any]] = {}
    for row in store.table("invoices"):
        cur = latest.get(row["id"])
        if cur is None or row["revision"] > cur["revision"]:
            latest[row["id"]] = row

    disputes = store.snapshot["disputes"]
    verifications = store.snapshot["verifications"]
    lines: list[dict[str, Any]] = []

    for line_id, row in latest.items():
        vkey = f"{line_id}|{row['revision']}"
        decision = verifications.get(vkey)
        dispute = disputes.get(f"invoice:{line_id}")
        # 人工核实“确认”会把供应商未确认行提升为已确认；驳回或争议成立则反转。
        confirmed = bool(row["confirmed"]) or decision == "confirmed"
        lines.append(_normalize(
            source="source", line_id=line_id, revision=row["revision"],
            row=row, confirmed_override=confirmed,
            rejected=decision == "rejected"
            or (dispute and dispute["status"] == "accepted"),
            dispute=dispute["status"] if dispute else None,
        ))

    for row in store.snapshot["manual_invoice_lines"]:
        dispute = disputes.get(f"adjustment:{row['line_id']}")
        lines.append(_normalize(
            source="manual", line_id=row["line_id"],
            revision=row.get("revision", 1), row=row,
            confirmed_override=bool(row["confirmed"]),
            rejected=bool(dispute and dispute["status"] == "accepted"),
            dispute=dispute["status"] if dispute else None,
        ))
    return lines


def _normalize(source: str, line_id: str, revision: int, row: dict[str, Any],
               rejected: bool, dispute: str | None,
               confirmed_override: bool | None = None) -> dict[str, Any]:
    return {
        "source": source,
        "line_id": line_id,
        "revision": revision,
        "provider": row["provider"],
        "period": row["period"],
        "team": row.get("team"),
        "request": row.get("request"),
        "attempt_id": row.get("attempt_id"),
        "currency": row["currency"],
        "amount": to_decimal(row["amount"]),
        "kind": row["kind"],
        "confirmed": bool(row["confirmed"]) if confirmed_override is None
        else confirmed_override,
        "rejected": rejected,
        "dispute": dispute,
        "origin": row.get("origin") or row.get("reason"),
    }


def _line_fen(store: Store, line: dict[str, Any]) -> int:
    rate = _rate(store, line["period"], line["currency"])
    return to_fen(line["amount"], rate)


def _rate(store: Store, period: str, currency: str) -> str:
    for f in store.table("fx"):
        if f["period"] == period and f["currency"] == currency:
            return f["cny_rate"]
    raise KeyError(
        f"缺少账期 {period} 币种 {currency} 的汇率；请先导入该账期 fx 记录")


# ---------------------------------------------------------------- 尝试级对账
def reconcile_attempts(store: Store, as_of: str | None = None) -> list[dict[str, Any]]:
    """每个尝试：预估、确认/待处理发票、差额与确认状态。"""
    attempts = {(a["request"], a["attempt_id"]): a for a in all_attempt_costs(store, as_of)}
    lines = effective_invoice_lines(store)
    attached: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    orphans: list[dict[str, Any]] = []
    for line in lines:
        key = (line["request"], line["attempt_id"])
        if key in attempts and key[0] is not None and key[1] is not None:
            attached[key].append(line)
        else:
            orphans.append(line)

    result: list[dict[str, Any]] = []
    for key, attempt in sorted(attempts.items()):
        bucket = attached.get(key, [])
        confirmed_fen = sum(
            _line_fen(store, ln) for ln in bucket
            if ln["confirmed"] and not ln["rejected"]
        )
        pending_fen = sum(
            _line_fen(store, ln) for ln in bucket
            if not ln["confirmed"] and not ln["rejected"]
        )
        est = attempt["estimated_fen"]
        state = "confirmed" if any(
            ln["confirmed"] and not ln["rejected"] and ln["kind"] == "usage"
            for ln in bucket
        ) else "estimated"
        result.append({
            "request": key[0],
            "attempt_id": key[1],
            "team": attempt["team"],
            "provider": attempt["provider"],
            "model": attempt["model"],
            "status": attempt["status"],
            "occurred": attempt["occurred"],
            "period": attempt["occurred"][:7],
            "price_id": attempt["price_id"],
            "estimated_fen": est,
            "confirmed_fen": confirmed_fen,
            "pending_fen": pending_fen,
            "variance_fen": confirmed_fen - est,
            "state": state,
            "lines": [_line_view(store, ln) for ln in bucket],
        })
    return result


def _line_view(store: Store, line: dict[str, Any]) -> dict[str, Any]:
    return {
        "line_id": line["line_id"],
        "revision": line["revision"],
        "source": line["source"],
        "kind": line["kind"],
        "currency": line["currency"],
        "amount": str(line["amount"]),
        "fen": _line_fen(store, line),
        "confirmed": line["confirmed"],
        "rejected": line["rejected"],
        "dispute": line["dispute"],
        "origin": line["origin"],
    }


def request_cost_view(store: Store, request_id: str,
                      as_of: str | None = None) -> dict[str, Any]:
    """一次请求的尝试链 + 已确认/预估/争议费用。"""
    rows = [r for r in reconcile_attempts(store, as_of) if r["request"] == request_id]
    if not rows:
        raise KeyError(f"未知请求 {request_id}")
    rows.sort(key=lambda r: r["attempt_id"])
    return {
        "request": request_id,
        "team": rows[0]["team"],
        "success": any(r["status"] == "success" for r in rows),
        "attempts": rows,
        "estimated_fen": sum(r["estimated_fen"] for r in rows),
        "confirmed_fen": sum(r["confirmed_fen"] for r in rows),
        "pending_fen": sum(r["pending_fen"] for r in rows),
        "disputed_fen": sum(
            abs(l["fen"]) for r in rows for l in r["lines"] if l["dispute"] == "open"
        ),
    }


# ---------------------------------------------------------------- 退款与未分配
def refund_breakdown(store: Store) -> dict[str, dict[str, int]]:
    """按团队拆解退款：缺口对冲 held 与实际返还 credit（人民币分）。"""
    attempts = {(a["request"], a["attempt_id"]): a for a in all_attempt_costs(store)}
    out: dict[str, dict[str, int]] = defaultdict(
        lambda: {"confirmed_refund_fen": 0, "pending_refund_fen": 0,
                 "refund_held_fen": 0, "refund_credit_fen": 0})
    for line in effective_invoice_lines(store):
        if line["kind"] != "refund" or line["rejected"]:
            continue
        fen = _line_fen(store, line)  # 负数
        team = line["team"] or attempts.get(
            (line["request"], line["attempt_id"]), {}
        ).get("team", "unallocated")
        bucket = out[team]
        if not line["confirmed"]:
            bucket["pending_refund_fen"] += fen  # 待核实承诺，零额度影响
            continue
        bucket["confirmed_refund_fen"] += fen
        attempt = attempts.get((line["request"], line["attempt_id"]))
        if attempt is None:
            # 悬空退款行：无法归属到具体尝试，全额留存，不返还团队。
            bucket["refund_held_fen"] += abs(fen)
            continue
        # 可释放封顶为预估成本。先抵消“已确认账单超出预估的部分”，
        # 剩余（把账单拉回到预估以下的部分）才真正返还团队额度。
        confirmed_usage = _confirmed_usage_on_target(store, line)
        excess_confirmed = max(0, confirmed_usage - attempt["estimated_fen"])
        released = max(0, abs(fen) - excess_confirmed)
        bucket["refund_held_fen"] += abs(fen) - released
        bucket["refund_credit_fen"] += released
    return dict(out)


def _confirmed_usage_on_target(store: Store, refund_line: dict[str, Any]) -> int:
    total = 0
    for line in effective_invoice_lines(store):
        if (line["kind"] == "usage" and line["confirmed"] and not line["rejected"]
                and line["request"] == refund_line["request"]
                and line["attempt_id"] == refund_line["attempt_id"]):
            total += _line_fen(store, line)
    return total


def unallocated_pool(store: Store, period: str) -> dict[str, Any]:
    """未分配费用池：保留无法唯一归属的已确认金额，扣除已生效分配。"""
    retained: list[dict[str, Any]] = []
    for line in effective_invoice_lines(store):
        if line["period"] != period or not line["confirmed"] or line["rejected"]:
            continue
        # 可唯一归属：能指到具体尝试，或内部调整行明确记了团队。
        attributable = (line["request"] is not None and line["attempt_id"] is not None) \
            or line["team"] is not None
        if line["kind"] == "shared" or not attributable:
            retained.append(_line_view(store, line))

    allocated = effective_allocations(store, period)
    allocated_by_line: dict[str, int] = defaultdict(int)
    for alloc in allocated:
        for part in alloc["parts"]:
            allocated_by_line[alloc["source_line_id"]] += part["fen"]

    pool_lines = []
    balance = 0
    for view in retained:
        remaining = view["fen"] - allocated_by_line.get(view["line_id"], 0)
        balance += remaining
        pool_lines.append({**view, "allocated_fen": allocated_by_line.get(view["line_id"], 0),
                           "remaining_fen": remaining})
    return {"period": period, "lines": pool_lines, "balance_fen": balance}


def effective_allocations(store: Store, period: str) -> list[dict[str, Any]]:
    """每条共享费用行只保留最后一次确认的分配口径（开放账期可重新确认）。

    源行被争议成立（accepted）而反转后，其历史分配自动失效：金额回到未分配
    池，团队已分配额度同步回退，但分配事件本身仍保留在日志中可审计。
    """
    dead_lines = {
        ln["line_id"] for ln in effective_invoice_lines(store) if ln["rejected"]
    }
    latest: dict[str, dict[str, Any]] = {}
    for event in store.snapshot["allocations"]:
        if event["period"] != period or event["source_line_id"] in dead_lines:
            continue
        latest[event["source_line_id"]] = event
    return list(latest.values())


# ---------------------------------------------------------------- 团队预算台账
def team_ledger(store: Store, team: str, period: str,
                as_of: str | None = None) -> dict[str, Any]:
    attempts = [a for a in all_attempt_costs(store, as_of)
                if a["team"] == team and a["occurred"][:7] == period]
    estimated = sum(a["estimated_fen"] for a in attempts)
    attempt_keys = {(a["request"], a["attempt_id"]) for a in attempts}

    confirmed_usage = 0
    pending_invoice = 0
    for line in effective_invoice_lines(store):
        if line["period"] != period or line["rejected"]:
            continue
        direct = line["team"] == team
        attached = (line["request"], line["attempt_id"]) in attempt_keys
        if not (direct or attached):
            continue
        fen = _line_fen(store, line)
        if line["kind"] in ("usage", "adjustment"):
            if line["confirmed"]:
                confirmed_usage += fen
            else:
                pending_invoice += fen

    refunds = refund_breakdown(store).get(
        team, {"confirmed_refund_fen": 0, "pending_refund_fen": 0,
               "refund_held_fen": 0, "refund_credit_fen": 0})

    shared_allocated = sum(
        part["fen"]
        for alloc in effective_allocations(store, period)
        for part in alloc["parts"] if part["team"] == team
    )

    # 敞口：已发生但还没有确认账单覆盖的预估成本（退款先补缺口部分同样冲抵）。
    exposure = max(
        0,
        estimated - confirmed_usage - refunds["refund_held_fen"],
    )
    committed = (confirmed_usage + refunds["confirmed_refund_fen"]
                 + shared_allocated + exposure)

    budget_row = store.fact("teams", f"{team}|{period}")
    base_budget = budget_row["budget_fen"] if budget_row else 0
    budget_added = sum(
        e["delta_fen"] for e in store.snapshot["budget_adjustments"]
        if e["team"] == team and e["period"] == period
    )
    budget = base_budget + budget_added

    reserved_held = sum(
        r["reserved_fen"] for r in store.snapshot["reservations"].values()
        if r["team"] == team and r["period"] == period and r["status"] == "held"
    )
    available = budget - committed - reserved_held

    return {
        "team": team,
        "period": period,
        "budget_fen": budget,
        "budget_base_fen": base_budget,
        "budget_added_fen": budget_added,
        "estimated_fen": estimated,
        "confirmed_usage_fen": confirmed_usage,
        "exposure_fen": exposure,
        "confirmed_refund_fen": refunds["confirmed_refund_fen"],
        "pending_refund_fen": refunds["pending_refund_fen"],
        "refund_held_fen": refunds["refund_held_fen"],
        "refund_credit_fen": refunds["refund_credit_fen"],
        "pending_invoice_fen": pending_invoice,
        "shared_allocated_fen": shared_allocated,
        "committed_fen": committed,
        "reserved_held_fen": reserved_held,
        "available_fen": available,
        "attempt_count": len(attempts),
    }


def all_team_ledgers(store: Store, period: str,
                     as_of: str | None = None) -> list[dict[str, Any]]:
    teams = sorted({r["team"] for r in store.table("teams") if r["period"] == period})
    return [team_ledger(store, t, period, as_of) for t in teams]
