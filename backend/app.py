
import hashlib, secrets, sqlite3, threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 停用门闩：停用/启用与入站写共用一把进程门闩，两侧抢写须排队。
# 后到者（试取闩失败、只能阻塞排队的一侧）只记“停用争用”。
LATCH_GATE = threading.Lock()
# 门闩册允许的两条停用支：甲=同事务拒绝、历史只读；乙=只进停用缓冲、正式计数冻结
LATCH_PATHS = ("甲", "乙")

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "入站契约"
SKELETON = "Schema 契约版本约束入站校验，破坏性变更进缓冲或拒收，并与落库结果对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS contracts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          subject TEXT NOT NULL,
          mode TEXT NOT NULL,
          track TEXT NOT NULL,
          status TEXT NOT NULL,
          required_fields TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ingress_batches(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT NOT NULL,
          payload_fields TEXT NOT NULL,
          contract_version TEXT,
          result TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS buffers(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id INTEGER NOT NULL,
          reason TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS disable_latches(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          contract_version TEXT UNIQUE NOT NULL,
          path TEXT NOT NULL,
          state TEXT NOT NULL,
          chosen_on TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS latch_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          contract_version TEXT NOT NULL,
          kind TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS disable_buffers(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT NOT NULL,
          payload_fields TEXT NOT NULL,
          contract_version TEXT,
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM contracts").fetchone()["c"] == 0:
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v3","订单入站","严格","正式","启用","order_id,amount,currency"))
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v4-shadow","订单入站","兼容","影子","试跑","order_id,amount,currency,channel"))
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v2","库存入站","严格","正式","停用","sku,qty"))
        # 历史已停用但无门闩册的正式契约：补登记甲支（同事务拒绝、历史只读），
        # 让契约状态与门闩册一致；这是补账而非值班选定，chosen_on 留空，
        # 值班当天第一次显式停用时仍可首次选定停用支。
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO disable_latches(contract_version,path,state,chosen_on,updated_at)
               SELECT ct.version, '甲', '停用', '', ?
               FROM contracts ct
               WHERE ct.track='正式' AND ct.status='停用'
                 AND NOT EXISTS (
                   SELECT 1 FROM disable_latches dl WHERE dl.contract_version=ct.version)""",
            (now,),
        )

@app.on_event("startup")
def _s():
    init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM contracts").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "contracts": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/contracts")
def contracts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM contracts ORDER BY id").fetchall()]}

class IngressIn(BaseModel):
    subject: str
    payload_fields: str  # comma fields present


class LatchBlocked(HTTPException):
    """门闩挡下：409，携带是否为后到争用。"""

    def __init__(self, detail: str, contended: bool = False):
        super().__init__(409, detail)
        self.contended = contended


@contextmanager
def latch_gate():
    """停用/启用/入站写共用的门闩。

    先试取：试取失败说明闩正被另一侧持有，本线程即后到者（争用），
    再阻塞排队等前手释放；不能用 locked() 预判，因为等待者被唤醒时
    前手往往已 release，locked() 会误报为无争用。
    """
    contended = not LATCH_GATE.acquire(blocking=False)
    if contended:
        LATCH_GATE.acquire()
    try:
        yield contended
    finally:
        LATCH_GATE.release()


def _log_latch_event(conn, version: str, kind: str, detail: str):
    conn.execute(
        "INSERT INTO latch_events(contract_version,kind,detail,created_at) VALUES (?,?,?,?)",
        (version, kind, detail, datetime.now().isoformat(timespec="seconds")),
    )


def _formal_contract(conn, version: str):
    return conn.execute("SELECT * FROM contracts WHERE version=?", (version,)).fetchone()


@app.post("/api/contracts/{version}/disable")
def disable_contract(version: str, path: str, authorization: str | None = Header(default=None)):
    """停用正式契约：路径选定写入门闩册，当天禁止改口；状态与门闩册同一事务生效。"""
    require_user(authorization)
    if path not in LATCH_PATHS:
        raise HTTPException(400, "停用支仅支持甲或乙")
    with latch_gate() as contended:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ctr = _formal_contract(conn, version)
            if not ctr:
                raise HTTPException(404, "契约不存在")
            if ctr["track"] != "正式":
                raise HTTPException(400, "仅正式契约可停用")
            now = datetime.now().isoformat(timespec="seconds")
            today = date.today().isoformat()
            latch = conn.execute(
                "SELECT * FROM disable_latches WHERE contract_version=?", (version,)
            ).fetchone()
            kind = "停用争用" if contended else "停用"

            if latch is not None:
                chosen_today = latch["chosen_on"] == today and latch["chosen_on"] != ""
                # 当天已显式选定却换支：偏离门闩册，拒绝并记“停用偏了”。
                # chosen_on 为空的是历史补登记，值班尚未选定，不受当天改口限制。
                if chosen_today and latch["path"] != path:
                    _log_latch_event(
                        conn, version, "停用偏了",
                        f"门闩册已选{latch['path']}支，当天不得改用{path}支",
                    )
                    conn.commit()
                    raise HTTPException(409, "停用偏了：当天停用支已写入门闩册，禁止改口")
                if latch["state"] == "停用":
                    # 补登记行（未选定）首次显式停用：落选定日与所选支
                    if latch["chosen_on"] == "":
                        conn.execute(
                            "UPDATE disable_latches SET path=?, chosen_on=?, updated_at=? WHERE contract_version=?",
                            (path, today, now, version),
                        )
                        _log_latch_event(conn, version, kind, f"首次选定{path}支，门闩册落定")
                        conn.commit()
                        return {"version": version, "state": "停用", "path": path}
                    _log_latch_event(conn, version, kind, f"{path}支停用保持（幂等）")
                    conn.commit()
                    return {"version": version, "state": "停用", "path": latch["path"], "idempotent": True}
                # 此前启用过、当天又换支：同样按偏离门闩册拒绝
                if chosen_today and latch["path"] != path:
                    _log_latch_event(conn, version, "停用偏了", f"当天改报{path}支被拒")
                    conn.commit()
                    raise HTTPException(409, "停用偏了：当天停用支已写入门闩册，禁止改口")
                conn.execute(
                    "UPDATE contracts SET status='停用' WHERE version=?", (version,)
                )
                # 补登记行首次显式选支要补 chosen_on；其余保留原选定日
                if latch["chosen_on"] == "":
                    conn.execute(
                        "UPDATE disable_latches SET path=?, state='停用', chosen_on=?, updated_at=? WHERE contract_version=?",
                        (path, today, now, version),
                    )
                else:
                    conn.execute(
                        "UPDATE disable_latches SET path=?, state='停用', updated_at=? WHERE contract_version=?",
                        (path, now, version),
                    )
                _log_latch_event(conn, version, kind, f"{path}支停用生效，入站写已闩死")
                conn.commit()
                return {"version": version, "state": "停用", "path": path}

            # 首次停用：门闩册落定，契约状态同一事务翻转（拒绝新入站同事务生效）
            conn.execute("UPDATE contracts SET status='停用' WHERE version=?", (version,))
            conn.execute(
                "INSERT INTO disable_latches(contract_version,path,state,chosen_on,updated_at) VALUES (?,?,?,?,?)",
                (version, path, "停用", today, now),
            )
            _log_latch_event(
                conn, version, kind,
                f"{path}支停用生效，契约状态与门闩册同一事务落库",
            )
            conn.commit()
            return {"version": version, "state": "停用", "path": path}


@app.post("/api/contracts/{version}/enable")
def enable_contract(version: str, authorization: str | None = Header(default=None)):
    """启用药闩：状态与门闩册同一事务翻回启用，停用支登记保留，历史不迁移。"""
    require_user(authorization)
    with latch_gate() as contended:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ctr = _formal_contract(conn, version)
            if not ctr:
                raise HTTPException(404, "契约不存在")
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute("UPDATE contracts SET status='启用' WHERE version=?", (version,))
            latch = conn.execute(
                "SELECT * FROM disable_latches WHERE contract_version=?", (version,)
            ).fetchone()
            if latch is not None:
                conn.execute(
                    "UPDATE disable_latches SET state='启用', updated_at=? WHERE contract_version=?",
                    (now, version),
                )
            else:
                # 无门闩册的正式契约被启用：建占位行，路径与选定日均留空，
                # 表示值班从未选定停用支；后续首次显式停用可自由选支。
                conn.execute(
                    "INSERT INTO disable_latches(contract_version,path,state,chosen_on,updated_at) VALUES (?,?,?,?,?)",
                    (version, "", "启用", "", now),
                )
            _log_latch_event(
                conn, version, "停用争用" if contended else "启用", "门闩解除，入站写恢复"
            )
            conn.commit()
            return {"version": version, "state": "启用"}


@app.get("/api/latches")
def latches(authorization: str | None = Header(default=None)):
    """停用门闩册、门闩事件与停用缓冲计数（只读视图）。"""
    require_user(authorization)
    with connect() as c:
        items = [dict(r) for r in c.execute(
            "SELECT * FROM disable_latches ORDER BY id").fetchall()]
        events = [dict(r) for r in c.execute(
            "SELECT * FROM latch_events ORDER BY id DESC LIMIT 50").fetchall()]
        dbuf = c.execute("SELECT COUNT(*) c FROM disable_buffers").fetchone()["c"]
    return {"items": items, "events": events, "disable_buffer_rows": dbuf}


@app.post("/api/ingress/validate")
def validate(body: IngressIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    fields = {x.strip() for x in body.payload_fields.split(",") if x.strip()}
    with latch_gate() as contended:
        with connect() as c:
            c.execute("BEGIN IMMEDIATE")
            # 门闩册为准：取该主题最新正式契约（不再只查启用态），停用态必须先被闩下
            ctr = c.execute(
                "SELECT * FROM contracts WHERE subject=? AND track='正式' ORDER BY id DESC LIMIT 1",
                (body.subject,),
            ).fetchone()
            if not ctr:
                raise HTTPException(404, "无正式契约")
            latch = c.execute(
                "SELECT * FROM disable_latches WHERE contract_version=?", (ctr["version"],)
            ).fetchone()
            now = datetime.now().isoformat(timespec="seconds")

            # 契约状态与门闩册不一致 = 偏离门闩册，拒绝并记“停用偏了”
            if (ctr["status"] == "停用") != (latch is not None and latch["state"] == "停用"):
                _log_latch_event(
                    c, ctr["version"], "停用偏了",
                    f"契约状态={ctr['status']} 与门闩册={latch['state'] if latch else '无册'} 不一致，拒绝写入",
                )
                c.commit()
                raise LatchBlocked("停用偏了：契约状态与停用门闩册不一致，入站写拒绝", contended)

            if ctr["status"] == "停用":
                path = latch["path"]
                if contended:
                    _log_latch_event(
                        c, ctr["version"], "停用争用",
                        f"{path}支门闩下后到入站被挡，未写正式入站",
                    )
                if path == "乙":
                    # 乙路径：只进停用缓冲，正式通过/拒收计数与入站列表禁止再增
                    cur = c.execute(
                        "INSERT INTO disable_buffers(subject,payload_fields,contract_version,reason,created_at) VALUES (?,?,?,?,?)",
                        (body.subject, body.payload_fields, ctr["version"], "停用后新入站仅进停用缓冲", now),
                    )
                    c.commit()
                    return {"result": "停用缓冲", "version": ctr["version"], "disable_buffer_id": cur.lastrowid}
                # 甲路径：停用与拒绝同一事务生效，仅允历史只读，不落任何新批次
                c.commit()
                raise LatchBlocked(
                    ("停用争用：" if contended else "") + f"契约{ctr['version']}已停用，拒绝新入站（历史只读）",
                    contended,
                )

            need = {x.strip() for x in ctr["required_fields"].split(",") if x.strip()}
            missing = sorted(need - fields)
            if missing:
                if ctr["mode"] == "兼容":
                    cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                                    (body.subject, body.payload_fields, ctr["version"], "缓冲", "缺字段:"+ ",".join(missing), now))
                    c.execute("INSERT INTO buffers(batch_id,reason) VALUES (?,?)", (cur.lastrowid, "缺字段进双写缓冲"))
                    c.commit()
                    return {"result":"缓冲", "version": ctr["version"], "missing": missing, "batch_id": cur.lastrowid}
                cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                                (body.subject, body.payload_fields, ctr["version"], "拒收", "缺字段:"+ ",".join(missing), now))
                c.commit()
                return {"result":"拒收", "version": ctr["version"], "missing": missing, "batch_id": cur.lastrowid}
            cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                            (body.subject, body.payload_fields, ctr["version"], "通过", "字段齐全", now))
            c.commit()
            return {"result":"通过", "version": ctr["version"], "batch_id": cur.lastrowid}

@app.get("/api/ingress")
def ingress(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM ingress_batches ORDER BY id DESC").fetchall()]}

@app.get("/api/buffers")
def buffers(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT b.*, i.subject, i.payload_fields FROM buffers b JOIN ingress_batches i ON i.id=b.batch_id ORDER BY b.id DESC""").fetchall()
        return {"items":[dict(r) for r in rows]}

@app.get("/api/reconcile")
def reconcile(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        total = c.execute("SELECT COUNT(*) c FROM ingress_batches").fetchone()["c"]
        passed = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='通过'").fetchone()["c"]
        buffered = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='缓冲'").fetchone()["c"]
        rejected = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='拒收'").fetchone()["c"]
        buf_rows = c.execute("SELECT COUNT(*) c FROM buffers").fetchone()["c"]
    return {"ingress_total": total, "passed": passed, "buffered": buffered, "rejected": rejected, "buffer_rows": buf_rows, "aligned": buffered == buf_rows}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
