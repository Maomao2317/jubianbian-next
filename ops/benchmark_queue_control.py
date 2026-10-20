"""Read-only synthetic queue benchmark: no network, media, or paid models."""

import ast
import json
from pathlib import Path
import sqlite3
import statistics
import time


def current_query():
    source = Path(__file__).resolve().parents[1] / "backend/app/worker.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_claim_next_task")
    return next(node.value for node in ast.walk(function) if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("SELECT task.id"))


def run():
    query = current_query()
    unhinted_query = query.replace(" FROM tasks INDEXED BY idx_tasks_ready_user", " FROM tasks", 1)
    assert unhinted_query != query, "expected a forced ready-queue index"
    baseline = "SELECT id, user_id FROM tasks WHERE status = 'queued' AND stage = 'queued' ORDER BY created_at, id"
    results = []
    for size, owners, uploading in [(100, 1, 0), (999, 1, 0), (999, 10, 0), (10000, 100, 0), (1000, 100, 50000)]:
        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            connection.executescript("""
                CREATE TABLE tasks(id TEXT PRIMARY KEY, user_id TEXT, status TEXT, stage TEXT, created_at TEXT);
                CREATE INDEX idx_tasks_user_id ON tasks(user_id, created_at);
                CREATE INDEX idx_tasks_user_status_created ON tasks(user_id, status, created_at DESC, id DESC);
                CREATE INDEX idx_tasks_admin_created ON tasks(created_at DESC, id DESC);
                CREATE INDEX idx_tasks_admin_status_created ON tasks(status, created_at DESC, id DESC);
                CREATE INDEX idx_tasks_ready_user ON tasks(user_id, created_at, id)
                  WHERE status = 'queued' AND stage = 'queued';
            """)
            connection.executemany("INSERT INTO tasks VALUES (?,?,?,?,?)", [
                (f"task-{index:06}", f"user-{index % owners:03}", "queued", "queued", "2026-10-15T00:00:00Z")
                for index in range(size)
            ] + [
                (f"upload-{index:06}", f"upload-user-{index % owners:03}", "queued", "uploading", "2026-10-15T00:00:00Z")
                for index in range(uploading)
            ])
            measurements = {}
            expected = None
            for label, sql in [("baseline", baseline), ("unhinted", unhinted_query), ("indexed", query)]:
                samples = []
                for _ in range(31):
                    start = time.perf_counter()
                    rows = connection.execute(sql).fetchall()
                    heads = {}
                    for row in rows:
                        heads.setdefault(str(row["user_id"] or ""), row["id"])
                    samples.append((time.perf_counter() - start) * 1000)
                if expected is None:
                    expected = heads
                assert heads == expected, "FIFO heads changed"
                measurements[label + "_median_ms"] = round(statistics.median(samples), 3)
                measurements[label + "_rows_fetched"] = len(rows)
            results.append({"queued": size, "users": owners, "uploading": uploading, **measurements,
                            "unhinted_plan": [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + unhinted_query)],
                            "indexed_plan": [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + query)]})
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    run()
