"""Automated evaluation runner for the BI agent.

Reads the competition practice questions (Excel 练习题 sheet + CSV
practice_questions.csv), the PDF flagship example, the supplementary test
cases (tests/test_supplementary.py) and the multi-turn/clarification flows,
runs each against the live API (--api, default) or the in-process engine
(--engine, offline deterministic rules mode), compares with the expected
answers and writes a pass/fail report (console + evaluation/auto_evaluate_report.json).

Usage (from the project root):
    .venv\\Scripts\\python.exe evaluation\\auto_evaluate.py --api     # server must run on 127.0.0.1:8000
    .venv\\Scripts\\python.exe evaluation\\auto_evaluate.py --engine  # offline, no server needed
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import httpx  # noqa: E402
import openpyxl  # noqa: E402

API_BASE = "http://127.0.0.1:8000"
RUN_TIMEOUT = 150

# Expected-answer tokens per practice question (both datasets). Bare-month
# questions on excel-main (two calendar years) need the year answered first.
PRACTICE_TOKENS = {
    "excel-main": {
        "Q01": ["10714"],
        "Q02": ["4858920.84"],
        "Q03": ["15.00%", "167927.39", "193119.68"],
        "Q04": ["数码家电", "105245.50"],
        "Q05": ["广州美妆个护商家004", "91.53%"],
        "Q06": ["2.00", "4.02"],
        "Q07": ["2378"],
        "Q08": ["先按订单汇总", "4858920.84", "5489740.42"],
    },
    "csv-demo": {
        "Q01": ["159"],
        "Q02": ["75484.21"],
        "Q03": ["18.43%", "12396.58", "14681.34"],
        "Q04": ["数码家电", "8564.60"],
        "Q05": ["岭南好物", "85.71%"],
        "Q06": ["1.83", "4.00"],
        "Q07": ["46"],
        "Q08": ["先按订单汇总", "75484.21", "83830.80"],
    },
}
AUTO_YEAR = {"excel-main": ["Q03", "Q04", "Q05"], "csv-demo": []}

PDF_CASE = {
    "id": "PDF",
    "dataset": "excel-main",
    "question": "2025年4月已送达订单商品销售额比3月变化多少？哪个品类贡献最大？能确定增长是促销带来的吗？",
    "tokens": ["167927.39", "193119.68", "25192.29", "15.00%", "65344.55", "数码家电", "促销"],
}

CHAIN = [
    ("F01a", "2025年4月商品销售额是多少？", ["193119.68"]),
    ("F01b", "只看家居生活", ["30053.57"]),
    ("F01c", "按商家拆开", ["广州家居生活商家044", "2839.24"]),
    ("F01d", "和上月比", ["49374.93", "-39.13%"]),
]

FLOW_F02 = ("4月销售额是多少？", "2024", ["152674.76", "346"])


def find_materials():
    """Locate the competition materials directory from the project parent."""
    source_root = PROJECT_ROOT.parent
    xlsx_candidates = sorted(source_root.rglob("ecommerce_agent_demo.xlsx"))
    if not xlsx_candidates:
        raise FileNotFoundError(f"在 {source_root} 找不到 ecommerce_agent_demo.xlsx")
    xlsx = xlsx_candidates[0]
    csv_candidates = sorted(xlsx.parent.rglob("practice_questions.csv"))
    csv_file = csv_candidates[0] if csv_candidates else None
    return xlsx, csv_file


def read_practice_questions(xlsx: Path, csv_file: Path | None) -> dict[str, dict[str, str]]:
    """Return {dataset_id: {qid: question}} from both practice-question sources."""
    result: dict[str, dict[str, str]] = {"excel-main": {}, "csv-demo": {}}
    book = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    try:
        rows = list(book["练习题"].iter_rows(values_only=True))
    finally:
        book.close()
    for row in rows[1:]:
        if row and row[0]:
            result["excel-main"][str(row[0])] = str(row[2])
    if csv_file:
        with csv_file.open("r", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                result["csv-demo"][row["question_id"]] = row["question"]
    return result


class ApiRunner:
    """Runs questions against the live FastAPI service (full end-to-end)."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.client = httpx.Client(timeout=httpx.Timeout(30, connect=10))

    def health(self):
        response = self.client.get(f"{self.base}/api/v1/health")
        response.raise_for_status()
        return response.json()

    def _wait(self, run_id: str):
        deadline = time.monotonic() + RUN_TIMEOUT
        while True:
            run = self.client.get(f"{self.base}/api/v1/runs/{run_id}").json()
            if run["status"] in ("completed", "failed", "cancelled", "awaiting_clarification"):
                return run
            if time.monotonic() > deadline:
                raise TimeoutError(f"运行超时：{run_id}")
            time.sleep(0.5)

    def ask(self, dataset_id: str, question: str, auto_answer: str | None = None, session_id: str | None = None):
        """One question -> (result_payload, error, clarification, session_id)."""
        if session_id is None:
            session = self.client.post(f"{self.base}/api/v1/sessions", json={"dataset_id": dataset_id}).json()
            session_id = session["id"]
        else:
            session = self.client.get(f"{self.base}/api/v1/sessions/{session_id}").json()
        accepted = self.client.post(
            f"{self.base}/api/v1/sessions/{session_id}/messages",
            json={
                "message": question,
                "client_message_id": uuid.uuid4().hex,
                "expected_context_version": session["context_version"],
            },
        ).json()
        run = self._wait(accepted["run_id"])
        clarification = None
        if run["status"] == "awaiting_clarification":
            clarification = run["clarification"]
            if auto_answer is None:
                return None, f"需要澄清：{clarification.get('field')}", clarification, session_id
            fresh = self.client.get(f"{self.base}/api/v1/sessions/{session_id}").json()
            self.client.post(
                f"{self.base}/api/v1/runs/{run['id']}/clarifications",
                json={
                    "clarification_id": clarification["id"],
                    "answer": auto_answer,
                    "expected_context_version": fresh["context_version"],
                },
            )
            run = self._wait(run["id"])
        if run["status"] != "completed":
            return None, run.get("error") or f"运行状态 {run['status']}", clarification, session_id
        result = self.client.get(f"{self.base}/api/v1/results/{run['result_id']}?offset=0&limit=100").json()
        return result, None, clarification, session_id


