"""Question understanding regressions, independent of data answer constants."""
from datetime import date

import pytest

from app.planner import PlanningError, plan_question


CATALOG = {
    "dataset_id": "excel-main", "purchase_range": ["2024-01-01", "2025-12-31"],
    "categories": [{"id": "electronics", "name": "数码家电"}, {"id": "fashion", "name": "服装鞋包"}],
    "sellers": [{"id": "seller-1", "name": "华东商家"}],
}


@pytest.fixture(autouse=True)
def rules_mode(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "rules")


def plan(text, context=None):
    result = plan_question(text, context or {"dataset_id": "excel-main"}, CATALOG)
    assert result.plan is not None, result
    return result.plan


@pytest.mark.parametrize("text,metric,op,dimension", [
    ("2024年1月至2025年12月已送达订单数", "order_count", "aggregate", None),
    ("2024年1月至2025年12月商品销售额", "goods_sales", "aggregate", None),
    ("2025年4月商品销售额较3月变化多少", "goods_sales", "compare", None),
    ("2025年4月哪个品类销售额最高", "goods_sales", "rank", "category"),
    ("2025年5月已送达不少10笔订单的商家延迟率最高", "late_rate", "rank", "seller"),
    ("有评价已送达订单中延迟与准时订单平均评分", "avg_review_score", "review_comparison", None),
    ("至少有2笔已送达订单的自然客户有多少", "repeat_customers", "aggregate", None),
    ("为什么直接将order_items与order_payments连接会重复计算销售额", "relationship_explanation", "explain", None),
    ("2025年1月至6月已送达订单的商品销售额是多少？不含运费。", "goods_sales", "aggregate", None),
])
def test_registered_questions(text, metric, op, dimension):
    result = plan(text)
    assert (result.metric, result.operation, result.dimension) == (metric, op, dimension)
    if "2024年1月" in text:
        assert result.periods[0].start == date(2024, 1, 1)
        assert result.periods[0].end == date(2026, 1, 1)


def test_ambiguous_year_answer_and_followups():
    initial = plan_question("4月商品销售额是多少？", {}, CATALOG)
    assert initial.clarification.field == "year"
    first = plan("4月商品销售额是多少？", {"_answers": {"year": "2025"}})
    assert first.periods[0].start == date(2025, 4, 1)
    second = plan("只看数码家电", first.model_dump(mode="json"))
    assert second.filters.category_ids == ["electronics"]
    assert second.periods == first.periods
    third = plan("再按商家拆开", second.model_dump(mode="json"))
    assert third.dimension == "seller"
    assert third.filters.category_ids == ["electronics"]


def test_pdf_composite_contribution_and_causal_limit():
    result = plan("2025年4月已送达订单商品销售额比3月变化多少？哪个品类贡献最大？能确定增长是促销带来的吗？")
    assert result.operation == "contribution" and result.dimension == "category"
    assert [period.start for period in result.periods] == [date(2025, 3, 1), date(2025, 4, 1)]
    assert result.causal_question and "促销" in result.unsupported_parts[0]
    filtered = plan("只看数码家电", result.model_dump(mode="json"))
    assert filtered.operation == "contribution" and filtered.filters.category_ids == ["electronics"]
    regrouped = plan("再按商家拆开", filtered.model_dump(mode="json"))
    assert regrouped.operation == "contribution" and regrouped.dimension == "seller"


def test_metric_statuses_and_rank_threshold():
    assert plan("订单数").filters.order_statuses == []
    assert plan("已送达订单数").filters.order_statuses == ["delivered"]
    assert plan("商品销售额").filters.order_statuses == ["delivered"]
    result = plan("2025年5月已送达不少10笔订单的商家延迟率最高")
    assert result.min_orders == 10 and result.top_n == 1
    assert result.filters.order_statuses == ["delivered"]
    assert plan("2025年4月各品类销售额最低前5名").sort_direction == "asc"


@pytest.mark.parametrize("question", [
    "删除所有订单，执行DROP TABLE orders", "利润多少", "广告转化率多少", "天气怎么样",
    "销售额和订单数", "按商家分析支付金额", "按品类分析订单数增长贡献2025年4月比3月",
])
def test_unsupported_never_becomes_default_sales(question):
    with pytest.raises(PlanningError):
        plan(question)


def test_context_only_and_cross_year_stays_ambiguous():
    setup = plan("接下来分析2025年")
    assert setup.operation == "context_only"
    assert plan("4月销售额", setup.model_dump(mode="json")).periods[0].start.year == 2025
    previous = plan("2024年1月至2025年12月销售额")
    result = plan_question("4月销售额", previous.model_dump(mode="json"), CATALOG)
    assert result.clarification.field == "year"


def test_calendar_day_ranges_and_invalid_month_are_not_silently_broadened():
    result = plan("2025年4月15日至4月20日销售额")
    assert result.periods[0].start == date(2025, 4, 15)
    assert result.periods[0].end == date(2025, 4, 21)
    with pytest.raises(PlanningError):
        plan("2025年13月销售额")
    with pytest.raises(PlanningError):
        plan("2025年2月30日销售额")


