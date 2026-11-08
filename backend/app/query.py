"""Deterministic metric compiler; values come from guarded, read-only SQL.

No question text, model output SQL, or reference answer enters this compiler.
Amounts stay DECIMAL from ingestion through JSON serialization.
"""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .schemas import AnalysisPlan, METRICS
from .sql_guard import validate_sql
from .warehouse import Warehouse, json_value


MONEY = {"goods_sales", "freight_amount", "payment_amount", "avg_monthly_goods_sales", "avg_order_goods_sales"}
RATES = {"late_rate", "low_review_rate"}
DELIVERED = {"goods_sales", "late_rate", "avg_review_score", "low_review_rate", "repeat_customers", "avg_monthly_goods_sales", "avg_order_goods_sales"}
DEFINITIONS = {
    "goods_sales": "商品明细 price 之和，不含运费；按品类/商家筛选时只合计对应明细。",
    "avg_monthly_goods_sales": "连续期间内已送达商品销售额按自然月平均：期间销售额合计 ÷ 日历月数（不含运费）；期间内无数据的月份计为0并单独提示。",
    "avg_order_goods_sales": "已送达商品销售额 ÷ 同范围去重订单数（每单平均，不含运费）。",
    "freight_amount": "商品明细 freight_value 之和，按品类/商家归属明细汇总。",
    "payment_amount": "支付明细 payment_value 之和；含运费，先按订单汇总，按支付方式时保留支付粒度。",
    "order_count": "筛选范围内 COUNT(DISTINCT order_id)；跨品类/商家的订单可属于多个组，各组不可相加。",
    "late_rate": "延迟订单数 / 全部已送达去重订单数；实际送达晚于预计送达记为延迟。缺少判定日期时保留全量分母，单独报告未知数，比例不可得。",
    "avg_review_score": "已送达且有有效评价的订单平均 review_score（1至5分），保留两位小数。",
    "low_review_rate": "已送达且有有效评价的去重订单中，review_score <= 2 的比例。",
    "repeat_customers": "以 customer_unique_id 识别自然客户，在所选期间内至少有2笔已送达去重订单的客户数。",
    "quality": "导入时对原始表执行的主键、外键、时间、金额及缺失值检查。",
    "relationship_explanation": "订单为交易主表；明细、支付、评价分别聚合后关联，避免一对多连接放大金额。",
}
DIM_LABELS = {"category": "品类", "seller": "商家", "customer_state": "客户省份", "payment_type": "支付方式", "order_status": "订单状态", "month": "月份", "delivery_status": "履约分组"}


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    out = value if isinstance(value, Decimal) else Decimal(str(value))
    if not out.is_finite():
        raise ValueError("计算结果不是有限数值")
    return out


