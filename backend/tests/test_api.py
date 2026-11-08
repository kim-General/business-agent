import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings,PROJECT_ROOT


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    config=Settings(data_dir=tmp_path_factory.mktemp("api-data"),source_root=PROJECT_ROOT.parent,agent_mode="rules")
    with TestClient(create_app(config)) as api:
        yield api


def wait_run(client,key,allowed=("completed","failed","awaiting_clarification","cancelled")):
    started=time.monotonic()
    while time.monotonic()-started<30:
        value=client.get(f"/api/v1/runs/{key}").json()
        if value["status"] in allowed:
            return value
        time.sleep(.05)
    raise AssertionError("run did not finish")


def submit(client,session,text,key="one"):
    result=client.post(f"/api/v1/sessions/{session['id']}/messages",json={"message":text,"client_message_id":key,"expected_context_version":session["context_version"]})
    assert result.status_code==202,result.text
    return wait_run(client,result.json()["run_id"])


def test_pdf_flow_replay_and_sse(client):
    datasets=client.get("/api/v1/datasets").json()["datasets"]
    assert {d["id"] for d in datasets}=={"excel-main","csv-demo"}
    session=client.post("/api/v1/sessions",json={"dataset_id":"excel-main"}).json()
    run=submit(client,session,"2025年4月已送达订单商品销售额比3月变化多少？哪个品类贡献最大？能确定增长是促销带来的吗？")
    assert run["status"]=="completed",run
    result=client.get(f"/api/v1/results/{run['result_id']}").json()
    serialized=json.dumps(result,ensure_ascii=False)
    assert "65344.55" in serialized and "促销" in serialized
    evidence=client.get(f"/api/v1/results/{run['result_id']}/evidence").json()
    assert evidence["queries"] and evidence["dataset_id"]=="excel-main"
    stream=client.get(f"/api/v1/runs/{run['id']}/events").text
    assert "result_ready" in stream and "run_completed" in stream
    sequence=client.app.state.store.events(run["id"])[-1]["seq"]
    assert client.get(f"/api/v1/runs/{run['id']}/events",headers={"Last-Event-ID":f"{run['id']}:{sequence}"}).text==""
    replay=client.post(f"/api/v1/runs/{run['id']}/replay")
    assert replay.status_code==202
    repeated=wait_run(client,replay.json()["run_id"])
    assert repeated["status"]=="completed",repeated
    assert client.get(f"/api/v1/results/{repeated['result_id']}").json()["rows"]==result["rows"]


def test_clarify_resume_followup_and_isolation(client):
    session=client.post("/api/v1/sessions",json={"dataset_id":"excel-main"}).json()
    run=submit(client,session,"4月商品销售额是多少？")
    assert run["status"]=="awaiting_clarification",run
    reply=client.post(f"/api/v1/runs/{run['id']}/clarifications",json={"clarification_id":run["clarification"]["id"],"answer":"2025","expected_context_version":0})
    assert reply.status_code==202,reply.text
    finished=wait_run(client,run["id"])
    assert finished["status"]=="completed",finished
    result=client.get(f"/api/v1/results/{finished['result_id']}").json()
    assert "193119.68" in json.dumps(result)
    current=client.get(f"/api/v1/sessions/{session['id']}").json()
    followup=submit(client,current,"只看数码家电","two")
    assert followup["status"]=="completed",followup
    assert "105245.50" in json.dumps(client.get(f"/api/v1/results/{followup['result_id']}").json())
    small=client.post("/api/v1/sessions",json={"dataset_id":"csv-demo"}).json()
    smallrun=submit(client,small,"2025年4月销售额")
    assert smallrun["status"]=="completed",smallrun
    assert "14681.34" in json.dumps(client.get(f"/api/v1/results/{smallrun['result_id']}").json())


