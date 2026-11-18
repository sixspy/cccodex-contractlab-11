"""复现/回归脚本：停用正式契约后，抓取任务入站不得再写“通过”。

历史缺陷：后端没有停用落库接口，页面本地的停用标记与后端结果对不上，
/ api/ingress/validate 仍把停用后的抓取任务写成“通过”。
现状：停用经门闩同事务落库（门闩册 + 契约状态），停用后入站写被闩死。

本脚本使用临时库，不触碰 data/app.db。
退出码 0 = 门闩已切断停用后写入；1 = 缺陷仍在。
"""
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="repro_latch_"))
    appmod.DB_PATH = tmp / "repro.db"
    appmod.SESSIONS.clear()
    appmod.init_db()

    c = TestClient(appmod.app)
    tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    subject, full = "订单入站", "order_id,amount,currency"

    before = c.get("/api/reconcile", headers=h).json()
    print("停用前对账:", before)

    # 正确停用姿势：门闩同事务落库（甲路径），不再是页面本地标记
    r = c.post("/api/contracts/v3/disable?path=甲", headers=h)
    assert r.status_code == 200, r.text
    print("停用落库:", r.json())

    blocked = c.post("/api/ingress/validate", headers=h,
                     json={"subject": subject, "payload_fields": full})
    after = c.get("/api/reconcile", headers=h).json()
    print("停用后抓取入站响应:", blocked.status_code, blocked.json())
    print("停用后对账:", after)

    latch = c.get("/api/latches", headers=h).json()
    print("门闩册:", latch["items"])

    fixed = (
        blocked.status_code == 409
        and after["passed"] == before["passed"]
        and after["ingress_total"] == before["ingress_total"]
        and any(x["contract_version"] == "v3" and x["state"] == "停用" for x in latch["items"])
    )
    if fixed:
        print("结论: 门闩生效 —— 停用后写入已切断，标记与结果一致")
        return 0
    print("结论: 缺陷仍在 —— 停用后仍有写入")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
