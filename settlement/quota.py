"""团队预算周期内的用量额度受理。

核心约束
========
* 必须完成（``deferrable=false``）的业务只能使用具备所需能力的模型，绝不因
  单价低换成能力不合适的模型；也不允许缩批量。
* 可延迟业务可以等待（进入等待清单，预算追加后自动重评估）或缩小批量。
* 评估同时考虑预算周期、已有费用（含未结用量敞口与待核实承诺备忘）、已保留
  额度，成本给区间（low=最便宜合规模型，high=最贵），并标注超支风险。
* 两份额度申请不能同时花同一余额：所有写操作在文件锁内完成；同一请求重复
  提交返回既有预留，不二次占用。
* 预留、释放、过期、实际消耗与同一批次（batch）全程可互相追溯。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .costing import fx_rate
from .ledger import team_ledger
from .money import to_decimal
from .store import Store, StoreError
from .timeutil import now_ts, parse_ts


def eligible_models(store: Store, capability: str) -> list[dict[str, Any]]:
    return [c for c in store.table("capabilities") if capability in c["skills"]]


def unit_cost_fen(store: Store, model: str, period: str, at: str | None = None
                  ) -> tuple[int, str]:
    """某模型在指定时刻（默认现在）生效价目下的单位成本（人民币分）。"""
    at = at or now_ts()
    rows = [p for p in store.table("prices")
            if p["model"] == model and parse_ts(p["start"]) <= parse_ts(at)]
    if not rows:
        raise StoreError(f"模型 {model} 在 {at} 没有生效价目")
    price = max(rows, key=lambda p: parse_ts(p["start"]))
    cap = next(c for c in store.table("capabilities") if c["model"] == model)
    rate = fx_rate(store.table("fx"), period, price["currency"])
    native = (
        Decimal(cap["input_per_unit"]) * to_decimal(price["per_1k_input"])
        + Decimal(cap["output_per_unit"]) * to_decimal(price["per_1k_output"])
    ) / Decimal(1000)
    from .money import to_fen
    return to_fen(native, rate), price["id"]


def evaluate(store: Store, request_id: str,
             as_of: str | None = None) -> dict[str, Any]:
    """比较符合业务限制的用量方案、成本区间与超支风险。只读，不占用额度。"""
    req = store.fact("requests", request_id)
    if req is None:
        raise StoreError(f"未知额度请求 {request_id}")
    if as_of is not None and parse_ts(as_of) < parse_ts(_request_received(store, req)):
        raise StoreError("该请求在 as-of 时点尚未进入系统")

    models = eligible_models(store, req["required_capability"])
    if not models:
        raise StoreError(f"没有模型具备能力 {req['required_capability']}，业务无法受理")

    options = []
    for cap in models:
        unit_fen, price_id = unit_cost_fen(store, cap["model"], req["period"], as_of)
        options.append({
            "model": cap["model"],
            "skills": cap["skills"],
            "price_id": price_id,
            "unit_fen": unit_fen,
            "total_fen": unit_fen * req["units"],
        })
    options.sort(key=lambda o: o["total_fen"])
    low, high = options[0]["total_fen"], options[-1]["total_fen"]

    ledger = team_ledger(store, req["team"], req["period"], as_of)
    available = ledger["available_fen"]
    if high <= available:
        risk = "within_budget"
    elif low <= available:
        risk = "partial_overrun"   # 选最便宜的合规模型仍可行
    else:
        risk = "over_budget"

    expired = parse_ts(as_of or now_ts()) > parse_ts(req["expires"])
    recommendation = _recommend(req, risk, expired)
    return {
        "request": req,
        "eligible_options": options,
        "cost_range_fen": {"low": low, "high": high},
        "available_fen": available,
        "overspend_risk": risk,
        "expired": expired,
        "recommendation": recommendation,
        "ledger_snapshot": {
            "committed_fen": ledger["committed_fen"],
            "exposure_fen": ledger["exposure_fen"],
            "reserved_held_fen": ledger["reserved_held_fen"],
            "pending_invoice_fen": ledger["pending_invoice_fen"],
        },
    }


def _request_received(store: Store, req: dict[str, Any]) -> str:
    # requests 契约没有 received；同批导入统一以最早导入时间为可见时点，
    # 这里用预算账期起点作为保守下界，as-of 主要用于 calls/invoices。
    return req["period"] + "-01T00:00:00+00:00"


def _recommend(req: dict[str, Any], risk: str, expired: bool) -> str:
    if expired:
        return "申请已到期，拒绝新预留"
    if risk == "within_budget":
        return "可直接确认预留"
    if risk == "partial_overrun":
        return "仅最便宜合规模型在预算内；可延迟业务建议等待，不可延迟业务按合规最低价受理"
    if req["deferrable"]:
        return "全部合规方案均超预算，进入等待清单或缩小批量"
    return "必须完成且全部合规方案超预算，生成财务差额单，不得换用不合规模型"


def _get_open_reservation(store: Store, request_id: str) -> dict[str, Any] | None:
    r = store.snapshot["reservations"].get(request_id)
    if r and r["status"] in ("held", "waiting"):
        return r
    return None


def reserve(store: Store, request_id: str, model: str | None = None,
            units: int | None = None, operator: str = "business",
            as_of: str | None = None) -> dict[str, Any]:
    """确认预留。幂等：同一请求已有生效预留时直接返回，不二次占用。"""
    req = store.fact("requests", request_id)
    if req is None:
        raise StoreError(f"未知额度请求 {request_id}")
    existing = _get_open_reservation(store, request_id)
    if existing:
        return {"idempotent_hit": True, "reservation": existing}

    ev = evaluate(store, request_id, as_of)
    if ev["expired"]:
        raise StoreError(f"请求 {request_id} 已过期")
    chosen_units = req["units"] if units is None else units
    if units is not None and not req["deferrable"] and units != req["units"]:
        raise StoreError("不可延迟业务不允许缩小批量")
    if chosen_units <= 0 or chosen_units > req["units"]:
        raise StoreError("批量必须在 1 到申请批量之间")

    eligible = {o["model"]: o for o in ev["eligible_options"]}
    if model is not None:
        if model not in eligible:
            raise StoreError(
                f"模型 {model} 不具备所需能力 {req['required_capability']}，拒绝降级")
        chosen = model
    else:
        # 未指定模型时按最贵合规方案保守预留，避免后续账单超支。
        chosen = ev["eligible_options"][-1]["model"]
    unit_fen, price_id = unit_cost_fen(store, chosen, req["period"], as_of)
    reserved_fen = unit_fen * chosen_units

    ledger = team_ledger(store, req["team"], req["period"], as_of)
    if reserved_fen > ledger["available_fen"]:
        if req["deferrable"]:
            payload = {"request_id": request_id, "team": req["team"],
                       "period": req["period"], "batch": req["batch"],
                       "preferred_model": model, "preferred_units": chosen_units,
                       "reason": "可延迟业务预算不足，进入等待清单",
                       "shortfall_fen": reserved_fen - ledger["available_fen"]}
            if request_id not in store.snapshot["waiting"]:
                store.append_event("reservation.waiting", payload)
            else:
                # 重评估仍不足：刷新缺口数字，不制造重复事件。
                store.snapshot["waiting"][request_id].update(payload)
            return {"waiting": True, "detail": payload}
        # 不可延迟：不能拒绝业务、不能降级模型，开财务差额单并按所需额度预留。
        shortfall_id = f"SF-{request_id}"
        if shortfall_id not in store.snapshot["shortfalls"]:
            store.append_event("shortfall.opened", {
                "id": shortfall_id, "team": req["team"], "period": req["period"],
                "request_id": request_id, "batch": req["batch"],
                "required_fen": reserved_fen,
                "available_fen": ledger["available_fen"],
                "gap_fen": reserved_fen - ledger["available_fen"],
                "reason": "必须完成业务超预算，等待财务追加预算",
            })
    payload = {
        "request_id": request_id,
        "team": req["team"],
        "period": req["period"],
        "batch": req["batch"],
        "units": chosen_units,
        "requested_units": req["units"],
        "model": chosen,
        "price_id": price_id,
        "unit_fen": unit_fen,
        "reserved_fen": reserved_fen,
        "basis": "high" if model is None else "specified",
        "operator": operator,
    }
    event = store.append_event("reservation.created", payload)
    stored = store.snapshot["reservations"][request_id]
    return {"waiting": False, "reservation": stored}


def cancel(store: Store, request_id: str, reason: str = "业务取消",
           operator: str = "business") -> dict[str, Any]:
    r = store.snapshot["reservations"].get(request_id)
    if not r or r["status"] != "held":
        raise StoreError(f"请求 {request_id} 没有可取消的生效预留")
    event = store.append_event("reservation.cancelled",
                               {"request_id": request_id, "reason": reason,
                                "operator": operator})
    return event["payload"]


def expire_due(store: Store, now: str | None = None) -> list[dict[str, Any]]:
    """过期扫描：到期仍 held 的预留自动释放，等待清单同步失效。"""
    now = now or now_ts()
    changed = []
    for req_id, req_row in list(store.snapshot["facts"]["requests"].items()):
        if parse_ts(now) <= parse_ts(req_row["expires"]):
            continue
        r = store.snapshot["reservations"].get(req_id)
        if r and r["status"] == "held":
            event = store.append_event("reservation.expired",
                                       {"request_id": req_id})
            changed.append(event["payload"])
        store.snapshot["waiting"].pop(req_id, None)
    return changed


def consume(store: Store, request_id: str, units: int, actual_fen: int,
            note: str = "", operator: str = "business") -> dict[str, Any]:
    """登记实际消耗（原子：成本入账与预留释放同一事件，杜绝半次处置）。

    实际成本来自后续导入/核对的真实用量；预留多于实际的部分立刻释放回可用
    额度，超出预留的部分立即反映为团队已承诺成本并可能触发差额单。
    """
    r = store.snapshot["reservations"].get(request_id)
    if not r or r["status"] != "held":
        raise StoreError(f"请求 {request_id} 没有 held 状态预留，无法登记消耗")
    if units <= 0 or units > r["units"]:
        raise StoreError("消耗批量必须在 1 到预留批量之间")
    if actual_fen < 0:
        raise StoreError("实际消耗金额不能为负；退款请走发票/争议流程")

    payload = {
        "request_id": request_id,
        "team": r["team"],
        "period": r["period"],
        "batch": r["batch"],
        "units": units,
        "reserved_fen": r["reserved_fen"],
        "consumed_fen": actual_fen,
        "note": note,
        "operator": operator,
    }
    event = store.append_event("reservation.consumed", payload)

    # 同一事务内补一笔内部 usage 调整行，保证成本与额度变化同时落账。
    adj_id = f"ADJ-CONSUME-{request_id}"
    store.append_event("adjustment.posted", {
        "line_id": adj_id,
        "provider": "internal",
        "period": r["period"],
        "team": r["team"],
        "request": request_id,
        "attempt_id": None,
        "currency": "CNY",
        "amount": str(Decimal(actual_fen) / Decimal(100)),
        "kind": "usage",
        "confirmed": True,
        "origin": "reservation.consumed",
        "reason": f"批次 {r['batch']} 实际消耗登记",
    })

    ledger = team_ledger(store, r["team"], r["period"])
    if ledger["available_fen"] < 0:
        sf_id = f"SF-CONSUME-{request_id}"
        if sf_id not in store.snapshot["shortfalls"]:
            store.append_event("shortfall.opened", {
                "id": sf_id, "team": r["team"], "period": r["period"],
                "request_id": request_id, "batch": r["batch"],
                "required_fen": actual_fen,
                "available_fen": ledger["available_fen"] + actual_fen,
                "gap_fen": -ledger["available_fen"],
                "reason": "实际消耗超出预留后可用额度为负",
            })
    return event["payload"]


def increase_budget(store: Store, team: str, period: str, delta_fen: int,
                    reason: str, operator: str = "finance") -> dict[str, Any]:
    """财务追加预算，并自动重评估等待清单。"""
    if delta_fen <= 0:
        raise StoreError("追加预算必须为正")
    if store.is_closed(period):
        raise StoreError(f"账期 {period} 已关账，不能追加预算")
    event = store.append_event("budget.increased", {
        "team": team, "period": period, "delta_fen": delta_fen,
        "reason": reason, "operator": operator,
    })
    reevaluated = reevaluate_waiting(store)
    return {"adjustment": event["payload"], "reevaluated": reevaluated}


def reevaluate_waiting(store: Store) -> list[dict[str, Any]]:
    """预算变化后重新评估等待清单；可受理的转 held 并出清单，仍不足的继续等待。

    按等待登记时的首选模型/批量重新受理，不擅自换成更贵的保守口径。
    """
    results = []
    for request_id in list(store.snapshot["waiting"]):
        entry = store.snapshot["waiting"][request_id]
        try:
            outcome = reserve(
                store, request_id,
                model=entry.get("preferred_model"),
                units=entry.get("preferred_units"),
            )
        except StoreError as exc:
            results.append({"request_id": request_id, "still_waiting": True,
                            "reason": str(exc)})
            continue
        if outcome.get("waiting"):
            # 预算仍不足：保留在等待清单，不制造重复事件。
            results.append({"request_id": request_id, "still_waiting": True,
                            "reason": outcome["detail"]["reason"]})
            continue
        store.snapshot["waiting"].pop(request_id, None)
        results.append({"request_id": request_id, **outcome})
    return results
