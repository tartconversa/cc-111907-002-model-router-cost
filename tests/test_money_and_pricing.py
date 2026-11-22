"""金额与价目口径测试：Decimal、汇率、生效价目、最大余数法、调价差额。"""

import unittest
from decimal import Decimal

from _helpers import SettlementTestCase
from settlement.costing import effective_price, estimate_attempt
from settlement.money import allocate_integer, fen_str, fen_with_rounding, to_fen


class FenTests(unittest.TestCase):
    def test_refund_negative_amount_keeps_sign(self):
        fen, _ = fen_with_rounding("-0.01", "7.10")
        self.assertEqual(fen, -7)

    def test_half_up_rounding_and_diff_trace(self):
        fen, diff = fen_with_rounding("0.015", "1")
        self.assertEqual(fen, 2)  # 1.5 分四舍五入
        self.assertEqual(diff, Decimal("-0.5"))

    def test_float_rejected(self):
        with self.assertRaises(TypeError):
            to_fen(0.1, "1")

    def test_allocate_integer_sums_exactly(self):
        for total, weights in [
            (10000, [252, 254]), (100, [1, 1, 1]), (7, [1, 2]),
            (-500, [3, 7]), (1, [333, 333, 334]),
        ]:
            parts = allocate_integer(total, weights)
            self.assertEqual(sum(parts), total, (total, weights))
            if total > 0:
                self.assertTrue(all(p >= 0 for p in parts))

    def test_allocate_zero_weight(self):
        self.assertEqual(allocate_integer(100, [0, 1]), [0, 100])

    def test_fen_str(self):
        self.assertEqual(fen_str(-7), "-0.07")
        self.assertEqual(fen_str(10000), "100.00")


class PriceEffectiveTests(SettlementTestCase):
    def test_price_version_switch_explains_variance(self):
        prices = self.store.table("prices")
        before = effective_price(prices, "A", "small", "2026-11-06T10:00:00Z")
        after = effective_price(prices, "A", "small", "2026-11-07T10:00:00Z")
        self.assertEqual(before["id"], "P1")
        self.assertEqual(after["id"], "P2")

    def test_attempt_cost_matches_hand_calc(self):
        # C4: A/small 528 输入 104 输出，发生在 11-07，适用 P2 (0.03/0.09 CNY)
        call = next(c for c in self.store.table("calls") if c["id"] == "C4")
        item = estimate_attempt(call, self.store.table("prices"),
                                self.store.table("fx"))
        native = Decimal(528) * Decimal("0.03") / 1000 + Decimal(104) * Decimal("0.09") / 1000
        expected = int((native * 100).quantize(Decimal("1")))
        self.assertEqual(item["estimated_fen"], expected)
        self.assertEqual(item["price_id"], "P2")
        self.assertEqual(item["cny_rate"], "1.00")

    def test_usd_attempt_uses_period_fx(self):
        call = next(c for c in self.store.table("calls") if c["id"] == "C0")
        item = estimate_attempt(call, self.store.table("prices"),
                                self.store.table("fx"))
        self.assertEqual(item["currency"], "USD")
        self.assertEqual(item["cny_rate"], "7.10")
        self.assertGreater(item["estimated_fen"], 0)

    def test_no_price_raises(self):
        with self.assertRaises(Exception):
            effective_price(self.store.table("prices"), "X", "y",
                            "2026-11-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
