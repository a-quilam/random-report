"""Build a synthetic *dev/staging* public release from offline fixtures.

This is a verification aid, not a data source. It runs the real nightly cycle
with the offline fake transports of ``tests.fixtures_ingest`` and publishes the
resulting release into a directory you choose, so the public pages can be
exercised end to end while no real credentials exist (docs/REQUIREMENTS.md
section 13: synthetic data is fine in dev/staging, never shown to visitors as if
it were real).

    python3 -m tests.dev_release /tmp/randrelease [--period 2025-07]
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python3 tests/dev_release.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from randreport.store import apply_migrations, connect  # noqa: E402
from tests import fixtures as fx  # noqa: E402
from tests import fixtures_ingest as fi  # noqa: E402

DONOR_NEWS = [
    fi.news_item(
        message_ids=("700",),
        category="ремонт",
        title="Отремонтировали вход в подвал",
        summary="Появились новые двери и свет в коридоре.",
    ),
    fi.news_item(
        message_ids=("701", "702"),
        classification="announcement",
        category=None,
        title="Анонс концерта",
        summary="Скоро концерт.",
    ),
]


def movements() -> list[dict]:
    return [
        fi.zen_row("tx_rent", "2025-07-03", 4_000_000, tag=["cat_rent"]),
        fi.zen_row("tx_utilities", "2025-07-05", 900_000, tag=["cat_utilities"]),
        fi.zen_row("tx_supplies", "2025-07-08", 250_000, tag=["cat_supplies"]),
        fi.zen_row("tx_event_costs", "2025-07-12", 400_000, tag=["cat_event_costs"]),
        fi.zen_row("tx_commission", "2025-07-04", 9_900, tag=["cat_commission"]),
        fi.zen_row("tx_donation", "2025-07-06", 1_200_000, direction="in", tag=["cat_donations"]),
        fi.zen_row("tx_event_share", "2025-07-18", 600_000, direction="in", tag=["cat_event_share"]),
        fi.zen_row("tx_warehouse", "2025-07-20", 150_000, direction="in", tag=["cat_warehouse"]),
        fi.zen_row("tx_subsidy", "2025-07-25", 800_000, direction="in", tag=["cat_commune_subsidy"]),
        fi.zen_row("tx_refund", "2025-07-22", 50_000, direction="in", tag=["cat_purchase_refund"]),
        fi.zen_row("tx_new_category", "2025-07-24", 300_000, direction="in", tag=["cat_brand_new"]),
        fi.zen_row("tx_balance_jump", "2025-07-26", 120_000, direction="in", tag=["cat_balance_adjustment"]),
        fi.zen_transfer("tx_move", "2025-07-27", 700_000, source="acc_card", target="acc_cash"),
        fi.zen_row("tx_private", "2025-07-28", 500_000, account="acc_personal", tag=["cat_rent"]),
        fi.zen_row("tx_mixed", "2025-07-29", 250_000, account="acc_mixed", tag=["cat_utilities"]),
        fi.zen_row("tx_extra_rent", "2025-07-30", 999_000, tag=["cat_rent"]),
    ]


def calendar() -> list[dict]:
    return [
        fi.teamup_event("ev_open", "2025-07-05T19:00:00+03:00", "2025-07-05T22:00:00+03:00", title="Открытый концерт"),
        fi.teamup_event("ev_reg", "2025-07-12T18:30:00+03:00", "2025-07-12T21:00:00+03:00", subcalendars=["sub_registration"], title="Мастер-класс по записи"),
        fi.teamup_event("ev_closed", "2025-07-15T11:15:00+03:00", "2025-07-15T13:45:00+03:00", subcalendars=["sub_closed"], title="Закрытая встреча коммунаров"),
        fi.teamup_event("ev_internal", "2025-07-16T12:00:00+03:00", "2025-07-16T13:00:00+03:00", subcalendars=["sub_internal"], title="Внутреннее собрание"),
        fi.teamup_event("ev_prep", "2025-07-18T10:00:00+03:00", "2025-07-18T12:00:00+03:00", subcalendars=["sub_preparation"], title="Репетиция"),
        fi.teamup_event("ev_unknown", "2025-07-19T14:00:00+03:00", "2025-07-19T15:00:00+03:00", subcalendars=["sub_lecture"], title="Лекция"),
    ]


def build(release_dir: str, *, period: str = "2025-07", db_path: str | None = None) -> dict:
    conn = connect(db_path) if db_path else connect()
    try:
        apply_migrations(conn)

        zen, _zen_transport = fi.zenmoney(rows=movements())
        cal, _cal_transport = fi.teamup(pages=[{"events": calendar(), "pages": {"next": None}}])
        tg, _tg_transport = fi.telegram(
            posts=[fi.post("700", "Мы отремонтировали вход в подвал."), fi.post("701", "Анонс концерта")], members=4128
        )
        ext = fi.extractor(conn, script=[fi.news_response(DONOR_NEWS)])

        report = fi.run_once(
            conn,
            [
                fi.zenmoney_adapter(zen),
                fi.teamup_adapter(cal),
                fi.telegram_adapter(tg),
                fi.news_adapter(ext),
            ],
            release_builder=fi.builder(period),
            publish_dir=release_dir,
            lease=str(Path(release_dir) / "nightly.lock"),
            publish=True,
        )
        return {
            "release_id": report.release_id,
            "published": report.published,
            "completeness": report.completeness,
            "sources": {name: out.status for name, out in report.sources.items()},
        }
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="tests.dev_release")
    parser.add_argument("release_dir")
    parser.add_argument("--period", default="2025-07")
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)
    Path(args.release_dir).mkdir(parents=True, exist_ok=True)
    print(json.dumps(build(args.release_dir, period=args.period, db_path=args.db), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
