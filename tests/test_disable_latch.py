"""停用门闩测试 —— 先红后绿。

口径（题目限定）：
- 停用必须闩死入站写：停用后禁止再写“通过”，停用标记不得只留在页面本地。
- 甲路径：停用与拒绝新入站同一事务生效，仅允历史只读，新入站不产生新批次。
- 乙路径：停用后新入站只进停用缓冲（disable_buffers），正式通过/拒收计数冻结。
- 路径选定写入“停用门闩册”（disable_latches），当天禁止改口；偏离门闩册记“停用偏了”。
- 停用与入站两侧同时抢门闩须排队，后到只记“停用争用”。
- 版本目录/入站列表/缓冲队列/对账账册/策略附页可读不可写（只允许新增表与门闩事件，
  契约表仅 status 列随停用/启用同事务翻转）。
"""
import threading

import pytest
from fastapi.testclient import TestClient

from app import app, init_db

PATH_A = "甲"   # 停用与拒绝新入站同一事务，历史只读
PATH_B = "乙"   # 停用后新入站只进停用缓冲，正式计数冻结
SUBJECT = "订单入站"
FULL = "order_id,amount,currency"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def auth(client):
    tok = client.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    return h


def _recon(client, h):
    return client.get("/api/reconcile", headers=h).json()


def _disable(client, h, path):
    return client.post(f"/api/contracts/v3/disable?path={path}", headers=h)


def test_red_after_disable_passed_count_must_not_grow(client, auth):
    """门闩失败测：停用后通过加一必须被红（先红，后端修好后转绿）。"""
    before = _recon(client, auth)
    r = _disable(client, auth, PATH_A)
    assert r.status_code == 200, r.text
    blocked = client.post("/api/ingress/validate", headers=auth,
                          json={"subject": SUBJECT, "payload_fields": FULL})
    after = _recon(client, auth)
    # 停用后正式通过计数禁止再增
    assert after["passed"] == before["passed"], "停用后仍写通过，门闩未闩死"
    # 停用后入站必须被门闩挡下，而不是静默放过
    assert blocked.status_code == 409 and "停用" in blocked.json()["detail"]
    # 甲路径：不产生任何新批次，入站列表对停用后新入站只读
    listed = client.get("/api/ingress", headers=auth).json()["items"]
    assert all(b["subject"] != SUBJECT for b in listed) or before["ingress_total"] == after["ingress_total"]
    assert after["ingress_total"] == before["ingress_total"]


def test_disable_persisted_server_side_not_page_local(client, auth):
    """停用必须落库：门闩册与契约状态同事务翻转，新会话同样被挡。"""
    assert _disable(client, auth, PATH_A).status_code == 200
    # 另起一个全新客户端/会话，停用仍然生效，证明标记不是页面本地
    other = TestClient(app)
    tok = other.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    h2 = {"Authorization": f"Bearer {tok}"}
    r = other.post("/api/ingress/validate", headers=h2,
                   json={"subject": SUBJECT, "payload_fields": FULL})
    assert r.status_code == 409
    latch = other.get("/api/latches", headers=h2).json()["items"]
    assert any(x["contract_version"] == "v3" and x["state"] == "停用" and x["path"] == PATH_A for x in latch)
    statuses = {x["version"]: x["status"] for x in other.get("/api/contracts", headers=h2).json()["items"]}
    assert statuses["v3"] == "停用"


def test_path_registered_then_locked_same_day(client, auth):
    """路径选定写入门闩册，当天禁止改口；偏离门闩册拒绝并记“停用偏了”。"""
    assert _disable(client, auth, PATH_A).status_code == 200
    # 换路径再停：拒绝，不允许混用停用支
    bad = _disable(client, auth, PATH_B)
    assert bad.status_code == 409 and "停用偏了" in bad.json()["detail"]
    events = client.get("/api/latches", headers=auth).json()["events"]
    assert any(e["kind"] == "停用偏了" for e in events)
    # 门闩册仍登记甲路径，未被改写
    latch = [x for x in client.get("/api/latches", headers=auth).json()["items"] if x["contract_version"] == "v3"][0]
    assert latch["path"] == PATH_A and latch["state"] == "停用"
    # 同路径重复停用是幂等的，不算改口
    again = _disable(client, auth, PATH_A)
    assert again.status_code == 200


