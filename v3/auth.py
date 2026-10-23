# -*- coding: utf-8 -*-
"""登录态凭据存档：健康诊断 / 原子写 / 自动备份 / 自愈修复 / 一键恢复。

为什么单独一个模块：登录态是整条链路里唯一的「外部状态」——它会被三方改写
（Camoufox 登录导出、内嵌工作台网页同步回写、手动粘贴 Cookie），任何一次写入
坏值都会让后续所有生图静默失败，而且报错往往和真实原因无关（表现为「生成失败」
「没有可用账号」，真正原因是库里的 __Secure-1PSID 是个空串或半截字符串）。

所以：**任何入口都不允许自己 pool.update(psid=...)**，一律走 auth.save()。

凭据只有两个：__Secure-1PSID（主键）+ __Secure-1PSIDTS（时间戳，Google 会
服务端吊销它）。少了前者一定登不上，少了后者通常很快失效，多带其它 Cookie
反而会触发错误 32050 CookieMismatch —— 所以存档里只存这两个。

状态（state）判定优先级：missing > malformed > revoked > partial > stale > ok

  missing    没有 __Secure-1PSID（占位账号 / 从未登录）
  malformed  PSID 存在但明显是脏数据（空串、undefined、含分号引号空白、被截断）
  revoked    协议层实测判定未登录（Google 已吊销会话）
  partial    只有 PSID，没有 PSIDTS（能发请求但不稳，Google 随时可能拒）
  stale      PSIDTS 太久没刷新（还能用，但建议刷新一次）
  ok         健康

usable = state in (ok, partial, stale) —— 即「现在拿去生图大概率能成」。
"""

import glob
import hashlib
import json
import os
import pathlib
import re
import shutil
import threading
import time

BASE = pathlib.Path(__file__).resolve().parent
PROFILES = BASE / "profiles"

STORE_NAME = "_creds.json"
BACKUP_TMPL = "_creds.auto-%d.json"
BAD_TMPL = "_creds.bad-%s.json"
SCHEMA = 1
BACKUP_KEEP = 5
BAD_KEEP = 3

# 长度下限：真实的 __Secure-1PSID 远长于此，比这短必然是截断/粘贴不全
PSID_MIN = 20
PSIDTS_MIN = 8
# PSIDTS 超过这么久没刷新就提示（只是提醒，不判死：Google 的吊销无法本地预知）
STALE_DAYS = 14
# 连续多少次「实测未登录」才把它判死 —— 单次可能是网络抖动/代理绕路
REVOKE_AFTER = 2

# 明显是占位符而不是真凭据
BAD_TOKENS = {"", "undefined", "null", "none", "nan", "n/a", "undefinedundefined"}
# 这些字符不该出现在 cookie 值里（= 不列：base64 结尾可能是填充符）
ILLEGAL_CHARS = re.compile(r"[\s;,\"'`<>|&\\]")

_WRITE_LOCK = threading.Lock()

MSGS = {
    "ok": "登录态正常",
    "partial": "缺少 __Secure-1PSIDTS，请求能发出去但随时会被 Google 拒，建议在「账号工作台」刷新一次",
    "stale": "__Secure-1PSIDTS 已超过 %d 天没刷新，建议在「账号工作台」打开一次让它自动续期" % STALE_DAYS,
    "revoked": "实测未登录（Google 已吊销该会话），需要重新登录或恢复上次可用凭据",
    "malformed": "__Secure-1PSID 是脏数据（空值/截断/含非法字符），不能用它发请求",
    "missing": "还没有登录态（新账号需先登录一次）",
}


# ---------------------------------------------------------------- 路径
def profile_dir(acc_id):
    return PROFILES / ("account_%s" % acc_id)


def store_path(acc_id):
    return profile_dir(acc_id) / STORE_NAME


def _backup_path(acc_id, idx):
    return profile_dir(acc_id) / (BACKUP_TMPL % idx)


