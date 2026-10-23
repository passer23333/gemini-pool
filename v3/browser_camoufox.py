# -*- coding: utf-8 -*-
"""账号浏览器：需要人工介入时才开的可见窗口。

与登录窗口的区别：登录窗口是为了导出 cookie，开完就关；
这里是给人用的 —— 过一下人机验证、看看会话状态、确认某个账号还能不能用。

日常出图完全不经过这里，v3 的立身之本（不常驻浏览器）不受影响。
"""
import pathlib
import threading
import time

import auth
import login_camoufox

BASE = pathlib.Path(__file__).resolve().parent
PROFILES = BASE / "profiles"
SYNC_EVERY = 8        # 每隔几秒看一眼窗口里是不是刚登录/刚续期了

# 每个账号最多一个窗口，避免 Firefox profile 争锁
_WINDOWS = {}
_WLOCK = threading.Lock()
_CLOSE = set()      # 请求关闭的账号 id，_run 循环里检查后退出


def _sync(page, ctx, acc_id, pool):
    """窗口里刚登录 / Google 刚续期 —— 顺手把凭据带回账号池。

    以前这个窗口纯给人看，用户在这里重登完后台还用着旧凭据，得再走一遍
    「重新登录」流程才生效。登录态的续期发生在浏览器里，就在这里收。
    写入一律走 auth.save：脏值、空值会被挡住，旧值会自动备份。
    """
    try:
        ok, _why = login_camoufox._logged_in(page)
        if not ok:
            return False
        ck = login_camoufox._cookies(ctx)
        psid, psidts = ck.get("__Secure-1PSID", ""), ck.get("__Secure-1PSIDTS", "")
        if not psid:
            return False
        acc = pool.get(acc_id) if pool is not None else None
        if acc and auth.clean(psid) == auth.clean(acc.get("psid")) \
                and auth.clean(psidts) == auth.clean(acc.get("psidts")):
            return False                      # 没变化，不打扰
        res = auth.save(acc_id, psid, psidts, source="browser-window", pool=pool,
                        email=(acc or {}).get("email") or "")
        if res.get("ok"):
            pool.update(acc_id, status="active")
            print(f"[browser] {acc_id} 凭据已从窗口同步回账号池")
        return res.get("ok", False)
    except Exception as e:
        print(f"[browser] 同步凭据失败: {type(e).__name__}: {e}")
        return False


def _run(acc_id, proxy, keep, pool=None):
    profile = PROFILES / f"account_{acc_id}"
    profile.mkdir(parents=True, exist_ok=True)
    try:
        with login_camoufox._launch(proxy, profile) as browser:
            ctx = login_camoufox._context(browser)
            page = login_camoufox._page(ctx)
            page.goto("https://gemini.google.com/app", timeout=120000,
                      wait_until="domcontentloaded")
            print(f"[browser] 窗口已打开: {acc_id}（最长保持 {keep}s）")
            t0 = time.time()
            next_sync = 0.0
            while time.time() - t0 < keep:
                with _WLOCK:
                    if acc_id in _CLOSE:
                        break
                try:
                    if page.is_closed():
                        break
                except Exception:
                    break
                if time.time() >= next_sync:
                    next_sync = time.time() + SYNC_EVERY
                    _sync(page, ctx, acc_id, pool)
                time.sleep(1)
            _sync(page, ctx, acc_id, pool)     # 关窗前最后一次：多半是在这里登录的
            print(f"[browser] 窗口结束: {acc_id}")
    except Exception as e:
        print(f"[browser] 开窗失败: {type(e).__name__}: {e}")
    finally:
        with _WLOCK:
            _WINDOWS.pop(acc_id, None)
            _CLOSE.discard(acc_id)
        # 与登录窗口共用 profile，占用标记必须在这里释放，
        # 否则关窗后「重新登录」会一直报「窗口已打开」。
        login_camoufox.release(acc_id)


def open_async(acc_id, proxy="", keep=3600, pool=None):
    """后台开窗，立即返回。"""
    # 与登录流程互斥：同一个 profile 不能同时被两个 Camoufox 实例打开
    if not login_camoufox.acquire(acc_id):
        return {"ok": False, "error": "该账号的浏览器窗口已打开（或正在登录），请先用完再开"}
    with _WLOCK:
        if acc_id in _WINDOWS:
            login_camoufox.release(acc_id)
            return {"ok": False, "error": "该账号的窗口已打开"}
        t = threading.Thread(target=_run, args=(acc_id, proxy, keep),
                             kwargs={"pool": pool}, daemon=True)
        _WINDOWS[acc_id] = t
        t.start()
    return {"ok": True}


def request_close(acc_id):
    """请求关闭某账号的窗口（异步）：置标志，_run 循环下一轮退出并释放 profile。"""
    with _WLOCK:
        if acc_id in _WINDOWS:
            _CLOSE.add(acc_id)
            return True
        return False


def open_windows():
    with _WLOCK:
        return list(_WINDOWS.keys())