def test_path_a_history_readonly_reject_same_transaction(client, auth):
    """甲路径：停用与拒绝同一事务；历史只读，停用后无新批次，拒收计数也不增。"""
    client.post("/api/ingress/validate", headers=auth,
                json={"subject": SUBJECT, "payload_fields": FULL})  # 历史一条通过
    before = _recon(client, auth)
    assert _disable(client, auth, PATH_A).status_code == 200
    for payload in (FULL, "order_id,amount"):  # 字段齐全与缺字段都一样被门闩挡
        r = client.post("/api/ingress/validate", headers=auth,
                        json={"subject": SUBJECT, "payload_fields": payload})
        assert r.status_code == 409
    after = _recon(client, auth)
    assert after == before  # 通过/拒收/缓冲/总数全部冻结
    # 历史仍可读
    history = client.get("/api/ingress", headers=auth).json()["items"]
    assert len(history) == before["ingress_total"] == 1


def test_path_b_disable_buffer_formal_counters_frozen(client, auth):
    """乙路径：停用后新入站只进停用缓冲，正式通过/拒收计数禁止再增。"""
    before = _recon(client, auth)
    assert _disable(client, auth, PATH_B).status_code == 200
    r1 = client.post("/api/ingress/validate", headers=auth,
                     json={"subject": SUBJECT, "payload_fields": FULL})
    r2 = client.post("/api/ingress/validate", headers=auth,
                     json={"subject": SUBJECT, "payload_fields": "order_id,amount"})
    assert r1.status_code == 200 and r1.json()["result"] == "停用缓冲"
    assert r2.status_code == 200 and r2.json()["result"] == "停用缓冲"
    after = _recon(client, auth)
    assert after["passed"] == before["passed"]
    assert after["rejected"] == before["rejected"]
    assert after["ingress_total"] == before["ingress_total"]  # 正式入站列表不新增
    rows = client.get("/api/latches", headers=auth).json()["disable_buffer_rows"]
    assert rows == 2


def test_enable_reopens_ingress_without_moving_history(client, auth):
    """启用后门闩解除，入站恢复；历史批次不迁移、不改写。"""
    client.post("/api/ingress/validate", headers=auth,
                json={"subject": SUBJECT, "payload_fields": FULL})
    _disable(client, auth, PATH_A)
    blocked = client.post("/api/ingress/validate", headers=auth,
                          json={"subject": SUBJECT, "payload_fields": FULL})
    assert blocked.status_code == 409
    en = client.post("/api/contracts/v3/enable", headers=auth)
    assert en.status_code == 200
    ok = client.post("/api/ingress/validate", headers=auth,
                     json={"subject": SUBJECT, "payload_fields": FULL})
    assert ok.status_code == 200 and ok.json()["result"] == "通过"
    latch = [x for x in client.get("/api/latches", headers=auth).json()["items"] if x["contract_version"] == "v3"][0]
    assert latch["state"] == "启用"
    statuses = {x["version"]: x["status"] for x in client.get("/api/contracts", headers=auth).json()["items"]}
    assert statuses["v3"] == "启用"


def test_seeded_disabled_contract_blocks_ingress(client, auth):
    """种子里已停用的 v2 正式契约，停用前就闩着，不得写通过。"""
    r = client.post("/api/ingress/validate", headers=auth,
                    json={"subject": "库存入站", "payload_fields": "sku,qty"})
    assert r.status_code == 409 and "停用" in r.json()["detail"]
    assert _recon(client, auth)["passed"] == 0
    # 历史停用态必须在门闩册补登记，状态与册一致（不得误判为“停用偏了”）
    latch = [x for x in client.get("/api/latches", headers=auth).json()["items"] if x["contract_version"] == "v2"][0]
    assert latch["state"] == "停用" and latch["path"] == PATH_A
    assert latch["chosen_on"] == ""  # 补登记未经值班选定，选定日留空
    assert "停用偏了" not in r.json()["detail"]


def test_enable_placeholder_then_first_disable_can_choose_path(client, auth):
    """无册契约启用时建占位行（未选定），首次显式停用可自由选支并落选定日。"""
    # v3 初始启用且无门闩册：enable 走占位分支
    client.post("/api/contracts/v3/enable", headers=auth)
    latch = [x for x in client.get("/api/latches", headers=auth).json()["items"] if x["contract_version"] == "v3"][0]
    assert latch["path"] == "" and latch["chosen_on"] == "" and latch["state"] == "启用"
    # 首次显式停用选乙支，落选定日
    r = client.post(f"/api/contracts/v3/disable?path={PATH_B}", headers=auth)
    assert r.status_code == 200 and r.json()["path"] == PATH_B
    latch = [x for x in client.get("/api/latches", headers=auth).json()["items"] if x["contract_version"] == "v3"][0]
    assert latch["path"] == PATH_B and latch["state"] == "停用" and latch["chosen_on"] != ""
    # 当天改口仍被拒
    bad = client.post(f"/api/contracts/v3/disable?path={PATH_A}", headers=auth)
    assert bad.status_code == 409 and "停用偏了" in bad.json()["detail"]


