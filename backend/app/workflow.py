from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from app.planner import plan_question
from app.schemas import AnalysisPlan
from app.store import Store


class AnalysisState(TypedDict, total=False):
    run_id: str
    question: str
    context: dict
    catalog: dict
    plan: dict
    clarification: dict | None
    result: dict
    replay_plan: dict | None


class RunCancelled(Exception):
    pass


class Workflow:
    def __init__(self, store: Store, settings, checkpointer):
        self.store, self.settings = store, settings
        graph=StateGraph(AnalysisState)
        graph.add_node("plan",self.plan)
        graph.add_node("clarify",self.clarify)
        graph.add_node("query",self.query)
        graph.add_node("verify",self.verify)
        graph.add_edge(START,"plan")
        graph.add_conditional_edges("plan",lambda state:"clarify" if state.get("clarification") else "query")
        graph.add_edge("clarify","plan")
        graph.add_edge("query","verify")
        graph.add_edge("verify",END)
        self.graph=graph.compile(checkpointer=checkpointer)

    def check_active(self,state):
        if self.store.run(state["run_id"])["status"] not in ("running",):
            raise RunCancelled("分析已中止")

    def plan(self,state):
        self.check_active(state)
        self.store.phase(state["run_id"],"planning","正在解析指标、时间和筛选条件")
        if state.get("replay_plan"):
            plan=AnalysisPlan.model_validate(state["replay_plan"])
            plan.planner_source="replay"
            return {"plan":plan.model_dump(mode="json"),"clarification":None}
        planned=plan_question(state["question"],state["context"],state["catalog"])
        if planned.clarification:
            return {"clarification":planned.clarification.model_dump(mode="json")}
        plan=planned.plan
        if plan.dataset_id!=state["catalog"]["dataset_id"]:
            raise ValueError("规划器不能切换当前数据集")
        for message in planned.warnings:
            self.store.event(state["run_id"],"warning",{"message":message})
        return {"plan":plan.model_dump(mode="json"),"clarification":None}

    def clarify(self,state):
        answer=interrupt(state["clarification"])
        context={**state["context"]}
        context["_answers"]={**context.get("_answers",{}),state["clarification"]["field"]:str(answer)}
        return {"context":context,"clarification":None}

    def query(self,state):
        self.check_active(state)
        self.store.save_plan(state["run_id"],state["plan"])
        self.store.phase(state["run_id"],"querying","正在按口径编译并执行只读查询")
        result=self.execute_process(state)
        self.store.event(state["run_id"],"query_ready",{"query_id":state["run_id"]})
        return {"result":result}

    def execute_process(self,state):
        allowed={k:v for k,v in os.environ.items() if k.upper() in {"SYSTEMROOT","WINDIR","PATH","TEMP","TMP","LANG"}}
        allowed.update(PYTHONIOENCODING="utf-8",PYTHONUTF8="1")
        backend=Path(__file__).resolve().parents[1]
        request=json.dumps({"data_dir":str(self.settings.data_dir),"source_root":str(self.settings.source_root),"plan":state["plan"]},ensure_ascii=False)
        process=subprocess.Popen([sys.executable,"-m","app.worker"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=backend,env=allowed,text=True,encoding="utf-8",creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        started=time.monotonic()
        first=True
        try:
            while True:
                self.check_active(state)
                if time.monotonic()-started>self.settings.query_timeout_seconds:
                    raise TimeoutError("查询超过执行时限，请缩小分析范围后重试")
                try:
                    stdout,stderr=process.communicate(request if first else None,timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    first=False
            if not stdout.strip():
                raise ValueError("查询执行器未返回结果："+stderr[-600:])
            response=json.loads(stdout.strip().splitlines()[-1])
            if not response.get("ok"):
                raise ValueError(response.get("error","查询执行失败"))
            return response["result"]
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def verify(self,state):
        self.check_active(state)
        self.store.phase(state["run_id"],"verifying","正在核验结果并整理查询依据")
        result=state["result"]
        if not isinstance(result.get("rows"),list) or not isinstance(result.get("columns"),list):
            raise ValueError("查询返回结构不完整")
        json.dumps(result,allow_nan=False)
        evidence=result.setdefault("evidence",{})
        source=(state.get("plan") or {}).get("planner_source") or ""
        if source=="fallback":
            evidence.update(planner_mode="rules-fallback",model=None,prompt_version="1")
        elif source=="replay":
            evidence.update(planner_mode="replay",model=None,prompt_version="1")
        elif source=="model":
            evidence.update(planner_mode="llm",model=os.getenv("LLM_MODEL","") or None,prompt_version="1")
        elif source=="rules":
            evidence.update(planner_mode="rules",model=None,prompt_version="1")
        else:
            evidence.update(planner_mode=os.getenv("AGENT_MODE","rules"),model=os.getenv("LLM_MODEL","") if os.getenv("AGENT_MODE")=="llm" else None,prompt_version="1")
        evidence.update(dataset_id=state["catalog"]["dataset_id"],dataset_hash=state["catalog"].get("dataset_hash"))
        return {"result":result}