class EngineRunner:
    """Runs questions against the in-process planner + engine (offline, rules mode)."""

    def __init__(self):
        import os
        os.environ["AGENT_MODE"] = "rules"
        from app.query import QueryEngine
        from app.warehouse import Warehouse

        warehouse = Warehouse(PROJECT_ROOT / "data", PROJECT_ROOT.parent)
        warehouse.ensure_ready()
        self.engine = QueryEngine(warehouse)
        self.catalogs = {dataset: warehouse.catalog(dataset) for dataset in ("excel-main", "csv-demo")}

    def health(self):
        import os
        return {"mode": os.getenv("AGENT_MODE", "rules"), "model": os.getenv("LLM_MODEL", "")}

    def ask(self, dataset_id: str, question: str, auto_answer: str | None = None, context: dict | None = None):
        from app.planner import PlanningError, plan_question

        context = dict(context) if context else {"dataset_id": dataset_id}
        try:
            planned = plan_question(question, context, self.catalogs[dataset_id])
        except PlanningError as exc:
            return None, str(exc), None, context
        clarification = None
        if planned.clarification:
            clarification = planned.clarification.model_dump(mode="json")
            if auto_answer is None:
                return None, f"需要澄清：{clarification.get('field')}", clarification, context
            answers = {**(context.get("_answers") or {}), planned.clarification.field: auto_answer}
            context = {**context, "_answers": answers}
            planned = plan_question(question, context, self.catalogs[dataset_id])
        if not planned.plan:
            return None, "规划器未产出计划", clarification, context
        result = self.engine.execute(planned.plan)
        return result, None, clarification, planned.plan.model_dump(mode="json")


def check(result, tokens) -> tuple[bool, list[str]]:
    text = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)
    missing = [token for token in tokens if token not in text]
    return (not missing), missing


def run_cases(runner, cases, report):
    passed = failed = 0
    for case in cases:
        result, error, clarification, _ = runner.ask(case["dataset"], case["question"], case.get("auto_answer"))
        if case.get("expect_error"):
            ok = error is not None and all(token in error for token in case["tokens"])
            detail = f"拒绝信息: {error[:80]}" if error else f"未拒绝而是完成: {clarification}"
        elif error:
            ok, detail = False, f"错误: {error[:120]}"
        else:
            ok, missing = check(result, case["tokens"])
            detail = "缺少关键答案: " + ",".join(missing) if missing else "通过"
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {case['id']:<8} {case['dataset']:<11} {case['question'][:36]:<38} {detail[:70]}")
        report["cases"].append({"id": case["id"], "dataset": case["dataset"], "question": case["question"], "status": status, "detail": detail})
        if ok:
            passed += 1
        else:
            failed += 1
    return passed, failed