def _formatted(value: Any, metric: str) -> Any:
    if value is None:
        return None
    number = _decimal(value)
    if metric in MONEY or metric == "avg_review_score":
        return format(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
    if metric in RATES:
        return format(number.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f")
    return int(number)


def _column(key: str, label: str, metric: str | None = None) -> dict:
    kind = "currency" if metric in MONEY else "percentage" if metric in RATES else "number" if metric == "avg_review_score" else "integer" if metric else "text"
    result = {"key": key, "label": label, "type": kind}
    if metric in MONEY:
        result["unit"] = "元"
    elif metric == "avg_review_score":
        result["unit"] = "分"
    return result


class QueryEngine:
    def __init__(self, warehouse: Warehouse):
        self.warehouse = warehouse

    @staticmethod
    def _validate(plan: AnalysisPlan):
        metric, op, dim = plan.metric, plan.operation, plan.dimension
        if op in {"compare", "contribution"} and len(plan.periods) != 2:
            raise ValueError("对比或贡献分析需要基期、当前期两个期间")
        if op not in {"compare", "contribution", "context_only", "explain"} and len(plan.periods) > 1:
            raise ValueError("该分析只支持一个期间")
        if dim == "payment_type" and metric != "payment_amount":
            raise ValueError("支付方式维度只支持支付金额；其他指标没有明确的支付拆分归属")
        if metric == "payment_amount" and (dim in {"category", "seller"} or plan.filters.category_ids or plan.filters.seller_ids):
            raise ValueError("支付金额不能按品类或商家拆分，支付记录没有对应的明细分摊规则")
        if metric in DELIVERED and plan.filters.order_statuses != ["delivered"]:
            raise ValueError("商品销售额、履约、评价和复购指标仅支持已送达订单口径")
        if metric == "repeat_customers" and plan.min_orders:
            raise ValueError("复购客户口径固定为至少2笔已送达订单，不支持其他门槛")
        if metric in {"avg_monthly_goods_sales", "avg_order_goods_sales"}:
            if op not in {"aggregate", "context_only"} or dim:
                raise ValueError("平均口径仅支持单期间汇总，不支持分组、对比或排名")
            if metric == "avg_monthly_goods_sales":
                if len(plan.periods) != 1:
                    raise ValueError("月均商品销售额需要一个连续期间")
                if plan.periods[0].start.day != 1 or plan.periods[0].end.day != 1:
                    raise ValueError("月均商品销售额需要整月期间（起止日都是月初），例如2025年1月1日至5月1日")
            if metric == "avg_order_goods_sales" and len(plan.periods) > 1:
                raise ValueError("客单价只支持一个期间或全部数据")
        if op == "review_comparison" and (metric not in {"avg_review_score", "low_review_rate"} or dim):
            raise ValueError("延迟/准时评价对比仅支持平均评分或低分评价率，不支持附加维度")
        if op == "contribution" and (not dim or metric not in MONEY):
            raise ValueError("贡献分析需要可加总金额指标及分组维度；去重订单、比率和复购不可直接相加")
        if op == "rank" and not dim:
            raise ValueError("排名分析需要分组维度")
        if op == "trend" and dim not in {None, "month"}:
            raise ValueError("趋势分析仅支持月份维度")
        if metric in {"quality", "relationship_explanation"} and op not in {"quality", "explain", "context_only", "aggregate"}:
            raise ValueError("该指标不支持所选分析方式")
        if op == "quality" and metric != "quality":
            raise ValueError("数据质量分析需要 quality 指标")
        if op == "explain" and metric != "relationship_explanation":
            raise ValueError("关联说明需要 relationship_explanation 指标")

    @staticmethod
    def _in(column: str, values: list, params: list) -> str:
        params.extend(values)
        return column + " IN (" + ",".join("?" for _ in values) + ")"

    def _item_filters(self, plan, alias, params):
        parts = []
        if plan.filters.category_ids:
            parts.append(self._in(f"{alias}.category_id", plan.filters.category_ids, params))
        if plan.filters.seller_ids:
            parts.append(self._in(f"{alias}.seller_id", plan.filters.seller_ids, params))
        return parts

    def _compile(self, plan: AnalysisPlan, period=None, dimension=None):
        """Return one SELECT, bound params and its documented input grain."""
        metric = plan.metric
        params: list = []
        dim = dimension
        money = metric in MONEY
        review = metric in {"avg_review_score", "low_review_rate"}
        table = "v_item_fact" if metric in {"goods_sales", "freight_amount", "avg_monthly_goods_sales", "avg_order_goods_sales"} else "v_payment_fact" if metric == "payment_amount" and dim == "payment_type" else "v_review_fact" if review else "v_order_fact"
        select = ["f.order_id", "f.customer_unique_id", "f.is_late"]
        joins = ""
        group_cols = []
        if dim in {"category", "seller"}:
            identifier = "category_id" if dim == "category" else "seller_id"
            label = "category_name" if dim == "category" else "seller_name"
            if table == "v_item_fact":
                select.extend([f"COALESCE(f.{identifier}, 'unknown') dimension_id", f"COALESCE(f.{label}, f.{identifier}, '未知') dimension"])
            else:
                item_parts = self._item_filters(plan, "i", params)
                item_where = " WHERE " + " AND ".join(item_parts) if item_parts else ""
                joins = f" JOIN (SELECT DISTINCT i.order_id, COALESCE(i.{identifier}, 'unknown') dimension_id, COALESCE(i.{label}, i.{identifier}, '未知') dimension FROM v_item_fact i{item_where}) d ON f.order_id=d.order_id"
                select.extend(["d.dimension_id", "d.dimension"])
            group_cols = ["dimension_id", "dimension"]
        elif dim == "month":
            select.append(f"CAST(DATE_TRUNC('month', f.{plan.date_field}) AS DATE) dimension")
            group_cols = ["dimension"]
        elif dim in {"customer_state", "payment_type", "order_status"}:
            select.append(f"COALESCE(f.{dim}, '未知') dimension")
            group_cols = ["dimension"]
        elif dim == "delivery_status":
            select.append("CASE WHEN f.is_late THEN '延迟' ELSE '准时' END dimension")
            group_cols = ["dimension"]
        conditions = []
        if period:
            conditions.extend([f"f.{plan.date_field} >= ?", f"f.{plan.date_field} < ?"])
            params.extend([period.start, period.end])
        if plan.filters.order_statuses:
            conditions.append(self._in("f.order_status", plan.filters.order_statuses, params))
        if plan.filters.customer_states:
            conditions.append(self._in("f.customer_state", plan.filters.customer_states, params))
        if table == "v_item_fact":
            conditions += self._item_filters(plan, "f", params)
        elif dim not in {"category", "seller"} and (plan.filters.category_ids or plan.filters.seller_ids):
            item_parts = self._item_filters(plan, "i", params)
            conditions.append("EXISTS (SELECT 1 FROM v_item_fact i WHERE i.order_id=f.order_id AND " + " AND ".join(item_parts) + ")")
        if dim == "delivery_status":
            conditions.append("f.is_late IS NOT NULL")
        if review:
            select.append("f.review_score")
            conditions.append("f.review_score BETWEEN 1 AND 5")
        if money:
            source = "price" if metric in {"goods_sales", "avg_monthly_goods_sales", "avg_order_goods_sales"} else "freight_value" if metric == "freight_amount" else "payment_value" if table == "v_payment_fact" else "payment_amount"
            select.append(f"f.{source} amount")
        base = "SELECT " + ", ".join(select) + f" FROM {table} f" + joins
        if conditions:
            base += " WHERE " + " AND ".join(conditions)
        grouping = ", ".join(group_cols)
        prefix = grouping + ", " if grouping else ""
        group_sql = " GROUP BY " + grouping if grouping else ""
        count = "COUNT(DISTINCT order_id)"
        if money:
            expressions = f"SUM(amount) value, {count} order_count, COUNT(DISTINCT CASE WHEN amount IS NULL THEN order_id END) unknown_amount_count"
        elif metric == "order_count":
            expressions = f"{count} value, {count} order_count"
        elif metric == "late_rate":
            expressions = f"COUNT(DISTINCT CASE WHEN is_late THEN order_id END) numerator, {count} denominator, COUNT(DISTINCT CASE WHEN is_late IS NULL THEN order_id END) unknown_count, {count} order_count"
        elif metric == "avg_review_score":
            expressions = f"SUM(review_score) numerator, {count} denominator, {count} order_count"
        elif metric == "low_review_rate":
            expressions = f"COUNT(DISTINCT CASE WHEN review_score<=2 THEN order_id END) numerator, {count} denominator, {count} order_count"
        else:
            customer_group = grouping + ", customer_unique_id" if grouping else "customer_unique_id"
            customer = f"SELECT {prefix}customer_unique_id, {count} customer_orders FROM base WHERE customer_unique_id IS NOT NULL GROUP BY {customer_group}"
            return (f"WITH base AS ({base}), customers AS ({customer}) SELECT {prefix}COUNT(CASE WHEN customer_orders>=2 THEN 1 END) value, SUM(customer_orders) order_count, COUNT(*) customer_count FROM customers{group_sql}", params, "自然客户×去重订单；客户期间内至少2笔已送达订单")
        return (f"WITH base AS ({base}) SELECT {prefix}{expressions} FROM base{group_sql}", params, "商品明细" if table == "v_item_fact" else "支付明细" if table == "v_payment_fact" else "评价订单" if review else "去重订单")

    @staticmethod
    def _run(con, query, evidence):
        sql, params, grain = query
        validate_sql(sql)
        con.execute("EXPLAIN " + sql, params).fetchall()
        cursor = con.execute(sql, params)
        names = [column[0] for column in cursor.description]
        result = [dict(zip(names, row)) for row in cursor.fetchall()]
        evidence.append({"sql": sql, "params": json_value(params), "grain": grain})
        return result

    @staticmethod
    def _value(row: dict, metric: str) -> Decimal | None:
        if row.get("unknown_amount_count", 0):
            return None
        if "denominator" in row:
            denominator = _decimal(row["denominator"])
            if not denominator or row.get("unknown_count", 0):
                return None
            return _decimal(row["numerator"]) / denominator
        return _decimal(row.get("value"))

    def _rows(self, records: list[dict], metric: str) -> list[dict]:
        rows = []
        for record in records:
            row = {k: json_value(v) for k, v in record.items() if k != "value"}
            row["value"] = _formatted(self._value(record, metric), metric)
            row["order_count"] = int(record.get("order_count") or 0)
            if "numerator" in row:
                row["numerator"] = json_value(record["numerator"])
                row["denominator"] = int(record["denominator"] or 0)
            rows.append(row)
        return rows

    @staticmethod
    def _display(value, metric):
        if value is None:
            return "不可得"
        if metric in RATES:
            return format((_decimal(value) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f") + "%"
        return str(value) + ("元" if metric in MONEY else "分" if metric == "avg_review_score" else "")

    @staticmethod
    def _sort(rows: list[dict], key: str, direction: str):
        # Missing values always sort last; ties have a stable dimension order.
        available = [r for r in rows if r.get(key) is not None]
        missing = [r for r in rows if r.get(key) is None]
        available.sort(key=lambda r: str(r.get("dimension_id", r.get("dimension", ""))))
        available.sort(key=lambda r: _decimal(r[key]), reverse=direction == "desc")
        return available + missing

    def execute(self, plan: AnalysisPlan | dict) -> dict:
        plan = plan if isinstance(plan, AnalysisPlan) else AnalysisPlan.model_validate(plan)
        self._validate(plan)
        manifest = self.warehouse.manifest(plan.dataset_id)
        profile = self.warehouse.profile(plan.dataset_id)
        warnings = list(plan.scope_notes)
        warnings.extend("未支持的需求：" + part for part in plan.unsupported_parts)
        if plan.causal_question:
            warnings.append("当前数据只能支持同期变化与相关性，不能证明因果；缺少促销、广告、成本等外部因素。")
        warnings.extend(f"数据质量：{c['name']}（{c['count']}条）；{c['detail']}" for c in profile["checks"] if c["status"] in {"warn", "fail"})
        queries = []
        metric, operation = plan.metric, plan.operation
        label = METRICS[metric]
        scope = {**plan.model_dump(mode="json"), "dataset_name": manifest["name"], "data_hash": manifest["hash"], "date_interval": "左闭右开", "status_scope": plan.filters.order_statuses or "全部状态", "definition": DEFINITIONS[metric]}
        result = {"title": label, "summary": "", "columns": [], "rows": [], "charts": [], "warnings": warnings, "scope": scope, "metric": metric,
                  "evidence": {"queries": queries, "metric_definition": DEFINITIONS[metric], "data_hash": manifest["hash"], "checks": profile["checks"], "warnings": warnings}}
        if operation == "context_only":
            result["title"] = "当前分析范围"
            period = "、".join(f"{p.label}（{p.start}至{p.end}，结束日不含）" for p in plan.periods) or "所选数据版本全部覆盖期"
            result["summary"] = f"【{manifest['name']}】当前指标为{label}；{period}；按{'下单' if plan.date_field == 'purchase_at' else '送达'}时间筛选。{DEFINITIONS[metric]}"
            return result
        if metric == "quality":
            result["columns"] = [_column("name", "检查项"), _column("status", "状态"), _column("count", "记录数", "order_count"), _column("detail", "说明")]
            result["rows"] = profile["checks"]
            failed = sum(c["status"] == "fail" for c in profile["checks"])
            result["summary"] = f"【{manifest['name']}】执行{len(profile['checks'])}项检查，{failed}项异常；关键数据问题{profile['critical_count']}条。" + "；".join(profile.get("limitations", []))
            return result
        if profile.get("critical_count", 0):
            raise ValueError("关键数据质量检查未通过，已阻止业务指标查询；请先查看数据质量。")
        if metric == "relationship_explanation":
            def disp(value, metric_id):
                return self._display(_formatted(value, metric_id), metric_id)
            with self.warehouse.connect_readonly(plan.dataset_id) as con:
                correct_items = self._run(con, self._compile(AnalysisPlan(dataset_id=plan.dataset_id, metric="goods_sales"), None, None), queries)[0]
                correct_payment = self._run(con, self._compile(AnalysisPlan(dataset_id=plan.dataset_id, metric="payment_amount"), None, None), queries)[0]
                fan_items = self._run(con, ("SELECT SUM(i.price) AS v FROM v_item_fact i JOIN v_payment_fact p ON i.order_id=p.order_id WHERE i.order_status='delivered'", [], "商品明细×支付直接关联（演示重复计算的放大效果）"), queries)[0]["v"]
                fan_payment = self._run(con, ("SELECT SUM(p.payment_value) AS v FROM v_item_fact i JOIN v_payment_fact p ON i.order_id=p.order_id WHERE p.order_status='delivered'", [], "商品明细×支付直接关联（演示重复计算的放大效果）"), queries)[0]["v"]
            goods_ok, goods_bad = disp(correct_items.get("value"), "goods_sales"), disp(fan_items, "goods_sales")
            pay_ok, pay_bad = disp(correct_payment.get("value"), "payment_amount"), disp(fan_payment, "payment_amount")
            result["summary"] = (f"【{manifest['name']}】" + DEFINITIONS[metric]
                + f"以当前数据集实测：已送达订单的正确商品销售额为{goods_ok}、支付金额为{pay_ok}；若把商品明细与支付表直接关联（一对多相乘），同一批订单的商品销售额会被放大为{goods_bad}、支付金额被放大为{pay_bad}，多出的金额即重复计算。"
                + "客户通过 customer_id 关联订单，customer_unique_id 用于复购去重；商品明细关联商品、品类与商家；地理表通过邮编辅助定位，不直接连接金额事实。商家延迟率表示包含该商家的订单履约状态，不能判定商家责任。")
            result["columns"] = [_column("relationship", "关系"), _column("grain", "安全统计粒度")]
            result["rows"] = [
                {"relationship": "订单 → 商品明细", "grain": "1对多；销售额与运费在明细层求和"},
                {"relationship": "订单 → 支付", "grain": "1对多；先按订单汇总后关联"},
                {"relationship": "订单 → 评价", "grain": "先检查一订单一评价；不能与商品明细直接展开"},
                {"relationship": "客户 → 订单", "grain": "按自然客户标识 customer_unique_id 去重"},
                {"relationship": "本数据集实测：正确商品销售额", "grain": f"{goods_ok}（已送达订单，明细直接求和）"},
                {"relationship": "本数据集实测：直接关联后的商品销售额", "grain": f"{goods_bad}（同一批订单被放大）"},
                {"relationship": "本数据集实测：正确支付金额", "grain": f"{pay_ok}（先按订单汇总）"},
                {"relationship": "本数据集实测：直接关联后的支付金额", "grain": f"{pay_bad}（同一批订单被放大）"},
            ]
            return result
        dimension = "month" if operation == "trend" else "delivery_status" if operation == "review_comparison" else plan.dimension
        if dimension in {"category", "seller"} and metric not in MONEY:
            warnings.append("同一订单或客户可能属于多个品类/商家，分组指标不可直接相加。")
        if metric == "late_rate" and dimension == "seller":
            warnings.append("商家延迟率按包含该商家的订单统计，表示履约关联，不代表责任归属。")
        if metric == "repeat_customers" and dimension:
            warnings.append("复购在各组内独立判定；同一客户可能出现在多个组，各组复购人数不可相加。")
        if metric in {"avg_review_score", "low_review_rate"}:
            warnings.append("评价指标只包含已送达且评分有效的订单；无评价订单不进入评价样本。")
        for period in plan.periods:
            coverage = manifest["purchase_range" if plan.date_field == "purchase_at" else "delivery_range"]
            if coverage[0] and (period.start.isoformat() < coverage[0][:10] or period.end.isoformat() > coverage[1][:10]):
                warnings.append(f"{period.label}与数据覆盖期{coverage[0][:10]}至{coverage[1][:10]}不完全一致，结果只统计现有数据。")
        with self.warehouse.connect_readonly(plan.dataset_id) as con:
            if metric == "avg_monthly_goods_sales":
                self._monthly_average(con, plan, result, queries)
            elif metric == "avg_order_goods_sales":
                self._average_per_order(con, plan, result, queries)
            elif operation in {"compare", "contribution"}:
                self._comparison(con, plan, dimension, result)
            else:
                period = plan.periods[0] if plan.periods else None
                records = self._run(con, self._compile(plan, period, dimension), queries)
                rows = self._rows(records, metric)
                if operation == "review_comparison":
                    all_reviews = self._run(con, self._compile(plan, period), queries)
                    all_count = int(all_reviews[0]["order_count"] or 0)
                    known_count = sum(r["order_count"] for r in rows)
                    result["scope"]["review_sample"] = {"valid_review_orders": all_count, "known_delivery_review_orders": known_count, "unknown_delivery_review_orders": all_count - known_count}
                    if all_count != known_count:
                        warnings.append(f"{all_count - known_count}笔有效评价订单缺少履约判定日期，未进入延迟/准时分组。")
                rows = [r for r in rows if r["order_count"] >= plan.min_orders]
                if dimension == "month":
                    rows.sort(key=lambda r: str(r.get("dimension") or ""))
                elif dimension:
                    rows = self._sort(rows, "value", plan.sort_direction)
                if operation == "rank":
                    rows = rows[:plan.top_n]
                    for index, row in enumerate(rows, 1):
                        row["rank"] = index
                if dimension == "month":
                    for row in rows:
                        raw = str(row.get("dimension") or "")
                        if re.match(r"\d{4}-\d{2}-\d{2}", raw):
                            row["dimension"] = f"{raw[:4]}年{int(raw[5:7])}月"
                    if rows:
                        values = [_decimal(row.get("value")) for row in rows]
                        if all(value is not None for value in values):
                            result["scope"]["monthly_total"] = _formatted(sum(values, Decimal(0)), metric)
                result["rows"] = rows
                columns = ([_column("rank", "排名", "order_count")] if operation == "rank" else [])
                if dimension:
                    columns += [_column("dimension", DIM_LABELS[dimension])]
                    if dimension in {"category", "seller"}:
                        columns += [_column("dimension_id", "标识")]
                columns += [_column("value", label, metric), _column("order_count", "样本订单数", "order_count")]
                if metric in RATES or metric == "avg_review_score":
                    columns += [_column("numerator", "评分合计" if metric == "avg_review_score" else "延迟订单数" if metric == "late_rate" else "低分订单数", "order_count"), _column("denominator", "样本分母", "order_count")]
                if metric == "late_rate":
                    columns += [_column("unknown_count", "履约时间未知订单数", "order_count")]
                if metric == "repeat_customers":
                    columns += [_column("customer_count", "自然客户数", "order_count")]
                result["columns"] = columns
                if dimension and rows:
                    result["charts"] = [{"type": "line" if dimension == "month" else "bar", "title": label, "x": "dimension", "y": ["value"]}]
                if not rows:
                    result["summary"] = "所选范围没有符合条件的样本。"
                elif not dimension:
                    result["summary"] = f"{label}为{self._display(rows[0]['value'], metric)}，样本订单数{rows[0]['order_count']}。"
                elif operation == "review_comparison":
                    result["summary"] = "；".join(f"{r['dimension']}订单{label}{self._display(r['value'], metric)}（有效评价{r['order_count']}笔）" for r in rows) + "。此对比为相关性观察，不能证明延迟导致评分变化。"
                elif operation == "rank":
                    result["summary"] = f"按{DIM_LABELS[dimension]}展示{len(rows)}组，{label}{'最高' if plan.sort_direction == 'desc' else '最低'}为{rows[0]['dimension']}，{self._display(rows[0]['value'], metric)}。"
                else:
                    result["summary"] = f"按{DIM_LABELS[dimension]}展示{len(rows)}组{label}，各组保留实际样本订单数。"
        if any(row.get("unknown_count", 0) for row in result["rows"]):
            warnings.append("存在无法判定履约是否延迟的已送达订单；保留全部已送达分母并将对应延迟率标记为不可得。")
        if any(row.get("value") is None for row in result["rows"]) and operation not in {"compare", "contribution"}:
            warnings.append("部分指标因无样本、金额未知或判定条件缺失而不可得，未替换为零。")
        result["warnings"][:] = list(dict.fromkeys(warnings))
        result["summary"] = f"【{manifest['name']}】{result['summary']}"
        return json_value(result)

    def _monthly_average(self, con, plan, result, queries):
        """Average monthly goods sales over one continuous month-aligned period.

        Months without any delivered order (or with unknown amounts) are
        reported as unavailable, never as zero sales; when any month is
        unavailable the overall average is unavailable too.
        """
        period = plan.periods[0]
        records = self._run(con, self._compile(plan, period, "month"), queries)
        by_month = {str(record["dimension"])[:7]: record for record in records}
        calendar_months = (period.end.year - period.start.year) * 12 + (period.end.month - period.start.month)
        total = Decimal(0)
        monthly_rows = []
        missing = []
        serial = period.start.year * 12 + period.start.month - 1
        for offset in range(calendar_months):
            year, month = serial // 12, serial % 12 + 1
            key = f"{year:04d}-{month:02d}"
            record = by_month.get(key)
            value = self._value(record, plan.metric) if record else None
            if value is not None:
                total += value
            monthly_rows.append({"dimension": f"{year}年{month}月", "value": _formatted(value, plan.metric), "order_count": int((record or {}).get("order_count") or 0)})
            if record is None or value is None:
                missing.append(key)
            serial += 1
        average = total / Decimal(calendar_months) if not missing and calendar_months else None
        if missing:
            result["warnings"].append(f"期间内{'、'.join(missing)}没有订单数据，无法判断该月销售额，不视为零；月均不可得。")
        result["rows"] = monthly_rows
        result["columns"] = [_column("dimension", "月份"), _column("value", "当月商品销售额", "goods_sales"), _column("order_count", "样本订单数", "order_count")]
        result["charts"] = [{"type": "line", "title": "各月商品销售额", "x": "dimension", "y": ["value"]}]
        result["scope"]["average"] = {"denominator_months": calendar_months, "missing_months": missing, "total": _formatted(total, plan.metric), "average": _formatted(average, plan.metric)}
        if average is None:
            result["summary"] = f"{period.label}（共{calendar_months}个自然月）月均商品销售额不可得：期间内{'、'.join(missing)}没有订单数据，未把缺失视为零。"
        else:
            result["summary"] = f"{period.label}（共{calendar_months}个自然月）月均商品销售额为{self._display(_formatted(average, plan.metric), plan.metric)}，期间合计{self._display(_formatted(total, plan.metric), plan.metric)}。"

    def _average_per_order(self, con, plan, result, queries):
        """Average goods sales per delivered order (AOV), one period or all-time."""
        period = plan.periods[0] if plan.periods else None
        records = self._run(con, self._compile(plan, period, None), queries)
        record = records[0]
        total = self._value(record, plan.metric)
        orders = int(record.get("order_count") or 0)
        average = total / Decimal(orders) if total is not None and orders else None
        result["rows"] = [{"value": _formatted(average, plan.metric), "order_count": orders, "numerator": _formatted(total, plan.metric), "denominator": orders}]
        result["columns"] = [_column("value", result["title"], plan.metric), _column("order_count", "样本订单数", "order_count"), _column("numerator", "商品销售额", "goods_sales"), _column("denominator", "分母订单数", "order_count")]
        result["scope"]["average_per_order"] = {"numerator": _formatted(total, plan.metric), "denominator_orders": orders, "value": _formatted(average, plan.metric)}
        span = f"{period.label}，" if period else "全部数据覆盖期，"
        result["summary"] = f"{span}客单价为{self._display(_formatted(average, plan.metric), plan.metric)}（商品销售额{self._display(_formatted(total, 'goods_sales'), 'goods_sales')} ÷ {orders}笔已送达订单，不含运费）。"

    def _comparison(self, con, plan, dimension, result):
        metric, evidence = plan.metric, result["evidence"]["queries"]
        base_records = self._run(con, self._compile(plan, plan.periods[0], dimension), evidence)
        current_records = self._run(con, self._compile(plan, plan.periods[1], dimension), evidence)
        # Compute platform totals separately: grouped distinct-order/rate metrics
        # are not additive, and a group may contain several items per order.
        totals = []
        if dimension:
            for period in plan.periods:
                totals.append(self._run(con, self._compile(plan, period), evidence)[0])
        else:
            totals = [base_records[0], current_records[0]]
        baseline, current = (self._value(r, metric) for r in totals)
        delta = current - baseline if baseline is not None and current is not None else None
        growth = delta / baseline if baseline and delta is not None else None
        result["scope"]["comparison"] = {"baseline_label": plan.periods[0].label, "current_label": plan.periods[1].label, "baseline": _formatted(baseline, metric), "current": _formatted(current, metric), "delta": _formatted(delta, metric), "growth": _formatted(growth, "late_rate"), "baseline_order_count": int(totals[0]["order_count"] or 0), "current_order_count": int(totals[1]["order_count"] or 0)}
        def key(row):
            return (row.get("dimension_id"), str(row.get("dimension", "")))
        left, right = ({key(r): r for r in records} for records in (base_records, current_records))
        rows = []
        for identity in sorted(left.keys() | right.keys(), key=str):
            a, b = left.get(identity), right.get(identity)
            # An absent monetary/count group contributes zero; an undefined
            # rate has no meaningful zero denominator.
            zero = Decimal(0) if metric in MONEY or metric == "order_count" or metric == "repeat_customers" else None
            av = self._value(a, metric) if a else zero
            bv = self._value(b, metric) if b else zero
            difference = bv - av if av is not None and bv is not None else None
            change = difference / av if av and difference is not None else None
            row = {"baseline_label": plan.periods[0].label, "current_label": plan.periods[1].label, "baseline": _formatted(av, metric), "current": _formatted(bv, metric), "delta": _formatted(difference, metric), "growth": _formatted(change, "late_rate"), "baseline_order_count": int((a or {}).get("order_count") or 0), "current_order_count": int((b or {}).get("order_count") or 0)}
            if dimension:
                row["dimension"] = json_value((a or b)["dimension"])
                if "dimension_id" in (a or b):
                    row["dimension_id"] = (a or b)["dimension_id"]
            for prefix, record in (("baseline", a), ("current", b)):
                if record and "denominator" in record:
                    row[prefix + "_denominator"] = int(record["denominator"] or 0)
                    row[prefix + "_numerator"] = json_value(record["numerator"])
                    if "unknown_count" in record:
                        row[prefix + "_unknown_count"] = record["unknown_count"]
            if plan.operation == "contribution":
                row["contribution"] = _formatted(difference / delta if delta and difference is not None else None, "late_rate")
            rows.append(row)
        if dimension == "month":
            for row in rows:
                raw = str(row.get("dimension") or "")
                if re.match(r"\d{4}-\d{2}-\d{2}", raw):
                    row["dimension"] = f"{raw[:4]}年{int(raw[5:7])}月"
        full_rows = rows
        result["scope"]["comparison"]["group_count"] = len(full_rows)
        full_group_delta = sum((_decimal(r["delta"]) for r in full_rows if r["delta"] is not None), Decimal(0))
        result["scope"]["comparison"]["full_group_delta"] = _formatted(full_group_delta, metric) if plan.operation == "contribution" else None
        if plan.operation == "contribution" and delta is not None:
            reconciled = abs(full_group_delta - delta) <= Decimal("0.01")
            result["scope"]["comparison"]["group_delta_reconciled"] = bool(reconciled)
            if not reconciled:
                result["warnings"].append("结果核验未通过：全部分组差额合计与整体变化不一致，请勿采用该贡献分解。")
        rows = [r for r in full_rows if max(r["baseline_order_count"], r["current_order_count"]) >= plan.min_orders]
        if dimension:
            rows = self._sort(rows, "delta", plan.sort_direction)
            rows = rows[:plan.top_n]
        result["rows"] = rows
        columns = [_column("dimension", DIM_LABELS[dimension])] if dimension else []
        if dimension in {"category", "seller"}:
            columns += [_column("dimension_id", "标识")]
        columns += [_column("baseline", plan.periods[0].label, metric), _column("current", plan.periods[1].label, metric), _column("delta", "变化额" if metric in MONEY else "变化值", metric), _column("growth", "变化率", "late_rate"), _column("baseline_order_count", "基期样本订单数", "order_count"), _column("current_order_count", "当前期样本订单数", "order_count")]
        if plan.operation == "contribution":
            columns += [_column("contribution", "占整体变化比例", "late_rate")]
        result["columns"] = columns
        result["charts"] = [{"type": "bar", "title": "变化贡献" if plan.operation == "contribution" else METRICS[metric], "x": "dimension" if dimension else "current_label", "y": ["delta"] if plan.operation == "contribution" else ["baseline", "current"]}]
        result["summary"] = f"{plan.periods[0].label}{METRICS[metric]}{self._display(_formatted(baseline, metric), metric)}，{plan.periods[1].label}{self._display(_formatted(current, metric), metric)}，变化{self._display(_formatted(delta, metric), metric)}，变化率{self._display(_formatted(growth, 'late_rate'), 'late_rate')}。"
        if plan.operation == "contribution":
            result["summary"] += f"贡献基于全部{result['scope']['comparison']['group_count']}个分组计算，展示{len(rows)}组；占比以整体净变化为分母，允许负值或超过100%。"
            if rows and rows[0].get("delta") is not None:
                result["summary"] += f"贡献最大的分组是{rows[0]['dimension']}，贡献额{self._display(_formatted(rows[0]['delta'], metric), metric)}。"
            if result["scope"]["comparison"].get("group_delta_reconciled") is True:
                result["summary"] += "全部分组差额合计已与整体变化核对一致。"
            if not delta:
                result["warnings"].append("整体净变化为零，变化贡献比例不可得。")
        if not baseline:
            result["warnings"].append("基期为零或无有效样本，变化率不可得。")
        if any(r.get("baseline_unknown_count", 0) or r.get("current_unknown_count", 0) for r in rows):
            result["warnings"].append("部分期间存在无法判定履约时间的订单，延迟率及变化值不可得。")
