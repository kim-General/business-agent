"""Expected values from the supplied materials, kept outside runtime context."""
import json
from pathlib import Path

import pytest

from app.schemas import AnalysisPlan,Period
from app.warehouse import Warehouse
from app.query import QueryEngine
from app.settings import PROJECT_ROOT


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    warehouse=Warehouse(tmp_path_factory.mktemp("golden-data"),PROJECT_ROOT.parent)
    warehouse.ensure_ready()
    return warehouse


@pytest.fixture(scope="module")
def engine(warehouse):
    return QueryEngine(warehouse)


@pytest.mark.parametrize("dataset,metric,expected",[
    ("excel-main","goods_sales","4858920.84"),
    ("excel-main","order_count","10714"),
    ("excel-main","payment_amount","5368469.28"),
    ("excel-main","repeat_customers","2378"),
    ("csv-demo","goods_sales","75484.21"),
    ("csv-demo","order_count","159"),
    ("csv-demo","repeat_customers","46"),
])
def test_registered_metric_goldens(engine,dataset,metric,expected):
    result=engine.execute(AnalysisPlan(dataset_id=dataset,metric=metric))
    assert expected in json.dumps(result["rows"],ensure_ascii=False),result
    assert result["evidence"]["queries"]


def test_pdf_comparison_and_complete_contribution(engine):
    periods=[Period(label="2025年3月",start="2025-03-01",end="2025-04-01"),Period(label="2025年4月",start="2025-04-01",end="2025-05-01")]
    compare=engine.execute(AnalysisPlan(dataset_id="excel-main",metric="goods_sales",operation="compare",periods=periods))
    text=json.dumps(compare["rows"],ensure_ascii=False)
    assert all(value in text for value in ("167927.39","193119.68","25192.29")),compare
    assert compare["scope"]["comparison"]["growth"]=="0.150019",compare
    contribution=engine.execute(AnalysisPlan(dataset_id="excel-main",metric="goods_sales",operation="contribution",dimension="category",periods=periods))
    assert "65344.55" in json.dumps(contribution["rows"],ensure_ascii=False)
    assert "105245.50" in json.dumps(contribution["rows"],ensure_ascii=False)
    assert contribution["rows"][0]["dimension"]=="数码家电",contribution
    assert contribution["scope"]["comparison"]["group_delta_reconciled"] is True,contribution
    assert "数码家电" in contribution["summary"] and "65344.55" in contribution["summary"],contribution


def test_public_question_q04_category_rank(engine):
    rank=engine.execute(AnalysisPlan(dataset_id="excel-main",metric="goods_sales",operation="rank",dimension="category",
                                     periods=[Period(label="2025年4月",start="2025-04-01",end="2025-05-01")],
                                     top_n=1,sort_direction="desc"))
    assert rank["rows"][0]["dimension"]=="数码家电",rank
    assert rank["rows"][0]["value"]=="105245.50",rank


def test_public_question_q08_quantified(engine):
    explain=engine.execute(AnalysisPlan(dataset_id="excel-main",metric="relationship_explanation",operation="explain"))
    assert "4858920.84" in explain["summary"],explain
    assert "5489740.42" in explain["summary"],explain
    assert "5368469.28" in explain["summary"],explain
    assert "11051018.49" in explain["summary"],explain
    assert len(explain["rows"])==8,explain
    assert any("商品明细×支付直接关联" in str(q.get("grain","")) for q in explain["evidence"]["queries"]),explain


def test_average_metric_goldens(engine):
    monthly = engine.execute(AnalysisPlan(dataset_id="excel-main", metric="avg_monthly_goods_sales",
                                           periods=[Period(label="2025年1月至4月", start="2025-01-01", end="2025-05-01")]))
    assert monthly["scope"]["average"]["average"] == "163111.49", monthly
    assert monthly["scope"]["average"]["total"] == "652445.97", monthly
    assert [row["value"] for row in monthly["rows"]] == ["141217.91", "150180.99", "167927.39", "193119.68"]
    assert [row["order_count"] for row in monthly["rows"]] and len(monthly["rows"]) == 4
    aov = engine.execute(AnalysisPlan(dataset_id="excel-main", metric="avg_order_goods_sales",
                                      periods=[Period(label="2025年4月", start="2025-04-01", end="2025-05-01")]))
    assert aov["rows"][0]["value"] == "472.18", aov
    assert aov["rows"][0]["order_count"] == 409
    assert aov["rows"][0]["numerator"] == "193119.68"


