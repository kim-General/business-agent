"""Constrained Chinese business-question planner.

The rule path composes metric, time, operation and filters independently. It
produces only the registered plan schema; executable SQL belongs to query.py.
"""
from __future__ import annotations

import os
import re
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

from .schemas import AnalysisPlan, Clarification, Period, PlanningResult


class PlanningError(ValueError):
    """A question cannot be answered by the registered data/metric contracts."""


def _clarify(field: str, question: str, values: list[tuple[str, str]]) -> PlanningResult:
    return PlanningResult(clarification=Clarification(
        field=field, question=question,
        options=[{"label": label, "value": value} for label, value in values],
    ))


def _next_month(year: int, month: int) -> date:
    return date(year + (month == 12), 1 if month == 12 else month + 1, 1)


def _month(year: int, month: int) -> Period:
    return Period(label=f"{year}年{month}月", start=date(year, month, 1), end=_next_month(year, month))


def _year(year: int) -> Period:
    return Period(label=f"{year}年", start=date(year, 1, 1), end=date(year + 1, 1, 1))


def _coverage(catalog: dict) -> tuple[date, date] | None:
    bounds = catalog.get("purchase_range") or []
    if len(bounds) != 2 or not all(bounds):
        return None
    try:
        return date.fromisoformat(str(bounds[0])[:10]), date.fromisoformat(str(bounds[1])[:10])
    except ValueError:
        return None


def _previous(context: dict, dataset_id: str) -> AnalysisPlan | None:
    if "metric" not in context:
        return None
    value = {key: val for key, val in context.items() if not key.startswith("_")}
    try:
        plan = AnalysisPlan.model_validate(value)
    except ValueError:
        return None
    return plan if plan.dataset_id == dataset_id else None


def _preflight(question: str):
    if re.search(r"\b(?:drop|delete|insert|update|alter|truncate|create|attach|copy|install|load)\b|删除.*(?:订单|数据|表)|修改.*(?:数据库|订单记录)|写入数据库|执行\s*SQL", question, re.I):
        raise PlanningError("只支持只读经营分析，不能执行 SQL 或修改、删除数据。")
    if re.search(r"利润|毛利|净利|成本|转化率|流量|访问量|点击率|广告回报|ROI|库存|预测", question, re.I):
        raise PlanningError("当前数据没有成本、流量、广告或库存等所需记录，不能计算该指标。可分析销售额、订单、履约、评价和复购。")


def plan_question(question: str, context: dict, catalog: dict) -> PlanningResult:
    """Build one reviewable analysis plan or request one explicit clarification."""
    question = question.strip()
    if not question:
        raise PlanningError("请输入需要分析的经营问题。")
    _preflight(question)
    if os.getenv("AGENT_MODE", "rules").strip().lower() == "llm":
        dataset_id = catalog.get("dataset_id") or context.get("dataset_id")
        _, clarification = _time(question, context, catalog, _previous(context, dataset_id))
        if clarification:
            return clarification
        metrics = _metrics(question)
        if len(metrics) > 1:
            raise PlanningError("一次分析支持一个指标。请分别提问：" + "、".join(metrics) + "。")
        from .model_adapter import request_plan, ModelPlanningError
        try:
            result = request_plan(question, context, catalog)
            if result.plan:
                result.plan.planner_source = "model"
            return result
        except (PlanningError, ModelPlanningError) as exc:
            # Transparent, labeled fallback: model planning failed (service
            # error or invalid output), so the deterministic rules parser
            # takes over. The result is explicitly marked, never disguised
            # as a model success. Disable with LLM_FALLBACK_RULES=0.
            if os.getenv("LLM_FALLBACK_RULES", "1").strip() != "1":
                raise
            try:
                fallback = _rules(question, context, catalog)
            except PlanningError:
                raise exc
            if fallback.plan:
                fallback.plan.planner_source = "fallback"
                fallback.plan.scope_notes.insert(0, "模型规划未成功，已回退规则解析：" + str(exc)[:200])
            return fallback
    return _rules(question, context, catalog)


_NUMBER = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
           "十一": 11, "十二": 12}


def _number(value: str) -> int:
    return int(value) if value.isdigit() else _NUMBER[value]


