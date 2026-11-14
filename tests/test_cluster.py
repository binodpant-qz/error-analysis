"""Smoke test for cluster.py on synthetic signatures (no real service data)."""
import json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", ".claude", "skills", "datadog-error-patterns", "scripts", "cluster.py")

def row(sig, ep, st, msg, n, cause=None):
    return dict(signature=sig, endpoint=ep, status_code=st, error_type="*fmt.wrapError",
                message=msg, count=n, cause=cause)

ROWS = (
    [row(f"t{i}", "POST /v1/webhook", 503, f"process update: query user by id[{i}]: context deadline exceeded", 20) for i in range(4)]
    + [row(f"c{i}", "POST /v1/transactions", 500, f"create account: saved owner user ID [{i}] ≠ incoming user ID [{i+9}]", 15) for i in range(3)]
    + [row(f"n{i}", "GET /v1/settings", 500, f"get package by id[{i}]: package not found: sql: no rows in result set", 10) for i in range(3)]
    + [row("x", "POST /v1/webhook", 503, "(no message)", 50, cause="timeout")]
    + [row("secret", "POST /v1/webhook", 503, "query by token[abcdefghijklmnopqrstuvwxyz.AO-J1Oabcdefghijkl]: context deadline exceeded", 5)]
)

def main():
    with tempfile.TemporaryDirectory() as d:
        inp, out = os.path.join(d, "s.json"), os.path.join(d, "c.json")
        json.dump(ROWS, open(inp, "w"))
        subprocess.run([sys.executable, SCRIPT, inp, "--out", out], check=True)
        res = json.load(open(out))
        text = json.dumps(res)
        assert res["total_errors"] == sum(r["count"] for r in ROWS)
        assert "abcdefghijklmnopqrstuvwxyz" not in text, "token leaked into output"
        causes = {m["cause"] for c in res["clusters"] for m in c["members"]}
        assert {"timeout", "conflict", "not_found"} <= causes, causes
        # each failure mode should land in a single cluster
        for cause in ("conflict", "not_found"):
            homes = {c["cluster_id"] for c in res["clusters"] for m in c["members"] if m["cause"] == cause}
            assert len(homes) == 1, (cause, homes)
    print("ok")

if __name__ == "__main__":
    main()
