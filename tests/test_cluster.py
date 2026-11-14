"""Tests for cluster.py on synthetic signatures (no real service data)."""
import json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT_DIR = os.path.join(HERE, "..", ".claude", "skills", "datadog-error-patterns", "scripts")
SCRIPT = os.path.join(SCRIPT_DIR, "cluster.py")
sys.path.insert(0, SCRIPT_DIR)

from cluster import dependency_of, template  # noqa: E402


def row(sig, ep, st, msg, n, cause=None):
    return dict(signature=sig, endpoint=ep, status_code=st, error_type="*fmt.wrapError",
                message=msg, count=n, cause=cause)


ROOT_CAUSE_ROWS = (
    [row(f"t{i}", "POST /v1/webhook", 503, f"process update: query user by id[{i}]: context deadline exceeded", 20) for i in range(4)]
    + [row(f"c{i}", "POST /v1/transactions", 500, f"create account: saved owner user ID [{i}] ≠ incoming user ID [{i+9}]", 15) for i in range(3)]
    + [row(f"n{i}", "GET /v1/settings", 500, f"get package by id[{i}]: package not found: sql: no rows in result set", 10) for i in range(3)]
    + [row("x", "POST /v1/webhook", 503, "(no message)", 50, cause="timeout")]
    + [row("secret", "POST /v1/webhook", 503, "query by token[abcdefghijklmnopqrstuvwxyz.AO-J1Oabcdefghijkl]: context deadline exceeded", 5)]
)

# Timeouts and not-found errors spread over two endpoints. By endpoint, each endpoint
# is one category even though it mixes failure modes.
ENDPOINT_ROWS = (
    [row(f"a{i}", "POST /v1/orders", 503, f"create order step{i}: query order by id[{i}]: context deadline exceeded", 10) for i in range(3)]
    + [row(f"b{i}", "POST /v1/orders", 500, f"create order step{i}: get plan[{i}]: plan not found", 10) for i in range(3)]
    + [row(f"c{i}", "GET /v1/invoices", 503, f"list invoices step{i}: query invoice by id[{i}]: context deadline exceeded", 10) for i in range(3)]
    + [row(f"d{i}", "GET /v1/invoices", 500, f"list invoices step{i}: get plan[{i}]: plan not found", 10) for i in range(3)]
)

# Timeouts to Zuora vs to the database, on the same endpoints. By dependency, the Zuora
# timeout and the Zuora bad-data error belong together, apart from the DB timeouts.
DEPENDENCY_ROWS = (
    [row(f"z{i}", ep, 503, f'update sub: get subscription: get request: Get "https://rest.na.zuora.com/v1/subscriptions/A-S{i}": timeout exceeded', 10)
     for i, ep in enumerate(["POST /v1/update", "GET /v1/manage", "POST /v1/cancel"])]
    + [row(f"zb{i}", ep, 500, "update sub: update zms subscription: unmarshal zms update subscription data: json: cannot unmarshal bool", 10)
       for i, ep in enumerate(["POST /v1/update", "GET /v1/manage"])]
    + [row(f"q{i}", ep, 503, f"update sub: query user package by user ID: query: userID[{i}]: query by user id: context deadline exceeded", 10)
       for i, ep in enumerate(["POST /v1/update", "GET /v1/manage", "POST /v1/cancel"])]
    + [row(f"s{i}", ep, 500, f"get package: query package by id[{i}]: sql: no rows in result set", 10)
       for i, ep in enumerate(["POST /v1/update", "GET /v1/manage"])]
)


def run(rows, *flags):
    with tempfile.TemporaryDirectory() as d:
        inp, out = os.path.join(d, "s.json"), os.path.join(d, "c.json")
        with open(inp, "w") as f:
            json.dump(rows, f)
        subprocess.run([sys.executable, SCRIPT, inp, "--out", out, *flags], check=True,
                       stdout=subprocess.DEVNULL)
        with open(out) as f:
            return json.load(f)


def homes(res, key, value):
    """Cluster IDs holding members for which key(member) equals value."""
    return {c["cluster_id"] for c in res["clusters"] for m in c["members"] if key(m) == value}


def test_root_cause():
    res = run(ROOT_CAUSE_ROWS)
    assert res["by"] == "root_cause"
    assert res["total_errors"] == sum(r["count"] for r in ROOT_CAUSE_ROWS)
    assert "abcdefghijklmnopqrstuvwxyz" not in json.dumps(res), "token leaked into output"

    causes = {m["cause"] for c in res["clusters"] for m in c["members"]}
    assert {"timeout", "conflict", "not_found"} <= causes, causes
    for cause in ("conflict", "not_found"):
        assert len(homes(res, lambda m: m["cause"], cause)) == 1, cause


def test_endpoint():
    res = run(ENDPOINT_ROWS, "--by", "endpoint", "--k", "2")
    assert res["by"] == "endpoint"

    orders = homes(res, lambda m: m["endpoints"][0], "POST /v1/orders")
    invoices = homes(res, lambda m: m["endpoints"][0], "GET /v1/invoices")
    assert len(orders) == 1 and len(invoices) == 1 and orders != invoices, res["clusters"]


def test_root_cause_splits_endpoint_rows_by_failure_mode():
    res = run(ENDPOINT_ROWS, "--k", "2")

    timeouts = homes(res, lambda m: m["cause"], "timeout")
    not_found = homes(res, lambda m: m["cause"], "not_found")
    assert len(timeouts) == 1 and len(not_found) == 1 and timeouts != not_found, res["clusters"]


def test_dependency():
    res = run(DEPENDENCY_ROWS, "--by", "dependency", "--k", "2")
    assert res["by"] == "dependency"

    zuora = homes(res, lambda m: m["dependency"], "zuora")
    db = homes(res, lambda m: m["dependency"], "db")
    assert len(zuora) == 1 and len(db) == 1 and zuora != db, res["clusters"]


def test_dependency_of():
    cases = {
        'get request: Get "https://rest.na.zuora.com/v1/orders": timeout exceeded': "zuora",
        "update zms subscription: unmarshal zms data: json: cannot unmarshal bool": "zuora",
        "get: appstoreapi: /inApps/v1/subscriptions/{transactionId}: http 401: Unauthorized": "apple",
        "process update from rtdn: process update: get subscription v2: get subscription v2: context deadline exceeded": "google_play",
        'sync subscription: Post "http://web-php.hhvm.svc.cluster.local./rpc/v1/subscriptions/sync": context deadline exceeded': "web_php",
        "begin tx: dial tcp: lookup abc.connect.psdb.cloud: i/o timeout": "db",
        # The deepest call wins: a DB query under a Zuora-named step is a DB failure.
        "build zuora subscription info: query subscription terms: query: subscriptionNumber[A-S1]: query by subscription number: context deadline exceeded": "db",
        "create or update ams: saved owner user ID [1] ≠ incoming user ID [2]": "internal",
        "(no message)": "unknown",
    }
    for message, want in cases.items():
        got = dependency_of(template(message))
        assert got == want, (message, got, want)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")


if __name__ == "__main__":
    main()
