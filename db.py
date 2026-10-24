"""Veritabanı (Neon Postgres): rapor geçmişi ve gönderilen uyarılar."""
import datetime as dt
import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    report_date  date             NOT NULL,
    symbol       text             NOT NULL,
    price        double precision NOT NULL,
    currency     text             NOT NULL,
    change_pct   double precision NOT NULL,
    strong_buy   int, buy int, hold int, sell int, strong_sell int,
    target_mean  double precision,
    target_low   double precision,
    target_high  double precision,
    sentiment    text             NOT NULL,
    summary      text             NOT NULL,
    news         jsonb            NOT NULL,  -- [{title, link, publisher, published_at}]
    created_at   timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (report_date, symbol)
);
CREATE TABLE IF NOT EXISTS alerts (
    id          bigserial        PRIMARY KEY,
    trade_date  date             NOT NULL,  -- ABD işlem günü
    symbol      text             NOT NULL,
    band        int              NOT NULL,  -- +1 = %3 yukarı, -2 = %6 aşağı ...
    change_pct  double precision NOT NULL,
    price       double precision NOT NULL,
    reason      text             NOT NULL,
    sent_at     timestamptz      NOT NULL DEFAULT now()
);
"""

REPORT_COLUMNS = (
    "report_date", "symbol", "price", "currency", "change_pct",
    "strong_buy", "buy", "hold", "sell", "strong_sell",
    "target_mean", "target_low", "target_high", "sentiment", "summary", "news",
)


def connect() -> psycopg.Connection:
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True, connect_timeout=20)
    conn.execute(SCHEMA)  # tablolar yoksa oluşturur
    return conn


def save_report(conn: psycopg.Connection, row: dict) -> None:
    """Aynı gün tekrar çalışırsa o günün satırının üzerine yazar."""
    cols = ", ".join(REPORT_COLUMNS)
    values = ", ".join(f"%({c})s" for c in REPORT_COLUMNS)
    excluded = ", ".join(f"EXCLUDED.{c}" for c in REPORT_COLUMNS)
    conn.execute(
        f"INSERT INTO reports ({cols}) VALUES ({values}) "
        f"ON CONFLICT (report_date, symbol) DO UPDATE SET ({cols}) = ({excluded})",
        {**row, "news": Jsonb(row["news"])},
    )


def last_report(conn: psycopg.Connection, symbol: str, before: dt.date) -> dict | None:
    return conn.execute(
        "SELECT * FROM reports WHERE symbol = %s AND report_date < %s ORDER BY report_date DESC LIMIT 1",
        (symbol, before),
    ).fetchone()


def alerted_bands(conn: psycopg.Connection, trade_date: dt.date, symbol: str) -> dict:
    """O gün gönderilmiş en yüksek basamaklar: {"up": 2, "down": 1} gibi."""
    return conn.execute(
        "SELECT COALESCE(MAX(band) FILTER (WHERE band > 0), 0) AS up, "
        "       COALESCE(-MIN(band) FILTER (WHERE band < 0), 0) AS down "
        "FROM alerts WHERE trade_date = %s AND symbol = %s",
        (trade_date, symbol),
    ).fetchone()


def record_alert(conn: psycopg.Connection, **alert) -> None:
    conn.execute(
        "INSERT INTO alerts (trade_date, symbol, band, change_pct, price, reason) "
        "VALUES (%(trade_date)s, %(symbol)s, %(band)s, %(change_pct)s, %(price)s, %(reason)s)",
        alert,
    )
