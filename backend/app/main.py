from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app.schemas import CreateSession, SubmitMessage, SubmitClarification
from app.settings import settings, PROJECT_ROOT
from app.store import Store, Conflict, NotFound
from app.warehouse import Warehouse
from app.workflow import Workflow, RunCancelled

logger=logging.getLogger(__name__)


def create_app(config=settings):
    @asynccontextmanager
    async def lifespan(app):
        config.data_dir.mkdir(parents=True,exist_ok=True)
        warehouse=Warehouse(config.data_dir,config.source_root)
        await asyncio.to_thread(warehouse.ensure_ready)
        store=Store(config.data_dir/"app.sqlite3")
        store.recover()
        checkpoint_connection=sqlite3.connect(config.data_dir/"checkpoints.sqlite3",check_same_thread=False)
        saver=SqliteSaver(checkpoint_connection)
        app.state.warehouse=warehouse
        app.state.store=store
        app.state.workflow=Workflow(store,config,saver)
        app.state.tasks={}
        yield
        for run_id,task in list(app.state.tasks.items()):
            store.finish_error(run_id,"服务关闭，分析已中止")
        if app.state.tasks:
            await asyncio.gather(*app.state.tasks.values(),return_exceptions=True)
        checkpoint_connection.close()

    app=FastAPI(title="经营分析 Agent",version="1.0.0",lifespan=lifespan)
    app.add_middleware(CORSMiddleware,allow_origins=config.allowed_origins.split(","),allow_methods=["GET","POST"],allow_headers=["Content-Type","Last-Event-ID","Authorization"])

    @app.exception_handler(Conflict)
    async def conflict(request,exc):
        return JSONResponse(status_code=409,content={"detail":str(exc)})

    @app.exception_handler(NotFound)
    async def missing(request,exc):
        return JSONResponse(status_code=404,content={"detail":str(exc)})

    def checked_dataset(dataset_id):
        known={d["id"] for d in app.state.warehouse.datasets()}
        if dataset_id not in known:
            raise HTTPException(404,"数据集不存在")

    async def perform(run_id,resume=None,replay_plan=None):
        store=app.state.store
        invoke_task=None
        try:
            if not store.start(run_id):
                return
            run=store.run(run_id)
            session=store.session(run["session_id"])
            catalog=app.state.warehouse.catalog(session["dataset_id"])
            initial=Command(resume=resume) if resume is not None else {"run_id":run_id,"question":run["question"],"context":session["context"],"catalog":catalog,"replay_plan":replay_plan}
            # Each run has its own checkpoint thread; successful business state is committed below.
            invoke_task=asyncio.create_task(asyncio.to_thread(app.state.workflow.graph.invoke,initial,{"configurable":{"thread_id":run_id},"recursion_limit":25}))
            output=await asyncio.wait_for(asyncio.shield(invoke_task),timeout=config.run_timeout_seconds)
            if output.get("__interrupt__"):
                store.pause(run_id,output["__interrupt__"][0].value)
            else:
                store.complete(run_id,output["result"],output["plan"])
        except RunCancelled:
            pass
        except asyncio.TimeoutError:
            store.finish_error(run_id,"分析超过总时限，已中止。请缩小范围后重新提交。")
        except Exception as exc:
            logger.exception("Analysis failed: %s",run_id)
            store.finish_error(run_id,str(exc) or "分析执行失败")
        finally:
            # A timeout must not orphan a thread that still owns the checkpoint
            # connection. Marked-terminal runs stop at the next node/process poll.
            if invoke_task is not None and not invoke_task.done():
                try:
                    await invoke_task
                except Exception:
                    pass
            app.state.tasks.pop(run_id,None)

    def enqueue(run_id,**kwargs):
        task=asyncio.create_task(perform(run_id,**kwargs))
        app.state.tasks[run_id]=task

    def accepted(run):
        return {"run_id":run["id"],"status":run["status"],"events_url":f"/api/v1/runs/{run['id']}/events"}

    @app.get("/api/v1/health")
    def health():
        return {"status":"ok","mode":config.agent_mode,"model":config.llm_model if config.agent_mode=="llm" else None,"exploration_enabled":False,"sql_mode":"compiled_metrics","single_user":True}

    @app.get("/api/v1/datasets")
    def datasets():
        return {"datasets":app.state.warehouse.datasets()}

    @app.get("/api/v1/datasets/{dataset_id}/catalog")
    def catalog(dataset_id:str):
        checked_dataset(dataset_id)
        return app.state.warehouse.catalog(dataset_id)

    @app.get("/api/v1/datasets/{dataset_id}/quality")
    def quality(dataset_id:str):
        checked_dataset(dataset_id)
        return app.state.warehouse.profile(dataset_id)

    @app.get("/api/v1/sessions")
    def sessions(dataset_id:str|None=None):
        return {"sessions":app.state.store.sessions(dataset_id)}

    @app.post("/api/v1/sessions",status_code=201)
    def new_session(body:CreateSession):
        checked_dataset(body.dataset_id)
        return app.state.store.create_session(body.dataset_id)

    @app.get("/api/v1/sessions/{session_id}")
    def session(session_id:str):
        return app.state.store.session(session_id,messages=True)

    @app.post("/api/v1/sessions/{session_id}/messages",status_code=202)
    async def message(session_id:str,body:SubmitMessage):
        if not body.message.strip():
            raise HTTPException(422,"请输入分析问题")
        run,created=app.state.store.create_run(session_id,body.message.strip(),body.client_message_id,body.expected_context_version)
        if created:
            enqueue(run["id"])
        return accepted(run)

    @app.get("/api/v1/runs/{run_id}")
    def run(run_id:str):
        value=app.state.store.run(run_id)
        return {k:v for k,v in value.items() if k not in ("client_key",)}

    @app.get("/api/v1/runs/{run_id}/events")
    async def events(run_id:str,request:Request,after_seq:int=Query(0,ge=0)):
        app.state.store.run(run_id)
        last=request.headers.get("last-event-id","")
        if last:
            try:
                prefix,seq=last.rsplit(":",1)
                if prefix!=run_id:
                    raise ValueError()
                after_seq=max(after_seq,int(seq))
            except ValueError:
                raise HTTPException(400,"事件恢复位置无效")

        async def stream():
            after=after_seq
            heartbeats=0
            while not await request.is_disconnected():
                chunks=app.state.store.events(run_id,after)
                for item in chunks:
                    after=item["seq"]
                    yield f"id: {item['id']}\nevent: {item['type']}\ndata: {json.dumps(item,ensure_ascii=False)}\n\n"
                status=app.state.store.run(run_id)["status"]
                if status in ("completed","failed","cancelled","awaiting_clarification") and not app.state.store.events(run_id,after):
                    return
                heartbeats+=1
                if heartbeats>=50:
                    yield ": heartbeat\n\n"
                    heartbeats=0
                await asyncio.sleep(.2)
        return StreamingResponse(stream(),media_type="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    @app.post("/api/v1/runs/{run_id}/clarifications",status_code=202)
    async def resume(run_id:str,body:SubmitClarification):
        if not body.answer.strip():
            raise HTTPException(422,"澄清答案不能为空")
        app.state.store.resume(run_id,body.clarification_id,body.expected_context_version,body.answer.strip())
        enqueue(run_id,resume=body.answer.strip())
        return accepted(app.state.store.run(run_id))

    @app.post("/api/v1/runs/{run_id}/cancel")
    def cancel(run_id:str):
        app.state.store.run(run_id)
        app.state.store.finish_error(run_id,"分析已取消，已确认条件保持不变。",cancel=True)
        return {"run_id":run_id,"status":app.state.store.run(run_id)["status"]}

    @app.get("/api/v1/results/{result_id}")
    def result(result_id:str,offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=1000)):
        value=app.state.store.result(result_id)
        rows=value["rows"]
        return {**value,"rows":rows[offset:offset+limit],"total":len(rows),"offset":offset,"limit":limit}

    @app.get("/api/v1/results/{result_id}/evidence")
    def evidence(result_id:str):
        return app.state.store.result(result_id,evidence=True)

    @app.post("/api/v1/runs/{run_id}/replay",status_code=202)
    async def replay(run_id:str):
        original=app.state.store.run(run_id)
        if original["status"]!="completed" or not original["result_id"]:
            raise Conflict("只有已完成的查询可以回放")
        evidence=app.state.store.result(original["result_id"],evidence=True)
        if evidence.get("metric_version")!="1":
            raise Conflict("指标口径版本不匹配，无法按原版本回放")
        catalog=app.state.warehouse.catalog(original["plan"]["dataset_id"])
        if evidence.get("dataset_hash")!=catalog.get("dataset_hash"):
            raise Conflict("数据文件版本已变化，无法按原版本回放")
        session=app.state.store.session(original["session_id"])
        new,created=app.state.store.create_run(session["id"],"回放："+original["question"],"replay_"+uuid.uuid4().hex,session["context_version"],replay_of=run_id)
        enqueue(new["id"],replay_plan=original["plan"])
        return accepted(new)

    dist=PROJECT_ROOT/"frontend"/"dist"
    if (dist/"assets").exists():
        app.mount("/assets",StaticFiles(directory=dist/"assets"),name="assets")

    @app.get("/",include_in_schema=False)
    def index():
        if (dist/"index.html").exists():
            return FileResponse(dist/"index.html")
        return {"message":"后端已启动。前端开发：cd frontend && npm run dev","docs":"/docs"}

    return app


app=create_app()