def test_backfilled_latch_allows_first_explicit_path_choice(client, auth):
    """补登记（未选定）的停用契约，值班当天第一次显式停用可自由选支并落选定日。"""
    # v2 种子停用且门闩册为补登记（chosen_on 空）
    r = client.post(f"/api/contracts/v2/disable?path={PATH_B}", headers=auth)
    assert r.status_code == 200 and r.json()["path"] == PATH_B
    latch = [x for x in client.get("/api/latches", headers=auth).json()["items"] if x["contract_version"] == "v2"][0]
    assert latch["path"] == PATH_B and latch["chosen_on"] != ""
    # 落定后当天再换甲支即被拒
    bad = client.post(f"/api/contracts/v2/disable?path={PATH_A}", headers=auth)
    assert bad.status_code == 409 and "停用偏了" in bad.json()["detail"]
    # 乙支门闩下新入站只进停用缓冲
    resp = client.post("/api/ingress/validate", headers=auth,
                       json={"subject": "库存入站", "payload_fields": "sku,qty"})
    assert resp.status_code == 200 and resp.json()["result"] == "停用缓冲"
    assert _recon(client, auth)["passed"] == 0


def test_disable_requires_auth_and_formal_track(client, auth):
    """未登录不可停用；影子契约不是正式契约，不可停用。"""
    assert client.post(f"/api/contracts/v3/disable?path={PATH_A}").status_code == 401
    r = client.post(f"/api/contracts/v4-shadow/disable?path={PATH_A}", headers=auth)
    assert r.status_code == 400


def test_disable_and_ingress_race_queues_loser_logs_contention(client, auth):
    """停用与入站两侧同时抢门闩：排队串行，后到只记“停用争用”，且无通过漏写。

    门闩是进程内服务层原语，两个工作线程并发驱动服务函数，共用 LATCH_GATE
    与同一事务路径（TestClient 门户线程容量为 1，无法跨请求并发）。
    确定性时序：停用侧先持闩并停在闩内，入站侧再起跑，确保它就是后到者。
    """
    import app as appmod

    disable_holding = threading.Event()  # 停用侧已取到闩、停在闩内
    may_finish = threading.Event()        # 停用侧被允许提交放闩
    ingress_queued = threading.Event()    # 入站侧已在门闩外排队
    outcomes = {}

    class _HoldGate:
        """组合式门闩：停用侧取闩后整段停车，给入站侧确定的抢写窗口。"""

        def __init__(self):
            self._lock = threading.Lock()

        def locked(self):
            return self._lock.locked()

        def acquire(self, blocking=True, timeout=-1):
            name = threading.current_thread().name
            ok = self._lock.acquire(blocking, timeout)
            if not ok and name == "ingress-side":
                ingress_queued.set()  # 试取失败 = 闩被持，入站确认进入排队
            if ok and name == "disable-side":
                disable_holding.set()
                may_finish.wait(timeout=5)  # 持闩不放，直到主线程确认入站已排队
            return ok

        def release(self):
            self._lock.release()

    real_gate = appmod.LATCH_GATE
    appmod.LATCH_GATE = _HoldGate()
    real_validate = appmod.validate
    real_disable = appmod.disable_contract

    def disable_side():
        outcomes["disable"] = real_disable("v3", PATH_A, auth["Authorization"])

    def ingress_side():
        try:
            real_validate(
                appmod.IngressIn(subject=SUBJECT, payload_fields=FULL),
                auth["Authorization"],
            )
        except appmod.LatchBlocked as e:
            outcomes["ingress"] = e

    t1 = threading.Thread(target=disable_side, name="disable-side")
    t2 = threading.Thread(target=ingress_side, name="ingress-side")
    t1.start()
    try:
        assert disable_holding.wait(timeout=5), "停用侧未取到门闩"
        t2.start()  # 停用持闩未提交，入站试取失败后阻塞排队
        assert ingress_queued.wait(timeout=5), "入站侧未进入排队"
    finally:
        may_finish.set()  # 确认入站已排队后，才放行停用提交放闩
        t1.join(timeout=5); t2.join(timeout=5)
        appmod.LATCH_GATE = real_gate

    # 停用先提交、入站后拿到闩：后到者被拒且记停用争用，无通过漏写
    assert outcomes["disable"]["state"] == "停用"
    blocked = outcomes["ingress"]
    assert blocked.status_code == 409 and blocked.contended is True
    events = client.get("/api/latches", headers=auth).json()["events"]
    assert any(e["kind"] == "停用争用" for e in events), "后到者未记停用争用"
    recon = _recon(client, auth)
    assert recon["passed"] == 0, "争用期间有通过漏写"
    assert recon["ingress_total"] == 0
