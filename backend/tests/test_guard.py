import pytest
from app.sql_guard import validate_sql,SQLSafetyError


@pytest.mark.parametrize("sql",[
    "DROP TABLE orders",
    "SELECT * FROM v_order_fact; DELETE FROM orders",
    "SELECT * FROM read_csv('secret.csv')",
    "SELECT * FROM orders",
    "SELECT * FROM other.main.v_order_fact",
    "SELECT read_text('secret') FROM v_order_fact",
    "WITH RECURSIVE t AS (SELECT * FROM v_order_fact) SELECT * FROM t",
    "COPY (SELECT * FROM v_order_fact) TO 'leak.csv'",
])
def test_guard_blocks_nonquery_and_external_access(sql):
    with pytest.raises(SQLSafetyError):
        validate_sql(sql)


def test_cte_and_bound_value_allowed():
    sql="WITH p AS (SELECT order_id, payment_amount FROM v_order_fact WHERE order_status=?) SELECT SUM(payment_amount) FROM p"
    assert validate_sql(sql)==sql
