"""发票核实、争议处置、账期关账与关账后更正。

处置去向
========
* 供应商未确认行（如“供应商说已退款”的 I4）可由财务 **核实确认/驳回**：
  核实确认才进入已确认口径并触发退款缺口逻辑；驳回则永不释放额度。
* 任意发票/调整行可开争议（open）。争议 **驳回** 行恢复正常记账；争议 **接受**
  则反转该行（usage 费用冲回、refund 不再返还、shared 金额退回未分配池），
  并影响团队后续可用额度。
* 已关账月份不改写：认可的更正以内部调整行进入指定的 **开放账期**，调整行
  保留 ``origin_line`` 与原账期的联系，原账与调整可互相追溯。
"""

from __future__ import annotations

from typing import Any

from .ledger import effective_invoice_lines
from .money import to_decimal
from .store import Store, StoreError


def _find_line(store: Store, line_id: str) -> dict[str, Any]:
    for line in effective_invoice_lines(store):
        if line["line_id"] == line_id:
            return line
    raise StoreError(f"未知发票/调整行 {line_id}")


def verify_invoice(store: Store, line_id: str, decision: str,
                   note: str = "", operator: str = "finance") -> dict[str, Any]:
    """对未确认发票行做人工核实：confirmed / rejected。"""
    if decision not in ("confirmed", "rejected"):
        raise StoreError("decision 必须是 confirmed 或 rejected")
    rows = {r["id"]: r for r in store.table("invoices")}
    if line_id not in rows:
        raise StoreError(f"{line_id} 不是来源发票行；内部调整行不需要核实")
    latest = max(
        (r for r in store.table("invoices") if r["id"] == line_id),
        key=lambda r: r["revision"],
    )
    if latest["confirmed"]:
        raise StoreError(f"{line_id} 供应商已确认，无需人工核实")
    payload = {"line_id": line_id, "revision": latest["revision"],
               "decision": decision, "note": note, "operator": operator}
    return store.append_event("invoice.verified", payload)["payload"]


def open_dispute(store: Store, line_id: str, reason: str,
                 operator: str = "finance") -> dict[str, Any]:
    line = _find_line(store, line_id)
    ref = f"{'adjustment' if line['source'] == 'manual' else 'invoice'}:{line_id}"
    if store.snapshot["disputes"].get(ref, {}).get("status") == "open":
        raise StoreError(f"{line_id} 已存在未结争议")
    payload = {"ref": ref, "line_id": line_id, "reason": reason,
               "operator": operator}
    return store.append_event("dispute.opened", payload)["payload"]


def resolve_dispute(store: Store, line_id: str, verdict: str,
                    note: str = "", operator: str = "finance") -> dict[str, Any]:
    """争议结论：rejected（驳回，恢复记账）或 accepted（接受，反转该行）。"""
    if verdict not in ("accepted", "rejected"):
        raise StoreError("verdict 必须是 accepted 或 rejected")
    line = _find_line(store, line_id)
    ref = f"{'adjustment' if line['source'] == 'manual' else 'invoice'}:{line_id}"
    dispute = store.snapshot["disputes"].get(ref)
    if not dispute or dispute["status"] != "open":
        raise StoreError(f"{line_id} 没有 open 状态争议")
    return store.append_event("dispute.resolved", {
        "ref": ref, "line_id": line_id, "verdict": verdict,
        "note": note, "operator": operator,
    })["payload"]