def test_public_question_goldens_late_rate_and_review_score(engine):
    late = engine.execute(AnalysisPlan(dataset_id="excel-main", metric="late_rate", operation="rank", dimension="seller",
                                       periods=[Period(label="2025年5月", start="2025-05-01", end="2025-06-01")],
                                       min_orders=10, top_n=1, sort_direction="desc"))
    assert late["rows"][0]["dimension"] == "广州美妆个护商家004", late
    assert late["rows"][0]["numerator"] == 54 and late["rows"][0]["denominator"] == 59, late
    assert late["rows"][0]["value"] == "0.915254", late
    scores = engine.execute(AnalysisPlan(dataset_id="excel-main", metric="avg_review_score", operation="review_comparison"))
    by_dim = {row["dimension"]: row for row in scores["rows"]}
    assert by_dim["延迟"]["value"] == "2.00" and by_dim["延迟"]["order_count"] == 2323, scores
    assert by_dim["准时"]["value"] == "4.02" and by_dim["准时"]["order_count"] == 6909, scores
    assert scores["scope"]["review_sample"]["valid_review_orders"] == 9232, scores


def test_average_edge_cases(engine):
    with pytest.raises(ValueError):
        engine.execute(AnalysisPlan(dataset_id="excel-main", metric="avg_monthly_goods_sales",
                                    periods=[Period(label="非整月", start="2025-01-15", end="2025-03-10")]))
    out = engine.execute(AnalysisPlan(dataset_id="excel-main", metric="avg_monthly_goods_sales",
                                      periods=[Period(label="覆盖外", start="2023-06-01", end="2024-01-01")]))
    assert out["scope"]["average"]["average"] is None, out
    assert out["scope"]["average"]["missing_months"], out
    assert "不可得" in out["summary"], out
    monthly = engine.execute(AnalysisPlan(dataset_id="excel-main", metric="avg_monthly_goods_sales",
                                          periods=[Period(label="2025年1月至4月", start="2025-01-01", end="2025-05-01")]))
    assert "163111.4925" not in monthly["summary"], monthly["summary"]
    assert "163111.49元" in monthly["summary"], monthly["summary"]
    assert monthly["columns"][1]["label"] == "当月商品销售额", monthly["columns"]
    assert monthly["charts"][0]["title"] == "各月商品销售额", monthly["charts"]


def test_csv_practice_questions_end_to_end(engine, warehouse):
    from app.planner import plan_question
    catalog = warehouse.catalog("csv-demo")
    cases = [
        ("2025年1月至6月共有多少笔已送达订单？", ("159",)),
        ("2025年1月至6月已送达订单的商品销售额是多少？不含运费。", ("75484.21",)),
        ("4月商品销售额较3月增长多少？", ("18.43%", "12396.58", "14681.34")),
        ("4月商品销售额最高的品类是什么？金额是多少？", ("数码家电", "8564.60")),
        ("5月已送达订单不少于3笔的商家中，延迟率最高的是谁？", ("岭南好物", "85.71%")),
        ("有评价的已送达订单中，延迟订单与准时订单的平均评分分别是多少？", ("1.83", "4.00")),
        ("有多少自然客户完成了至少2笔已送达订单？", ("46",)),
        ("直接关联商品明细和支付表为什么会导致金额重复？", ("先按订单汇总", "75484.21", "83830.80")),
    ]
    for question, expected in cases:
        result = plan_question(question, {"dataset_id": "csv-demo"}, catalog)
        assert result.plan is not None, question
        res = engine.execute(result.plan)
        text = json.dumps(res, ensure_ascii=False)
        for token in expected:
            assert token in text, (question, token, text)
    growth = plan_question("4月商品销售额较3月增长多少？", {"dataset_id": "csv-demo"}, catalog)
    assert engine.execute(growth.plan)["scope"]["comparison"]["growth"] == "0.184306"


def test_version_catalog_has_no_reference_answers(warehouse):
    for dataset in warehouse.datasets():
        assert "practice_questions" not in dataset
        assert "expected_answer" not in json.dumps(dataset,ensure_ascii=False)
