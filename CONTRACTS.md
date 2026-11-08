# Implementation contracts (v1)

Project root: `business-agent`. Python package root `backend`, imports `app.schemas`.
Dataset IDs: `excel-main` / `csv-demo`. All API paths prefixed `/api/v1`.

## Backend module interfaces

`Warehouse(data_dir: Path, source_root: Path)`; `ensure_ready()` idempotently ingests and returns manifests; `datasets() -> list[dict]`; `catalog(dataset_id) -> dict`; `profile(dataset_id) -> dict` (quality); `connect_readonly(dataset_id)`.
Datasets: `{id,name,hash,purchase_range:[min,max],table_counts:{table:count},quality_summary:{...}}`.
Catalog: `{dataset_id,dataset_hash,purchase_range:[min,max],categories:[{id,name}],sellers:[{id,name}],metrics:[{id,name}],...}`.

`QueryEngine(warehouse).execute(plan: AnalysisPlan|dict) -> dict`:
`{title,summary,columns:[{key,label,type,unit?}],rows:[object],charts:[{type,title,x,y}],warnings:[str],scope:object,metric:str,evidence:object}`.
Evidence includes `{queries:[{sql,params,grain}],metric_definition,data_hash,checks:[...],warnings:[...]}`.
Comparison periods ordered baseline,current. Compare rows must expose labeled periods/values and delta/growth if possible. Contribution rows category label, baseline,current,delta. Charts x/y must reference row keys. Decimal values JSON strings; no NaN/Infinity. Context_only/explain may have no queries.

`plan_question(question: str, context: dict, catalog: dict) -> PlanningResult` synchronous; context is previous successful `AnalysisPlan.model_dump(mode='json')` plus `_answers` mapping latest clarification field to answer. Empty context still has dataset_id. Planner reads `.env` mode `AGENT_MODE=rules|llm`, defaults rules. LLM env `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. Missing key/HTTP/invalid model plan -> explicit error, no silent fallback. Plan data shape in schemas.py, only root edits schemas.
Root owns actual LangGraph workflow/checkpointer and API/storage; planner owns understanding. Scope notes explain partial data coverage; causal_question yields known evidence limit. Unknown business questions must not become arbitrary sales query.

## API/frontend

`GET /health` -> `{status,mode,model?,exploration_enabled:false}`
`GET /datasets` -> `{datasets:[...]}`
`GET /datasets/{id}/catalog` -> catalog
`GET /datasets/{id}/quality` -> `{checks:[{name,status,count,detail}],...}`
`GET /sessions?dataset_id=` -> `{sessions:[Session]}`
`POST /sessions {dataset_id}` -> Session
Session: `{id,title,dataset_id,context,context_version,created_at,updated_at}`
`GET /sessions/{id}` -> Session plus `messages:[{id,role,content,run_id?,result_id?,status?,created_at}]`.
`POST /sessions/{id}/messages {message,client_message_id,expected_context_version}` -> HTTP202 `{run_id,status,events_url}`.
`GET /runs/{id}` -> `{id,session_id,status,phase,question,result_id?,clarification?,error?,created_at}`.
Clarification: `{id,field,question,options:[{label,value}]}`.
`GET /runs/{id}/events?after_seq=0`: persisted SSE, event name and data `{id,run_id,seq,type,payload}`. payload varies: phase_changed `{phase,label}`, clarification_required `{clarification}`, result_ready `{result_id,context_version}`, answer_ready `{summary}`, warning `{message}`, run_completed `{result_id}`, run_failed `{message}`, run_cancelled `{}`. Event ID `${run_id}:${seq}`. Supports Last-Event-ID replay. Heartbeats comments. On awaiting clarification frontend may close stream; reconnect after resume from last seq.
`POST /runs/{id}/clarifications {clarification_id,answer,expected_context_version}` -> HTTP202 `{run_id,status,events_url}`.
`POST /runs/{id}/cancel` -> `{run_id,status}`.
`GET /results/{id}?offset=0&limit=100` -> `{id,run_id,title,summary,columns,rows,charts,warnings,scope,metric,total,offset,limit,context_version}` (max1000).
`GET /results/{id}/evidence` -> evidence.
`POST /runs/{id}/replay` -> HTTP202 new `{run_id,status,events_url}` with exact saved dataset hash/plan; no LLM replanning. May conflict if same session active.

Run states: queued/running/awaiting_clarification/completed/failed/cancelled.
Per session one active run, idempotency + optimistic context_version. Failed/cancelled run never commits context. Results saved before result_ready. Errors JSON `{detail:string}`. No auth credentials in URL or browser.
Frontend Vite proxy `/api` to `http://127.0.0.1:8000`, FastAPI can serve built frontend/dist. Frontend no framework internal event assumptions.
