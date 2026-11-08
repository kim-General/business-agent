import pytest
from app.store import Store, Conflict


def test_idempotency_and_session_serialization(tmp_path):
    store=Store(tmp_path/"app.sqlite3")
    session=store.create_session("excel-main")
    run,created=store.create_run(session["id"],"2025年销售额", "one",0)
    same,again=store.create_run(session["id"],"2025年销售额", "one",0)
    assert created and not again and same["id"]==run["id"]
    with pytest.raises(Conflict):
        store.create_run(session["id"],"2024年销售额","two",0)
    assert store.start(run["id"])
    store.finish_error(run["id"],"取消",cancel=True)
    assert not store.complete(run["id"],{"rows":[],"columns":[],"summary":"迟到结果"},{"dataset_id":"excel-main"})
    assert store.session(session["id"])["context_version"]==0
    assert len(store.session(session["id"],messages=True)["messages"])==2


def test_result_context_and_events_commit_before_visibility(tmp_path):
    store=Store(tmp_path/"app.sqlite3")
    session=store.create_session("excel-main")
    run,_=store.create_run(session["id"],"查询","one",0)
    store.start(run["id"])
    store.complete(run["id"],{"rows":[{"value":"12.34"}],"columns":[],"summary":"完成","evidence":{"queries":[]}}, {"dataset_id":"excel-main","metric":"goods_sales"})
    events=store.events(run["id"])
    ready=next(e for e in events if e["type"]=="result_ready")
    assert store.result(ready["payload"]["result_id"])["rows"][0]["value"]=="12.34"
    assert store.session(session["id"])["context_version"]==ready["payload"]["context_version"]==1
    assert store.events(run["id"],events[-1]["seq"])==[]
    with pytest.raises(Conflict):
        store.create_run(session["id"],"旧版本查询","two",0)
