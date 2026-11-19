"""Capture the original SQL plans and a functional test run on the DZ 2 stand."""

import argparse
import json
from pathlib import Path

from run import compose


QUERIES = {
    "list-count": "SELECT count(*) FROM tickets t WHERE t.status='new'",
    "list-page": (
        "SELECT t.*,u.name AS author,c.name AS category FROM tickets t "
        "JOIN users u ON u.id=t.user_id JOIN categories c ON c.id=t.category_id "
        "WHERE t.status='new' ORDER BY t.id DESC LIMIT 20"
    ),
    "detail-comments": (
        "SELECT c.*,u.name AS author,u.role FROM comments c "
        "JOIN users u ON u.id=c.user_id WHERE c.ticket_id=12345 ORDER BY c.created_at,c.id"
    ),
    "summary-tickets": "SELECT id,category_id,created_at,status FROM tickets",
    "summary-answers": (
        "SELECT c.ticket_id,c.created_at FROM comments c JOIN users u ON u.id=c.user_id "
        "WHERE u.role='staff' ORDER BY c.created_at"
    ),
}


def query(sql):
    return compose("exec", "-T", "db", "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
                   "-U", "rasul_zeynalov", "-d", "rasul_zeynalov", "-c", sql)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    out = parser.parse_args().out
    out.mkdir(parents=True, exist_ok=True)
    print("docker compose exec -T api python seed.py work", flush=True)
    seed = json.loads(compose("exec", "-T", "api", "python", "seed.py", "work"))
    (out / "diagnosis-counts.json").write_text(
        json.dumps(seed["counts"], indent=2), encoding="utf-8",
    )
    for operation, sql in QUERIES.items():
        print(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", flush=True)
        raw = query("SET search_path TO rasul_zeynalov; "
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)
        data = json.loads(raw)
        (out / f"{operation}.explain.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8",
        )
        print(f"  {data[0]['Execution Time']:.3f} ms", flush=True)

    print("docker compose exec -T api python -m pytest -q", flush=True)
    result = compose("exec", "-T", "api", "python", "-m", "pytest", "-q")
    (out / "functional-tests.txt").write_text(result + "\n", encoding="utf-8")
    print(result, flush=True)


if __name__ == "__main__":
    main()
