import pytest

from alertwhy import sqlguard as g

OK = [
    "SELECT COUNT(*) AS c FROM core_transactions WHERE customer_id='C9004'",
    "SELECT a.customer_id, COUNT(*) c FROM core_transactions a JOIN kyc k ON a.customer_id=k.customer_id GROUP BY 1",
    "WITH x AS (SELECT * FROM kyc) SELECT * FROM x",
    "SELECT * FROM sanctions WHERE name = 'a;b' ",
]
BAD = [
    "DROP TABLE kyc", "UPDATE kyc SET name='x'", "DELETE FROM kyc", "INSERT INTO kyc VALUES (1)",
    "SELECT 1; SELECT 2", "SELECT * FROM kyc -- x", "SELECT * FROM kyc /* x */",
    "SELECT * FROM read_csv('/etc/passwd')", "SELECT * FROM kyc, read_parquet('x')", "SELECT * FROM kyc, sanctions",
    "SELECT * FROM core.transactions", "SELECT * FROM core_pg.transactions", "SELECT * FROM information_schema.tables",
    "SELECT * FROM kyc UNION SELECT * FROM secrets", "SELECT getenv('HOME') FROM kyc", "", "   ",
    "SELECT * FROM kyc LIMIT 5 OFFSET 2", "ATTACH 'x' AS y", "COPY kyc TO '/tmp/x'", "PRAGMA database_list",
]


@pytest.mark.parametrize("q", OK)
def test_allowed(q):
    out = g.validate(q)
    assert out.lower().startswith(("select", "with")) and "limit" in out.lower()


@pytest.mark.parametrize("q", BAD)
def test_blocked(q):
    with pytest.raises(g.SQLGuardError):
        g.validate(q)


def test_limit_is_clamped_and_added():
    assert g.validate("SELECT * FROM kyc").endswith("LIMIT 50")
    assert g.validate("SELECT * FROM kyc LIMIT 100000").endswith("LIMIT 200")
    assert g.validate("SELECT * FROM kyc LIMIT 7;").endswith("LIMIT 7")
