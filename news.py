"""Ajanların kullandığı haber aracı.

yfinance'in Ticker.news alanı (ve Agno'nun YFinanceTools haber aracı) artık
boş liste dönüyor; yf.Search ise gerçek haberleri linkleriyle veriyor.
"""
import datetime as dt
import json

import yfinance as yf


def get_company_news(symbol: str, num_stories: int = 5) -> str:
    """Use this function to get the latest news about a company.

    Args:
        symbol (str): The stock symbol, e.g. "NVDA".
        num_stories (int): Number of news stories to return. Defaults to 5.

    Returns:
        str: JSON list of news stories with title, publisher, link and publish time (UTC).
    """
    news = yf.Search(symbol, news_count=num_stories).news
    return json.dumps(
        [
            {
                "title": n["title"],
                "publisher": n["publisher"],
                "link": n["link"],
                "published": dt.datetime.fromtimestamp(n["providerPublishTime"], dt.timezone.utc).isoformat(),
            }
            for n in news
        ],
        ensure_ascii=False,
    )
