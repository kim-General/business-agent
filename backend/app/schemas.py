from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

METRICS = {
    "goods_sales": "商品销售额",
    "avg_monthly_goods_sales": "月均商品销售额",
    "avg_order_goods_sales": "客单价",
    "order_count": "订单数",
    "payment_amount": "支付金额",
    "freight_amount": "运费",
    "late_rate": "延迟率",
    "avg_review_score": "平均评分",
    "low_review_rate": "低分评价率",
    "repeat_customers": "复购客户数",
    "quality": "数据质量",
    "relationship_explanation": "数据关联说明",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Period(StrictModel):
    label: str
    start: date
    end: date

    @model_validator(mode="after")
    def valid_range(self):
        if self.start >= self.end:
            raise ValueError("期间结束日期必须晚于开始日期（左闭右开）")
        return self


class PlanFilters(StrictModel):
    category_ids: list[str] = Field(default_factory=list)
    seller_ids: list[str] = Field(default_factory=list)
    order_statuses: list[Literal["delivered", "cancelled", "unavailable", "shipped"]] = Field(default_factory=lambda: ["delivered"])
    customer_states: list[str] = Field(default_factory=list)


class AnalysisPlan(StrictModel):
    schema_version: Literal["1"] = "1"
    dataset_id: str
    metric: Literal["goods_sales", "avg_monthly_goods_sales", "avg_order_goods_sales", "order_count", "payment_amount", "freight_amount", "late_rate", "avg_review_score", "low_review_rate", "repeat_customers", "quality", "relationship_explanation"] = "goods_sales"
    operation: Literal["aggregate", "trend", "rank", "compare", "contribution", "review_comparison", "quality", "explain", "context_only"] = "aggregate"
    periods: list[Period] = Field(default_factory=list, max_length=2)
    date_field: Literal["purchase_at", "delivered_at"] = "purchase_at"
    dimension: Literal["category", "seller", "customer_state", "payment_type", "order_status", "month"] | None = None
    filters: PlanFilters = Field(default_factory=PlanFilters)
    top_n: int = Field(default=10, ge=1, le=100)
    min_orders: int = Field(default=0, ge=0, le=100000)
    sort_direction: Literal["asc", "desc"] = "desc"
    causal_question: bool = False
    question: str = ""
    unsupported_parts: list[str] = Field(default_factory=list)
    scope_notes: list[str] = Field(default_factory=list)
    planner_source: Literal["", "rules", "model", "fallback", "replay"] = ""


class Clarification(StrictModel):
    field: str
    question: str
    options: list[dict[str, str]] = Field(default_factory=list)


class PlanningResult(StrictModel):
    plan: AnalysisPlan | None = None
    clarification: Clarification | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def one_outcome(self):
        if (self.plan is None) == (self.clarification is None):
            raise ValueError("必须返回分析计划或澄清之一")
        return self


class CreateSession(StrictModel):
    dataset_id: str = "excel-main"


class SubmitMessage(StrictModel):
    message: str = Field(min_length=1, max_length=4000)
    client_message_id: str = Field(min_length=1, max_length=120)
    expected_context_version: int = Field(ge=0)


class SubmitClarification(StrictModel):
    clarification_id: str
    answer: str = Field(min_length=1, max_length=1000)
    expected_context_version: int = Field(ge=0)
