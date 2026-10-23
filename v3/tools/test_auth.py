# -*- coding: utf-8 -*-
"""登录态模块单测：状态判定 / 坏值拒写 / 备份轮转 / 恢复 / 自愈 / 判死节流。

跑法：  python tools/test_auth.py
不碰真实数据：账号库与凭据存档都落在临时目录里。
"""
import pathlib
import shutil
import sys
import tempfile
import time

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import accounts
import auth

TMP = None
POOL = None
FAILED = []


def _ok(cond, name, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  → " + str(extra)) if extra else ""))
    if not cond:
        FAILED.append(name)


GOOD_PSID = "g.a000" + "x" * 60          # 长度够、字符干净
GOOD_TS = "sidts-CjIB" + "y" * 40


def setup():
    global TMP, POOL
    TMP = pathlib.Path(tempfile.mkdtemp(prefix="gpool_auth_"))
    auth.PROFILES = TMP / "profiles"
    auth.PROFILES.mkdir(parents=True, exist_ok=True)
    POOL = accounts.AccountPool(path=str(TMP / "pool.db"))


def teardown():
    try:
        shutil.rmtree(str(TMP))
    except Exception:
        pass


def new_acc(email="a@x.com"):
    return POOL.upsert(email, "", "")


# ---------------------------------------------------------------- 用例
def t_migrate():
    print("\n[1] 老库补列")
    row = POOL.get(new_acc())
    _ok("login_state" in row and "psid_saved_at" in row and "login_fails" in row,
        "迁移后账号行带登录态字段", sorted(row.keys())[:3])


def t_diagnose():
    print("\n[2] 状态机")
    cases = [
        (("", ""), "missing", False),
        (("   ", ""), "missing", False),
        (("undefined", "x" * 20), "missing", False),
        (("short", GOOD_TS), "malformed", False),
        ((GOOD_PSID + "; extra", GOOD_TS), "malformed", False),
        (('"' + GOOD_PSID + '"', GOOD_TS), "ok", True),          # 引号会被洗掉
        ((GOOD_PSID, ""), "partial", True),
        ((GOOD_PSID, "abc"), "partial", True),                    # TS 太短当作没有
        ((GOOD_PSID, GOOD_TS), "ok", True),
    ]
    for (psid, ts), want_state, want_ok in cases:
        d = auth.diagnose_creds(psid, ts)
        _ok(d["state"] == want_state and d["usable"] == want_ok,
            "状态 %s（psid=%r ts=%r）" % (want_state, psid[:12], ts[:6]),
            "得到 %s/%s" % (d["state"], d["usable"]))

    d = auth.diagnose_creds(GOOD_PSID, GOOD_TS, revoked=True)
    _ok(d["state"] == "revoked" and not d["usable"], "revoked 判定", d["state"])
    old = time.time() - 30 * 86400
    d = auth.diagnose_creds(GOOD_PSID, GOOD_TS, saved_at=old)
    _ok(d["state"] == "stale" and d["usable"], "stale 只是提醒，仍可用", d["state"])
    d = auth.diagnose_creds(GOOD_PSID, GOOD_TS, saved_at=old, checked_at=time.time())
    _ok(d["state"] == "ok", "近期体检过就不算 stale", d["state"])


def t_refuse():
    print("\n[3] 坏值拒写（最关键：不能让一次脏同步毁掉好凭据）")
    aid = new_acc()
    r = auth.save(aid, GOOD_PSID, GOOD_TS, source="test", pool=POOL)
    _ok(r.get("ok") and r["state"] == "ok", "好凭据写入成功", r.get("state"))
    for bad, why in (("", "空值"), ("undefined", "占位符"), ("ab;cd", "含分号"),
                     ("x" * 5, "太短")):
        r = auth.save(aid, bad, GOOD_TS, source="test-bad", pool=POOL)
        _ok(r.get("refused") is True, "拒写：%s" % why, r.get("reason"))
    acc = POOL.get(aid)
    _ok(acc["psid"] == GOOD_PSID and acc["psidts"] == GOOD_TS,
        "拒绝后库里仍是原来的好凭据", acc["psid"][:10])
    r = auth.save(aid, "", "", source="test-placeholder", pool=POOL, allow_empty=True)
    _ok(r.get("ok"), "占位场景（allow_empty）放行", r.get("state"))


def t_backup_rotate():
    print("\n[4] 备份轮转与恢复")
    aid = new_acc()
    versions = []
    for i in range(7):
        psid = GOOD_PSID[:-4] + ("%04d" % i)
        auth.save(aid, psid, GOOD_TS, source="v%d" % i, pool=POOL)
        versions.append(psid)
    bks = auth.list_backups(aid)
    _ok(len(bks) <= auth.BACKUP_KEEP, "备份最多留 %d 份" % auth.BACKUP_KEEP, len(bks))
    _ok(POOL.get(aid)["psid"] == versions[-1], "当前是最后写入的那份")
    r = auth.restore(aid, pool=POOL)
    _ok(r.get("ok"), "恢复最新可用备份", r.get("restored_from"))
    _ok(POOL.get(aid)["psid"] in versions[:-1], "恢复后是历史版本",
        POOL.get(aid)["psid"][-4:])
    # 手工备份（.bak-）也要认
    fp = auth.store_path(aid)
    shutil.copy2(str(fp), str(auth.profile_dir(aid) / ("%s.bak-20260101-000000"
                                                       % auth.STORE_NAME)))
    _ok(any(b.get("manual") for b in auth.list_backups(aid)), "手工备份也进列表")


def t_restore_bad():
    print("\n[5] 恢复只挑可用的备份")
    aid = new_acc()
    auth.save(aid, GOOD_PSID, GOOD_TS, source="good", pool=POOL)
    auth.save(aid, GOOD_PSID[:-4] + "1111", GOOD_TS, source="good2", pool=POOL)
    # 手工塞一份「坏」备份，并让它变成最新的
    bad = auth.profile_dir(aid) / ("%s.bak-20990101-000000" % auth.STORE_NAME)
    bad.write_text('{"psid":"","psidts":"","saved_at":%d}' % (time.time() + 86400 * 900),
                   encoding="utf-8")
    r = auth.restore(aid, pool=POOL)
    _ok(r.get("ok") and POOL.get(aid)["psid"] in (GOOD_PSID, GOOD_PSID[:-4] + "1111"),
        "跳过不可用备份，恢复到好凭据", r.get("restored_from") or r.get("error"))


def t_repair():
    print("\n[6] 自愈")
    # 6a 清洗外壳
    aid = new_acc()
    POOL.update(aid, psid='"  %s  "' % GOOD_PSID, psidts=GOOD_TS, psid_saved_at=time.time())
    r = auth.repair(aid, pool=POOL)
    _ok(r.get("ok") and POOL.get(aid)["psid"] == GOOD_PSID,
        "清洗引号空白", r.get("action"))
    # 6b 补回 PSIDTS（走真实路径：一次只同步到 PSID 的写入把 TS 冲掉了）
    aid = new_acc()
    auth.save(aid, GOOD_PSID, GOOD_TS, source="full", pool=POOL)
    auth.save(aid, GOOD_PSID, "", source="web-sync", pool=POOL)
    _ok(POOL.get(aid)["login_state"] == "partial", "同步丢掉 TS 后判为 partial")
    r = auth.repair(aid, pool=POOL)
    _ok(r.get("ok") and POOL.get(aid)["psidts"] == GOOD_TS,
        "从备份补回 PSIDTS", r.get("action"))
    # 6c 凭据被吊销 → 换备份里的可用那份
    aid = new_acc()
    auth.save(aid, GOOD_PSID, GOOD_TS, source="first", pool=POOL)
    auth.save(aid, GOOD_PSID[:-4] + "9999", GOOD_TS, source="second", pool=POOL)
    POOL.update(aid, login_state="revoked", login_msg="x")
    r = auth.repair(aid, pool=POOL)
    _ok(r.get("ok") and POOL.get(aid)["login_state"] != "revoked",
        "吊销后自动换上备份里的凭据", r.get("action"))
    # 6d 什么都没有 → 明确说要重新登录
    aid = new_acc()
    r = auth.repair(aid, pool=POOL)
    _ok((not r.get("ok")) and r.get("action") == "relogin",
        "无凭据时不硬编，只说要重新登录", r.get("message"))


def t_mark():
    print("\n[7] 判死节流（单次不算，连续才算）")
    aid = new_acc()
    auth.save(aid, GOOD_PSID, GOOD_TS, source="t", pool=POOL)
    r = auth.mark_result(aid, False, "not_logged_in", pool=POOL)
    _ok(r.get("state") == "suspect" and POOL.get(aid)["status"] == "active",
        "第一次未登录只记账，不停用", r.get("state"))
    r = auth.mark_result(aid, False, "not_logged_in", pool=POOL)
    _ok(r.get("state") == "revoked" and POOL.get(aid)["status"] == "login_required",
        "连续两次才判死", r.get("state"))
    # 限流不算登录态问题
    aid2 = new_acc()
    auth.save(aid2, GOOD_PSID, GOOD_TS, source="t", pool=POOL)
    auth.mark_result(aid2, False, "rate_limited", pool=POOL)
    _ok(POOL.get(aid2)["login_fails"] == 0 and POOL.get(aid2)["status"] == "active",
        "限流不记到登录态头上")
    # 生图时实测未登录 → force 立即判死
    aid3 = new_acc()
    auth.save(aid3, GOOD_PSID, GOOD_TS, source="t", pool=POOL)
    auth.mark_result(aid3, False, "not_logged_in", pool=POOL, force=True)
    _ok(POOL.get(aid3)["login_state"] == "revoked", "生图实测未登录立即判死")
    # 成功清零
    auth.mark_result(aid3, True, pool=POOL)
    _ok(POOL.get(aid3)["login_fails"] == 0 and POOL.get(aid3)["login_state"] == "ok",
        "成功后清零")
    _ok(POOL.get(aid3)["status"] == "active", "成功后回到可调度状态")


def t_snapshot_safe():
    print("\n[8] 快照不含凭据明文（/health 是不鉴权的）")
    aid = new_acc()
    auth.save(aid, GOOD_PSID, GOOD_TS, source="t", pool=POOL)
    snap = POOL.snapshot()
    row = next(x for x in snap if x["id"] == aid)
    blob = str(row)
    _ok(GOOD_PSID not in blob and GOOD_TS not in blob, "列表中不含完整凭据")
    _ok(row["auth"]["usable"] is True, "列表带可用的登录态摘要", row["auth"]["state"])


def t_need_login():
    print("\n[9] need_login 只报「活跃但登不上」的")
    good = new_acc("good@x.com")
    auth.save(good, GOOD_PSID, GOOD_TS, source="t", pool=POOL)
    bad = new_acc("bad@x.com")
    POOL.update(bad, status="active", login_state="missing", psid="")
    off = new_acc("off@x.com")
    POOL.update(off, status="disabled", psid="")
    ids = [a["id"] for a in auth.need_login_accounts(POOL)]
    _ok(bad in ids and good not in ids and off not in ids,
        "只报活跃且不可用的", ids)


def main():
    setup()
    try:
        t_migrate()
        t_diagnose()
        t_refuse()
        t_backup_rotate()
        t_restore_bad()
        t_repair()
        t_mark()
        t_snapshot_safe()
        t_need_login()
    finally:
        teardown()
    print("\n" + ("全部通过" if not FAILED else "失败 %d 项：%s" % (len(FAILED), FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
