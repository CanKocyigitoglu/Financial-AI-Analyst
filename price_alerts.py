"""Fiyat uyarıları.

Fiyat kontrolü yapay zekâsız yapılır; model sadece bir hisse eşiği
aştığında hareketin olası sebebini haberlerden açıklamak için çağrılır.
Aynı uyarı gün içinde tekrar gönderilmez: hisse %3'ü geçince bir kez,
%6'yı geçince bir kez daha uyarı gelir (yön başına ayrı ayrı). Gönderilen
uyarılar veritabanındaki `alerts` tablosuna kaydedilir.
"""
import datetime as dt
from zoneinfo import ZoneInfo

import yfinance as yf
from agno.agent import Agent
from agno.models.groq import Groq
from pydantic import BaseModel, Field

import db
from daily_report import LOCAL_TZ, NEWS_CANDIDATES, NEWS_PER_TICKER, WATCHLIST, send_telegram

ALERT_THRESHOLD = 3.0  # yüzde
MARKET_TZ = ZoneInfo("America/New_York")


class MoveExplanation(BaseModel):
    news_ids: list[int] = Field(description="Hareketi açıklayan en fazla 3 haberin numarası")
    reason: str = Field(description="Hareketin olası sebebini anlatan 1-2 cümlelik Türkçe açıklama")


explainer = Agent(
    name="Move Explainer",
    model=Groq(id="openai/gpt-oss-120b", max_retries=5),
    instructions=[
        "Sana bir hissenin bugünkü fiyat hareketi ve son haber başlıkları verilecek.",
        "Hareketin olası sebebini sadece bu haberlere dayanarak açıkla; sebep uydurma.",
        "Haberler hareketi açıklamıyorsa bunu açıkça söyle.",
        "Türkçe yaz.",
    ],
    output_schema=MoveExplanation,
)


def new_band(change_pct: float, alerted: dict) -> int:
    """Uyarı gerekiyorsa yeni eşik basamağını (+1 = %3 yukarı, -2 = %6 aşağı...), gerekmiyorsa 0 döner."""
    band = int(change_pct / ALERT_THRESHOLD)
    if band > alerted.get("up", 0) or -band > alerted.get("down", 0):
        return band
    return 0


def build_alert(symbol: str, info: dict) -> tuple[str, str]:
    """Uyarı mesajını ve modelin yazdığı sebebi döner."""
    change = info["regularMarketChangePercent"]
    news = yf.Search(symbol, news_count=NEWS_CANDIDATES).news
    prompt = "\n".join(
        [f"Hisse: {symbol}", f"Bugünkü değişim: {change:+.2f}%", "Haberler:"]
        + [f"{i}. {n['title']} ({n['publisher']})" for i, n in enumerate(news)]
    )
    explanation = explainer.run(prompt).content
    if not isinstance(explanation, MoveExplanation):
        raise ValueError(f"Model beklenen formatta cevap vermedi: {explanation!r}")

    ids = [i for i in dict.fromkeys(explanation.news_ids) if 0 <= i < len(news)][:NEWS_PER_TICKER]
    arrow = "📈" if change > 0 else "📉"
    lines = [
        f"## {arrow} {symbol} {change:+.2f}% · {info['regularMarketPrice']:.2f} {info['currency']}",
        "",
        f"**Olası sebep:** {explanation.reason}",
    ]
    if ids:
        lines += ["", "**Haberler:**"]
    for i in ids:
        n = news[i]
        when = dt.datetime.fromtimestamp(n["providerPublishTime"], LOCAL_TZ).strftime("%d.%m %H:%M")
        lines.append(f"- [{n['title']}]({n['link']}) — {n['publisher']}, {when}")
    return "\n".join(lines), explanation.reason


def main() -> None:
    today = dt.datetime.now(MARKET_TZ).date()  # ABD işlem günü
    conn = db.connect()  # bağlanamazsa uyarı gönderme: hangilerinin gittiğini bilemeyiz, tekrar tekrar gider

    errors = []
    for symbol in WATCHLIST:
        try:
            info = yf.Ticker(symbol).info
            if info.get("marketState") != "REGULAR":
                print(f"{symbol}: piyasa açık değil ({info.get('marketState')}), atlandı.")
                continue
            change = info["regularMarketChangePercent"]
            band = new_band(change, db.alerted_bands(conn, today, symbol))
            print(f"{symbol}: {change:+.2f}%" + (" → uyarı" if band else ""))
            if band:
                text, reason = build_alert(symbol, info)
                send_telegram([text])
                db.record_alert(  # sadece gönderildikten sonra kaydet
                    conn, trade_date=today, symbol=symbol, band=band,
                    change_pct=float(change), price=float(info["regularMarketPrice"]), reason=reason,
                )
        except Exception as e:  # bir hisse hata verirse diğerleri yine kontrol edilsin
            errors.append(f"{symbol}: {e}")

    if errors:  # GitHub çalışmayı başarısız sayıp e-posta göndersin
        raise SystemExit("Hatalar:\n" + "\n".join(errors))


if __name__ == "__main__":
    main()
