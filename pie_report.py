"""Pie raporu (modelsiz, ayrı bot): borsa açıkken her saat başı her pie için ayrı mesaj.

Pie'lar yerelde `pielarim.json`'da durur (repoya girmez). Dosya değişince
`python pie_report.py upload` ile veritabanına yüklenir; bulut oradan okur.
Mesajda her hisse için alıştan bu yana (ortalama alış fiyatına göre) ve bugünkü
değişim, pie için toplam değer, bugünkü kazanç ve yatırılan paraya göre kâr/zarar olur.
"""
import datetime as dt
import json
import sys
from collections import defaultdict

import yfinance as yf
from psycopg.types.json import Jsonb

import db
from daily_report import LOCAL_TZ, send_telegram
from price_alerts import MARKET_TZ

SCHEMA = """
CREATE TABLE IF NOT EXISTS pies (
    id          int         PRIMARY KEY,
    data        jsonb       NOT NULL,  -- pielarim.json'daki pie, olduğu gibi
    uploaded_at timestamptz NOT NULL DEFAULT now()
);
"""


def holdings(pie: dict) -> dict:
    """Hisse başına alımlar: {"NVDA": [(adet, alış fiyatı, alış tarihi), ...]}."""
    out = defaultdict(list)
    for stock in pie["stocks"]:
        if stock["sales"]:  # formatı henüz belli değil; yanlış hesaplamaktansa dur
            raise ValueError(f"{pie['name']}: satış kayıtları henüz desteklenmiyor")
        for p in stock["purchases"]:
            out[p["stock_symbol"]].append(
                (p["quantity"], p["purchase_price"], dt.date.fromisoformat(p["purchase_date"]))
            )
    return out


def build_message(pie: dict, quotes: dict, today: dt.date, now: dt.datetime) -> str:
    cur = pie["currency"]
    rows, value, cost, day_gain = [], 0.0, 0.0, 0.0
    for symbol, buys in holdings(pie).items():
        q = quotes[symbol]
        price = q["regularMarketPrice"]
        qty = sum(n for n, _, _ in buys)
        sym_cost = sum(n * pp for n, pp, _ in buys)
        value += qty * price
        cost += sym_cost
        # bugün alınan payın bugünkü kazancı dünkü kapanıştan değil alış fiyatından başlar
        day_gain += sum(n * (price - (pp if d == today else q["regularMarketPreviousClose"])) for n, pp, d in buys)
        rows.append((qty * price / sym_cost * 100 - 100, symbol, q["regularMarketChangePercent"]))

    invested = sum(d["amount"] for d in pie["deposits"]) - sum(w["amount"] for w in pie["withdrawals"])
    total = value + invested - cost  # yatırılmamış nakit de dahil
    profit = total - invested
    lines = [f"## 🥧 {pie['name']} · {now:%d.%m %H:%M}", ""]
    for ret, symbol, day in sorted(rows, reverse=True):
        lines.append(f"{'📈' if ret >= 0 else '📉'} **{symbol}** {ret:+.2f}% (bugün {day:+.2f}%)")
    lines += [
        "",
        f"**Toplam:** {total:.2f} {cur} (bugün {day_gain:+.2f} {cur})",
        f"**Yatırılan:** {invested:.2f} {cur} → {profit:+.2f} {cur} ({profit / invested * 100:+.2f}%)",
    ]
    return "\n".join(lines)


def upload(path: str = "pielarim.json") -> None:
    """pielarim.json'ı veritabanına yükler (eskilerin yerine geçer)."""
    with open(path, encoding="utf-8") as f:
        pies = json.load(f)
    for pie in pies:
        holdings(pie)  # format sorunu varsa yüklemeden dur
    conn = db.connect()
    conn.execute(SCHEMA)
    with conn.transaction():
        conn.execute("DELETE FROM pies")
        for pie in pies:
            conn.execute("INSERT INTO pies (id, data) VALUES (%s, %s)", (pie["id"], Jsonb(pie)))
    print(f"{len(pies)} pie yüklendi: " + ", ".join(p["name"] for p in pies))


def main() -> None:
    conn = db.connect()
    conn.execute(SCHEMA)
    pies = [r["data"] for r in conn.execute("SELECT data FROM pies ORDER BY id")]
    if not pies:
        raise SystemExit("Veritabanında pie yok; önce: python pie_report.py upload")

    quotes, errors = {}, []
    for symbol in sorted({s for pie in pies for s in holdings(pie)}):
        try:
            quotes[symbol] = yf.Ticker(symbol).info
        except Exception as e:
            errors.append(f"{symbol}: {e}")

    today = dt.datetime.now(MARKET_TZ).date()  # ABD işlem günü
    if quotes and not any(
        dt.datetime.fromtimestamp(q["regularMarketTime"], MARKET_TZ).date() == today for q in quotes.values()
    ):
        print("Bugün ABD borsası açılmadı (hafta sonu/tatil); mesaj gönderilmedi.")
        return

    now = dt.datetime.now(LOCAL_TZ)
    sections = []
    for pie in pies:
        try:
            sections.append(build_message(pie, quotes, today, now))
        except Exception as e:  # bir pie hesaplanamazsa diğerleri yine gitsin
            errors.append(f"{pie['name']}: {e!r}")
    send_telegram(sections, "TELEGRAM_PIE_TOKEN")  # her pie ayrı mesaj

    if errors:  # GitHub çalışmayı başarısız sayıp e-posta göndersin
        raise SystemExit("Hatalar:\n" + "\n".join(errors))


if __name__ == "__main__":
    if sys.argv[1:2] == ["upload"]:
        upload(*sys.argv[2:])
    else:
        main()
