"""Allocation overrides through the closed desk: reversible per-operation exclusion.

Covers the card's desk clauses end to end over the real WSGI app and the real
nightly pipeline (offline fixtures only):

* «Исключить из отчёта» / «вернуть» a single operation of a *partly* related
  category — with CSRF, an audit entry and an untouched primary source;
* protection when the operation's ownership is not confirmed;
* the versioned allocation policy imported from the desk and applied by the next
  build, with the agreed rent and the bank payment kept apart;
* a frozen report version and the account balances are never rewritten by a
  policy change.
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from randreport.admin import app as admin_app
from randreport.admin import desk, members, reports
from randreport.domain.timeframes import month_period
from randreport.store import connect
from randreport.store import repository as repo
from randreport.web.build import build_and_publish
from tests import fixtures as fx
from tests import fixtures_admin as fa

PERIOD = "2025-07"


class AllocationDeskCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = fa.admin_case_dir(prefix="randreport-alloc-desk-")
        self.db = self.root / "randreport.sqlite"
        self.releases = self.root / "releases"
        self.provider = fa.FakeOidcProvider()
        self.desk = admin_app.AdminDesk(
            db_path=self.db,
            secret=fa.SERVER_SECRET,
            oidc=fa.oidc_client(self.provider),
            release_dir=self.releases,
            secure_cookies=False,
        )
        self.addCleanup(self.desk.close)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.conn = connect(str(self.db))
        self.addCleanup(self.conn.close)
        members.ensure_bootstrap(self.conn, fa.ADMIN_ID)
        self.http = fa.WsgiClient(self.desk)
        fa.login(self.http, self.provider)

    # --- helpers ----------------------------------------------------------- #

    def csrf(self, path: str = f"/admin/money/tx_supplies") -> str:
        page = self.http.request("GET", path)
        self.assertEqual(200, page.status)
        return fa.csrf_from(page)

    def post(self, action: str, form: dict[str, str]):
        return self.http.request("POST", f"/admin/actions/{action}", form=form)

    def rebuild(self) -> tuple[str, dict]:
        """Rebuild the period's release and return ``(release_id, money section)``."""

        built = build_and_publish(self.conn, release_dir=self.releases, periods=[month_period(PERIOD)])
        _label, release_id = built[-1]
        row = repo.release(self.conn, release_id)
        assert row is not None
        return release_id, json.loads(row["payload"])["money"]

    def source_row(self, transaction_id: str) -> dict:
        row = self.conn.execute(
            "SELECT amount_kopecks, deleted, category_id FROM finance_transactions WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        return {key: row[key] for key in row.keys()}


class ReversibleExclusionTests(AllocationDeskCase):
    def test_excluding_one_operation_needs_csrf_and_is_reversible(self):
        before_id, before = self.rebuild()
        before_expense = before["metrics"]["expense_kopecks"]
        token = self.csrf()

        refused = self.post("report/exclude", {"transaction_id": "tx_supplies", "reason": "нет токена"})
        self.assertEqual(403, refused.status)

        done = self.post("report/exclude", {"csrf": token, "transaction_id": "tx_supplies", "reason": "часть не относится"})
        self.assertEqual(303, done.status)
        self.assertTrue(repo.active_report_exclusions(self.conn), "the exclusion must be recorded")

        after_id, after = self.rebuild()
        self.assertNotEqual(before_id, after_id)
        self.assertEqual(before_expense - 250_000, after["metrics"]["expense_kopecks"])

        # the primary source is untouched: the row, its amount and its category
        row = self.source_row("tx_supplies")
        self.assertEqual(250_000, row["amount_kopecks"])
        self.assertEqual(0, row["deleted"])
        self.assertEqual("cat_supplies", row["category_id"])

        # the audit trail names the action and the operation
        actions = [item["action"] for item in self.conn.execute("SELECT action FROM audit_log").fetchall()]
        self.assertIn("report.operation.excluded", actions)

        # and it is reversible: the operation comes back into the report
        card = self.http.request("GET", "/admin/money/tx_supplies")
        self.assertIn("Вернуть в отчёт", card.text)
        restore = self.post("report/restore", {"csrf": self.csrf(), "transaction_id": "tx_supplies"})
        self.assertEqual(303, restore.status)
        restored_id, restored = self.rebuild()
        self.assertFalse(repo.active_report_exclusions(self.conn))
        # the projection is content-addressed: restoring reproduces the original release
        self.assertEqual(before_id, restored_id)
        self.assertEqual(before_expense, restored["metrics"]["expense_kopecks"])

    def test_an_operation_of_an_unconfirmed_category_is_protected(self):
        page = self.http.request("GET", "/admin/money/tx_new_category")
        self.assertEqual(200, page.status)
        denied = self.post(
            "report/exclude",
            {"csrf": fa.csrf_from(page), "transaction_id": "tx_new_category", "reason": "наугад"},
        )
        self.assertEqual(409, denied.status)
        self.assertIn("принадлежность", denied.text)
        self.assertFalse(repo.active_report_exclusions(self.conn))

    def test_the_settings_page_shows_the_rules_and_the_exclusions(self):
        page = self.http.request("GET", "/admin/settings")
        self.assertEqual(200, page.status)
        self.assertIn("Правила распределения подвала", page.text)
        self.assertIn("Исключённых операций нет.", page.text)
        self.assertIn("Импорт политики", page.text)


class OutOfWindowCardTests(AllocationDeskCase):
    """The card and both actions reach an operation older than the list window.

    The public list is bounded by ``desk.MONEY_LIMIT`` operations; the *card* is
    not.  Covered here: an operation that fell out of the window opens, is
    excluded exactly, is reversible, leaves the primary source alone — and a row
    outside the contour stays invisible to a lookup by identifier.
    """

    FILLER = desk.MONEY_LIMIT + 50

    def push_out_of_the_window(self, *transaction_ids: str) -> None:
        """Push the named 2025-07 operations behind ``FILLER`` newer public rows."""

        from datetime import date as _date

        rows = [
            fx.expense(f"tx_filler_{index:04d}", _date(2026, 1, 1), 1_000, "cat_supplies")
            for index in range(self.FILLER)
        ]
        repo.upsert_movements(self.conn, rows)
        listed = {row["transaction_id"] for row in desk.money_rows(self.conn)}
        self.assertEqual(desk.MONEY_LIMIT, len(listed))
        for transaction_id in transaction_ids:
            self.assertNotIn(transaction_id, listed)

    def test_an_operation_outside_the_list_window_opens_and_is_excluded(self):
        self.push_out_of_the_window("tx_supplies")

        # the card is reachable by its own identifier, with the same amount as the source row
        card = desk.money_card(self.conn, "tx_supplies")
        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(250_000, card["amount_kopecks"])
        self.assertEqual("2025-07-08", card["occurred_on"])
        self.assertEqual("cat_supplies", card["category_key"])

        page = self.http.request("GET", "/admin/money/tx_supplies")
        self.assertEqual(200, page.status)
        self.assertIn("Расходники", page.text)

        before_id, before = self.rebuild()
        before_expense = before["metrics"]["expense_kopecks"]

        excluded = self.post(
            "report/exclude",
            {"csrf": fa.csrf_from(page), "transaction_id": "tx_supplies", "reason": "часть не относится"},
        )
        self.assertEqual(303, excluded.status)
        self.assertEqual(("tx_supplies",), repo.active_report_exclusions(self.conn))

        after_id, after = self.rebuild()
        self.assertNotEqual(before_id, after_id)
        self.assertEqual(before_expense - 250_000, after["metrics"]["expense_kopecks"])
        self.assertEqual(1, after["metrics"]["allocation_excluded_operations"])

        # the primary source is untouched
        row = self.source_row("tx_supplies")
        self.assertEqual(250_000, row["amount_kopecks"])
        self.assertEqual(0, row["deleted"])
        self.assertEqual("cat_supplies", row["category_id"])

        # and it is reversible: «Вернуть в отчёт» restores the exact sum
        card_after = self.http.request("GET", "/admin/money/tx_supplies")
        self.assertEqual(200, card_after.status)
        self.assertIn("Вернуть в отчёт", card_after.text)
        restore = self.post("report/restore", {"csrf": fa.csrf_from(card_after), "transaction_id": "tx_supplies"})
        self.assertEqual(303, restore.status)
        restored_id, restored = self.rebuild()
        self.assertEqual(before_id, restored_id)
        self.assertEqual(before_expense, restored["metrics"]["expense_kopecks"])
        self.assertFalse(repo.active_report_exclusions(self.conn))

    def test_an_out_of_window_operation_of_an_unconfirmed_category_is_refused(self):
        # the 409 protection is about ownership, not about the list window
        self.push_out_of_the_window("tx_new_category")
        card = self.http.request("GET", "/admin/money/tx_new_category")
        self.assertEqual(200, card.status)
        refused = self.post(
            "report/exclude",
            {"csrf": fa.csrf_from(card), "transaction_id": "tx_new_category", "reason": "наугад"},
        )
        self.assertEqual(409, refused.status)
        self.assertIn("принадлежность", refused.text)
        self.assertFalse(repo.active_report_exclusions(self.conn))

    def test_a_lookup_by_identifier_never_widens_the_contour(self):
        # a private and a quarantined account, and an idle id: all stay invisible
        for transaction_id in ("tx_private", "tx_mixed", "tx_not_there"):
            with self.subTest(transaction_id=transaction_id):
                self.assertIsNone(desk.money_card(self.conn, transaction_id))
                page = self.http.request("GET", f"/admin/money/{transaction_id}")
                self.assertEqual(404, page.status)


class PolicyImportTests(AllocationDeskCase):
    POLICY = {
        "version": "owner-alloc-1",
        "share_rules": {"cat_utilities": {"numerator": 1, "denominator": 3, "rounding": "half_up"}},
        "rent": {"monthly_kopecks": 9_000_000},
        "rent_category_ids": ["cat_rent"],
    }

    def test_an_imported_policy_is_applied_by_the_next_build(self):
        self.rebuild()  # the baseline live release
        imported = self.post(
            "allocation/import",
            {"csrf": self.csrf("/admin/settings"), "allocation_json": json.dumps(self.POLICY)},
        )
        self.assertEqual(303, imported.status)

        _release_id, money = self.rebuild()
        metrics = money["metrics"]
        self.assertEqual(9_000_000, metrics["rent_computed_kopecks"])
        # tx_rent (4 000 000) and tx_extra_rent (999 000) are the actual payments
        self.assertEqual(4_999_000, metrics["rent_bank_kopecks"])
        self.assertTrue(metrics["rent_differs"])
        self.assertTrue(metrics["allocation_applied"])
        # utilities 900 000 -> one third = 300 000 exactly
        self.assertEqual(900_000, metrics["shared_expenses_before_kopecks"])
        self.assertEqual(300_000, metrics["shared_expenses_after_kopecks"])

    def test_a_broken_policy_is_refused_with_a_human_sentence(self):
        refused = self.post(
            "allocation/import",
            {"csrf": self.csrf("/admin/settings"), "allocation_json": json.dumps({"rent": {"monthly_kopecks": 1.5}})},
        )
        self.assertEqual(409, refused.status)
        self.assertIn("копеек", refused.text)

    def test_a_policy_change_never_rewrites_a_frozen_report_or_a_balance(self):
        self.rebuild()
        reports.close_period(self.conn, label=PERIOD, actor=fa.ADMIN_ID, reason="закрытие перед правкой")
        frozen = repo.latest_report_snapshot(self.conn, PERIOD)
        self.assertIsNotNone(frozen)
        balances_before = [b.balance_kopecks for b in repo.load_balances(self.conn)]

        self.post(
            "allocation/import",
            {"csrf": self.csrf("/admin/settings"), "allocation_json": json.dumps(self.POLICY)},
        )
        self.rebuild()

        again = repo.latest_report_snapshot(self.conn, PERIOD)
        assert again is not None and frozen is not None
        self.assertEqual(frozen["payload"], again["payload"])
        self.assertEqual(1, len(repo.report_versions(self.conn, PERIOD)))
        self.assertEqual(balances_before, [b.balance_kopecks for b in repo.load_balances(self.conn)])


if __name__ == "__main__":
    unittest.main()