# ---------------------------------------------------------------- 规范化
def clean(v):
    """清掉从网页/终端粘贴时夹带的外壳：引号、空白、结尾分号、"name=" 前缀。

    只做无损清洗 —— 清洗后如果还是脏的，交给 malformed 判定，绝不猜着补全。
    """
    s = v if isinstance(v, str) else ("" if v is None else str(v))
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = s.strip()
    # "name=value" 整条粘进来时只取值部分
    if "=" in s and s.split("=", 1)[0].strip() in ("__Secure-1PSID", "__Secure-1PSIDTS"):
        s = s.split("=", 1)[1].strip()
    for q in ('"', "'", "`"):
        if len(s) >= 2 and s[0] == q and s[-1] == q:
            s = s[1:-1].strip()
    s = s.strip().rstrip(";").strip()
    if re.search(r"%3[AaDd]|[%][0-9A-Fa-f]{2}", s):   # 被 URL 编码过
        try:
            from urllib.parse import unquote
            dec = unquote(s)
            if dec and not ILLEGAL_CHARS.search(dec):
                s = dec
        except Exception:
            pass
    return s


def rev_of(psid, psidts):
    return hashlib.sha1(("%s|%s" % (psid or "", psidts or "")).encode(
        "utf-8", "ignore")).hexdigest()[:12]


# ---------------------------------------------------------------- 诊断（纯函数）
def diagnose_creds(psid, psidts="", saved_at=0, checked_at=0, revoked=False,
                   fails=0, now=None):
    """给一对凭据判健康度。不碰磁盘、不碰数据库，测试和调用方都好用。"""
    now = now or time.time()
    psid = clean(psid)
    psidts = clean(psidts)

    if psid.lower() in BAD_TOKENS:
        state = "missing"
    elif ILLEGAL_CHARS.search(psid) or len(psid) < PSID_MIN:
        state = "malformed"
    elif revoked:
        state = "revoked"
    elif psidts.lower() in BAD_TOKENS:
        state = "partial"
    elif len(psidts) < PSIDTS_MIN or ILLEGAL_CHARS.search(psidts):
        # PSIDTS 长度异常/含非法字符：当作「没有」处理，比带着坏值发请求安全
        state = "partial"
    elif saved_at and now - saved_at > STALE_DAYS * 86400 \
            and (not checked_at or now - checked_at > STALE_DAYS * 86400):
        state = "stale"
    else:
        state = "ok"

    level = {"ok": "ok", "partial": "warn", "stale": "warn"}.get(state, "error")
    usable = state in ("ok", "partial", "stale")
    d = {
        "state": state, "level": level, "usable": usable,
        "message": MSGS.get(state, ""),
        "psid": psid, "psidts": psidts,
        "has_psid": bool(psid), "has_psidts": bool(psidts and psidts.lower() not in BAD_TOKENS),
        "saved_at": saved_at or 0, "checked_at": checked_at or 0,
        "fails": int(fails or 0), "rev": rev_of(psid, psidts),
    }
    d["detail"] = _detail(d)
    d["actions"] = actions_for(state)
    return d


def _detail(d):
    """鼠标悬停时看的细节，也是排查时的第一手信息。"""
    bits = ["PSID %s" % (("有（%d 字符，%s…）" % (len(d["psid"]), d["psid"][:8]))
                         if d["has_psid"] else "无"),
            "PSIDTS %s" % ("有" if d["has_psidts"] else "无")]
    if d["saved_at"]:
        bits.append("写入于 %s" % time.strftime("%m-%d %H:%M", time.localtime(d["saved_at"])))
    if d["checked_at"]:
        bits.append("上次体检 %s" % time.strftime("%m-%d %H:%M", time.localtime(d["checked_at"])))
    if d["fails"]:
        bits.append("连续未登录 %d 次" % d["fails"])
    return "；".join(bits)


def actions_for(state):
    """界面据此决定显示哪些按钮，免得用户看着报错不知道点哪个。"""
    if state == "ok":
        return []
    if state == "stale":
        return ["refresh"]
    if state == "partial":
        return ["refresh", "restore_backup"]
    if state == "revoked":
        return ["restore_backup", "relogin"]
    if state == "malformed":
        return ["repair", "restore_backup", "relogin"]
    return ["relogin"]       # missing


def from_row(row):
    """从账号行（DB dict）直接诊断：列表页、闸门都用它，零 IO。"""
    return diagnose_creds(
        (row or {}).get("psid") or "", (row or {}).get("psidts") or "",
        (row or {}).get("psid_saved_at") or 0,
        (row or {}).get("login_checked_at") or 0,
        ((row or {}).get("login_state") or "") == "revoked",
        (row or {}).get("login_fails") or 0)


