"""Haber analiz sistemi: toplama → eleme → tam metin → LLM analizi → piyasa tepkisi.

Günlük rapor ve fiyat uyarılarından ayrı çalışır: kendi tabloları (news_db.py),
kendi workflow'u ve ayrı bir Groq modeli vardır. Groq limitleri model başına
ayrı olduğu için raporun kullandığı modelin kotasını harcamaz.
"""
import datetime as dt
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
import trafilatura
import yfinance as yf
from agno.agent import Agent
from agno.models.groq import Groq
from pydantic import BaseModel, Field

import news_db
from daily_report import WATCHLIST

MODEL_ID = "openai/gpt-oss-20b"  # rapor ve uyarılar gpt-oss-120b kullanıyor
NEWS_PER_SYMBOL = 10
MAX_ANALYSES_PER_RUN = 8  # bir çalışmada en fazla bu kadar tam analiz (token sınırı)
BODY_CHARS = 6000  # modele gönderilen metin (~1.500 token)
MARKET_TZ = ZoneInfo("America/New_York")
OPEN, CLOSE = dt.time(9, 30), dt.time(16, 0)
WINDOW_OFFSETS = {"gun": 0, "1g": 1, "5g": 5}  # ilk seansın kapanışından kaç işlem günü sonra
WINDOWS = ("1s", *WINDOW_OFFSETS)
# yfinance sektörü -> sektör ETF'i (SPDR)
SECTOR_ETFS = {
    "Technology": "XLK", "Consumer Cyclical": "XLY", "Communication Services": "XLC",
    "Energy": "XLE", "Financial Services": "XLF", "Healthcare": "XLV", "Industrials": "XLI",
    "Consumer Defensive": "XLP", "Utilities": "XLU", "Real Estate": "XLRE", "Basic Materials": "XLB",
}


class Triage(BaseModel):
    relevant_ids: list[int] = Field(description="Esas olarak bu şirket hakkında olan haberlerin numaraları")


class NewsAnalysis(BaseModel):
    relevance: Literal["ana konu", "yan konu", "ilgisiz"]
    event_type: Literal[
        "bilanço/kazanç", "ürün/teknoloji", "anlaşma/ortaklık", "analist notu",
        "dava/düzenleme", "yönetim", "makro/sektör", "piyasa yorumu", "diğer",
    ]
    direction: Literal["olumlu", "nötr", "olumsuz"]
    strength: Literal[1, 2, 3] = Field(description="Beklenen etkinin gücü: 1 zayıf, 2 orta, 3 güçlü")
    horizon: Literal["anlık", "haftalar", "aylar+"] = Field(description="Hisse fiyatına etkisi ne zaman beklenir")
    nature: Literal["olay", "analiz/yorum", "spekülasyon"] = Field(
        description="Gerçekleşmiş olay mı, yazarın yorumu mu, doğrulanmamış iddia/söylenti mi"
    )
    novelty: Literal["yeni", "tekrar"] = Field(description="Yeni bilgi mi, bilinen bir şeyin tekrarı/özeti mi")
    summary: str = Field(description="Haberin bu şirket için ana iddiası, tek cümle, Türkçe")


triager = Agent(
    model=Groq(id=MODEL_ID, max_retries=5),
    output_schema=Triage,
    instructions=[
        "Sana bir hisse kodu ve numaralı haber başlıkları verilecek.",
        "Yalnızca esas konusu bu şirket olan haberlerin numaralarını seç; şirketin sadece geçtiği haberleri seçme.",
    ],
)
analyst = Agent(
    model=Groq(id=MODEL_ID, max_retries=5),
    output_schema=NewsAnalysis,
    instructions=[
        "Sana bir hisse kodu ve bir haber verilecek. Haberi bu şirketin hissesi açısından değerlendir.",
        "Sadece metindeki bilgilere dayan; metinde olmayan bilgi ekleme.",
    ],
)


def fetch_body(url: str) -> str | None:
    try:
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True, timeout=20)
        return trafilatura.extract(r.text) or None
    except Exception:
        return None  # metin alınamazsa haber başlıktan analiz edilir


def collect_and_analyze(conn) -> None:
    budget = MAX_ANALYSES_PER_RUN
    for symbol in WATCHLIST:
        items = yf.Search(symbol, news_count=NEWS_PER_SYMBOL).news
        for n in items:
            news_db.save_article(conn, n)
        done = news_db.analyzed(conn, symbol, [n["uuid"] for n in items])
        new = [n for n in items if n["uuid"] not in done]
        if not new:
            continue

        # 1) Başlıklardan eleme: ucuz bir çağrıyla sadece bu şirketle ilgili haberler ayrılır
        prompt = f"Hisse: {symbol}\n" + "\n".join(f"{i}. {n['title']} ({n['publisher']})" for i, n in enumerate(new))
        triage = triager.run(prompt).content
        if not isinstance(triage, Triage):
            raise ValueError(f"Eleme beklenen formatta değil: {triage!r}")
        keep = {i for i in triage.relevant_ids if 0 <= i < len(new)}
        for i, n in enumerate(new):
            if i not in keep:
                news_db.save_analysis(conn, n["uuid"], symbol, MODEL_ID, {"relevance": "ilgisiz"})

        # 2) İlgili haberler tam metinle analiz edilir (sınır dolarsa kalanlar sonraki çalışmaya kalır)
        for i in sorted(keep):
            if budget == 0:
                return
            n = new[i]
            body = news_db.get_body(conn, n["uuid"]) or fetch_body(n["link"])
            news_db.set_body(conn, n["uuid"], body)
            published = dt.datetime.fromtimestamp(n["providerPublishTime"], dt.timezone.utc)
            text = body[:BODY_CHARS] if body else "(Metin alınamadı; sadece başlığa göre değerlendir.)"
            a = analyst.run(
                f"Hisse: {symbol}\nBaşlık: {n['title']}\nYayıncı: {n['publisher']}\n"
                f"Yayın zamanı (UTC): {published:%Y-%m-%d %H:%M}\n\nMetin:\n{text}"
            ).content
            if not isinstance(a, NewsAnalysis):
                raise ValueError(f"Analiz beklenen formatta değil: {a!r}")
            news_db.save_analysis(conn, n["uuid"], symbol, MODEL_ID, a.model_dump())
            budget -= 1
            print(f"{symbol}: {a.direction} {a.strength} | {a.event_type} | {n['title'][:70]}")


