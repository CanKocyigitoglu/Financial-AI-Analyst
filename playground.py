from agno.agent import Agent
from agno.tools.yfinance import YFinanceTools
from agno.tools.websearch import WebSearchTools
from dotenv import load_dotenv
from agno.models.groq import Groq

from agno.os import AgentOS
from news import get_company_news
# Load environment variables from .env file
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

agent_os=AgentOS(agents=[finance_agent,web_search_agent])
app=agent_os.get_app()

if __name__=="__main__":
    agent_os.serve(app="playground:app",reload=True)

