"""对账、退款缺口、未分配池与争议处置测试。"""

import unittest

from _helpers import SettlementTestCase
from settlement import disputes as dispute_mod
from settlement.ledger import (effective_invoice_lines, reconcile_attempts,
                                refund_breakdown, request_cost_view,
                                unallocated_pool)
from settlement.allocation import confirm_allocation, propose_allocation
from settlement.reports import integrity_check


class ReconciliationTests(SettlementTestCase):
    def test_invoice_revision_replaces_old_value(self):
        rows = reconcile_attempts(self.store)
        r1 = next(r for r in rows if r["request"] == "R1")
        # I1 rev2 = 0.02 CNY -> 2 分；rev1 的 0.03 不再生效
        self.assertEqual(r1["confirmed_fen"], 2)
        lines = [l for l in effective_invoice_lines(self.store) if l["line_id"] == "I1"]
        self.assertEqual(len(lines), 1)
        self.assertEqual(str(lines[0]["amount"]), "0.02")

    def test_failed_attempt_is_billable_and_chain_visible(self):
        view = request_cost_view(self.store, "R0")
        self.assertTrue(view["success"])
        attempts = {a["attempt_id"]: a for a in view["attempts"]}
        self.assertEqual(attempts["A1"]["status"], "failed")
        self.assertEqual(attempts["A2"]["status"], "success")
        # 失败重试同样有预估成本
        self.assertGreater(attempts["A1"]["estimated_fen"], 0)
        self.assertEqual(view["estimated_fen"],
                         attempts["A1"]["estimated_fen"] + attempts["A2"]["estimated_fen"])

    def test_unconfirmed_refund_has_zero_quota_effect(self):
        refunds = refund_breakdown(self.store)
        self.assertEqual(refunds["ops"]["pending_refund_fen"], -7)
        self.assertEqual(refunds["ops"]["refund_credit_fen"], 0)

    def test_confirmed_refund_capped_by_estimate_when_invoice_exceeds(self):
        # R0/A1：预估 6，确认用量 14；退款 7 全部留存，零返还
        with self.store.transaction():
            dispute_mod.verify_invoice(self.store, "I4", "confirmed")
        refunds = refund_breakdown(self.store)
        self.assertEqual(refunds["ops"]["confirmed_refund_fen"], -7)
        self.assertEqual(refunds["ops"]["refund_credit_fen"], 0)
        self.assertEqual(refunds["ops"]["refund_held_fen"], 7)

    def test_refund_fills_gap_when_usage_reversed(self):
        with self.store.transaction():
            dispute_mod.open_dispute(self.store, "I2", "存疑")
            dispute_mod.resolve_dispute(self.store, "I2", "accepted")
            dispute_mod.verify_invoice(self.store, "I4", "confirmed")
        refunds = refund_breakdown(self.store)
        # I2 反转后 A1 无确认账单，退款 7 先补预估 6 的缺口，剩 1 返还
        self.assertEqual(refunds["ops"]["refund_held_fen"], 0)
        self.assertEqual(refunds["ops"]["refund_credit_fen"], 7)

    def test_shared_fee_stays_unallocated_until_confirmed(self):
        pool = unallocated_pool(self.store, "2026-11")
        self.assertEqual(pool["balance_fen"], 10000)
        proposal = propose_allocation(self.store, "2026-11", "I3")
        for plan in proposal["plans"]:
            if plan["feasible"]:
                self.assertEqual(plan["sum_check_fen"], 10000)
        # 仅提议不确认：池子不变
        self.assertEqual(unallocated_pool(self.store, "2026-11")["balance_fen"], 10000)

    def test_confirmed_allocation_drains_pool_and_feeds_teams(self):
        with self.store.transaction():
            confirm_allocation(self.store, "2026-11", "I3", "requests")
        pool = unallocated_pool(self.store, "2026-11")
        self.assertEqual(pool["balance_fen"], 0)
        from settlement.ledger import team_ledger
        care = team_ledger(self.store, "care", "2026-11")
        ops = team_ledger(self.store, "ops", "2026-11")
        self.assertEqual(care["shared_allocated_fen"] + ops["shared_allocated_fen"], 10000)

    def test_dispute_accepted_returns_shared_fee_to_pool(self):
        with self.store.transaction():
            confirm_allocation(self.store, "2026-11", "I3", "usage")
            dispute_mod.open_dispute(self.store, "I3", "存疑")
            dispute_mod.resolve_dispute(self.store, "I3", "accepted")
        # 行被反转：池子重新为 0（行本身不再计入），团队分摊回退
        pool = unallocated_pool(self.store, "2026-11")
        self.assertEqual(pool["balance_fen"], 0)
        from settlement.ledger import team_ledger
        self.assertEqual(team_ledger(self.store, "care", "2026-11")["shared_allocated_fen"], 0)
        # 历史分配事件仍保留可审计
        self.assertTrue(any(
            e["type"] == "allocation.confirmed"
            for e in self._events()))

    def test_dispute_rejected_keeps_invoice_normal(self):
        with self.store.transaction():
            dispute_mod.open_dispute(self.store, "I4", "存疑")
            dispute_mod.resolve_dispute(self.store, "I4", "rejected")
        refunds = refund_breakdown(self.store)
        # 驳回争议：I4 仍是未确认退款，只在 pending 备忘
        self.assertEqual(refunds["ops"]["pending_refund_fen"], -7)
        self.assertEqual(refunds["ops"]["refund_credit_fen"], 0)

    def test_integrity_holds_through_all_above(self):
        with self.store.transaction():
            dispute_mod.verify_invoice(self.store, "I4", "confirmed")
            confirm_allocation(self.store, "2026-11", "I3", "usage")
        self.assertTrue(integrity_check(self.store)["ok"])

    def _events(self):
        for line in self.store.events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                import json
                yield json.loads(line)


if __name__ == "__main__":
    unittest.main()
