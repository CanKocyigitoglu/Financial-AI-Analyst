from agno.agent import Agent
from agno.team import Team
from agno.models.groq import Groq
from agno.tools.yfinance import YFinanceTools
from agno.tools.websearch import WebSearchTools

from dotenv import load_dotenv
load_dotenv()

## web search agent
web_search_agent=Agent(
    name="Web Search Agent",
    role="Search the web for the information",
    model=Groq(id="openai/gpt-oss-120b"),
    tools=[WebSearchTools()],
    instructions=["Alway include sources"],
    markdown=True,

)

## Financial agent
finance_agent=Agent(
    name="Finance AI Agent",
    model=Groq(id="openai/gpt-oss-120b"),
    tools=[
        YFinanceTools(enable_stock_price=True, enable_analyst_recommendations=True, enable_stock_fundamentals=True,
                      enable_company_news=True),
    ],
    instructions=["Use tables to display the data"],
    markdown=True,

)

multi_ai_agent=Team(
    members=[web_search_agent,finance_agent],
    model=Groq(id="openai/gpt-oss-120b"),
    instructions=["Always include sources","Use table to display the data"],
    markdown=True,
)

multi_ai_agent.print_response("Summarize analyst recommendation and share the latest news for NVDA",stream=True)