def usable(row):
    """闸门专用：这个账号现在能不能直接拿来生图。"""
    return from_row(row)["usable"]


# ---------------------------------------------------------------- 快照读
def read_store(acc_id, quarantine=True):
    """容错读快照。损坏文件改名隔离（留证据），不会反复读到坏数据。"""
    fp = store_path(acc_id)
    out = {"state": "missing", "psid": "", "psidts": "", "saved_at": 0,
           "from": "", "rev": "", "error": "", "file": str(fp), "quarantined": ""}
    if not fp.is_file():
        return out
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        out.update(state="corrupt", error=str(e))
        if quarantine:
            try:
                bad = profile_dir(acc_id) / (BAD_TMPL % time.strftime("%Y%m%d-%H%M%S"))
                shutil.move(str(fp), str(bad))
                out["quarantined"] = str(bad)
                _prune(bad, BAD_KEEP)
            except Exception:
                pass
        return out
    if isinstance(data, dict):
        out.update(psid=data.get("psid") or "", psidts=data.get("psidts") or "",
                   saved_at=data.get("saved_at") or 0, from_=data.get("from") or "",
                   rev=data.get("rev") or "")
        out["from"] = out.pop("from_")
        # 文件在且解析成功就是有存档；里面有没有凭据由 diagnose_creds 判
        out["state"] = "ok" if out["psid"] else "empty"
    return out


def _prune(path, keep):
    d = path.parent
    base = path.name.split("-")[0]
    try:
        items = sorted(glob.glob(str(d / (base + "*"))), key=os.path.getmtime, reverse=True)
        for f in items[keep:]:
            try:
                os.remove(f)
            except Exception:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------- 写
def _atomic_write(fp, payload):
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = "%s.tmp.%d" % (fp, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, str(fp))          # 同盘 rename 原子：要么旧文件要么新文件


def backup(acc_id):
    """轮转备份当前快照（1 号最新）。内容没变就不重复留档。"""
    fp = store_path(acc_id)
    if not fp.is_file():
        return ""
    try:
        cur = fp.read_bytes()
        json.loads(cur.decode("utf-8"))
    except Exception:
        return ""                     # 坏文件本身就是证据，不进备份链
    if len(cur) < 30:
        return ""
    latest = _backup_path(acc_id, 1)
    try:
        if latest.is_file() and latest.read_bytes() == cur:
            return ""
    except Exception:
        pass
    for i in range(BACKUP_KEEP, 1, -1):
        src, dst = _backup_path(acc_id, i - 1), _backup_path(acc_id, i)
        if src.is_file():
            try:
                os.replace(str(src), str(dst))
            except Exception:
                pass
    try:
        shutil.copy2(str(fp), str(latest))
        return str(latest)
    except Exception:
        return ""


def payload_of(psid, psidts, source, email=""):
    now = time.time()
    return {"schema": SCHEMA, "saved_at": now, "from": source,
            "email": email or "", "rev": rev_of(psid, psidts),
            "psid": psid, "psidts": psidts}


def save(acc_id, psid, psidts="", source="api", pool=None, email="",
         allow_empty=False):
    """唯一的写入口：清洗 → 诊断 → 坏值拒写 → 备份旧的 → 原子写 → 落库。

    返回 {"ok":True, "state","rev","saved_at"} 或
         {"ok":False,"refused":True,"state","reason","message"}。
    拒写是有意的：宁可保留旧的好凭据，也不能让一次脏同步把账号变成死账号。
    """
    psid, psidts = clean(psid), clean(psidts)
    d = diagnose_creds(psid, psidts, now=time.time())
    if d["state"] == "missing" and not allow_empty:
        return {"ok": False, "refused": True, "state": "missing",
                "reason": "empty_psid",
                "message": "拒绝写入：__Secure-1PSID 为空（当前账号已有凭据，"
                           "不会被空值覆盖）"}
    if d["state"] == "malformed":
        return {"ok": False, "refused": True, "state": "malformed",
                "reason": "malformed_psid",
                "message": "拒绝写入：__Secure-1PSID 是脏数据（%d 字符%s），"
                           "请重新登录或粘贴完整的 Cookie 头"
                           % (len(psid), "，含非法字符" if ILLEGAL_CHARS.search(psid) else "")}

    with _WRITE_LOCK:
        backup(acc_id)
        _atomic_write(store_path(acc_id), payload_of(psid, psidts, source, email))
    now = time.time()
    fields = {"psid": psid, "psidts": psidts, "psid_saved_at": now,
              "login_state": d["state"], "login_msg": d["message"],
              "login_fails": 0}
    if pool is None:
        from accounts import AccountPool
        pool = AccountPool()
    pool.update(acc_id, **fields)
    return {"ok": True, "state": d["state"], "level": d["level"], "usable": d["usable"],
            "rev": d["rev"], "saved_at": now, "message": d["message"]}