def close_period(store: Store, period: str, force: bool = False,
                 operator: str = "finance") -> dict[str, Any]:
    """关账：检查未结事项；已关账月份不可再改，只能向开放账期另记调整。"""
    if store.is_closed(period):
        raise StoreError(f"账期 {period} 已关账")
    warnings = []
    open_disputes = [d for d in store.snapshot["disputes"].values()
                     if d["status"] == "open"]
    if open_disputes:
        warnings.append(f"存在 {len(open_disputes)} 笔未结争议")
    open_shortfalls = [s for s in store.snapshot["shortfalls"].values()
                       if s["status"] == "open" and s["period"] == period]
    if open_shortfalls:
        warnings.append(f"存在 {len(open_shortfalls)} 笔未处置差额单")
    pending_lines = [ln for ln in effective_invoice_lines(store)
                     if ln["period"] == period and not ln["confirmed"]
                     and not ln["rejected"]]
    if pending_lines:
        warnings.append(f"存在 {len(pending_lines)} 行待核实发票（如供应商口头退款）")
    from .ledger import unallocated_pool
    pool = unallocated_pool(store, period)
    if pool["balance_fen"]:
        warnings.append(
            f"未分配池仍有余额 {pool['balance_fen']} 分，未确认分配口径")
    if warnings and not force:
        raise StoreError("关账被阻止：" + "；".join(warnings) + "（可用 force 强制）")
    event = store.append_event("period.closed",
                               {"period": period, "operator": operator,
                                "warnings": warnings})
    return {"closed": event["payload"], "warnings": warnings}


def post_correction(store: Store, origin_line_id: str, target_period: str,
                    amount_cny: str, kind: str, reason: str,
                    request: str | None = None, attempt_id: str | None = None,
                    team: str | None = None, operator: str = "finance"
                    ) -> dict[str, Any]:
    """关账后更正：原账不动，调整金额进入开放账期并保留原账联系。

    ``amount_cny`` 为人民币元的十进制字符串，正负号语义与发票一致
    （负=冲减/退款）。目标账期必须开放。
    """
    if store.is_closed(target_period):
        raise StoreError(f"目标账期 {target_period} 已关账，不能承接更正")
    origin = _find_line(store, origin_line_id)
    if origin["period"] == target_period:
        raise StoreError("同账期更正应直接使用发票修订版本或争议流程，无需调整行")
    line_id = f"ADJ-{origin_line_id}-{target_period}"
    if any(x["line_id"] == line_id for x in store.snapshot["manual_invoice_lines"]):
        raise StoreError(f"更正行 {line_id} 已存在，避免重复记账")
    amount = to_decimal(amount_cny)
    payload = {
        "line_id": line_id,
        "provider": origin["provider"],
        "period": target_period,
        "team": team or origin.get("team"),
        "request": request if request is not None else origin.get("request"),
        "attempt_id": attempt_id if attempt_id is not None else origin.get("attempt_id"),
        "currency": "CNY",
        "amount": str(amount),
        "kind": kind,
        "confirmed": True,
        "origin_line": origin_line_id,
        "origin_period": origin["period"],
        "origin": "period.correction",
        "reason": reason,
        "operator": operator,
    }
    event = store.append_event("adjustment.posted", payload)

    # 更正使已确认额度不再充足时：在执行业务保留（不篡改其获批历史），
    # 对后续可延迟请求由额度评估自然受限，同时生成财务差额单。
    from .ledger import team_ledger
    target_team = payload["team"]
    if target_team:
        ledger = team_ledger(store, target_team, target_period)
        if ledger["available_fen"] < 0:
            sf_id = f"SF-CORR-{line_id}"
            if sf_id not in store.snapshot["shortfalls"]:
                store.append_event("shortfall.opened", {
                    "id": sf_id, "team": target_team, "period": target_period,
                    "request_id": None, "batch": None,
                    "required_fen": 0,
                    "available_fen": ledger["available_fen"],
                    "gap_fen": -ledger["available_fen"],
                    "reason": f"更正 {origin_line_id} 使可用额度为负",
                })
    return event["payload"]


def resolve_shortfall(store: Store, shortfall_id: str, reason: str,
                      operator: str = "finance") -> dict[str, Any]:
    sf = store.snapshot["shortfalls"].get(shortfall_id)
    if not sf:
        raise StoreError(f"未知差额单 {shortfall_id}")
    if sf["status"] != "open":
        raise StoreError(f"差额单 {shortfall_id} 已处置")
    return store.append_event("shortfall.resolved",
                              {"id": shortfall_id, "reason": reason,
                               "operator": operator})["payload"]
