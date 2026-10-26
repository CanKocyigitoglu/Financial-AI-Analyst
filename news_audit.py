"""Haftalık haber–fiyat denetim raporu (Telegram).

Her ilgili haberin yönü (olumlu/olumsuz), hissenin aynı süredeki piyasaya göre
hareketiyle karşılaştırılır. Tek bir zıtlık kaynağın yanlış olduğunu göstermez
(beklenti fiyatlanmış olabilir, aynı gün başka haber olabilir, haber uzun vadeli
olabilir); anlamlı olan, çok sayıda haberde kaynak/olay türü bazında oranlardır.
"""
import datetime as dt
from collections import defaultdict

from agno.agent import Agent
from agno.models.groq import Groq
from pydantic import BaseModel, Field

import news_db
from daily_report import LOCAL_TZ, send_telegram
from news_pipeline import MODEL_ID

WINDOW = "gun"  # haberden sonraki ilk seans kapanışı
THRESHOLD = 0.5  # yüzde: piyasaya göre bundan küçük hareket "tepki yok" sayılır
DAYS = 7
GROUPS = {"publisher": "Kaynak", "event_type": "Olay türü", "nature": "Nitelik", "horizon": "Zaman ufku"}


class Contradiction(BaseModel):
    explanation: str = Field(description="Haber ile fiyat hareketi arasındaki zıtlığın olası sebepleri, 1-2 cümle, Türkçe")


explainer = Agent(
    model=Groq(id=MODEL_ID, max_retries=5),
    output_schema=Contradiction,
    instructions=[
        "Bir haberin yönü ile hissenin piyasaya göre hareketi zıt çıktı. Olası sebepleri kısaca açıkla.",
        "Şunları düşün: beklenti önceden fiyatlanmış olabilir, aynı gün başka haberler olabilir, "
        "haber uzun vadeli olabilir, haber bir yorum veya spekülasyon olabilir.",
        "Sadece verilen bilgilere dayan; yeni olay uydurma. Kesin konuşma, 'olabilir' gibi ifadeler kullan.",
    ],
)


def label(direction: str, abnormal: float) -> str:
    if abs(abnormal) < THRESHOLD:
        return "tepki yok"
    return "uyumlu" if (abnormal > 0) == (direction == "olumlu") else "zıt"


def rates(rows: list[dict]) -> str:
    n = len(rows)
    c = defaultdict(int)
    for r in rows:
        c[r["label"]] += 1
    return f"{n} haber · uyumlu %{round(c['uyumlu'] * 100 / n)} · zıt %{round(c['zıt'] * 100 / n)} · tepki yok %{round(c['tepki yok'] * 100 / n)}"


