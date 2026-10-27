"""Günlük hisse raporu.

Rakamlar ve haber linkleri doğrudan yfinance'ten gelir; model sadece
en ilgili haberleri seçer ve kısa bir yorum yazar. Böylece model rakam
veya link uyduramaz. Her rapor veritabanına kaydedilir; bir sonraki rapor
öncekiyle karşılaştırılır.
"""
import datetime as dt
import html
import os
import re
import warnings
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
import yfinance as yf
from agno.agent import Agent
from agno.models.groq import Groq
from dotenv import load_dotenv
from pydantic import BaseModel, Field

import db

load_dotenv()
warnings.filterwarnings("ignore", module="yfinance")  # pandas uyarıları raporu kirletmesin

WATCHLIST = ["NVDA", "AAPL", "MSFT", "TSLA"]
NEWS_CANDIDATES = 8  # modelin seçeceği aday haber sayısı
NEWS_PER_TICKER = 3
REPORTS_DIR = Path(__file__).parent / "reports"
LOCAL_TZ = ZoneInfo("Europe/London")  # GitHub sunucusu UTC'de çalışır; saatler İngiltere saatiyle yazılsın
# yfinance alanı -> (veritabanı sütunu, rapordaki etiket)
RATINGS = {
    "strongBuy": ("strong_buy", "Güçlü Al"),
    "buy": ("buy", "Al"),
    "hold": ("hold", "Tut"),
    "sell": ("sell", "Sat"),
    "strongSell": ("strong_sell", "Güçlü Sat"),
}


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
        "Son rapor verildiyse, ona göre neyin değiştiğini yorumda kısaca belirt.",
        "Yorumu Türkçe yaz.",
    ],
    output_schema=TickerInsight,
)


def ask(agent: Agent, prompt: str, schema: type[BaseModel]):
    """Model ara sıra yarım JSON döndürüyor (Groq 400, json_validate_failed); 3 kez dener."""
    for _ in range(3):
        out = agent.run(prompt).content
        if isinstance(out, schema):
            return out
    raise ValueError(f"{schema.__name__} beklenen formatta değil: {out!r}")


def fetch_data(symbol: str) -> dict:
    ticker = yf.Ticker(symbol)
    price = ticker.fast_info["last_price"]
    prev_close = ticker.fast_info["regular_market_previous_close"]  # "previous_close" gece seansını da katıyor
    return {
        "symbol": symbol,
        "currency": ticker.fast_info["currency"],
        "price": price,
        "change_pct": (price - prev_close) / prev_close * 100,
        "ratings": ticker.recommendations.iloc[0],  # içinde bulunulan ay
        "targets": ticker.analyst_price_targets,
        "news": yf.Search(symbol, news_count=NEWS_CANDIDATES).news,
    }


def build_prompt(d: dict, prev: dict | None) -> str:
    r, t = d["ratings"], d["targets"]
    lines = [
        f"Hisse: {d['symbol']}",
        f"Fiyat: {d['price']:.2f} {d['currency']} (günlük değişim {d['change_pct']:+.2f}%)",
        f"Analistler: {r['strongBuy']} güçlü al, {r['buy']} al, {r['hold']} tut, "
        f"{r['sell']} sat, {r['strongSell']} güçlü sat",
        f"Hedef fiyat: ortalama {t['mean']:.2f}, en düşük {t['low']:.2f}, en yüksek {t['high']:.2f}",
    ]
    if prev:
        lines.append(
            f"Son rapor ({prev['report_date']:%d.%m}): fiyat {prev['price']:.2f}, "
            f"görünüm {prev['sentiment']}, yorum: {prev['summary']}"
        )
    lines.append("Haberler:")
    lines += [f"{i}. {n['title']} ({n['publisher']})" for i, n in enumerate(d["news"])]
    return "\n".join(lines)


