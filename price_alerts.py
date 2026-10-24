"""Fiyat uyarıları.

Fiyat kontrolü yapay zekâsız yapılır; model sadece bir hisse eşiği
aştığında hareketin olası sebebini haberlerden açıklamak için çağrılır.
Aynı uyarı gün içinde tekrar gönderilmez: hisse %3'ü geçince bir kez,
%6'yı geçince bir kez daha uyarı gelir (yön başına ayrı ayrı).
"""
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf
from agno.agent import Agent
from agno.models.groq import Groq
from pydantic import BaseModel, Field

from daily_report import LOCAL_TZ, NEWS_CANDIDATES, NEWS_PER_TICKER, WATCHLIST, send_telegram

ALERT_THRESHOLD = 3.0  # yüzde
STATE_FILE = Path(__file__).parent / "alert_state.json"
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


def build_alert(symbol: str, info: dict) -> str:
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
    return "\n".join(lines)


def main() -> None:
    today = dt.datetime.now(MARKET_TZ).date().isoformat()  # ABD işlem günü
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if state.get("date") != today:
        state = {"date": today, "alerted": {}}

    errors = []
    for symbol in WATCHLIST:
        try:
            info = yf.Ticker(symbol).info
            if info.get("marketState") != "REGULAR":
                print(f"{symbol}: piyasa açık değil ({info.get('marketState')}), atlandı.")
                continue
            change = info["regularMarketChangePercent"]
            alerted = state["alerted"].setdefault(symbol, {"up": 0, "down": 0})
            band = new_band(change, alerted)
            print(f"{symbol}: {change:+.2f}%" + (" → uyarı" if band else ""))
            if band:
                send_telegram([build_alert(symbol, info)])
                alerted["up" if band > 0 else "down"] = abs(band)  # sadece gönderildikten sonra işaretle
        except Exception as e:  # bir hisse hata verirse diğerleri yine kontrol edilsin
            errors.append(f"{symbol}: {e}")

    STATE_FILE.write_text(json.dumps(state))
    if errors:  # GitHub çalışmayı başarısız sayıp e-posta göndersin
        raise SystemExit("Hatalar:\n" + "\n".join(errors))


if __name__ == "__main__":
    main()
