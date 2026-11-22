"""用量成本估算：按生效价目、账期汇率解释每次可计费尝试。

业务口径
========
* 成功业务量按 **请求** 统计（一个请求无论重试多少次，成功只算一笔业务）。
* 费用按 **实际可计费尝试** 核算：失败尝试只要供应商可计费，同样产生成本。
* 单价取调用 ``occurred`` 时刻已经生效（``start <= occurred``）的最新价目版本，
  并记录命中的价目 id，方便解释 11 月 7 日调价前后的差额。
* 折算走 :mod:`settlement.money` 的统一口径，逐尝试四舍五入到人民币分，舍入差
  逐笔留痕，不做汇总后一次性舍入。
* ``received`` 语义：所有查询都支持 ``as_of``，事实在 ``received`` 之后才可见。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .money import fen_with_rounding
from .timeutil import parse_ts, period_of


class CostingError(Exception):
    pass


def effective_price(prices: list[dict[str, Any]], provider: str, model: str,
                    occurred: str) -> dict[str, Any]:
    """返回调用时刻生效的最新价目版本。"""
    candidates = [
        p for p in prices
        if p["provider"] == provider and p["model"] == model
        and parse_ts(p["start"]) <= parse_ts(occurred)
    ]
    if not candidates:
        raise CostingError(f"{provider}/{model} 在 {occurred} 没有生效价目")
    return max(candidates, key=lambda p: parse_ts(p["start"]))


def fx_rate(fx_rows: list[dict[str, Any]], period: str, currency: str) -> str:
    for f in fx_rows:
        if f["period"] == period and f["currency"] == currency:
            return f["cny_rate"]
    raise CostingError(f"缺少账期 {period} 币种 {currency} 的汇率")


def estimate_attempt(call: dict[str, Any], prices: list[dict[str, Any]],
                     fx_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """单次尝试的预估成本（人民币分，含价目/汇率/舍入差留痕）。"""
    price = effective_price(prices, call["provider"], call["model"], call["occurred"])
    period = period_of(call["occurred"])
    rate = fx_rate(fx_rows, period, price["currency"])
    # 全部金额计算使用 Decimal，禁止 float 承载金额。
    from decimal import Decimal
    native_decimal = (
        Decimal(call["input_tokens"]) * Decimal(price["per_1k_input"])
        + Decimal(call["output_tokens"]) * Decimal(price["per_1k_output"])
    ) / Decimal(1000)
    fen, rounding = fen_with_rounding(native_decimal, rate)
    return {
        "call_id": call["id"],
        "request": call["request"],
        "attempt_id": call["attempt_id"],
        "team": call["team"],
        "provider": call["provider"],
        "model": call["model"],
        "occurred": call["occurred"],
        "status": call["status"],
        "price_id": price["id"],
        "currency": price["currency"],
        "native_amount": str(native_decimal),
        "cny_rate": rate,
        "estimated_fen": fen,
        "rounding_fen": str(rounding),
        "input_tokens": call["input_tokens"],
        "output_tokens": call["output_tokens"],
    }


def visible_calls(store, as_of: str | None = None) -> list[dict[str, Any]]:
    """按 received 可见性过滤调用事实。"""
    calls = store.table("calls")
    if as_of is None:
        return calls
    return [c for c in calls if c["received"] <= as_of]


def all_attempt_costs(store, as_of: str | None = None) -> list[dict[str, Any]]:
    prices = store.table("prices")
    fx_rows = store.table("fx")
    return [estimate_attempt(c, prices, fx_rows) for c in visible_calls(store, as_of)]


def request_chains(store, as_of: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """请求 -> 按 occurred 排序的尝试链（含每笔预估成本）。"""
    chains: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in all_attempt_costs(store, as_of):
        chains[item["request"]].append(item)
    for items in chains.values():
        items.sort(key=lambda x: (parse_ts(x["occurred"]), x["attempt_id"]))
    return dict(chains)


def success_units(store, as_of: str | None = None) -> dict[str, int]:
    """每团队成功业务量（成功请求去重计数）。"""
    succeeded: dict[str, set[str]] = defaultdict(set)
    for call in visible_calls(store, as_of):
        if call["status"] == "success":
            succeeded[call["team"]].add(call["request"])
    return {team: len(reqs) for team, reqs in succeeded.items()}


def team_daily_estimated(store, as_of: str | None = None) -> dict[str, dict[str, int]]:
    """团队 -> 日期(YYYY-MM-DD) -> 预估成本分，用于趋势报表。"""
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in all_attempt_costs(store, as_of):
        out[item["team"]][item["occurred"][:10]] += item["estimated_fen"]
    return {team: dict(days) for team, days in out.items()}