def since_last(prev: dict, d: dict, sentiment: str) -> str:
    """Önceki rapora göre değişenler; rakamlar kodla hesaplanır."""
    change = (d["price"] - prev["price"]) / prev["price"] * 100
    parts = [f"fiyat {change:+.2f}% ({prev['price']:.2f} → {d['price']:.2f})"]
    r = d["ratings"]
    moved = [f"{label} {prev[col]}→{int(r[key])}" for key, (col, label) in RATINGS.items() if prev[col] != int(r[key])]
    parts.append("analistler: " + (", ".join(moved) if moved else "değişmedi"))
    if prev["sentiment"] != sentiment:
        parts.append(f"görünüm: {prev['sentiment']} → {sentiment}")
    return f"**Son rapordan bu yana ({prev['report_date']:%d.%m}):** " + " · ".join(parts)


def build_section(symbol: str, today: dt.date, conn) -> tuple[str, dict]:
    """Rapordaki bölümü ve veritabanına yazılacak satırı döner."""
    d = fetch_data(symbol)
    prev = db.last_report(conn, symbol, today) if conn else None
    if prev:  # önceki raporda gösterilen haberleri tekrar gösterme
        shown = {n["link"] for n in prev["news"]}
        d["news"] = [n for n in d["news"] if n["link"] not in shown]
    insight = ask(analyst, build_prompt(d, prev), TickerInsight)

    # Model sadece numara seçer; başlık ve link her zaman yfinance verisinden gelir.
    ids = [i for i in dict.fromkeys(insight.news_ids) if 0 <= i < len(d["news"])][:NEWS_PER_TICKER]
    news = [d["news"][i] for i in ids]
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
    ]
    if prev:
        lines.append(since_last(prev, d, insight.sentiment))
    lines += ["", "**Haberler:**"]
    for n in news:
        when = dt.datetime.fromtimestamp(n["providerPublishTime"], LOCAL_TZ).strftime("%d.%m %H:%M")
        lines.append(f"- [{n['title']}]({n['link']}) — {n['publisher']}, {when}")
    if not news:
        lines.append("Yeni haber yok.")

    row = {
        "report_date": today,
        "symbol": symbol,
        "price": float(d["price"]),
        "currency": d["currency"],
        "change_pct": float(d["change_pct"]),
        **{col: int(r[key]) for key, (col, _) in RATINGS.items()},
        "target_mean": float(t["mean"]),
        "target_low": float(t["low"]),
        "target_high": float(t["high"]),
        "sentiment": insight.sentiment,
        "summary": insight.summary,
        "news": [
            {
                "title": n["title"],
                "link": n["link"],
                "publisher": n["publisher"],
                "published_at": dt.datetime.fromtimestamp(n["providerPublishTime"], dt.timezone.utc).isoformat(),
            }
            for n in news
        ],
    }
    return "\n".join(lines), row


def to_telegram_html(md: str) -> str:
    """Raporun Markdown'ını Telegram'ın desteklediği basit HTML'e çevirir."""
    text = html.escape(md, quote=False)
    text = re.sub(r"^#+ (.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"\[(.+?)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)


def send_telegram(sections: list[str], token_env: str = "TELEGRAM_TOKEN") -> None:
    """token_env: hangi bot (rapor, favori uyarıları, izleme listesi); sohbet numarası aynı."""
    token, chat_id = os.getenv(token_env), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print(f"{token_env} / TELEGRAM_CHAT_ID ayarlı değil; Telegram'a gönderilmedi.")
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
    today = dt.datetime.now(LOCAL_TZ).date()
    try:
        conn = db.connect()
    except Exception as e:  # veritabanına ulaşılamasa da rapor yine gönderilsin
        print(f"Veritabanına bağlanılamadı: {e}")
        conn = None

    sections, rows = [f"# Günlük Hisse Raporu — {today}"], []
    for symbol in WATCHLIST:
        try:
            section, row = build_section(symbol, today, conn)
            sections.append(section)
            rows.append(row)
        except Exception as e:  # bir hisse hata verirse diğerleri yine raporlansın
            sections.append(f"## {symbol}\n\nVeri alınamadı: {e}")

    report = "\n\n".join(sections) + "\n"
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"{today}.md"
    path.write_text(report, encoding="utf-8")
    print(report)
    print(f"Kaydedildi: {path}")
    send_telegram(sections)

    if conn is None:  # GitHub çalışmayı başarısız sayıp e-posta göndersin
        raise SystemExit("Rapor gönderildi ama veritabanına kaydedilemedi.")
    for row in rows:
        db.save_report(conn, row)


if __name__ == "__main__":
    main()