def _time(question: str, context: dict, catalog: dict, previous: AnalysisPlan | None):
    """Return periods and an optional clarification, before any model is called."""
    answers = context.get("_answers") or {}
    coverage = _coverage(catalog)
    latest = _month(coverage[1].year, coverage[1].month) if coverage else None
    if answers.get("period"):
        answer = str(answers["period"])
        if answer in {"latest", "latest_month", "数据最新月份", "最新月份"} and latest:
            return [latest], None
        nested, clarification = _time(answer, {key: value for key, value in context.items() if key != "_answers"}, catalog, previous)
        if nested or clarification:
            return nested, clarification
    today = datetime.now(timezone(timedelta(hours=8))).date()
    relative = re.search(r"本月|这个月|当月|上个月|上月|今年|去年|本季度|上季度|最近\s*(\d+)\s*(?:个)?月", question)
    absolute_text = question[:relative.start()] + question[relative.end():] if relative else question
    if relative and not re.search(r"(?:19|20)\d{2}\s*年|(?:\d{1,2}|[一二三四五六七八九十]{1,2})\s*月", absolute_text):
        token = relative.group(0)
        if token in {"本月", "这个月", "当月"}:
            periods = [_month(today.year, today.month)]
        elif token in {"上月", "上个月"}:
            # 追问场景：“和上月比”在有单月上下文时，指上一确认期间的前一个月。
            if previous and len(previous.periods) == 1:
                p = previous.periods[0]
                span = (p.end.year - p.start.year) * 12 + (p.end.month - p.start.month)
                if span == 1:
                    prev_end = p.start
                    prev_start = date(p.start.year, p.start.month, 1) - timedelta(days=1)
                    base = date(prev_start.year, prev_start.month, 1)
                    return [Period(label=f"{base.year}年{base.month}月", start=base, end=prev_end), Period(label=p.label, start=p.start, end=p.end)], None
            end = date(today.year, today.month, 1)
            start = end - timedelta(days=1)
            periods = [_month(start.year, start.month)]
        elif token in {"今年", "去年"}:
            periods = [_year(today.year - (token == "去年"))]
        elif token in {"本季度", "上季度"}:
            month = (today.month - 1) // 3 * 3 + 1
            if token == "上季度":
                month -= 3
            year = today.year - (month < 1)
            month = month + 12 if month < 1 else month
            end_month = month + 3
            periods = [Period(label=f"{year}年第{(month - 1) // 3 + 1}季度", start=date(year, month, 1), end=date(year + (end_month > 12), end_month - 12 if end_month > 12 else end_month, 1))]
        else:
            count = int(relative.group(1))
            if not 1 <= count <= 120:
                raise PlanningError("最近月份数须在1到120之间。")
            serial = today.year * 12 + today.month - count
            periods = [Period(label=f"最近{count}个月", start=date(serial // 12, serial % 12 + 1, 1), end=_next_month(today.year, today.month))]
        if coverage and (periods[0].start > coverage[1] or periods[0].end <= coverage[0]):
            return [], _clarify("period", f"“{token}”按今天 {today.isoformat()} 解释，超出数据覆盖 {coverage[0]} 至 {coverage[1]}。要改查哪个期间？", [(f"数据最新月份（{latest.label}）", "latest_month")])
        return periods, None

    years = [int(value) for value in re.findall(r"(?<!\d)((?:19|20)\d{2})\s*年", question)]
    invalid_month = re.search(r"(?<!\d)(\d{1,2})\s*月", question)
    if invalid_month and not 1 <= int(invalid_month.group(1)) <= 12:
        raise PlanningError("月份必须在1到12之间。")
    months = list(re.finditer(r"(?<!\d)(?:(?P<year>(?:19|20)\d{2})\s*年\s*)?(?P<month>1[0-2]|0?[1-9]|十一|十二|[一二三四五六七八九十])\s*月", question))
    explicit_year = years[0] if years else (today.year - ("去年" in question) if re.search(r"今年|去年", question) else None)
    answer_year = re.search(r"(?:19|20)\d{2}", str(answers.get("year", "")))
    inherited_years = {period.start.year for period in previous.periods} if previous else set()
    if previous:
        inherited_years |= { (period.end - timedelta(days=1)).year for period in previous.periods }
    inferred_year = explicit_year or (int(answer_year.group()) if answer_year else None)
    if inferred_year is None and len(inherited_years) == 1:
        inferred_year = next(iter(inherited_years))
    if inferred_year is None and coverage and coverage[0].year == coverage[1].year:
        inferred_year = coverage[0].year

    half = re.search(r"(?:(?P<half_year>(?:19|20)\d{2})\s*年\s*)?(?P<half>上|下)半年", question)
    if half:
        half_year = int(half.group("half_year")) if half.group("half_year") else inferred_year
        if half_year is None:
            choices = range(coverage[0].year, coverage[1].year + 1) if coverage else []
            return [], _clarify("year", "请确认这半年属于哪一年？", [(f"{year}年", str(year)) for year in choices])
        if half.group("half") == "上":
            return [Period(label=f"{half_year}年上半年", start=date(half_year, 1, 1), end=date(half_year, 7, 1))], None
        return [Period(label=f"{half_year}年下半年", start=date(half_year, 7, 1), end=date(half_year + 1, 1, 1))], None

    mranges = list(re.finditer(r"(?:(?P<mrange_year>(?:19|20)\d{2})\s*年\s*)?(?P<mrange_start>\d{1,2}|[一二三四五六七八九十]{1,2})\s*(?:至|到|[-—~～])\s*(?P<mrange_end>\d{1,2}|[一二三四五六七八九十]{1,2})\s*月(?:份)?", question))
    if len(mranges) > 1:
        raise PlanningError("一次支持一个连续期间，或两个期间的对比；多个月份范围请分别提问。")
    if mranges:
        mrange = mranges[0]
        start_month = _number(mrange.group("mrange_start"))
        end_month = _number(mrange.group("mrange_end"))
        if not (1 <= start_month <= 12 and 1 <= end_month <= 12):
            raise PlanningError("月份必须在1到12之间。")
        if start_month > end_month:
            raise PlanningError("跨年的月份范围请分别写明年份，例如“2024年12月至2025年2月”。")
        range_year = int(mrange.group("mrange_year")) if mrange.group("mrange_year") else inferred_year
        if range_year is None:
            choices = range(coverage[0].year, coverage[1].year + 1) if coverage else []
            return [], _clarify("year", "请确认这些月份属于哪一年？", [(f"{year}年", str(year)) for year in choices])
        return [Period(label=f"{range_year}年{start_month}月至{end_month}月", start=date(range_year, start_month, 1), end=_next_month(range_year, end_month))], None

    if months:
        if inferred_year is None and any(not match.group("year") for match in months):
            choices = range(coverage[0].year, coverage[1].year + 1) if coverage else []
            return [], _clarify("year", "请确认这些月份属于哪一年？当前数据和上下文不能唯一确定年份。", [(f"{year}年", str(year)) for year in choices])
        day_matches = list(re.finditer(r"(?<!\d)(?:(?P<year>(?:19|20)\d{2})\s*年\s*)?(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*[日号]", question))
        if day_matches:
            if len(day_matches) != len(months) or len(day_matches) > 2:
                raise PlanningError("请为日期范围的两端都写明月和日，避免混用整月与单日。")
            dates = []
            current_year = inferred_year
            try:
                for match in day_matches:
                    current_year = int(match.group("year")) if match.group("year") else current_year
                    dates.append(date(current_year, int(match.group("month")), int(match.group("day"))))
            except ValueError:
                raise PlanningError("日期值无效，请检查月份和日。") from None
            if dates[0] > dates[-1]:
                raise PlanningError("期间开始日期不能晚于结束日期。")
            return [Period(label="至".join(value.isoformat() for value in dates), start=dates[0], end=dates[-1] + timedelta(days=1))], None
        periods = []
        current_year = inferred_year
        for match in months:
            current_year = int(match.group("year")) if match.group("year") else current_year
            periods.append(_month(current_year, _number(match.group("month"))))
        if len(periods) > 2:
            raise PlanningError("一次支持一个连续期间，或两个期间的对比。请简化月份范围。")
        if len(periods) == 2:
            between = question[months[0].end():months[1].start()]
            if re.search(r"至|到|[-—~～]", between):
                if periods[0].start > periods[1].start:
                    raise PlanningError("期间开始月份不能晚于结束月份。")
                return [Period(label=f"{periods[0].label}至{periods[1].label}", start=periods[0].start, end=periods[1].end)], None
            if re.search(r"比|较|对比|相比|环比|增长|变化|贡献|与|和", question):
                # In the usual “4月比3月”, the earlier calendar period is baseline.
                return sorted(periods, key=lambda period: period.start), None
            raise PlanningError("两个离散月份需要说明对比关系，或用“至”指定连续范围。")
        if re.search(r"环比|比上月|较上月|相比上月", question):
            first = periods[0].start - timedelta(days=1)
            return [_month(first.year, first.month), periods[0]], None
        if re.search(r"同比|比去年|较去年", question):
            return [_month(periods[0].start.year - 1, periods[0].start.month), periods[0]], None
        return periods, None

    iso_dates = re.findall(r"\b((?:19|20)\d{2}-\d{2}-\d{2})\b", question)
    if iso_dates:
        try:
            dates = [date.fromisoformat(value) for value in iso_dates]
        except ValueError:
            raise PlanningError("日期格式或日期值无效。") from None
        if len(dates) > 2 or dates[0] > dates[-1]:
            raise PlanningError("请提供有效的连续日期范围。")
        return [Period(label="至".join(iso_dates), start=dates[0], end=dates[-1] + timedelta(days=1))], None

    quarter = re.search(r"(?:第)?([1-4一二三四])\s*季度|Q([1-4])", question, re.I)
    if quarter:
        if inferred_year is None:
            choices = range(coverage[0].year, coverage[1].year + 1) if coverage else []
            return [], _clarify("year", "请确认该季度属于哪一年？", [(f"{year}年", str(year)) for year in choices])
        q = _number(quarter.group(1) or quarter.group(2))
        start = date(inferred_year, (q - 1) * 3 + 1, 1)
        end = date(inferred_year + (q == 4), 1 if q == 4 else q * 3 + 1, 1)
        return [Period(label=f"{inferred_year}年第{q}季度", start=start, end=end)], None
    if years:
        if len(years) == 2 and re.search(r"至|到|[-—~～]", question):
            if years[0] > years[1]:
                raise PlanningError("开始年份不能晚于结束年份。")
            return [Period(label=f"{years[0]}年至{years[1]}年", start=date(years[0], 1, 1), end=date(years[1] + 1, 1, 1))], None
        if len(years) == 2 and re.search(r"比|较|对比|相比|增长|变化|贡献", question):
            return [_year(year) for year in sorted(years)], None
        if len(years) > 1:
            raise PlanningError("请明确年份是连续范围还是期间对比。")
        return [_year(years[0])], None
    if re.search(r"全部(?:历史|期间)?|全历史|整个数据|全量", question):
        return [], None
    return deepcopy(previous.periods) if previous else [], None


def _metrics(question: str) -> list[str]:
    tests = [
        ("relationship_explanation", r"items.*payments|payments.*items|一对多|重复计算|重复统计|(?:明细|商品表).*(?:支付|付款).*(?:关联|连接|重复)|(?:关联|连接).*(?:重复|放大)|表.{0,5}关系"),
        ("quality", r"数据质量|质量检查|缺失值|孤儿记录|主键|外键|异常数据"),
        ("repeat_customers", r"复购|重复购买|至少有?\s*(?:\d+|[两二])\s*(?:笔|个|次|单).{0,6}(?:订单)?(?:的)?(?:自然)?客户|(?:自然)?客户.{0,10}至少有?\s*(?:\d+|[两二])\s*(?:笔|个|次|单)"),
        ("low_review_rate", r"低分(?:评价)?率|差评率|低评分比例"),
        ("avg_review_score", r"平均(?:评价)?评分|平均评价|评分均值|平均分|评价分数|评分"),
        ("late_rate", r"延迟率|延误率|逾期率|延迟比例|迟到率"),
        ("payment_amount", r"支付(?:总)?金额|支付额|实付金额|实付总额|付款金额"),
        ("freight_amount", r"(?<!不含)(?<!不包含)(?<!含)运费|物流费用"),
        ("avg_monthly_goods_sales", r"月均|平均每月|每月平均|月度平均"),
        ("avg_order_goods_sales", r"客单价|平均每单|每单平均|单均|每单"),
        ("goods_sales", r"(?:商品)?销售(?:总)?额|销售金额|成交额|GMV"),
        ("order_count", r"订单(?:总)?数|订单量|多少(?:笔|个|单)?(?:已送达)?订单|订单.*多少(?:笔|单)|几笔订单"),
    ]
    matches = [metric for metric, pattern in tests if re.search(pattern, question, re.I)]
    if "relationship_explanation" in matches:
        return ["relationship_explanation"]
    if "quality" in matches:
        return ["quality"]
    # The broader “评分” token is part of the low-score rate wording.
    if "low_review_rate" in matches and "avg_review_score" in matches and not re.search(r"平均|均值", question):
        matches.remove("avg_review_score")
    if "repeat_customers" in matches:
        matches = [metric for metric in matches if metric != "order_count"]
    if "avg_monthly_goods_sales" in matches or "avg_order_goods_sales" in matches:
        # Only drop goods_sales when the 销售额 token belongs to the average
        # phrase itself (e.g. "月均销售额"). A separate "销售额和客单价"
        # asks for two metrics and must hit the multi-metric guard instead.
        remainder = question
        for metric, pattern in [
            ("avg_monthly_goods_sales", r"(?:月均|平均每月|每月平均|月度平均)\s*(?:商品)?(?:销售(?:总)?额|销售金额|成交额|GMV)?"),
            ("avg_order_goods_sales", r"(?:客单价|平均每单|每单平均|单均|每单)\s*(?:商品)?(?:销售(?:总)?额|销售金额|成交额|GMV)?"),
        ]:
            if metric in matches:
                remainder = re.sub(pattern, "", remainder, count=1, flags=re.I)
        if not re.search(r"(?:商品)?销售(?:总)?额|销售金额|成交额|GMV", remainder, re.I):
            matches = [metric for metric in matches if metric != "goods_sales"]
    return matches


def _rules(question: str, context: dict, catalog: dict) -> PlanningResult:
    dataset_id = catalog.get("dataset_id") or context.get("dataset_id")
    if not dataset_id:
        raise PlanningError("尚未选择数据集。")
    previous = _previous(context, dataset_id)
    periods, clarification = _time(question, context, catalog, previous)
    if clarification:
        return clarification
    metrics = _metrics(question)
    if len(metrics) > 1:
        raise PlanningError("一次分析支持一个指标。请分别提问：" + "、".join(metrics) + "。")
    followup = bool(previous and (not metrics or re.search(r"只看|仅看|再按|继续|同样|改为|换成|其中", question)))
    plan = previous.model_copy(deep=True) if followup else AnalysisPlan(dataset_id=dataset_id)
    plan.question = question
    plan.periods = periods
    plan.causal_question = False
    plan.unsupported_parts = []
    plan.scope_notes = []
    if metrics:
        plan.metric = metrics[0]
    if not metrics:
        if re.fullmatch(r"(?:接下来|以后|下面|本次)?\s*(?:分析|查询|看|设为|使用|切换到)?\s*(?:19|20)\d{2}年(?:的数据|数据)?[。！!，,\s]*", question):
            plan.operation = "context_only"
            plan.scope_notes.append("已设置后续分析期间，尚未查询经营指标。")
            return PlanningResult(plan=plan)
        modifier = re.search(r"只看|仅看|再按|按.+(?:拆|分|汇总)|改为|换成|继续|同样|其中|(?:19|20)\d{2}年|\d{1,2}月|本月|上月|去年|今年|季度|趋势|排名|排行|贡献|促销|升序|降序|不限|取消.+筛选|全部品类|全部商家", question)
        if not previous or not modifier:
            raise PlanningError("未识别到已注册的经营指标。可询问销售额、订单数、支付金额、运费、延迟率、评分、复购或数据质量。")
    return _compose(plan, question, catalog, followup, context.get("_answers") or {})


def _entity_ids(question: str, entities: list[dict]) -> list[str]:
    matched = []
    for entity in entities:
        name = str(entity.get("name") or "")
        identifier = str(entity["id"])
        if name and name in question or re.search(r"(?<![A-Za-z0-9_])" + re.escape(identifier) + r"(?![A-Za-z0-9_])", question, re.I):
            matched.append(identifier)
    return matched


def _avg_options(periods: list[Period]) -> list[tuple[str, str]]:
    """Average-metric clarification options that fit the requested period shape."""
    options: list[tuple[str, str]] = []
    if len(periods) == 1:
        start, end = periods[0].start, periods[0].end
        if start.day == 1 and end.day == 1:
            options.append(("按月份平均（月均销售额）", "avg_monthly_goods_sales"))
    if not periods:
        options.append(("按订单平均（客单价，全部数据）", "avg_order_goods_sales"))
    elif len(periods) == 1:
        options.append(("按订单平均（客单价）", "avg_order_goods_sales"))
    return options


def _compose(plan: AnalysisPlan, question: str, catalog: dict, followup: bool, answers: dict) -> PlanningResult:
    """Combine orthogonal clauses and reject combinations without a definition."""
    metric = plan.metric
    filters = plan.filters
    if re.search(r"平均|均值", question) and metric not in {"avg_monthly_goods_sales", "avg_order_goods_sales", "avg_review_score"}:
        if metric == "goods_sales":
            chosen = str(answers["metric"]) if answers.get("metric") else None
            if chosen:
                if chosen not in {"avg_monthly_goods_sales", "avg_order_goods_sales"}:
                    raise PlanningError("请选择有效的平均口径。")
                if chosen == "avg_monthly_goods_sales":
                    if len(plan.periods) != 1:
                        raise PlanningError("月均销售额需要一个连续期间，例如“2025年1-4月份的平均销售额”。")
                    if plan.periods[0].start.day != 1 or plan.periods[0].end.day != 1:
                        raise PlanningError("月均销售额需要整月期间（起止日都是月初）。")
                elif len(plan.periods) > 1:
                    raise PlanningError("客单价只支持一个期间或全部数据，不支持期间对比。")
                plan.metric = chosen
                metric = plan.metric
            elif len(plan.periods) == 1:
                start, end = plan.periods[0].start, plan.periods[0].end
                span = (end.year - start.year) * 12 + (end.month - start.month)
                if start.day == 1 and end.day == 1 and span >= 2:
                    plan.metric = "avg_monthly_goods_sales"
                    metric = plan.metric
                    plan.scope_notes.append("“平均”按连续期间的月均口径解释（期间销售额合计 ÷ 日历月数）。")
                else:
                    return _clarify("metric", "“平均销售额”按什么平均？", _avg_options(plan.periods))
            elif len(plan.periods) == 0:
                return _clarify("metric", "“平均销售额”按什么平均？", _avg_options(plan.periods))
            else:
                raise PlanningError("平均口径暂不支持期间对比；请分别提问各期间的平均值。")
        else:
            raise PlanningError("当前没有该指标的“平均”口径。可询问平均销售额（月均或每单）、平均评分。")
    category_ids = _entity_ids(question, catalog.get("categories", []))
    seller_ids = _entity_ids(question, catalog.get("sellers", []))
    if category_ids:
        filters.category_ids = category_ids
    if seller_ids:
        filters.seller_ids = seller_ids
    if re.search(r"所有品类|全部品类|取消品类筛选|不限品类", question):
        filters.category_ids = []
    if re.search(r"所有商家|全部商家|取消商家筛选|不限商家", question):
        filters.seller_ids = []
    if re.search(r"(?:只看|仅看|筛选|限定)(?:的)?\s*[^，。？?]{1,30}", question) and not (category_ids or seller_ids):
        if not re.search(r"只看(?:已送达|已发货|取消|不可用|全部)|仅看(?:已送达|已发货|取消|不可用|全部)|(?:客户|州|状态)", question):
            raise PlanningError("未在当前数据目录中找到该品类或商家，请使用目录中的名称或ID。")

    statuses = []
    for status, pattern in [("delivered", r"已送达|已签收|已完成|delivered"), ("cancelled", r"已取消|取消订单|已撤销|cancelled|canceled"), ("shipped", r"已发货|运输中|shipped"), ("unavailable", r"不可用|unavailable")]:
        if re.search(pattern, question, re.I):
            statuses.append(status)
    delivered_only = {"goods_sales", "late_rate", "avg_review_score", "low_review_rate", "repeat_customers", "avg_monthly_goods_sales", "avg_order_goods_sales"}
    if metric in delivered_only:
        if statuses and statuses != ["delivered"]:
            raise PlanningError("该指标定义采用已送达订单，不能改为其他订单状态。普通订单数支持按状态统计。")
        filters.order_statuses = ["delivered"]
    elif statuses:
        filters.order_statuses = statuses
    elif not followup or re.search(r"全部状态|所有状态|不分状态", question):
        filters.order_statuses = []

    states = re.search(r"(?:客户(?:所在)?州|州)\s*[:：为是=]?\s*([A-Za-z]{2}(?:\s*[,、和与]\s*[A-Za-z]{2})*)", question)
    if states:
        filters.customer_states = re.findall(r"[A-Za-z]{2}", states.group(1).upper())
    if re.search(r"所有州|全部州|不限州", question):
        filters.customer_states = []

    dimensions = []
    for dim, pattern in [("category", r"按(?:商品)?品类|各(?:个)?品类|哪个品类|哪些品类|品类.{0,12}(?:最高|最低|排名|排行|贡献|增长)|(?:最高|最低|最大|最小).{0,8}品类|(?:前|后)\s*\d+\s*(?:名|家|个)?\s*(?:的)?(?:商品)?品类"),
                         ("seller", r"按商家|各(?:个)?商家|哪个商家|哪些商家|商家.{0,20}(?:最高|最低|排名|排行)|(?:最高|最低).{0,8}商家|(?:前|后)\s*\d+\s*(?:名|家|个)?\s*(?:的)?商家"),
                         ("customer_state", r"按(?:客户(?:所在)?)?州|各州|哪个州|各客户州|(?:前|后)\s*\d+\s*(?:名|家|个)?\s*(?:的)?(?:客户)?州"),
                         ("payment_type", r"按支付方式|各支付方式|哪种支付方式"),
                         ("order_status", r"按(?:订单)?状态|各(?:订单)?状态"),
                         ("month", r"按月|每月|逐月|月度|各月|月份趋势")]:
        if re.search(pattern, question):
            dimensions.append(dim)
    if len(dimensions) > 1:
        raise PlanningError("一次分析支持一个分组维度，请分别按品类、商家或月份提问。")
    if dimensions:
        plan.dimension = dimensions[0]
    elif answers.get("dimension"):
        resolved = {"品类": "category", "商家": "seller", "客户州": "customer_state"}.get(str(answers["dimension"]), str(answers["dimension"]))
        if resolved not in {"category", "seller", "customer_state"}:
            raise PlanningError("请选择有效的分组维度。")
        plan.dimension = resolved

    minimum = re.search(r"(?:不少于?|至少|不低于|>=|≥)\s*(\d+)\s*(?:笔|个|单)?(?:已送达)?(?:订单)?", question)
    if minimum and metric == "repeat_customers":
        if int(minimum.group(1)) != 2:
            raise PlanningError("复购客户口径固定为至少2笔已送达订单，暂不支持更高门槛；请先查复购客户数再另作筛选分析。")
    if minimum and metric != "repeat_customers":
        plan.min_orders = int(minimum.group(1))
        if plan.min_orders > 100000:
            raise PlanningError("最低订单门槛须不超过100000。")
    top = re.search(r"(?:前|后|top\s*)\s*(\d+|十|[一二三四五六七八九])\s*(?:名|个|家|类)?", question, re.I)
    if top:
        plan.top_n = _number(top.group(1))
        if not 1 <= plan.top_n <= 100:
            raise PlanningError("排名数量须在1到100之间。")
    elif re.search(r"(?:哪个|哪家|哪一|最)[^？?。]{0,20}(?:最高|最低|最大|最小)|最高|最低|最大|最小", question):
        plan.top_n = 1
    if re.search(r"最低|最小|最少|升序|从低到高|从小到大|后\s*\d+", question):
        plan.sort_direction = "asc"
    elif re.search(r"最高|最大|最多|降序|从高到低|从大到小|前\s*\d+", question):
        plan.sort_direction = "desc"

    if metric == "quality":
        plan.operation, plan.dimension = "quality", None
        plan.periods = []
    elif metric == "relationship_explanation":
        plan.operation, plan.dimension = "explain", None
        plan.periods = []
    elif metric in {"avg_monthly_goods_sales", "avg_order_goods_sales"}:
        if plan.dimension and plan.dimension != "month":
            raise PlanningError("平均口径目前只支持整体汇总，不支持按品类、商家等分组；请改为分别提问。")
        plan.operation, plan.dimension = "aggregate", None
    elif re.search(r"延迟|延误|逾期", question) and re.search(r"准时|按时|未延迟", question) and metric in {"avg_review_score", "low_review_rate"}:
        plan.operation, plan.dimension = "review_comparison", None
    elif re.search(r"贡献", question):
        plan.operation = "contribution"
        if not plan.dimension:
            return _clarify("dimension", "要按什么维度分析增长贡献？", [("品类", "category"), ("商家", "seller"), ("客户州", "customer_state")])
    elif len(plan.periods) == 2:
        if not (followup and plan.operation == "contribution"):
            plan.operation = "compare"
    elif re.search(r"排名|排行|最高|最低|最大|最小|最多|最少|前\s*\d+|后\s*\d+|top\s*\d+", question, re.I):
        plan.operation = "rank"
    elif plan.dimension == "month" or re.search(r"趋势|走势", question):
        plan.operation, plan.dimension = "trend", "month"
    elif not followup or plan.operation in {"context_only", "explain", "quality", "compare", "contribution"}:
        plan.operation = "aggregate"

    if re.search(r"(?:按|以)(?:实际)?送达(?:时间|日期)|送达日期口径", question):
        plan.date_field = "delivered_at"
    elif re.search(r"下单(?:时间|日期)|购买(?:时间|日期)", question):
        plan.date_field = "purchase_at"

    if plan.operation == "rank" and not plan.dimension:
        return _clarify("dimension", "请确认排名的分组维度。", [("品类", "category"), ("商家", "seller"), ("客户州", "customer_state")])
    if plan.operation == "contribution":
        if len(plan.periods) != 2:
            raise PlanningError("增长贡献需要两个明确期间，例如“2025年4月比3月，按品类分析销售额贡献”。")
        if metric not in {"goods_sales", "freight_amount", "payment_amount"}:
            raise PlanningError("贡献分析仅支持可加总金额指标；去重订单、比率和复购不能直接相加。")
    if plan.operation in {"rank", "trend", "review_comparison"} and len(plan.periods) > 1:
        raise PlanningError("该分析只支持一个期间，请分别提问排名、趋势或评价对比。")
    if metric == "payment_amount" and (plan.dimension in {"category", "seller"} or filters.category_ids or filters.seller_ids):
        raise PlanningError("支付金额没有品类或商家的分摊规则，不能按品类、商家拆分或筛选。")
    if plan.dimension == "payment_type" and metric != "payment_amount":
        raise PlanningError("按支付方式拆分仅支持支付金额，其他指标没有明确归属。")

    if re.search(r"促销|广告|活动|投放", question) and re.search(r"导致|造成|带来|原因|因为|确定|证明|归因", question):
        plan.causal_question = True
        plan.unsupported_parts.append("当前材料没有促销活动、折扣规则或广告投放记录，无法确定促销造成增长。")
    coverage = _coverage(catalog)
    if coverage:
        for period in plan.periods:
            if period.start < coverage[0] or period.end > coverage[1] + timedelta(days=1):
                plan.scope_notes.append(f"所选期间 {period.label} 的结果仅覆盖已有数据 {coverage[0]} 至 {coverage[1]}，缺少的日期不视为零。")
    if metric == "repeat_customers":
        plan.scope_notes.append("以自然客户 customer_unique_id 识别；复购为所选期间内至少2笔已送达去重订单，期间外订单不计入门槛。")
    if plan.dimension == "seller" and metric == "late_rate":
        plan.scope_notes.append("商家延迟率按商家参与的去重订单统计，不构成延迟责任归因。")
    plan.planner_source = "rules"
    return PlanningResult(plan=plan)
