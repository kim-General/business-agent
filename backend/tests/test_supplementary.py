"""Supplementary Q&A regression cases.

Every expected value in this file was verified twice: produced by the
planner+engine, then independently recomputed with raw SQL over the base
tables (not the analytic views). See evaluation/test_cases.md.
"""

import json

import pytest

from app.planner import plan_question, PlanningError
from app.query import QueryEngine
from app.settings import PROJECT_ROOT
from app.warehouse import Warehouse


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    warehouse = Warehouse(tmp_path_factory.mktemp("supplementary-data"), PROJECT_ROOT.parent)
    warehouse.ensure_ready()
    return warehouse


@pytest.fixture(scope="module")
def engine(warehouse):
    return QueryEngine(warehouse)


@pytest.fixture(scope="module")
def catalogs(warehouse):
    return {"excel-main": warehouse.catalog("excel-main"), "csv-demo": warehouse.catalog("csv-demo")}


COMPLETED = [
    ("E01", "excel-main", "2024年4月商品销售额较3月变化多少？", ["-1320.40", "-0.86%"]),
    ("E02", "excel-main", "2024年4月商品销售额最高的品类是什么？", ["家居生活", "48031.59"]),
    ("E03", "excel-main", "2025年6月商品销售额是多少？", ["227480.05", "446"]),
    ("E04", "excel-main", "2025年各月商品销售额", ["2025年12月", "324671.50", "2630580.08"]),
    ("E05", "excel-main", "2025年商品销售额按客户所在州分组", ["广东", "491833.04"]),
    ("E06", "excel-main", "2025年4月按支付方式拆分支付金额", ["credit_card", "144603.07"]),
    ("E07", "excel-main", "2025年4月已送达订单的运费是多少？", ["19295.39", "409"]),
    ("E08", "excel-main", "2024年1月至2025年12月低分评价率是多少？", ["16.77%", "1548"]),
    ("E09", "excel-main", "2025年5月已送达订单不少于20笔的商家中，延迟率最低的是谁？", ["广州美妆个护商家004", "91.53%"]),
    ("E10", "excel-main", "2025年客单价是多少？", ["458.53", "2630580.08", "5737"]),
    ("E11", "excel-main", "2025年3月比2月销售额变化多少？哪个品类贡献最大？", ["家居生活", "7162.37", "11.82%"]),
    ("E13", "excel-main", "2025年10月商品销售额是多少？", ["237786.23", "561"]),
    ("E14", "excel-main", "2024年7月至9月商品销售额", ["573512.81", "1272"]),
    ("E15", "excel-main", "2025年低分评价率是多少？", ["16.94%", "834"]),
    ("E16", "excel-main", "2025年4月15日至4月20日商品销售额", ["48414.51", "103"]),
    ("E17", "excel-main", "2025年上半年月均商品销售额是多少？", ["上半年", "181576.60", "1089459.61"]),
    ("E18", "excel-main", "2025年4月已送达订单客单价", ["472.18", "193119.68", "409"]),
    ("E19", "excel-main", "2025年4月商品销售额前3名的商家", ["深圳数码家电商家115", "8867.31"]),
    ("E21", "excel-main", "2025年各订单状态的订单数", ["delivered", "5737", "cancelled", "238"]),
    ("E22", "excel-main", "2025年4月商品销售额比3月，按客户州看贡献", ["北京", "8507.73", "25192.29"]),
    ("C09", "csv-demo", "2025年3月商品销售额是多少？", ["12396.58", "24"]),
    ("C10", "csv-demo", "2025年各月订单数", ["2025年6月", "35"]),
    ("C11", "csv-demo", "2025年6月已送达订单数是多少？", ["30"]),
    ("C12", "csv-demo", "2025年按支付方式的支付金额", ["credit_card", "65801.44"]),
    ("C13", "csv-demo", "2025年客单价", ["474.74", "75484.21", "159"]),
    ("C14", "csv-demo", "2025年1月至6月的月均商品销售额", ["12580.70", "共6个自然月"]),
    ("C15", "csv-demo", "2025年6月低分评价率", ["25.93%", "7"]),
    ("C16", "csv-demo", "2025年6月商品销售额最高的商家", ["沪上家居", "3616.08"]),
    ("C18", "csv-demo", "4月销售额比3月增长了多少", ["18.43%", "2284.76"]),
]

REJECTED = [
    ("E12", "excel-main", "2025年完成至少3笔已送达订单的自然客户有多少？", "复购客户口径固定为至少2笔"),
    ("E20", "excel-main", "2025年利润率是多少？", "没有成本"),
    ("C17", "csv-demo", "2025年利润", "没有成本"),
]


@pytest.mark.parametrize("cid,dataset,question,tokens", COMPLETED)
def test_supplementary_completed(cid, dataset, question, tokens, engine, catalogs):
    result = plan_question(question, {"dataset_id": dataset}, catalogs[dataset])
    assert result.plan is not None, question
    res = engine.execute(result.plan)
    text = json.dumps(res, ensure_ascii=False)
    for token in tokens:
        assert token in text, (cid, token, res.get("summary"))


@pytest.mark.parametrize("cid,dataset,question,error_token", REJECTED)
def test_supplementary_rejected(cid, dataset, question, error_token, catalogs):
    with pytest.raises(PlanningError, match=error_token):
        plan_question(question, {"dataset_id": dataset}, catalogs[dataset])


def test_supplementary_monthly_row_counts(engine, catalogs):
    yearly = plan_question("2025年各月商品销售额", {"dataset_id": "excel-main"}, catalogs["excel-main"])
    rows = engine.execute(yearly.plan)["rows"]
    assert len(rows) == 12
    assert rows[0]["dimension"] == "2025年1月" and rows[-1]["dimension"] == "2025年12月"
    csv_monthly = plan_question("2025年各月订单数", {"dataset_id": "csv-demo"}, catalogs["csv-demo"])
    csv_rows = engine.execute(csv_monthly.plan)["rows"]
    assert len(csv_rows) == 6
    assert csv_rows[0]["dimension"] == "2025年1月" and csv_rows[-1]["dimension"] == "2025年6月"


def test_supplementary_multiturn_chain(engine, catalogs):
    ctx = {"dataset_id": "excel-main"}
    steps = [
        ("2025年4月商品销售额是多少？", ["193119.68"]),
        ("只看家居生活", ["30053.57"]),
        ("按商家拆开", ["广州家居生活商家044", "2839.24"]),
        ("和上月比", ["49374.93", "-39.13%"]),
    ]
    for question, tokens in steps:
        result = plan_question(question, ctx, catalogs["excel-main"])
        assert result.plan is not None, question
        res = engine.execute(result.plan)
        text = json.dumps(res, ensure_ascii=False)
        for token in tokens:
            assert token in text, (question, token, res.get("summary"))
        ctx = result.plan.model_dump(mode="json")


def test_supplementary_year_clarification_flow(engine, catalogs):
    first = plan_question("4月销售额是多少？", {"dataset_id": "excel-main"}, catalogs["excel-main"])
    assert first.clarification and first.clarification.field == "year"
    answered = plan_question("4月销售额是多少？", {"dataset_id": "excel-main", "_answers": {"year": "2024"}}, catalogs["excel-main"])
    res = engine.execute(answered.plan)
    text = json.dumps(res, ensure_ascii=False)
    assert "152674.76" in text and "346" in text
