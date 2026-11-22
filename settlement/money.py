"""金额工具：全应用内部以人民币整数分记账。

设计约定
========
* 来源账单的原币金额是十进制字符串，先转 ``Decimal`` 精确计算，再按账期汇率
  折算人民币，最后用四舍五入（ROUND_HALF_UP）落到整数分。
* 舍入产生的差额不丢弃：调用方可以用 :func:`fen_with_rounding` 同时拿到整数分
  与舍入差（人民币分的小数部分），写入成本明细的 ``rounding_fen`` 字段，使每
  一笔折算都可解释。
* 不允许用浮点（float）承载金额。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext, getcontext

# 金额计算给足精度；输入本身很短，40 位足够覆盖本应用全部场景。
getcontext().prec = 40

FEN_QUANTUM = Decimal("1")
_CENT = Decimal("100")


def to_decimal(value: str | int | Decimal) -> Decimal:
    """把来源字段安全转为 Decimal，拒绝 float，避免二进制误差。"""
    if isinstance(value, float):
        raise TypeError("金额不允许使用 float，请使用十进制字符串或 int")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def fen_with_rounding(
    amount_native: str | Decimal,
    cny_rate: str | Decimal,
) -> tuple[int, Decimal]:
    """原币金额按账期汇率折算为人民币分。

    返回 ``(整数分, 舍入差)``。舍入差 = 未舍入的人民币分 - 整数分，保留 6 位
    小数，正数表示被向下舍掉、负数表示被向上进位。
    """
    with localcontext() as ctx:
        ctx.prec = 40
        raw = (to_decimal(amount_native) * to_decimal(cny_rate) * _CENT)
        rounded = raw.quantize(FEN_QUANTUM, rounding=ROUND_HALF_UP)
        diff = (raw - rounded).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return int(rounded), diff


def to_fen(amount_native: str | Decimal, cny_rate: str | Decimal) -> int:
    """折算为人民币整数分（不关心舍入差时使用）。"""
    fen, _ = fen_with_rounding(amount_native, cny_rate)
    return fen


def fen_str(fen: int) -> str:
    """整数分格式化为人民币元字符串，仅用于展示。"""
    sign = "-" if fen < 0 else ""
    fen = abs(fen)
    return f"{sign}{fen // 100}.{fen % 100:02d}"


def allocate_integer(total_fen: int, weights: list[int]) -> list[int]:
    """按整数权重把一笔整数分拆成整数分，使用最大余数法。

    保证各份之和恒等于 ``total_fen``（含负数：负的共享费用按相同口径分摊），
    不会出现“均摊后对不上”的差异。权重之和必须为正；零权重团队分得 0。

    与业务上的“未分配”概念不同：本函数用于已经确认分配口径之后的数学拆分，
    无法唯一归属的金额在调用本函数之前就应留在未分配池，而不是在这里强行摊。
    """
    if total_fen == 0 or not weights:
        return [0] * len(weights)
    weight_sum = sum(weights)
    if weight_sum <= 0:
        raise ValueError("权重之和必须为正")

    sign = -1 if total_fen < 0 else 1
    amount = abs(total_fen)
    raw = [amount * w / weight_sum for w in weights]
    floors = [int(x) for x in raw]
    remainder = amount - sum(floors)
    # 小数部分最大的先拿余数；同余数时按列表顺序，保证结果确定。
    order = sorted(range(len(weights)), key=lambda i: (-(raw[i] - floors[i]), i))
    parts = floors[:]
    for k in range(remainder):
        parts[order[k]] += 1
    return [sign * p for p in parts]