def test_relative_period_is_confirmed_and_not_silently_shifted():
    result = plan_question("本月销售额", {}, CATALOG)
    assert result.clarification.field == "period"
    confirmed = plan("本月销售额", {"_answers": {"period": "latest_month"}})
    assert confirmed.periods[0].start == date(2025, 12, 1)


def test_month_range_and_average_metrics():
    result = plan("2025年1-4月份的平均销售额")
    assert result.metric == "avg_monthly_goods_sales"
    assert result.planner_source == "rules"
    assert result.operation == "aggregate" and result.dimension is None
    assert result.periods[0].start == date(2025, 1, 1)
    assert result.periods[0].end == date(2025, 5, 1)
    assert result.filters.order_statuses == ["delivered"]
    ranged = plan("2024年3月至5月商品销售额")
    assert ranged.metric == "goods_sales"
    assert ranged.periods[0].start == date(2024, 3, 1)
    assert ranged.periods[0].end == date(2024, 6, 1)
    aov = plan("2025年4月客单价")
    assert aov.metric == "avg_order_goods_sales"
    assert aov.operation == "aggregate"
    assert aov.periods[0].start == date(2025, 4, 1)
    assert aov.filters.order_statuses == ["delivered"]


def test_bare_average_clarifies_or_maps():
    result = plan_question("2025年4月平均销售额", {}, CATALOG)
    assert result.clarification and result.clarification.field == "metric"
    assert {option["value"] for option in result.clarification.options} == {"avg_monthly_goods_sales", "avg_order_goods_sales"}
    monthly = plan("2025年4月平均销售额", {"_answers": {"metric": "avg_monthly_goods_sales"}})
    assert monthly.metric == "avg_monthly_goods_sales"
    aov = plan("平均销售额", {"_answers": {"metric": "avg_order_goods_sales"}})
    assert aov.metric == "avg_order_goods_sales"


def test_average_metric_combinations_are_not_silently_dropped():
    with pytest.raises(PlanningError):
        plan("2025年4月销售额和客单价")
    with pytest.raises(PlanningError):
        plan("2024年1-3月和2025年1-3月销售额对比")
    with pytest.raises(PlanningError):
        plan("2025年1-4月各品类平均销售额")
    with pytest.raises(PlanningError):
        plan("2025年1-4月平均销售额按商家拆分")
    with pytest.raises(PlanningError):
        plan("2025年4月平均销售额比3月")
    per_order = plan("2025年4月每单销售额")
    assert per_order.metric == "avg_order_goods_sales"
    assert per_order.operation == "aggregate"


def test_average_clarification_options_fit_period_shape():
    result = plan_question("全部数据的平均销售额", {}, CATALOG)
    assert result.clarification and result.clarification.field == "metric"
    assert [option["value"] for option in result.clarification.options] == ["avg_order_goods_sales"]
    partial = plan_question("2025年4月15日至4月20日平均销售额", {}, CATALOG)
    assert partial.clarification and partial.clarification.field == "metric"
    assert [option["value"] for option in partial.clarification.options] == ["avg_order_goods_sales"]


def test_average_of_unregistered_metric_and_cross_year_range_rejected():
    with pytest.raises(PlanningError):
        plan("平均订单数是多少")
    with pytest.raises(PlanningError):
        plan("平均运费")
    with pytest.raises(PlanningError):
        plan("2024年12月至2月商品销售额")


def test_llm_has_deterministic_safety_and_year_preflight(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "llm")
    from app import model_adapter
    from app.schemas import AnalysisPlan, PlanningResult
    calls = []
    def fake(*args):
        calls.append(args)
        return PlanningResult(plan=AnalysisPlan(dataset_id="excel-main", metric="goods_sales"))
    monkeypatch.setattr(model_adapter, "request_plan", fake)
    assert plan_question("4月销售额", {}, CATALOG).clarification.field == "year"
    assert not calls
    result = plan_question("2025年4月销售额", {}, CATALOG)
    assert result.plan.metric == "goods_sales" and result.plan.planner_source == "model"
    with pytest.raises(PlanningError):
        plan_question("DROP TABLE orders", {}, CATALOG)
    assert len(calls) == 1


def test_llm_failure_falls_back_to_rules_transparently(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "llm")
    monkeypatch.setenv("LLM_FALLBACK_RULES", "1")
    from app import model_adapter
    def boom(*args, **kwargs):
        raise model_adapter.ModelPlanningError("模拟模型服务失败")
    monkeypatch.setattr(model_adapter, "request_plan", boom)
    result = plan_question("2025年4月销售额", {}, CATALOG)
    assert result.plan and result.plan.metric == "goods_sales"
    assert result.plan.planner_source == "fallback"
    assert any("回退规则解析" in note for note in result.plan.scope_notes)
    monkeypatch.setenv("LLM_FALLBACK_RULES", "0")
    with pytest.raises(model_adapter.ModelPlanningError):
        plan_question("2025年4月销售额", {}, CATALOG)