def main() -> None:
    conn = news_db.connect()
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)
    counts = conn.execute(
        "SELECT count(*) FILTER (WHERE a.relevance = 'ilgisiz') AS ilgisiz, "
        "       count(*) FILTER (WHERE a.relevance <> 'ilgisiz') AS ilgili, "
        "       count(*) FILTER (WHERE a.direction = 'nötr') AS notr "
        "FROM news_analysis a JOIN news_articles n USING (uuid) WHERE n.published_at >= %s",
        (since,),
    ).fetchone()
    rows = conn.execute(
        "SELECT a.uuid, a.symbol, a.direction, a.strength, a.event_type, a.nature, a.horizon, a.novelty, "
        "       a.summary, n.title, n.publisher, n.published_at, r.stock_ret, r.market_ret, r.sector_ret, "
        "       r.sector_etf, r.stock_ret - r.market_ret AS abnormal "
        "FROM news_analysis a JOIN news_articles n USING (uuid) "
        "JOIN news_reaction r ON r.uuid = a.uuid AND r.symbol = a.symbol AND r.win = %s "
        "WHERE a.relevance <> 'ilgisiz' AND a.direction <> 'nötr' AND n.published_at >= %s "
        "ORDER BY n.published_at",
        (WINDOW, since),
    ).fetchall()
    for r in rows:
        r["label"] = label(r["direction"], r["abnormal"])

    today = dt.datetime.now(LOCAL_TZ).date()
    header = [
        f"# Haftalık Haber–Fiyat Denetimi — {today - dt.timedelta(days=DAYS - 1):%d.%m}–{today:%d.%m}",
        "",
        f"İncelenen haber: {counts['ilgili'] + counts['ilgisiz']} · ilgili: {counts['ilgili']} "
        f"(nötr: {counts['notr']}) · elenen: {counts['ilgisiz']}",
        f"Fiyat tepkisi ölçülen (olumlu/olumsuz): {len(rows)}",
        "",
        "Ölçü: haberden sonraki ilk seans kapanışında hissenin S&P 500'e (SPY) göre hareketi; "
        f"±%{THRESHOLD} altı \"tepki yok\".",
    ]
    if not rows:
        send_telegram(["\n".join(header + ["", "Bu hafta değerlendirilecek yeterli veri yok."])])
        return
    header += ["", f"**Genel:** {rates(rows)}"]

    groups = []
    for key, title in GROUPS.items():
        by = defaultdict(list)
        for r in rows:
            by[r[key]].append(r)
        lines = [f"**{title}:**"] + [
            f"- {name}: {rates(rs)}" for name, rs in sorted(by.items(), key=lambda kv: -len(kv[1]))[:10]
        ]  # en çok haberi olan 10 satır: Telegram mesaj sınırı
        groups.append("\n".join(lines))
    groups.append("Not: Az sayıda haberden çıkan oranlar yanıltıcı olabilir; aynı gün aynı hisseye ait haberler aynı fiyat hareketini paylaşır.")

    contradictions = sorted((r for r in rows if r["label"] == "zıt"), key=lambda r: -abs(r["abnormal"]))[:3]
    details = []
    for r in contradictions:
        same_day = conn.execute(
            "SELECT n.title FROM news_analysis a JOIN news_articles n USING (uuid) "
            "WHERE a.symbol = %s AND a.relevance <> 'ilgisiz' AND a.uuid <> %s "
            "AND n.published_at BETWEEN %s AND %s",
            (r["symbol"], r["uuid"], r["published_at"] - dt.timedelta(hours=12), r["published_at"] + dt.timedelta(hours=12)),
        ).fetchall()
        prompt = "\n".join([
            f"Hisse: {r['symbol']}",
            f"Haber: {r['title']} ({r['publisher']})",
            f"Özet: {r['summary']}",
            f"Yön: {r['direction']} (güç {r['strength']}), zaman ufku: {r['horizon']}, nitelik: {r['nature']}, {r['novelty']}",
            f"Hisse: {r['stock_ret']:+.2f}%, S&P 500: {r['market_ret']:+.2f}%, sektör ({r['sector_etf']}): {r['sector_ret']:+.2f}%",
            "Aynı gün hisseyle ilgili diğer haberler: " + ("; ".join(s["title"] for s in same_day) or "yok"),
        ])
        explanation = explainer.run(prompt).content
        why = explanation.explanation if isinstance(explanation, Contradiction) else "(açıklama alınamadı)"
        details.append(
            f"**{r['symbol']} · {r['published_at'].astimezone(LOCAL_TZ):%d.%m %H:%M}** — {r['title']} ({r['publisher']})\n"
            f"Haber: {r['direction']} · Hisse piyasaya göre {r['abnormal']:+.2f}% "
            f"(hisse {r['stock_ret']:+.2f}%, S&P 500 {r['market_ret']:+.2f}%, {r['sector_etf']} {r['sector_ret']:+.2f}%)\n"
            f"Olası sebep: {why}"
        )

    messages = ["\n".join(header), "\n\n".join(groups)]
    if details:
        messages.append("## En belirgin zıtlıklar\n\n" + "\n\n".join(details))
    send_telegram(messages)


if __name__ == "__main__":
    main()
