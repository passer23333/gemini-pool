# -*- coding: utf-8 -*-
"""v3 账号池：SQLite 存储 + 健康度评分 + 冷却 + 轮换。"""
import json
import pathlib
import sqlite3
import threading
import time
import uuid

DB_PATH = pathlib.Path(__file__).with_name("pool.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    email         TEXT,
    psid          TEXT NOT NULL,
    psidts        TEXT,
    proxy         TEXT DEFAULT '',
    status        TEXT DEFAULT 'active',     -- active | banned | limit
    ok_count      INTEGER DEFAULT 0,
    fail_count    INTEGER DEFAULT 0,
    cooldown_till REAL DEFAULT 0,
    last_used     REAL DEFAULT 0,
    last_error    TEXT DEFAULT '',
    created_at    REAL DEFAULT 0,
    -- 登录态（凭据健康度）字段，详见 auth.py
    login_state      TEXT DEFAULT '',      -- ok | partial | stale | revoked | malformed | missing
    login_msg        TEXT DEFAULT '',
    login_checked_at REAL DEFAULT 0,       -- 上次实测（生图/体检/巡检）时间
    psid_saved_at    REAL DEFAULT 0,       -- 上次写入凭据的时间，用于判「太久没刷新」
    login_fails      INTEGER DEFAULT 0     -- 连续「实测未登录」次数
);
CREATE TABLE IF NOT EXISTS stats (
    ts     REAL, account TEXT, ok INTEGER, elapsed REAL, err TEXT
);
-- 行动日志：记录账号/配置等人为操作，与 stats（每次生成）区分开
CREATE TABLE IF NOT EXISTS action_log (
    ts     REAL, level TEXT, action TEXT, detail TEXT
);
"""


class AccountPool:
    def __init__(self, path=DB_PATH, max_per_account=1):
        self.path = str(path)
        self.max_per_account = max_per_account   # 单账号最大在途数，>1 易触发限流
        self._lock = threading.Lock()
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)
        # 每个账号一把锁：同账号串行（避免限流），不同账号可并行
        self._acc_locks = {}
        self._inflight = {}                      # 在途计数，用于跨账号分摊
        self._sched = threading.Lock()

    def _conn(self):
        if not hasattr(self._local, "c"):
            self._local.c = sqlite3.connect(self.path, timeout=10)
            self._local.c.row_factory = sqlite3.Row
        return self._local.c

    # ---------- 迁移 ----------
    # 老库没有登录态这几列，登录态模块读不到就会把每个账号都判成「不可用」。
    # 逐个 PRAGMA 检查补列，已建好的库不用重建、账号不丢。
    EXTRA_COLS = {
        "login_state": "TEXT DEFAULT ''",
        "login_msg": "TEXT DEFAULT ''",
        "login_checked_at": "REAL DEFAULT 0",
        "psid_saved_at": "REAL DEFAULT 0",
        "login_fails": "INTEGER DEFAULT 0",
    }

    def _migrate(self, c):
        have = {r["name"] for r in c.execute("PRAGMA table_info(accounts)")}
        for col, ddl in self.EXTRA_COLS.items():
            if col not in have:
                c.execute("ALTER TABLE accounts ADD COLUMN %s %s" % (col, ddl))

    def acc_lock(self, acc_id):
        with self._lock:
            return self._acc_locks.setdefault(acc_id, threading.Lock())

    # ---------- 导入 ----------
    def import_from_v2(self, db_json_path):
        """从 v2 的 db.json 导入/更新账号（只取两个关键 cookie）。"""
        data = json.loads(pathlib.Path(db_json_path).read_text(encoding="utf-8"))
        n = 0
        with self._conn() as c:
            for a in data.get("accounts", []):
                cookies = {x["name"]: x["value"] for x in a.get("cookies", [])
                           if x.get("name") in ("__Secure-1PSID", "__Secure-1PSIDTS")}
                psid = cookies.get("__Secure-1PSID")
                if not psid:
                    continue
                c.execute("""INSERT INTO accounts(id,email,psid,psidts,proxy,status,created_at)
                             VALUES(?,?,?,?,?,?,?)
                             ON CONFLICT(id) DO UPDATE SET
                               psid=excluded.psid, psidts=excluded.psidts,
                               email=excluded.email, proxy=excluded.proxy""",
                          (a["id"], a.get("email", ""), psid, cookies.get("__Secure-1PSIDTS", ""),
                           a.get("proxy", "") or "", a.get("status", "active"), time.time()))
                n += 1
        return n

    # ---------- 维护 ----------
    @staticmethod
    def parse_cookie_string(raw):
        """从浏览器复制的整条 Cookie 头里提取两个关键值。"""
        out = {}
        for seg in (raw or "").split(";"):
            name, _, val = seg.strip().partition("=")
            if name in ("__Secure-1PSID", "__Secure-1PSIDTS") and val:
                out[name] = val
        return out

    def upsert(self, email, psid, psidts="", proxy="", acc_id=None):
        acc_id = acc_id or f"acc_{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self._conn() as c:
            c.execute("""INSERT INTO accounts(id,email,psid,psidts,proxy,status,created_at,
                                              psid_saved_at)
                         VALUES(?,?,?,?,?,'active',?,?)
                         ON CONFLICT(id) DO UPDATE SET psid=excluded.psid,
                           psidts=excluded.psidts, email=excluded.email,
                           proxy=excluded.proxy, psid_saved_at=excluded.psid_saved_at""",
                      (acc_id, email, psid, psidts, proxy, now, now))
        return acc_id

    def remove(self, acc_id):
        with self._conn() as c:
            c.execute("DELETE FROM accounts WHERE id=?", (acc_id,))

    def update(self, acc_id, **fields):
        allowed = {"email", "psid", "psidts", "proxy", "status",
                   "login_state", "login_msg", "login_checked_at",
                   "psid_saved_at", "login_fails"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sql = "UPDATE accounts SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?"
        with self._conn() as c:
            c.execute(sql, list(fields.values()) + [acc_id])

    def reset_stats(self, acc_id):
        with self._conn() as c:
            c.execute("""UPDATE accounts SET ok_count=0, fail_count=0, cooldown_till=0,
                         last_error='', login_fails=0 WHERE id=?""", (acc_id,))

    # ---------- 选择 ----------
    def pick(self, exclude=()):
        """仅按最近使用选择（不占额），保留给只读查询。"""
        now = time.time()
        rows = self._conn().execute(
            "SELECT * FROM accounts WHERE status='active' AND cooldown_till < ? ORDER BY last_used ASC",
            (now,)).fetchall()
        for r in rows:
            if r["id"] not in exclude:
                return dict(r)
        return None

    # ---------- 占用式调度 ----------
    def reserve(self, exclude=(), wait=90.0):
        """占用一个账号：优先选在途最少的（保证并发请求分摊到不同账号）。
        所有账号都忙时等待，直到有空位或超时。"""
        deadline = time.time() + wait
        while True:
            with self._sched:
                now = time.time()
                rows = self._conn().execute(
                    "SELECT * FROM accounts WHERE status='active' AND cooldown_till < ?",
                    (now,)).fetchall()
                free = []
                for r in rows:
                    if r["id"] in exclude:
                        continue
                    n = self._inflight.get(r["id"], 0)
                    if n < self.max_per_account:
                        free.append((n, r["last_used"], r["ok_count"], r))
                if free:
                    free.sort(key=lambda t: (t[0], t[1], -t[2]))
                    r = free[0][3]
                    self._inflight[r["id"]] = self._inflight.get(r["id"], 0) + 1
                    return dict(r)
            if time.time() >= deadline:
                return None
            time.sleep(0.2)

    def reserve_id(self, acc_id, wait=90.0):
        """占用**指定**账号（续接会话时用）。

        会话是账号私有的，续接请求不能参与轮换，否则拿到别的账号就接不上。
        但仍要走占用流程：不占名额的话并发的续接请求会把同一账号打满，
        界面上的「在途」列也看不到这次请求。
        """
        deadline = time.time() + wait
        while True:
            with self._sched:
                r = self._conn().execute("SELECT * FROM accounts WHERE id=?",
                                         (acc_id,)).fetchone()
                if not r:
                    return None
                n = self._inflight.get(acc_id, 0)
                if n < self.max_per_account:
                    self._inflight[acc_id] = n + 1
                    return dict(r)
            if time.time() >= deadline:
                return None
            time.sleep(0.2)

    def release(self, acc_id):
        with self._sched:
            n = self._inflight.get(acc_id, 0)
            self._inflight[acc_id] = max(0, n - 1)

    def inflight(self):
        return dict(self._inflight)

    def get(self, acc_id):
        r = self._conn().execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
        return dict(r) if r else None

    def all(self):
        return [dict(r) for r in self._conn().execute("SELECT * FROM accounts ORDER BY created_at")]

    # ---------- 反馈 ----------
    def report(self, acc_id, ok, elapsed=0.0, err=""):
        now = time.time()
        with self._conn() as c:
            if ok:
                c.execute("UPDATE accounts SET ok_count=ok_count+1, last_used=?, last_error='' WHERE id=?",
                          (now, acc_id))
            else:
                c.execute("""UPDATE accounts SET fail_count=fail_count+1, last_used=?,
                             last_error=?, cooldown_till=? WHERE id=?""",
                          (now, err[:300], now + self._cooldown(acc_id), acc_id))
            c.execute("INSERT INTO stats(ts,account,ok,elapsed,err) VALUES(?,?,?,?,?)",
                      (now, acc_id, 1 if ok else 0, elapsed, err[:300]))

    def _cooldown(self, acc_id):
        r = self.get(acc_id)
        fails = (r or {}).get("fail_count", 0)
        # 指数退避：连续失败 1 次冷 30s，2 次 2min，3 次以上 10min
        return 30 if fails < 1 else 120 if fails < 2 else 600

    # ---------- 日志 ----------
    def log_action(self, action, detail="", level="info"):
        """记录一次人为操作（增删改账号、登录、改配置等）。"""
        with self._conn() as c:
            c.execute("INSERT INTO action_log(ts,level,action,detail) VALUES(?,?,?,?)",
                      (time.time(), level, action, detail[:500]))

    def recent_actions(self, limit=200):
        return [dict(r) for r in self._conn().execute(
            "SELECT ts,level,action,detail FROM action_log ORDER BY ts DESC LIMIT ?",
            (limit,))]

    def recent_stats(self, limit=200):
        """接口日志：每次生成的账号、成败、耗时、错误。

        stats.account 存的是账号 id，这里 left join 出邮箱，省得 UI 再映射一次。
        """
        return [dict(r) for r in self._conn().execute(
            """SELECT s.ts, s.ok, s.elapsed, s.err,
                      s.account AS account_id, COALESCE(a.email, s.account) AS account
               FROM stats s LEFT JOIN accounts a ON s.account = a.id
               ORDER BY s.ts DESC LIMIT ?""", (limit,))]

    def clear_logs(self, which="all"):
        with self._conn() as c:
            if which in ("all", "action"):
                c.execute("DELETE FROM action_log")
            if which in ("all", "api"):
                c.execute("DELETE FROM stats")

    def health(self, acc_id):
        r = self.get(acc_id)
        if not r:
            return 0.0
        ok, fail = r["ok_count"], r["fail_count"]
        if ok + fail == 0:
            return 1.0
        score = ok / (ok + fail)
        if r["cooldown_till"] > time.time():
            score *= 0.3
        return round(score, 3)

    def snapshot(self):
        # 带上 proxy：控制台「代理」列直接读这里，否则永远显示「全局」
        # auth 摘要：界面「登录态」列直接读这里，省得每行再发一次请求问后端
        import auth
        out = []
        for a in self.all():
            d = auth.from_row(a)
            # 凭据明文绝不能随列表下发：/health 是不鉴权的，账号列表也可能被
            # 转发到别的机器上。只留判断结果和「前 8 位」这种排查用指纹。
            d.pop("psid", None)
            d.pop("psidts", None)
            out.append({"id": a["id"], "email": a["email"], "status": a["status"],
                        "ok": a["ok_count"], "fail": a["fail_count"],
                        "health": self.health(a["id"]),
                        "cooldown": max(0, round(a["cooldown_till"] - time.time())),
                        "proxy": a["proxy"] or "",
                        "last_error": a["last_error"], "auth": d})
        return out
