"""Exercise deployed recharge arithmetic and ledgers in a disposable database."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix="jbb-recharge-check-") as directory:
        # Set this before importing any application module. No business DB is
        # opened and no HTTP/provider call is made by this verifier.
        os.environ["JBB_DATA_DIR"] = directory
        from app import auth_store, config, routes
        from fastapi import HTTPException
        from starlette.requests import Request

        assert config.DB_PATH.parent.resolve() == Path(directory).resolve()
        assert routes.POINTS_PER_YUAN == 12.8
        auth_store.init_db()
        with closing(auth_store.db()) as connection, connection:
            for user_id in ("smoke-admin", "smoke-user"):
                connection.execute(
                    "INSERT INTO users(id,email,password_hash,name,credits,created_at) VALUES (?,?,?,?,?,?)",
                    (user_id, user_id + "@example.test", "unused", "isolated-check", 0, "now"))

        async def recharge(amount):
            async def receive():
                return {"type": "http.request", "body": json.dumps({"rmb_amount": amount}).encode()}
            request = Request({"type": "http", "method": "POST", "path": "/isolated-check", "headers": []}, receive)
            return await routes.admin_user_credits(request, "smoke-user")

        with patch.object(routes, "_require_admin", return_value={"id": "smoke-admin"}):
            one = asyncio.run(recharge(1))
            hundred = asyncio.run(recharge(100))
            assert one["pointsAmount"] == 12.8 and one["balance"] == 12.8
            assert hundred["pointsAmount"] == 1280 and hundred["balance"] == 1292.8
            for invalid in (None, "bad", 0, -1, "NaN", "Infinity", 1_000_001):
                try:
                    asyncio.run(recharge(invalid))
                except HTTPException as error:
                    assert error.status_code == 400
                else:
                    raise AssertionError("Accepted invalid recharge")

        with closing(auth_store.db()) as connection:
            assert connection.execute("SELECT credits FROM users WHERE id='smoke-user'").fetchone()[0] == 1292.8
            assert connection.execute("SELECT count(*),round(sum(amount),1) FROM credit_ledger").fetchone()[:] == (2, 1292.8)
            assert connection.execute("SELECT count(*),round(sum(points_amount),1) FROM admin_recharge_ledger").fetchone()[:] == (2, 1292.8)
        print("RECHARGE_VERIFIED", json.dumps({"pointsPerYuan": 12.8, "oneYuanPoints": 12.8,
              "hundredYuanPoints": 1280, "ledgersConsistent": True, "invalidInputsRejected": 7,
              "businessDatabaseChanged": False}), flush=True)


if __name__ == "__main__":
    main()
