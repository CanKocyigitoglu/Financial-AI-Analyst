"""Günlük hisse raporu.

Rakamlar ve haber linkleri doğrudan yfinance'ten gelir; model sadece
en ilgili haberleri seçer ve kısa bir yorum yazar. Böylece model rakam
veya link uyduramaz.
"""
import datetime as dt
import html
import os
import re
import warnings
from pathlib import Path
from typing import Literal

import httpx
import yfinance as yf
from agno.agent import Agent
from agno.models.groq import Groq
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()
warnings.filterwarnings("ignore", module="yfinance")  # pandas uyarıları raporu kirletmesin

WATCHLIST = ["NVDA", "AAPL", "MSFT", "TSLA"]
NEWS_CANDIDATES = 8  # modelin seçeceği aday haber sayısı
NEWS_PER_TICKER = 3
REPORTS_DIR = Path(__file__).parent / "reports"
TR_TZ = dt.timezone(dt.timedelta(hours=3))  # Türkiye 2016'dan beri sabit UTC+3


class TickerInsight(BaseModel):
    news_ids: list[int] = Field(description="Hisseyle en doğrudan ilgili en fazla 3 haberin numarası")
    sentiment: Literal["olumlu", "nötr", "olumsuz"]
    summary: str = Field(description="Verilen verilere dayanan 2-3 cümlelik Türkçe yorum")


analyst = Agent(
    name="Report Analyst",
    model=Groq(id="openai/gpt-oss-120b", max_retries=5),
    instructions=[
        "Sana bir hissenin fiyat, analist ve haber verileri verilecek.",
        "Sadece verilen verileri kullan; rakam, olay veya kaynak uydurma.",
        "Haberlerden yalnızca bu şirketle doğrudan ilgili olanları seç.",
        "Yorumu Türkçe yaz.",
    ],
    output_schema=TickerInsight,
)


def fetch_data(symbol: str) -> dict:
    ticker = yf.Ticker(symbol)
    price = ticker.fast_info["last_price"]
    prev_close = ticker.fast_info["previous_close"]
    return {
        "symbol": symbol,
        "currency": ticker.fast_info["currency"],
        "price": price,
        "change_pct": (price - prev_close) / prev_close * 100,
        "ratings": ticker.recommendations.iloc[0],  # içinde bulunulan ay
        "targets": ticker.analyst_price_targets,
        "news": yf.Search(symbol, news_count=NEWS_CANDIDATES).news,
    }


def build_prompt(d: dict) -> str:
    r, t = d["ratings"], d["targets"]
    lines = [
        f"Hisse: {d['symbol']}",
        f"Fiyat: {d['price']:.2f} {d['currency']} (günlük değişim {d['change_pct']:+.2f}%)",
        f"Analistler: {r['strongBuy']} güçlü al, {r['buy']} al, {r['hold']} tut, "
        f"{r['sell']} sat, {r['strongSell']} güçlü sat",
        f"Hedef fiyat: ortalama {t['mean']:.2f}, en düşük {t['low']:.2f}, en yüksek {t['high']:.2f}",
        "Haberler:",
    ]
    lines += [f"{i}. {n['title']} ({n['publisher']})" for i, n in enumerate(d["news"])]
    return "\n".join(lines)


def build_section(symbol: str) -> str:
    d = fetch_data(symbol)
    insight = analyst.run(build_prompt(d)).content
    if not isinstance(insight, TickerInsight):
        raise ValueError(f"Model beklenen formatta cevap vermedi: {insight!r}")

    # Model sadece numara seçer; başlık ve link her zaman yfinance verisinden gelir.
    ids = [i for i in dict.fromkeys(insight.news_ids) if 0 <= i < len(d["news"])][:NEWS_PER_TICKER]
    r, t = d["ratings"], d["targets"]
    total = r["strongBuy"] + r["buy"] + r["hold"] + r["sell"] + r["strongSell"]

    lines = [
        f"## {symbol} — {d['price']:.2f} {d['currency']} ({d['change_pct']:+.2f}%) · {insight.sentiment}",
        "",
        insight.summary,
        "",
        f"**Analistler ({total}):** {r['strongBuy']} Güçlü Al · {r['buy']} Al · {r['hold']} Tut · "
        f"{r['sell']} Sat · {r['strongSell']} Güçlü Sat",
        f"**Hedef fiyat:** ort. {t['mean']:.2f} (en düşük {t['low']:.2f} – en yüksek {t['high']:.2f})",
        "",
        "**Haberler:**",
    ]
    for i in ids:
        n = d["news"][i]
        when = dt.datetime.fromtimestamp(n["providerPublishTime"], TR_TZ).strftime("%d.%m %H:%M")
        lines.append(f"- [{n['title']}]({n['link']}) — {n['publisher']}, {when}")
    return "\n".join(lines)


def to_telegram_html(md: str) -> str:
    """Raporun Markdown'ını Telegram'ın desteklediği basit HTML'e çevirir."""
    text = html.escape(md, quote=False)
    text = re.sub(r"^#+ (.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"\[(.+?)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)


def send_telegram(sections: list[str]) -> None:
    token, chat_id = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID ayarlı değil; Telegram'a gönderilmedi.")
        return
    for section in sections:  # her hisse ayrı mesaj: Telegram'ın 4096 karakter sınırına takılmasın
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": to_telegram_html(section),
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
            timeout=30,
        )
        if not r.json().get("ok"):  # hata mesajında token geçmesin diye URL'yi yazdırmıyoruz
            raise RuntimeError(f"Telegram gönderimi başarısız: {r.json().get('description')}")


def main() -> None:
    today = dt.date.today().isoformat()
    sections = [f"# Günlük Hisse Raporu — {today}"]
    for symbol in WATCHLIST:
        try:
            sections.append(build_section(symbol))
        except Exception as e:  # bir hisse hata verirse diğerleri yine raporlansın
            sections.append(f"## {symbol}\n\nVeri alınamadı: {e}")

    report = "\n\n".join(sections) + "\n"
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"{today}.md"
    path.write_text(report, encoding="utf-8")
    print(report)
    print(f"Kaydedildi: {path}")
    send_telegram(sections)


if __name__ == "__main__":
    main()