# ---------------------------------------------------------------- 备份 / 恢复
def list_backups(acc_id):
    """最近可用的备份（新→旧），带诊断结论；界面据此显示能不能恢复。"""
    out = []
    for i in range(1, BACKUP_KEEP + 1):
        fp = _backup_path(acc_id, i)
        if not fp.is_file():
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        d = diagnose_creds(data.get("psid") or "", data.get("psidts") or "",
                           data.get("saved_at") or 0)
        out.append({"name": fp.name, "saved_at": data.get("saved_at") or 0,
                    "from": data.get("from") or "", "state": d["state"],
                    "usable": d["usable"], "psid_hint": (d["psid"] or "")[:8],
                    "has_psidts": d["has_psidts"]})
    for fp in glob.glob(str(profile_dir(acc_id) / ("%s.bak-*" % STORE_NAME))):
        try:
            data = json.loads(pathlib.Path(fp).read_text(encoding="utf-8"))
        except Exception:
            continue
        d = diagnose_creds(data.get("psid") or "", data.get("psidts") or "",
                           data.get("saved_at") or 0)
        out.append({"name": pathlib.Path(fp).name, "saved_at": data.get("saved_at")
                    or os.path.getmtime(fp), "from": data.get("from") or "",
                    "state": d["state"], "usable": d["usable"],
                    "psid_hint": (d["psid"] or "")[:8], "has_psidts": d["has_psidts"],
                    "manual": True})
    out.sort(key=lambda x: x.get("saved_at") or 0, reverse=True)
    return out


def restore(acc_id, name=None, pool=None):
    """从备份恢复：默认挑最新的「可用」备份，避免恢复出另一份坏凭据。"""
    items = list_backups(acc_id)
    if not items:
        return {"ok": False, "error": "没有可用备份"}
    if name:
        pick = next((b for b in items if b["name"] == name), None)
        if not pick:
            return {"ok": False, "error": "备份 %s 不存在" % name}
        if not pick["usable"]:
            return {"ok": False, "error": "该备份本身不可用（%s）" % pick["state"]}
    else:
        pick = next((b for b in items if b["usable"]), None)
        if not pick:
            return {"ok": False, "error": "所有备份都不含有效登录态，需要重新登录"}
    fp = _backup_path(acc_id, int(name.split("-")[-1].split(".")[0])) \
        if name and name.startswith("_creds.auto-") else None
    if fp is None:
        fp = profile_dir(acc_id) / pick["name"]
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": "读取备份失败: %s" % e}
    res = save(acc_id, data.get("psid") or "", data.get("psidts") or "",
               source="restore:" + pick["name"], pool=pool,
               email=data.get("email") or "")
    if res.get("ok"):
        res["restored_from"] = pick["name"]
        if pool is not None:
            pool.update(acc_id, status="active")
    return res


