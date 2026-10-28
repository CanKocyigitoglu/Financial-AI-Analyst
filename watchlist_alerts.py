"""İzleme listesi uyarıları (modelsiz, ayrı bot).

Favori uyarılarıyla (price_alerts.py) aynı basamak mantığı: hisse %3'ü geçince
bir kez, %6'yı geçince bir kez daha (yön başına). Groq kullanılmaz; mesajda
sadece hisse, günlük değişim ve fiyat olur. Aynı kontrolde eşiği geçen hisseler
tek mesajda toplanır. Gönderilenler ayrı tabloda (`watch_alerts`) tutulur, favori
uyarılarını etkilemez.
"""
import datetime as dt

import yfinance as yf

import db
from daily_report import send_telegram
from price_alerts import MARKET_TZ, new_band

WATCHLIST = [  # 148 ABD hissesi (favoriler de dahil)
    "AAPL", "MSFT", "NVDA", "AVGO", "ADBE", "CRM", "AMD", "CSCO", "ACN", "QCOM",
    "TXN", "IBM", "AMAT", "INTU", "ORCL", "NOW", "PANW", "MU", "UNH", "LLY",
    "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "AMGN", "ISRG", "CVS",
    "SYK", "BMY", "GILD", "REGN", "MDT", "BDX", "JPM", "V", "MA", "BAC",
    "WFC", "MS", "GS", "AXP", "BLK", "C", "SPGI", "CME", "CB", "USB",
    "PNC", "MCO", "SCHW", "MRSH", "AMZN", "TSLA", "HD", "MCD", "NKE", "LOW",
    "SBUX", "TJX", "BKNG", "CMG", "MAR", "F", "GM", "ORLY", "LVS", "TGT",
    "GOOGL", "META", "NFLX", "DIS", "TMUS", "VZ", "T", "CMCSA", "TTWO", "CHTR",
    "FOXA", "OMC", "GE", "CAT", "UNP", "RTX", "HON", "DE", "LMT", "UPS",
    "BA", "MMM", "ADP", "WM", "FDX", "NOC", "EMR", "WMT", "PG", "KO",
    "PEP", "COST", "PM", "MO", "MDLZ", "CL", "KMB", "EL", "STZ", "SYY",
    "XOM", "CVX", "COP", "EOG", "SLB", "MPC", "PSX", "VLO", "DVN", "HAL",
    "OKE", "BKR", "LIN", "SHW", "APD", "ECL", "NUE", "FCX", "PPG", "DOW",
    "NEM", "ALB", "PLD", "AMT", "EQIX", "O", "CCI", "PSA", "SPG", "DLR",
    "SBAC", "WY", "NEE", "SO", "DUK", "D", "SRE", "AEP",
]
LINES_PER_MESSAGE = 30  # Telegram'ın 4096 karakter sınırı

SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_alerts (
    id          bigserial        PRIMARY KEY,
    trade_date  date             NOT NULL,  -- ABD işlem günü
    symbol      text             NOT NULL,
    band        int              NOT NULL,  -- +1 = %3 yukarı, -2 = %6 aşağı ...
    change_pct  double precision NOT NULL,
    price       double precision NOT NULL,
    sent_at     timestamptz      NOT NULL DEFAULT now()
);
"""


def alerted_bands(conn, trade_date: dt.date, symbol: str) -> dict:
    """O gün gönderilmiş en yüksek basamaklar: {"up": 2, "down": 1} gibi."""
    return conn.execute(
        "SELECT COALESCE(MAX(band) FILTER (WHERE band > 0), 0) AS up, "
        "       COALESCE(-MIN(band) FILTER (WHERE band < 0), 0) AS down "
        "FROM watch_alerts WHERE trade_date = %s AND symbol = %s",
        (trade_date, symbol),
    ).fetchone()


def main() -> None:
    today = dt.datetime.now(MARKET_TZ).date()  # ABD işlem günü
    conn = db.connect()  # bağlanamazsa uyarı gönderme: hangilerinin gittiğini bilemeyiz, tekrar tekrar gider
    conn.execute(SCHEMA)

    lines, rows, errors, closed = [], [], [], 0
    for symbol in WATCHLIST:
        try:
            info = yf.Ticker(symbol).info
            if info.get("marketState") != "REGULAR":
                closed += 1
                continue
            change, price = info["regularMarketChangePercent"], info["regularMarketPrice"]
            band = new_band(change, alerted_bands(conn, today, symbol))
            print(f"{symbol}: {change:+.2f}%" + (" → uyarı" if band else ""))
            if band:
                arrow = "📈" if change > 0 else "📉"
                name = info.get("shortName") or symbol
                lines.append(f"{arrow} **{symbol}** ({name}) {change:+.2f}% · {price:.2f} {info['currency']}")
                rows.append((today, symbol, band, float(change), float(price)))
        except Exception as e:  # bir hisse hata verirse diğerleri yine kontrol edilsin
            errors.append(f"{symbol}: {e}")

    if lines:
        chunks = [lines[i:i + LINES_PER_MESSAGE] for i in range(0, len(lines), LINES_PER_MESSAGE)]
        send_telegram(["\n".join(c) for c in chunks], "TELEGRAM_WATCH_TOKEN")
        with conn.cursor() as cur:  # sadece gönderildikten sonra kaydet
            cur.executemany(
                "INSERT INTO watch_alerts (trade_date, symbol, band, change_pct, price) VALUES (%s, %s, %s, %s, %s)",
                rows,
            )
    if closed:
        print(f"{closed} hisse: piyasa açık değil, atlandı.")

    if errors:  # GitHub çalışmayı başarısız sayıp e-posta göndersin
        raise SystemExit("Hatalar:\n" + "\n".join(errors))


if __name__ == "__main__":
    main()
