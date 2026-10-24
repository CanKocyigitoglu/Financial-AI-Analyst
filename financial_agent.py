from agno.agent import Agent
from agno.team import Team
from agno.models.groq import Groq
from agno.tools.yfinance import YFinanceTools
from agno.tools.websearch import WebSearchTools

from dotenv import load_dotenv
from news import get_company_news
load_dotenv()

## web search agent
web_search_agent=Agent(
    name="Web Search Agent",
    role="Search the web for the information",
    model=Groq(id="openai/gpt-oss-120b"),
    tools=[WebSearchTools()],
    instructions=["Alway include sources", "Only cite links that appear in your search results.",
                  "You cannot open web pages; answer from the search results."],
    tool_call_limit=4,  # döngüye girip binlerce token harcamasın
    retries=2,  # gpt-oss bazen olmayan "web_open" aracını çağırıp hata veriyor; tekrar dene
    markdown=True,

)

## Financial agent
finance_agent=Agent(
    name="Finance AI Agent",
    model=Groq(id="openai/gpt-oss-120b"),
    tools=[
        YFinanceTools(enable_stock_price=True, enable_analyst_recommendations=True, enable_stock_fundamentals=True),
        get_company_news,  # YFinanceTools'un haber aracı boş dönüyor; bkz. news.py
    ],
    instructions=[
        "Use tables to display the data",
        "Only report numbers and news that come from your tool results; never guess or invent them.",
        "Include the link for every news story, exactly as returned by the tool.",
        "Skip news stories that are not mainly about the requested company.",
        "If a tool returns no data or an error, say so plainly.",
    ],
    markdown=True,

)

multi_ai_agent=Team(
    members=[web_search_agent,finance_agent],
    model=Groq(id="openai/gpt-oss-120b"),
    instructions=["Always include sources","Use table to display the data",
                  "Only use information returned by the team members; if a member could not get data, say so instead of filling the gap."],
    markdown=True,
)

if __name__=="__main__":
    multi_ai_agent.print_response("Summarize analyst recommendation and share the latest news for NVDA",stream=True)

