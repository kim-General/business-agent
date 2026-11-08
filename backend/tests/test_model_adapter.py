import json
import httpx
import pytest
from app.model_adapter import request_plan,ModelPlanningError


CATALOG={"dataset_id":"excel-main","categories":[{"id":"electronics","name":"数码家电"}],"sellers":[]}


def configure(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL","https://model.example/v1")
    monkeypatch.setenv("LLM_API_KEY","test-secret-not-real")
    monkeypatch.setenv("LLM_MODEL","test-model")


def reply(data):
    return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps(data)}}]},request=httpx.Request("POST","https://model.example/v1/chat/completions"))


def test_actual_adapter_request_shape_and_plan_validation(monkeypatch):
    configure(monkeypatch)
    def fake(url,**kwargs):
        assert url.endswith("/v1/chat/completions")
        assert kwargs["json"]["response_format"]["type"]=="json_object"
        assert "test-secret" not in json.dumps(kwargs["json"])
        return reply({"plan":{"dataset_id":"excel-main","metric":"goods_sales","periods":[{"label":"2025年4月","start":"2025-04-01","end":"2025-05-01"}]}})
    monkeypatch.setattr(httpx,"post",fake)
    result=request_plan("2025年4月销售额",{},CATALOG)
    assert result.plan.metric=="goods_sales" and result.plan.periods[0].start.year==2025


def test_llm_cannot_smuggle_sql_or_unknown_entity(monkeypatch):
    configure(monkeypatch)
    calls=[]
    def fake(*args,**kwargs):
        calls.append(1)
        return reply({"plan":{"dataset_id":"excel-main","sql":"DROP TABLE orders","filters":{"seller_ids":["unknown"]}}})
    monkeypatch.setattr(httpx,"post",fake)
    with pytest.raises(ModelPlanningError):
        request_plan("销售额",{},CATALOG)
    assert len(calls)==3


def test_response_format_rejected_then_retried_without(monkeypatch):
    configure(monkeypatch)
    calls = []
    def fake(url, **kwargs):
        calls.append(dict(kwargs["json"]))
        if "response_format" in kwargs["json"]:
            return httpx.Response(400, text='{"error":{"message":"response_format not supported"}}', request=httpx.Request("POST", url))
        return reply({"plan": {"dataset_id": "excel-main", "metric": "goods_sales", "periods": [{"label": "2025年4月", "start": "2025-04-01", "end": "2025-05-01"}]}})
    monkeypatch.setattr(httpx, "post", fake)
    result = request_plan("2025年4月销售额", {}, CATALOG)
    assert result.plan.metric == "goods_sales"
    assert len(calls) == 2
    assert "response_format" not in calls[1]


def test_missing_credentials_and_http_failure_not_silent_fallback(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY",raising=False)
    with pytest.raises(ModelPlanningError,match="需要配置"):
        request_plan("销售额",{},CATALOG)
    configure(monkeypatch)
    def fake(*args,**kwargs):
        raise httpx.ConnectError("no connection")
    monkeypatch.setattr(httpx,"post",fake)
    with pytest.raises(ModelPlanningError,match="连接失败"):
        request_plan("销售额",{},CATALOG)