# ---------------------------------------------------------------- 自愈
def repair(acc_id, pool=None):
    """自愈：清洗 → 补回缺失的 PSIDTS → 实在不行才说要重新登录。

    不会凭空造出登录态：真的没了就明确回 action=relogin，
    而不是写一份看起来正常、实际登不上的凭据。
    """
    if pool is None:
        from accounts import AccountPool
        pool = AccountPool()
    acc = pool.get(acc_id)
    if not acc:
        return {"ok": False, "error": "账号不存在"}
    before = from_row(acc)
    out = {"before": before["state"], "state_before": before["state"],
           "level_before": before["level"]}

    raw_psid, raw_psidts = clean(acc.get("psid")), clean(acc.get("psidts"))

    # 1) 库里的值只是带了脏外壳 → 洗完就能用
    if raw_psid and (raw_psid != (acc.get("psid") or "") or raw_psidts != (acc.get("psidts") or "")):
        res = save(acc_id, raw_psid, raw_psidts, source="repair:clean", pool=pool,
                   email=acc.get("email") or "")
        if res.get("ok") and res["state"] in ("ok", "stale"):
            out.update({"ok": True, "action": "cleaned", "state": res["state"],
                        "message": "已清洗凭据外壳（多余的空白/引号/编码）"})
            return out

    # 2) 缺 PSIDTS → 从「同一个 PSID 的备份」里把它补回来
    if before["has_psid"] and not before["has_psidts"]:
        for b in list_backups(acc_id):
            fp = profile_dir(acc_id) / b["name"]
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if clean(data.get("psid")) != raw_psid:
                continue
            ts = clean(data.get("psidts"))
            if not ts:
                continue
            res = save(acc_id, raw_psid, ts, source="repair:psidts", pool=pool,
                       email=acc.get("email") or "")
            if res.get("ok"):
                out.update({"ok": True, "action": "psidts_restored",
                            "state": res["state"], "from": b["name"],
                            "message": "已从备份补回 __Secure-1PSIDTS"})
                return out

    # 3) 当前凭据已被吊销 → 备份里还有别的可用的就换上
    if before["state"] in ("revoked", "malformed", "missing"):
        for b in list_backups(acc_id):
            if not b["usable"]:
                continue
            fp = profile_dir(acc_id) / b["name"]
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            res = save(acc_id, data.get("psid") or "", data.get("psidts") or "",
                       source="repair:restore", pool=pool, email=acc.get("email") or "")
            if res.get("ok"):
                pool.update(acc_id, status="active")
                out.update({"ok": True, "action": "restored", "state": res["state"],
                            "from": b["name"],
                            "message": "已从备份 %s 恢复可用凭据" % b["name"]})
                return out

    out.update({"ok": False, "state": before["state"], "action": "relogin",
                "message": {"missing": "账号里没有凭据，需要重新登录",
                            "malformed": "凭据是脏数据且备份里也没有可用的，需要重新登录",
                            "revoked": "凭据已被吊销且备份均已失效，需要重新登录"}.get(
                                before["state"], "需要重新登录")})
    return out


# ---------------------------------------------------------------- 结果反馈
def mark_result(acc_id, ok, kind="", pool=None, force=False):
    """把一次真实请求的结果写回登录态（成功清零、未登录累计到阈值才判死）。

    只统计 not_logged_in：限流/内容拦截说明凭据是好的，不能算它头上。
    """
    if pool is None:
        from accounts import AccountPool
        pool = AccountPool()
    acc = pool.get(acc_id)
    if not acc:
        return {"ok": False, "error": "账号不存在"}
    now = time.time()
    login = kind == "not_logged_in"
    if ok:
        fields = {"login_fails": 0, "login_checked_at": now,
                  "login_state": "ok", "login_msg": ""}
        # 实测通过就说明凭据是好的 —— 账号必须回到可调度状态，否则它会
        # 一直挂着「待重登」永不参与轮换，变成一个谁也发现不了的死账号。
        if acc.get("status") == "login_required":
            fields["status"] = "active"
        pool.update(acc_id, **fields)
        return {"ok": True, "state": "ok"}
    if not login:
        pool.update(acc_id, login_checked_at=now)
        return {"ok": True, "state": "unchanged"}
    fails = int(acc.get("login_fails") or 0) + 1
    if fails >= REVOKE_AFTER or force:
        pool.update(acc_id, login_fails=fails, login_checked_at=now,
                    login_state="revoked", login_msg=MSGS["revoked"],
                    status="login_required")
        return {"ok": True, "state": "revoked", "fails": fails}
    pool.update(acc_id, login_fails=fails, login_checked_at=now)
    return {"ok": True, "state": "suspect", "fails": fails}


def need_login_accounts(pool=None):
    """哪些账号现在拿去生图一定失败 —— 界面顶部告警条用。"""
    if pool is None:
        from accounts import AccountPool
        pool = AccountPool()
    return [a for a in pool.all() if a.get("status") == "active" and not usable(a)]
