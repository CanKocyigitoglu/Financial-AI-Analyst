"""Haber analiz sisteminin tabloları.

Rapor ve uyarı tablolarından (db.py) ayrıdır: buradaki bir hata günlük raporu
veya fiyat uyarılarını etkilemez.
"""
import datetime as dt

import psycopg

import db

NEWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_articles (
    uuid          text        PRIMARY KEY,
    link          text        NOT NULL,
    title         text        NOT NULL,
    publisher     text        NOT NULL,
    published_at  timestamptz NOT NULL,
    related       text[]      NOT NULL,  -- yfinance'in ilişkilendirdiği hisseler
    body          text,                  -- tam metin; alınamadıysa NULL
    first_seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS news_analysis (
    uuid        text        NOT NULL REFERENCES news_articles(uuid),
    symbol      text        NOT NULL,
    relevance   text        NOT NULL,  -- ana konu / yan konu / ilgisiz
    event_type  text,
    direction   text,                  -- olumlu / nötr / olumsuz
    strength    int,                   -- 1 zayıf, 2 orta, 3 güçlü
    horizon     text,                  -- anlık / haftalar / aylar+
    nature      text,                  -- olay / analiz/yorum / spekülasyon
    novelty     text,                  -- yeni / tekrar
    summary     text,
    model       text        NOT NULL,
    analyzed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (uuid, symbol)
);
CREATE TABLE IF NOT EXISTS news_reaction (
    uuid        text             NOT NULL,
    symbol      text             NOT NULL,
    win         text             NOT NULL,  -- 1s (ilk saat), gun (seans sonu), 1g (ertesi gün), 5g (5 işlem günü)
    base_time   timestamptz      NOT NULL,
    end_time    timestamptz      NOT NULL,
    stock_ret   double precision NOT NULL,  -- yüzde
    market_ret  double precision NOT NULL,  -- SPY, yüzde
    sector_etf  text             NOT NULL,
    sector_ret  double precision NOT NULL,  -- yüzde
    measured_at timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (uuid, symbol, win),
    FOREIGN KEY (uuid, symbol) REFERENCES news_analysis (uuid, symbol)
);
"""


def connect() -> psycopg.Connection:
    conn = db.connect()
    conn.execute(NEWS_SCHEMA)
    return conn


def save_article(conn: psycopg.Connection, n: dict) -> None:
    """yf.Search haberini kaydeder; zaten varsa dokunmaz."""
    conn.execute(
        "INSERT INTO news_articles (uuid, link, title, publisher, published_at, related) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (uuid) DO NOTHING",
        (
            n["uuid"], n["link"], n["title"], n["publisher"],
            dt.datetime.fromtimestamp(n["providerPublishTime"], dt.timezone.utc),
            n.get("relatedTickers") or [],
        ),
    )


def set_body(conn: psycopg.Connection, uuid: str, body: str | None) -> None:
    conn.execute("UPDATE news_articles SET body = %s WHERE uuid = %s AND body IS NULL", (body, uuid))


def get_body(conn: psycopg.Connection, uuid: str) -> str | None:
    return conn.execute("SELECT body FROM news_articles WHERE uuid = %s", (uuid,)).fetchone()["body"]


def analyzed(conn: psycopg.Connection, symbol: str, uuids: list[str]) -> set[str]:
    rows = conn.execute(
        "SELECT uuid FROM news_analysis WHERE symbol = %s AND uuid = ANY(%s)", (symbol, uuids)
    ).fetchall()
    return {r["uuid"] for r in rows}


def save_analysis(conn: psycopg.Connection, uuid: str, symbol: str, model: str, a: dict) -> None:
    conn.execute(
        "INSERT INTO news_analysis (uuid, symbol, relevance, event_type, direction, strength, horizon, "
        "nature, novelty, summary, model) VALUES (%(uuid)s, %(symbol)s, %(relevance)s, %(event_type)s, "
        "%(direction)s, %(strength)s, %(horizon)s, %(nature)s, %(novelty)s, %(summary)s, %(model)s) "
        "ON CONFLICT (uuid, symbol) DO NOTHING",
        {
            "event_type": None, "direction": None, "strength": None, "horizon": None,
            "nature": None, "novelty": None, "summary": None,
            **a, "uuid": uuid, "symbol": symbol, "model": model,
        },
    )


def pending_reactions(conn: psycopg.Connection, since: dt.datetime) -> list[dict]:
    """Analiz edilmiş ilgili haberler ve henüz ölçülmemiş pencereleri."""
    return conn.execute(
        "SELECT a.uuid, a.symbol, n.published_at, "
        "       COALESCE(array_agg(r.win) FILTER (WHERE r.win IS NOT NULL), '{}') AS done "
        "FROM news_analysis a JOIN news_articles n USING (uuid) "
        "LEFT JOIN news_reaction r ON r.uuid = a.uuid AND r.symbol = a.symbol "
        "WHERE a.relevance <> 'ilgisiz' AND a.direction IS NOT NULL AND n.published_at >= %s "
        "GROUP BY a.uuid, a.symbol, n.published_at",
        (since,),
    ).fetchall()


def save_reaction(conn: psycopg.Connection, r: dict) -> None:
    conn.execute(
        "INSERT INTO news_reaction (uuid, symbol, win, base_time, end_time, stock_ret, market_ret, "
        "sector_etf, sector_ret) VALUES (%(uuid)s, %(symbol)s, %(win)s, %(base_time)s, %(end_time)s, "
        "%(stock_ret)s, %(market_ret)s, %(sector_etf)s, %(sector_ret)s) ON CONFLICT DO NOTHING",
        r,
    )