class Prices:
    """Bir çalışma boyunca fiyat verisini bir kez indirir."""

    def __init__(self):
        self._bars, self._daily, self._sectors = {}, {}, {}
        self.sessions = sorted({ts.date() for ts in self.bars("SPY").index})  # işlem günleri

    def bars(self, symbol: str):
        if symbol not in self._bars:  # 5 dk'lık seans verisi (yfinance son ~60 günü verir)
            self._bars[symbol] = yf.Ticker(symbol).history(period="60d", interval="5m", auto_adjust=False)
        return self._bars[symbol]

    def daily_close(self, symbol: str, day: dt.date) -> float | None:
        """Resmi günlük kapanış (5 dk'lık son mumdan biraz farklı olabilir)."""
        if symbol not in self._daily:
            closes = yf.Ticker(symbol).history(period="3mo", interval="1d", auto_adjust=False)["Close"]
            self._daily[symbol] = {ts.date(): float(v) for ts, v in closes.items()}
        return self._daily[symbol].get(day)

    def base(self, symbol: str, t: dt.datetime) -> tuple[float, dt.datetime] | None:
        """Haber anında bilinen fiyat: seans içindeyse son 5 dk'lık mum, değilse son resmi kapanış."""
        t_ny = t.astimezone(MARKET_TZ)
        if t_ny.date() in self.sessions and OPEN <= t_ny.time() < CLOSE:
            return self.price_at(symbol, t)
        before = [d for d in self.sessions if d < t_ny.date() or (d == t_ny.date() and t_ny.time() >= CLOSE)]
        price = self.daily_close(symbol, before[-1]) if before else None
        return (price, dt.datetime.combine(before[-1], CLOSE, MARKET_TZ)) if price else None

    def sector_etf(self, symbol: str) -> str:
        if symbol not in self._sectors:
            self._sectors[symbol] = SECTOR_ETFS.get(yf.Ticker(symbol).info.get("sector"), "SPY")
        return self._sectors[symbol]

    def price_at(self, symbol: str, t: dt.datetime) -> tuple[float, dt.datetime] | None:
        """t anında bilinen son fiyat: bitişi t'den önce olan son 5 dk'lık mumun kapanışı."""
        bars = self.bars(symbol)
        ends = bars.index + dt.timedelta(minutes=5)
        done = bars[ends <= t]
        if done.empty:
            return None
        return float(done["Close"].iloc[-1]), done.index[-1].to_pydatetime() + dt.timedelta(minutes=5)


def window_end(published: dt.datetime, sessions: list[dt.date], win: str) -> dt.datetime | None:
    """Pencerenin bittiği an; henüz belli değilse None."""
    t = published.astimezone(MARKET_TZ)
    first = next((d for d in sessions if d > t.date() or (d == t.date() and t.time() < CLOSE)), None)
    if first is None:
        return None
    if win == "1s":  # haberden (seans dışındaysa açılıştan) sonraki ilk saat
        start = max(t, dt.datetime.combine(first, OPEN, MARKET_TZ))
        return min(start + dt.timedelta(hours=1), dt.datetime.combine(first, CLOSE, MARKET_TZ))
    i = sessions.index(first) + WINDOW_OFFSETS[win]
    return dt.datetime.combine(sessions[i], CLOSE, MARKET_TZ) if i < len(sessions) else None


def measure_reactions(conn) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    pending = news_db.pending_reactions(conn, since=now - dt.timedelta(days=55))
    if not pending:
        return
    prices = Prices()
    for p in pending:
        etf = prices.sector_etf(p["symbol"])
        for win in WINDOWS:
            if win in p["done"]:
                continue
            end = window_end(p["published_at"], prices.sessions, win)
            if end is None or now < end + dt.timedelta(minutes=20):  # veri gelsin diye kısa bekleme
                continue
            rets = {}
            for key, sym in (("stock", p["symbol"]), ("market", "SPY"), ("sector", etf)):
                base = prices.base(sym, p["published_at"])
                if win == "1s":
                    stop = prices.price_at(sym, end)
                    # bitiş mumu pencere sonuna yakın olmalı (eksik veriyle ölçme)
                    stop = stop[0] if stop and stop[1] >= end - dt.timedelta(minutes=10) else None
                else:
                    stop = prices.daily_close(sym, end.date())
                if not base or not stop:
                    break
                rets[key] = (stop / base[0] - 1) * 100
            else:
                news_db.save_reaction(conn, {
                    "uuid": p["uuid"], "symbol": p["symbol"], "win": win,
                    "base_time": prices.base(p["symbol"], p["published_at"])[1], "end_time": end,
                    "stock_ret": rets["stock"], "market_ret": rets["market"],
                    "sector_etf": etf, "sector_ret": rets["sector"],
                })


def main() -> None:
    conn = news_db.connect()
    collect_and_analyze(conn)
    measure_reactions(conn)


if __name__ == "__main__":
    main()