def run_chain(runner, report):
    session_id = None   # API 模式：同一会话接力
    context = None      # 引擎模式：上一轮计划作为上下文
    ok = True
    for cid, question, tokens in CHAIN:
        if isinstance(runner, ApiRunner):
            result, error, _, session_id = runner.ask("excel-main", question, None, session_id)
        else:
            result, error, _, context = runner.ask("excel-main", question, None, context)
        if error:
            print(f"[FAIL] {cid}: {error[:100]}")
            report["flows"].append({"id": cid, "status": "FAIL", "detail": error})
            ok = False
            break
        passed, missing = check(result, tokens)
        print(f"[{'PASS' if passed else 'FAIL'}] {cid} 追问 {question[:30]}" + (f" 缺少: {','.join(missing)}" if missing else ""))
        report["flows"].append({"id": cid, "status": "PASS" if passed else "FAIL", "detail": "" if passed else "缺少: " + ",".join(missing)})
        ok = ok and passed
    return ok


def run_clarification_flow(runner, report):
    question, answer, tokens = FLOW_F02
    result, error, clarification, carry = runner.ask("excel-main", question, None)
    if not clarification:
        print(f"[FAIL] F02 未触发年份澄清: {error}")
        report["flows"].append({"id": "F02", "status": "FAIL", "detail": f"未澄清: {error}"})
        return False
    if clarification.get("field") != "year":
        print(f"[FAIL] F02 澄清字段不是 year: {clarification}")
        report["flows"].append({"id": "F02", "status": "FAIL", "detail": f"字段: {clarification.get('field')}"})
        return False
    if isinstance(runner, ApiRunner):
        # 澄清已在独立会话出现；带自动答案重问一次（新会话）
        result, error, _, _ = runner.ask("excel-main", question, answer)
    else:
        result, error, _, _ = runner.ask("excel-main", question, answer)
    if error:
        print(f"[FAIL] F02 澄清后回答失败: {error}")
        report["flows"].append({"id": "F02", "status": "FAIL", "detail": error})
        return False
    ok, missing = check(result, tokens)
    print(f"[{'PASS' if ok else 'FAIL'}] F02 裸月份澄清 -> 选{answer} " + (f"缺少: {','.join(missing)}" if missing else ""))
    report["flows"].append({"id": "F02", "status": "PASS" if ok else "FAIL", "detail": "" if ok else "缺少: " + ",".join(missing)})
    return ok


def main():
    parser = argparse.ArgumentParser(description="BI Agent 自动化测评")
    parser.add_argument("--api", action="store_true", help="通过运行中的 API 服务测评（默认）")
    parser.add_argument("--engine", action="store_true", help="离线引擎模式测评（无需启动服务）")
    parser.add_argument("--base", default=API_BASE, help="API 服务地址")
    args = parser.parse_args()
    use_api = not args.engine

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "api" if use_api else "engine",
        "cases": [],
        "flows": [],
    }

    xlsx, csv_file = find_materials()
    print(f"材料: {xlsx}")
    print(f"CSV 练习题: {csv_file or '未找到'}")
    runner = ApiRunner(args.base) if use_api else EngineRunner()
    health = runner.health()
    print(f"服务: mode={health.get('mode')} model={health.get('model')}\n")

    practice = read_practice_questions(xlsx, csv_file)
    cases = []
    for dataset, rows in practice.items():
        for qid, question in rows.items():
            cases.append({
                "id": f"{'EX' if dataset == 'excel-main' else 'CSV'}-{qid}",
                "dataset": dataset,
                "question": question,
                "tokens": PRACTICE_TOKENS[dataset][qid],
                "auto_answer": "2025" if qid in AUTO_YEAR[dataset] else None,
            })
    cases.append(PDF_CASE)
    from tests.test_supplementary import COMPLETED, REJECTED
    for cid, dataset, question, tokens in COMPLETED:
        cases.append({"id": cid, "dataset": dataset, "question": question, "tokens": tokens})
    for cid, dataset, question, token in REJECTED:
        cases.append({"id": cid, "dataset": dataset, "question": question, "tokens": [token], "expect_error": True})

    passed, failed = run_cases(runner, cases, report)
    print(f"\n单题: {passed}/{passed + failed} 通过")
    chain_ok = run_chain(runner, report)
    flow_ok = run_clarification_flow(runner, report)
    print(f"追问链: {'通过' if chain_ok else '失败'} | 澄清流: {'通过' if flow_ok else '失败'}")

    report_path = PROJECT_ROOT / "evaluation" / "auto_evaluate_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告: {report_path}")
    sys.exit(0 if (failed == 0 and chain_ok and flow_ok) else 1)


if __name__ == "__main__":
    main()