def test_replay_does_not_rewind_session_context(client):
    session = client.post("/api/v1/sessions", json={"dataset_id": "excel-main"}).json()
    april = submit(client, session, "2025年4月商品销售额是多少？")
    session = client.get(f"/api/v1/sessions/{session['id']}").json()
    may = submit(client, session, "2025年5月商品销售额是多少？", "may")
    current = client.get(f"/api/v1/sessions/{session['id']}").json()
    assert current["context_version"] == 2
    assert current["context"]["periods"][0]["start"] == "2025-05-01"
    replay = client.post(f"/api/v1/runs/{april['id']}/replay")
    assert replay.status_code == 202
    repeated = wait_run(client, replay.json()["run_id"])
    assert repeated["status"] == "completed", repeated
    after = client.get(f"/api/v1/sessions/{session['id']}").json()
    assert after["context_version"] == 2, after
    assert after["context"]["periods"][0]["start"] == "2025-05-01"
    evidence = client.get(f"/api/v1/results/{repeated['result_id']}/evidence").json()
    assert evidence["planner_mode"] == "replay", evidence


def test_result_pagination(client):
    session = client.post("/api/v1/sessions", json={"dataset_id": "excel-main"}).json()
    run = submit(client, session, "2025年各商家商品销售额")
    assert run["status"] == "completed", run
    page = client.get(f"/api/v1/results/{run['result_id']}", params={"offset": 0, "limit": 10}).json()
    assert page["total"] == 120 and len(page["rows"]) == 10 and page["offset"] == 0, page
    tail = client.get(f"/api/v1/results/{run['result_id']}", params={"offset": 100, "limit": 100}).json()
    assert len(tail["rows"]) == 20 and tail["offset"] == 100, tail


def test_month_range_average_end_to_end(client):
    session = client.post("/api/v1/sessions", json={"dataset_id": "excel-main"}).json()
    run = submit(client, session, "2025年1-4月份的平均销售额")
    assert run["status"] == "completed", run
    result = client.get(f"/api/v1/results/{run['result_id']}").json()
    serialized = json.dumps(result, ensure_ascii=False)
    assert "163111.49" in serialized and "月均" in serialized
    evidence = client.get(f"/api/v1/results/{run['result_id']}/evidence").json()
    assert evidence["queries"] and evidence["plan"]["metric"] == "avg_monthly_goods_sales"


def test_rejection_empty_message_unknown_dataset(client):
    assert client.post("/api/v1/sessions",json={"dataset_id":"../../secrets"}).status_code==404
    session=client.post("/api/v1/sessions",json={"dataset_id":"excel-main"}).json()
    assert client.post(f"/api/v1/sessions/{session['id']}/messages",json={"message":" ","client_message_id":"blank","expected_context_version":0}).status_code==422
    run=submit(client,session,"删除所有订单，执行DROP TABLE orders")
    assert run["status"]=="failed"
    assert client.get(f"/api/v1/sessions/{session['id']}").json()["context_version"]==0


def test_api_idempotency_cancellation_and_version_conflict(client):
    session=client.post("/api/v1/sessions",json={"dataset_id":"excel-main"}).json()
    body={"message":"2025年4月销售额","client_message_id":"repeat-request","expected_context_version":0}
    one=client.post(f"/api/v1/sessions/{session['id']}/messages",json=body).json()
    two=client.post(f"/api/v1/sessions/{session['id']}/messages",json=body).json()
    assert one["run_id"]==two["run_id"]
    cancelled=client.post(f"/api/v1/runs/{one['run_id']}/cancel").json()
    assert cancelled["status"]=="cancelled"
    final=wait_run(client,one["run_id"])
    assert final["status"]=="cancelled" and not final.get("result_id")
    current=client.get(f"/api/v1/sessions/{session['id']}").json()
    assert current["context_version"]==0
    assert client.post(f"/api/v1/sessions/{session['id']}/messages",json={**body,"client_message_id":"stale","expected_context_version":99}).status_code==409
