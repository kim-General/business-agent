"""Versioned, typed imports and grain-safe analytic views.

Only the importer writes these databases. Serving code opens independent read-only
connections with external access and automatic extensions disabled.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import threading
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import openpyxl

SCHEMAS: dict[str, dict[str, str]] = {
    "customers": {"customer_id": "VARCHAR", "customer_unique_id": "VARCHAR", "customer_zip_code": "VARCHAR", "customer_city": "VARCHAR", "customer_state": "VARCHAR"},
    "geolocation": {"zip_code": "VARCHAR", "latitude": "DOUBLE", "longitude": "DOUBLE", "city": "VARCHAR", "state": "VARCHAR"},
    "orders": {"order_id": "VARCHAR", "customer_id": "VARCHAR", "order_status": "VARCHAR", "purchase_at": "TIMESTAMP", "approved_at": "TIMESTAMP", "shipped_at": "TIMESTAMP", "delivered_at": "TIMESTAMP", "estimated_delivery_at": "TIMESTAMP"},
    "order_items": {"order_id": "VARCHAR", "order_item_id": "INTEGER", "product_id": "VARCHAR", "seller_id": "VARCHAR", "shipping_limit_at": "TIMESTAMP", "price": "DECIMAL(18,2)", "freight_value": "DECIMAL(18,2)"},
    "order_payments": {"order_id": "VARCHAR", "payment_sequence": "INTEGER", "payment_type": "VARCHAR", "payment_installments": "INTEGER", "payment_value": "DECIMAL(18,2)"},
    "order_reviews": {"review_id": "VARCHAR", "order_id": "VARCHAR", "review_score": "INTEGER", "review_title": "VARCHAR", "review_message": "VARCHAR", "review_created_at": "TIMESTAMP", "seller_answered_at": "TIMESTAMP"},
    "products": {"product_id": "VARCHAR", "category_id": "VARCHAR", "product_name": "VARCHAR", "product_name_length": "INTEGER", "product_description_length": "INTEGER", "product_photos_count": "INTEGER", "product_weight_g": "INTEGER", "product_length_cm": "INTEGER", "product_height_cm": "INTEGER", "product_width_cm": "INTEGER", "base_price": "DECIMAL(18,2)"},
    "sellers": {"seller_id": "VARCHAR", "seller_name": "VARCHAR", "seller_zip_code": "VARCHAR", "seller_city": "VARCHAR", "seller_state": "VARCHAR"},
    "categories": {"category_id": "VARCHAR", "category_name_en": "VARCHAR", "category_name_zh": "VARCHAR"},
}
PRIMARY_KEYS = {"customers": ["customer_id"], "geolocation": ["zip_code"], "orders": ["order_id"], "order_items": ["order_id", "order_item_id"], "order_payments": ["order_id", "payment_sequence"], "order_reviews": ["review_id"], "products": ["product_id"], "sellers": ["seller_id"], "categories": ["category_id"]}
FOREIGN_KEYS = [("orders", "customer_id", "customers", "customer_id"), ("order_items", "order_id", "orders", "order_id"), ("order_items", "product_id", "products", "product_id"), ("order_items", "seller_id", "sellers", "seller_id"), ("order_payments", "order_id", "orders", "order_id"), ("order_reviews", "order_id", "orders", "order_id"), ("products", "category_id", "categories", "category_id")]
DATASETS = {"excel-main": ("主数据（Excel，2024—2025）", "ecommerce_agent_demo.xlsx"), "csv-demo": ("小样本演示（CSV，2025上半年）", "ecommerce_agent_demo_csv.zip")}
VIEW_NAMES = frozenset({"v_order_fact", "v_item_fact", "v_seller_order", "v_review_fact", "v_payment_fact"})
IMPORT_VERSION = "warehouse-v5"
SAFE_CONFIG = {"enable_external_access": "false", "autoinstall_known_extensions": "false", "autoload_known_extensions": "false", "allow_unsigned_extensions": "false", "threads": "2", "memory_limit": "256MB", "max_temp_directory_size": "0B"}


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _typed(value: Any, sql_type: str) -> Any:
    if value is None or value == "":
        return None
    if sql_type.startswith("DECIMAL"):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    if sql_type == "INTEGER":
        return int(value)
    if sql_type == "DOUBLE":
        return float(value)
    if sql_type == "TIMESTAMP":
        return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return str(value)


class Warehouse:
    def __init__(self, data_dir: Path, source_root: Path):
        self.data_dir = Path(data_dir).resolve()
        self.source_root = Path(source_root).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._manifests: dict[str, dict] = {}

    def _db_path(self, dataset_id: str) -> Path:
        if dataset_id not in DATASETS:
            raise ValueError(f"未知数据版本：{dataset_id}")
        return self.data_dir / f"{dataset_id}.duckdb"

    def _source(self, filename: str) -> Path:
        direct = self.source_root / filename
        if direct.is_file():
            return direct
        # Only locate the exact competition filenames. Never expose this search
        # to the LLM or accept source paths from HTTP requests.
        candidates = sorted(self.source_root.rglob(filename))
        if not candidates:
            raise FileNotFoundError(f"在材料目录找不到 {filename}：{self.source_root}")
        return candidates[0]

    def ensure_ready(self) -> list[dict]:
        with self._lock:
            for dataset_id, (name, filename) in DATASETS.items():
                source = self._source(filename)
                digest = _sha(source)
                target = self._db_path(dataset_id)
                manifest_path = self.data_dir / f"{dataset_id}.json"
                old = None
                if target.exists() and manifest_path.exists():
                    old = json.loads(manifest_path.read_text(encoding="utf-8"))
                if old and old.get("source_hash") == digest and old.get("import_version") == IMPORT_VERSION:
                    self._manifests[dataset_id] = old
                    continue
                tables, questions, notes = self._read_source(source)
                temp = self.data_dir / f"{dataset_id}.building.duckdb"
                if temp.exists():
                    temp.unlink()
                con = duckdb.connect(str(temp), config=SAFE_CONFIG)
                try:
                    con.execute("BEGIN TRANSACTION")
                    for table, schema in SCHEMAS.items():
                        declarations = ", ".join(f'"{c}" {t}' for c, t in schema.items())
                        con.execute(f'CREATE TABLE "{table}" ({declarations})')
                        rows = tables[table]
                        for start in range(0, len(rows), 500):
                            chunk = rows[start:start + 500]
                            template = "(" + ",".join("?" for _ in schema) + ")"
                            con.execute(f'INSERT INTO "{table}" VALUES ' + ",".join(template for _ in chunk), [v for row in chunk for v in row])
                    self._create_views(con)
                    quality = self._quality(con)
                    counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in SCHEMAS}
                    purchase_range = con.execute("SELECT MIN(purchase_at), MAX(purchase_at) FROM orders").fetchone()
                    delivery_range = con.execute("SELECT MIN(delivered_at), MAX(delivered_at) FROM orders").fetchone()
                    data_hash = hashlib.sha256(f"{digest}:{IMPORT_VERSION}".encode()).hexdigest()
                    manifest = json_value({"id": dataset_id, "name": name, "hash": data_hash, "source_hash": digest, "source_file": source.name, "import_version": IMPORT_VERSION, "purchase_range": purchase_range, "delivery_range": delivery_range, "table_counts": counts, "imported_at": datetime.now(timezone.utc), "quality_summary": {"failed": sum(c["status"] == "fail" for c in quality["checks"]), "warnings": sum(c["status"] == "warn" for c in quality["checks"]), "critical_count": quality["critical_count"]}, "source_notes": notes})
                    con.execute("CREATE TABLE dataset_metadata (manifest_json VARCHAR, quality_json VARCHAR)")
                    con.execute("INSERT INTO dataset_metadata VALUES (?, ?)", [json.dumps(manifest, ensure_ascii=False), json.dumps(json_value(quality), ensure_ascii=False)])
                    con.execute("COMMIT")
                    con.execute("CHECKPOINT")
                except Exception:
                    con.close()
                    if temp.exists():
                        temp.unlink()
                    raise
                else:
                    con.close()
                os.replace(temp, target)
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                self._manifests[dataset_id] = manifest
            return self.datasets()

    def _read_source(self, source: Path) -> tuple[dict, list, list]:
        tables: dict = {}
        questions: list = []
        notes: list = []
        if source.suffix.lower() == ".xlsx":
            book = openpyxl.load_workbook(source, read_only=True, data_only=True)
            try:
                for name, schema in SCHEMAS.items():
                    values = iter(book[name].values)
                    header = list(next(values))
                    if header != list(schema):
                        raise ValueError(f"{name} 表字段与数据字典不一致")
                    tables[name] = [tuple(_typed(v, t) for v, t in zip(row, schema.values())) for row in values if any(v is not None for v in row)]
                if "说明" in book.sheetnames:
                    notes = [list(row) for row in book["说明"].values if any(v is not None for v in row)]
            finally:
                book.close()
        else:
            with zipfile.ZipFile(source) as archive:
                files = {Path(n).name: n for n in archive.namelist() if not n.endswith("/")}
                for name, schema in SCHEMAS.items():
                    body = archive.read(files[f"{name}.csv"]).decode("utf-8-sig")
                    reader = csv.DictReader(io.StringIO(body))
                    if reader.fieldnames != list(schema):
                        raise ValueError(f"{name} CSV字段与数据字典不一致")
                    tables[name] = [tuple(_typed(row[c], t) for c, t in schema.items()) for row in reader]
                if "README.md" in files:
                    notes = [archive.read(files["README.md"]).decode("utf-8-sig")]
        return tables, questions, notes

    @staticmethod
    def _create_views(con):
        con.execute("""CREATE VIEW v_order_fact AS
          WITH items AS (SELECT order_id, SUM(price) goods_sales, SUM(freight_value) freight_amount, COUNT(*) item_count, COUNT(DISTINCT seller_id) seller_count FROM order_items GROUP BY order_id),
          payments AS (SELECT order_id, SUM(payment_value) payment_amount, COUNT(*) payment_count FROM order_payments GROUP BY order_id)
          SELECT o.*, c.customer_unique_id, c.customer_city, c.customer_state,
            i.goods_sales, i.freight_amount, i.item_count, i.seller_count,
            p.payment_amount, COALESCE(p.payment_count, 0) payment_count,
            CASE WHEN o.order_status='delivered' AND o.delivered_at IS NOT NULL AND o.estimated_delivery_at IS NOT NULL THEN o.delivered_at > o.estimated_delivery_at ELSE NULL END is_late
          FROM orders o LEFT JOIN customers c USING(customer_id) LEFT JOIN items i USING(order_id) LEFT JOIN payments p USING(order_id)""")
        con.execute("""CREATE VIEW v_item_fact AS SELECT i.*, o.customer_id, o.customer_unique_id, o.customer_state, o.purchase_at, o.delivered_at, o.estimated_delivery_at, o.order_status, o.is_late,
          p.category_id, p.product_name, c.category_name_zh category_name, s.seller_name
          FROM order_items i JOIN v_order_fact o USING(order_id) LEFT JOIN products p USING(product_id)
          LEFT JOIN categories c USING(category_id) LEFT JOIN sellers s USING(seller_id)""")
        con.execute("""CREATE VIEW v_seller_order AS SELECT order_id, seller_id, MAX(seller_name) seller_name,
          MAX(customer_id) customer_id, MAX(customer_unique_id) customer_unique_id, MAX(customer_state) customer_state,
          MAX(purchase_at) purchase_at, MAX(delivered_at) delivered_at, MAX(estimated_delivery_at) estimated_delivery_at,
          MAX(order_status) order_status, BOOL_OR(is_late) is_late,
          SUM(price) goods_sales, SUM(freight_value) freight_amount, COUNT(*) item_count
          FROM v_item_fact GROUP BY order_id, seller_id""")
        con.execute("""CREATE VIEW v_review_fact AS SELECT r.*, o.customer_id, o.customer_unique_id, o.customer_state,
          o.purchase_at, o.delivered_at, o.estimated_delivery_at, o.order_status, o.is_late
          FROM order_reviews r JOIN v_order_fact o USING(order_id)""")
        con.execute("""CREATE VIEW v_payment_fact AS SELECT p.*, o.customer_id, o.customer_unique_id, o.customer_state,
          o.purchase_at, o.delivered_at, o.order_status, o.is_late
          FROM order_payments p JOIN v_order_fact o USING(order_id)""")

    @staticmethod
    def _quality(con) -> dict:
        checks = []
        critical = 0

        def add(name, sql, detail, *, severity="fail", fatal=False):
            nonlocal critical
            count = int(con.execute(sql).fetchone()[0])
            checks.append({"name": name, "status": severity if count else "pass", "count": count, "detail": detail})
            if fatal:
                critical += count

        for table, keys in PRIMARY_KEYS.items():
            names = ",".join(keys)
            add(f"{table}主键重复", f"SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM {table} GROUP BY {names} HAVING COUNT(*)>1)", f"主键：{names}", fatal=True)
            condition = " OR ".join(f"{k} IS NULL" for k in keys)
            add(f"{table}主键缺失", f"SELECT COUNT(*) FROM {table} WHERE {condition}", f"主键不能为空：{names}", fatal=True)
        for child, ck, parent, pk in FOREIGN_KEYS:
            add(f"{child}.{ck}外键", f"SELECT COUNT(*) FROM {child} c LEFT JOIN {parent} p ON c.{ck}=p.{pk} WHERE p.{pk} IS NULL", f"关联 {parent}.{pk}", fatal=True)
        add("同订单多条评价", "SELECT COUNT(*) FROM (SELECT order_id FROM order_reviews GROUP BY order_id HAVING COUNT(*)>1)", "评价均分和覆盖率需要订单级口径；当前版本不自动选择或丢弃评价", fatal=True)
        for name, condition in [("审批早于下单", "approved_at<purchase_at"), ("发货早于审批", "shipped_at<approved_at"), ("送达早于发货", "delivered_at<shipped_at"), ("预计送达早于下单", "estimated_delivery_at<purchase_at"), ("非已送达却有送达时间", "order_status<>'delivered' AND delivered_at IS NOT NULL")]:
            add(name, f"SELECT COUNT(*) FROM orders WHERE {condition}", "检查业务时间与状态一致性")
        add("缺少下单时间", "SELECT COUNT(*) FROM orders WHERE purchase_at IS NULL", "不能将缺失下单时间的订单归入期间", severity="warn")
        add("已送达订单缺少履约判定日期", "SELECT COUNT(*) FROM orders WHERE order_status='delivered' AND (delivered_at IS NULL OR estimated_delivery_at IS NULL)", "延迟率保留全部已送达分母，存在未知日期时对应比例不可得；延迟/准时评价对比另报排除样本数", severity="warn")
        add("订单缺少明细", "SELECT COUNT(*) FROM v_order_fact WHERE item_count IS NULL", "订单金额未知，不能替换为零", fatal=True)
        add("商品或运费金额缺失", "SELECT COUNT(*) FROM order_items WHERE price IS NULL OR freight_value IS NULL", "金额缺失会使合计不完整，阻止业务查询", fatal=True)
        add("支付金额缺失", "SELECT COUNT(*) FROM order_payments WHERE payment_value IS NULL", "支付金额缺失会使合计不完整，阻止业务查询", fatal=True)
        add("已送达订单缺少支付", "SELECT COUNT(*) FROM v_order_fact WHERE order_status='delivered' AND payment_count=0", "支付金额未知，不能替换为零")
        add("有支付订单金额不一致", "SELECT COUNT(*) FROM v_order_fact WHERE payment_count>0 AND ABS(payment_amount-goods_sales-freight_amount)>0.01", "仅比较有支付订单，支付金额与商品加运费差额绝对值大于0.01元")
        add("商品或运费为负", "SELECT COUNT(*) FROM order_items WHERE price<0 OR freight_value<0", "当前材料没有退款流水，负值需另行解释")
        add("支付金额为负", "SELECT COUNT(*) FROM order_payments WHERE payment_value<0", "当前材料没有退款流水")
        add("评分超出范围", "SELECT COUNT(*) FROM order_reviews WHERE review_score IS NULL OR review_score<1 OR review_score>5", "评分范围1至5")
        add("送达跨出下单数据覆盖期", "SELECT COUNT(*) FROM orders WHERE delivered_at>(SELECT MAX(purchase_at) FROM orders)", "合理的履约跨期，不能用送达时间替代下单时间筛选月报", severity="warn")
        expected_nulls = con.execute("SELECT COUNT(*) FILTER(WHERE order_status IN ('cancelled','unavailable') AND approved_at IS NULL), COUNT(*) FILTER(WHERE order_status<>'delivered' AND delivered_at IS NULL) FROM orders").fetchone()
        checks.append({"name": "状态对应的合理空值", "status": "info", "count": expected_nulls[1], "detail": f"取消/不可用且无审批时间{expected_nulls[0]}笔；未送达且无送达时间{expected_nulls[1]}笔。保留空值。"})
        relations = con.execute("""SELECT (SELECT COUNT(*) FROM v_order_fact WHERE item_count>1),
          (SELECT COUNT(*) FROM v_order_fact WHERE payment_count>1),
          (SELECT COUNT(*) FROM v_order_fact WHERE seller_count>1),
          (SELECT COUNT(DISTINCT customer_unique_id) FROM customers),
          (SELECT COUNT(*) FROM orders WHERE order_id NOT IN (SELECT order_id FROM order_reviews))""").fetchone()
        missing = {table: {col: con.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" IS NULL').fetchone()[0] for col in schema} for table, schema in SCHEMAS.items()}
        return json_value({"checks": checks, "critical_count": critical, "relationship_counts": dict(zip(["multi_item_orders", "split_payment_orders", "multi_seller_orders", "natural_customers", "orders_without_reviews"], relations)), "null_counts": missing, "status_counts": dict(con.execute("SELECT order_status, COUNT(*) FROM orders GROUP BY order_status ORDER BY order_status").fetchall()), "limitations": ["Excel与CSV是不同版本，查询与参考答案不能混用", "数据不包含促销活动、广告成本、退款流水或商品采购成本", "商家延迟率按包含该商家的订单履约状态统计，不能确定责任归属", "日期为材料中的无时区本地时间；期间使用左闭右开区间"]})

    def datasets(self) -> list[dict]:
        return [self._manifests[k] for k in DATASETS if k in self._manifests]

    def manifest(self, dataset_id: str) -> dict:
        self._db_path(dataset_id)
        if dataset_id not in self._manifests:
            path = self.data_dir / f"{dataset_id}.json"
            if not path.exists():
                raise ValueError("数据尚未导入，请先初始化")
            self._manifests[dataset_id] = json.loads(path.read_text(encoding="utf-8"))
        return self._manifests[dataset_id]

    def connect_readonly(self, dataset_id: str):
        con = duckdb.connect(str(self._db_path(dataset_id)), read_only=True, config=SAFE_CONFIG)
        con.execute("SET lock_configuration=true")
        return con

    def catalog(self, dataset_id: str) -> dict:
        from .schemas import METRICS
        manifest = self.manifest(dataset_id)
        with self.connect_readonly(dataset_id) as con:
            categories = [{"id": k, "name": v} for k, v in con.execute("SELECT category_id, category_name_zh FROM categories ORDER BY category_id").fetchall()]
            sellers = [{"id": k, "name": v} for k, v in con.execute("SELECT seller_id, seller_name FROM sellers ORDER BY seller_id").fetchall()]
        return {"dataset_id": dataset_id, "dataset_hash": manifest["hash"], "purchase_range": manifest["purchase_range"], "delivery_range": manifest["delivery_range"], "categories": categories, "sellers": sellers, "metrics": [{"id": k, "name": v} for k, v in METRICS.items()], "table_counts": manifest["table_counts"], "grain_notes": {"v_order_fact": "一订单一行；商品、运费、支付分别先按订单汇总", "v_item_fact": "一商品明细一行", "v_seller_order": "一商家一订单一行；商家履约非责任归因", "v_review_fact": "一评价一订单一行", "v_payment_fact": "一笔支付一行；仅支付金额可按支付方式求和"}, "exploration_enabled": False}

    def profile(self, dataset_id: str) -> dict:
        manifest = self.manifest(dataset_id)
        with self.connect_readonly(dataset_id) as con:
            quality = json.loads(con.execute("SELECT quality_json FROM dataset_metadata").fetchone()[0])
        return {"dataset_id": dataset_id, "dataset_hash": manifest["hash"], "table_counts": manifest["table_counts"], "purchase_range": manifest["purchase_range"], "delivery_range": manifest["delivery_range"], **quality}
