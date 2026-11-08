# Dependencies and design references

This project was independently implemented for the supplied competition task.
No SQLBot, WrenAI, Vanna, or DB-GPT application source was copied into this project.

Design references (consulted 2026-11-07):

- SQLBot: https://github.com/dataease/SQLBot — product workflow and FastAPI/Vue separation. Its modified GPLv3 license and branding conditions apply if its code is incorporated in future.
- WrenAI: https://github.com/Canner/WrenAI — semantic context, explicit model grain, query validation. Current core and historical app have different licenses.
- Vanna: https://github.com/vanna-ai/vanna — typed tool context and streamed result components. Archived repository; no runtime dependency here.
- LangGraph SQL Agent: https://docs.langchain.com/oss/python/langgraph/sql-agent — explicit state graph and clarification lifecycle.

Runtime dependencies retain their respective package licenses. See backend/requirements.lock.txt and frontend/package-lock.json for exact installed versions. Principal dependencies include FastAPI, Pydantic, LangGraph, DuckDB, SQLGlot, openpyxl, Vue, Vite, Pinia, Element Plus and Apache ECharts. This notice does not replace licenses distributed with those packages.
