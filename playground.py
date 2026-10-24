from agno.agent import Agent
from agno.tools.yfinance import YFinanceTools
from agno.tools.websearch import WebSearchTools
from dotenv import load_dotenv
from agno.models.groq import Groq

from agno.os import AgentOS
# Load environment variables from .env file
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

agent_os=AgentOS(agents=[finance_agent,web_search_agent])
app=agent_os.get_app()

if __name__=="__main__":
    agent_os.serve(app="playground:app",reload=True)

