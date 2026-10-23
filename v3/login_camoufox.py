# -*- coding: utf-8 -*-
"""Camoufox 登录模块：cookie 失效时开窗重登，自动回写账号池。

v3 主链路是纯 HTTP，不需要浏览器；浏览器只在「登录态维护」这一件事上登场：
Google 会服务端吊销 __Secure-1PSIDTS，而刷新它必须走真实浏览器会话。

登录流程沿用 v2 的成熟做法（engine/gemini_browser.py 的 open_login_window）：
  1. Camoufox 以 headful + 持久化 profile 启动（profile 复用可免密重登）
  2. 打开 gemini.google.com/app，每 2s 轮询登录态（头像 + 输入框 + URL 三重判断）
  3. 登录成功后等 3s 让 __Secure-1PSIDTS 稳定，再导出全部 cookies
  4. 取两个关键 cookie 写入 SQLite，并立即用协议层验一次（确认真的可用）

运行前提：装 camoufox 的那个 Python 解释器（本项目为系统 Python 3.10）。
"""
import os
import pathlib
import sys
import threading
import time

import auth          # 登录态唯一写入口（清洗 / 校验 / 备份 / 拒绝坏值）

BASE = pathlib.Path(__file__).resolve().parent
PROFILES = BASE / "profiles"

# 登录任务状态，供 UI 轮询
_STATE = {
    "state": "idle",        # idle | opening | waiting | verifying | success | error | cancelled
    "account": "",
    "message": "",
    "since": 0.0,
    "cookies": 0,
}
_LOCK = threading.Lock()
_THREAD = None

# profile 级互斥：登录窗口与「账号浏览器」窗口共用同一个 profiles/account_<id>，
# 而 Firefox profile 带独占锁，两个 Camoufox 实例同时打开会启动失败甚至写坏会话。
# 两者各自的检查（登录看 _THREAD、开窗看 _WINDOWS）都看不见对方，因此在这里统一登记。
_BUSY = set()
_BUSY_LOCK = threading.Lock()


def acquire(acc_id):
    """占用某账号的 profile；已被占用则返回 False（调用方应给出明确提示）。"""
    with _BUSY_LOCK:
        if acc_id in _BUSY:
            return False
        _BUSY.add(acc_id)
        return True


def release(acc_id):
    with _BUSY_LOCK:
        _BUSY.discard(acc_id)


def busy_profiles():
    with _BUSY_LOCK:
        return sorted(_BUSY)


def _set(state, message="", account=None, cookies=0):
    with _LOCK:
        _STATE.update(state=state, message=message, since=time.time())
        if account is not None:
            _STATE["account"] = account
        if cookies:
            _STATE["cookies"] = cookies


def _msg(message):
    """只更新提示语，不重置计时（否则轮询时 elapsed 永远停在最后一次更新）。"""
    with _LOCK:
        _STATE["message"] = message


def status():
    with _LOCK:
        d = dict(_STATE)
    d["elapsed"] = round(time.time() - d["since"], 1) if d["since"] else 0
    d["running"] = d["state"] in ("opening", "waiting", "verifying")
    d["busy"] = busy_profiles()                 # 当前被浏览器占用的 profile（账号 id）
    return d


def available():
    """camoufox 是否可导入（决定 UI 上「重新登录」按钮是否可用）。"""
    try:
        import camoufox  # noqa: F401
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------- Camoufox 封装 ----------------
def _launch(proxy, profile):
    """启动 Camoufox。

    注：不要传 geoip=True。它能在带代理时对齐指纹，但需要额外装 camoufox[geoip]，
    未安装时会在进入上下文阶段抛 NotInstalledGeoIPExtra（构造阶段捕获不到）。
    """
    from camoufox.sync_api import Camoufox
    kwargs = {"headless": False, "os": "windows", "timeout": 120000}
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    if profile:
        kwargs["persistent_context"] = True
        kwargs["user_data_dir"] = str(profile)
    return Camoufox(**kwargs)


def _context(browser):
    """统一拿到 BrowserContext。

    Camoufox 有两种返回：普通模式给 Browser（靠 .contexts[0] 取）；
    persistent_context=True 时直接给 BrowserContext，它没有 .contexts，
    直接当成 context 用即可（这行原先会撞 AttributeError）。
    """
    if getattr(browser, "contexts", None):
        return browser.contexts[0]
    if hasattr(browser, "pages") and hasattr(browser, "cookies"):
        return browser
    return browser.new_context()


def _page(context):
    pages = context.pages
    if pages:
        for p in pages[1:]:
            try:
                p.close()
            except Exception:
                pass
        return pages[0]
    return context.new_page()


def _logged_in(page):
    """三重判断：URL 不是登录页 + 有输入框 + 有非默认头像。"""
    try:
        st = page.evaluate("""() => {
            const body = document.body ? document.body.innerText : '';
            const hasInput = !!document.querySelector('.ql-editor');
            let avatar = '';
            for (const img of document.querySelectorAll('img')) {
                const s = img.src || '';
                if (s.includes('googleusercontent.com/a/') && !s.includes('default-user')
                    && img.naturalWidth >= 24) { avatar = s; break; }
            }
            return { hasInput, avatar, url: location.href, bodyHead: body.slice(0, 600) };
        }""")
    except Exception as e:
        return False, f"检测失败: {e}"
    url = st.get("url", "")
    if "accounts.google.com" in url or "/signin" in url.lower():
        return False, "在登录页，等待输入账号"
    if "gemini.google.com/app" in url and st.get("avatar") and st.get("hasInput"):
        return True, "已登录"
    if st.get("hasInput") and "Sign in" not in st.get("bodyHead", "") \
            and "登录" not in st.get("bodyHead", ""):
        return True, "已登录（弱检测）"
    return False, "尚未登录"


def _cookies(context):
    out = {}
    for c in context.cookies():
        out[c["name"]] = c["value"]
    return out


# ---------------- 主流程 ----------------
def _run(acc_id, proxy, timeout, pool, new_account=False):
    if not acquire(acc_id):
        # 同一个 profile 已被另一个 Camoufox 实例占用（多半是「浏览器」窗口还开着）
        _set("error", "该账号的浏览器窗口已打开，请先关闭它再重试", acc_id)
        return
    profile = PROFILES / f"account_{acc_id}"
    profile.mkdir(parents=True, exist_ok=True)
    _set("opening", f"启动浏览器（profile: {profile.name}）", acc_id)
    try:
        with _launch(proxy, profile) as browser:
            ctx = _context(browser)
            page = _page(ctx)
            page.goto("https://gemini.google.com/app", timeout=120000,
                      wait_until="domcontentloaded")
            _set("waiting", "请在弹出的窗口里完成 Google 登录", acc_id)
            t0 = time.time()
            ok = False
            while time.time() - t0 < timeout:
                # 用户直接关掉窗口就不再空等到超时（新增账号时尤其常见）
                try:
                    if page.is_closed():
                        _set("cancelled", "窗口已关闭，未检测到登录", acc_id)
                        return
                except Exception:
                    pass
                ok, why = _logged_in(page)
                if ok:
                    break
                _msg(f"{why}（{int(time.time() - t0)}s）")
                time.sleep(2)
            if not ok:
                _set("error", f"登录超时（{timeout}s）", acc_id)
                return
            time.sleep(3)   # 等 __Secure-1PSIDTS 落定
            ck = _cookies(ctx)
            psid = ck.get("__Secure-1PSID", "")
            psidts = ck.get("__Secure-1PSIDTS", "")
            if not psid:
                _set("error", "已登录但未取到 __Secure-1PSID", acc_id)
                return
            _set("verifying", "登录成功，正在用协议层验证 cookie", acc_id, len(ck))
        # 窗口已关，脏活交给协议层
        ok2, msg, kind = verify(psid, psidts, proxy)
        if not ok2:
            # 验证没过就**绝不写库**：新拿到的这份凭据已被证明不能用，
            # 覆盖上去等于把一个也许还能用的旧凭据换成确定的坏凭据。
            if kind == "not_logged_in":
                auth.mark_result(acc_id, False, kind, pool=pool)
            _set("error", f"cookie 验证未通过：{msg}", acc_id)
            return
        res = auth.save(acc_id, psid, psidts, source="camoufox-login", pool=pool)
        if not res.get("ok"):
            _set("error", "凭据校验未通过：" + str(res.get("message")), acc_id)
            return
        pool.update(acc_id, status="active")
        pool.reset_stats(acc_id)
        pool.log_action("login.success", f"{acc_id} 通过浏览器登录更新凭据"
                        + ("" if res.get("state") == "ok"
                           else f"（注意：{res.get('message')}）"))
        _set("success", f"已更新 cookie 并通过验证（{msg}）", acc_id, len(ck))
    except Exception as e:
        _set("error", f"{type(e).__name__}: {e}", acc_id)
    finally:
        release(acc_id)
        # 新增账号流程：没拿到 cookie（超时/关窗/失败）就把占位账号删掉，
        # 否则账号池里会留下永远登不上的死账号。
        if new_account:
            _drop_placeholder(pool, acc_id)


def _drop_placeholder(pool, acc_id):
    """只删「从未拿到 cookie」的账号，重登场景（已有 psid）不受影响。"""
    try:
        a = pool.get(acc_id)
        if a and not (a.get("psid") or "").strip():
            pool.remove(acc_id)
            pool.log_action("account.add-cancelled",
                            f"{a.get('email') or acc_id} 未完成登录，占位账号已移除")
    except Exception as e:
        print(f"[login] 清理占位账号失败: {e}")


def verify(psid, psidts, proxy=""):
    """用协议层引导一次，确认 cookie 真的能取到 SNlM0e。

    返回 (ok, msg, kind)：kind 是失败类型，调用方据此决定要不要记登录态
    （只有 not_logged_in 说明凭据真的废了，网络错误不算）。
    """
    try:
        from gemini_web import GeminiWebClient
        cli = GeminiWebClient(psid, psidts, proxy)
        tk = cli.tokens(force=True)
        return True, f"令牌长度 {len(tk['at'])}", ""
    except Exception as e:
        return False, str(e)[:160], getattr(e, "kind", "")


def login_async(acc_id, proxy="", timeout=600, pool=None, new_account=False):
    """后台开窗登录，立即返回；进度用 status() 查。"""
    global _THREAD
    if status()["running"]:
        return {"ok": False, "error": "已有登录窗口在运行"}
    if pool is None:
        from accounts import AccountPool
        pool = AccountPool()
    _set("opening", "准备中", acc_id)
    _THREAD = threading.Thread(
        target=_run, args=(acc_id, proxy, timeout, pool),
        kwargs={"new_account": new_account}, daemon=True)
    _THREAD.start()
    return {"ok": True}


def login_new_async(email="", proxy="", timeout=600, pool=None):
    """新增账号的主流做法：先建占位账号，再开窗让用户在浏览器里登录。

    登录成功 → 自动导出 cookie 写回并置 active；
    用户关窗/超时/失败 → 占位账号自动删除，不留死账号。
    """
    if pool is None:
        from accounts import AccountPool
        pool = AccountPool()
    if status()["running"]:
        return {"ok": False, "error": "已有登录窗口在运行，请先完成或关闭它"}
    import uuid as _u
    email = (email or "").strip()
    acc_id = pool.upsert(email or f"acc_{_u.uuid4().hex[:8]}", "", "", proxy)
    pool.update(acc_id, status="login_required", login_state="missing",
                login_msg=auth.MSGS["missing"])   # 未拿到 cookie 前不参与调度
    pool.log_action("account.add-by-login", email or acc_id)
    r = login_async(acc_id, proxy, timeout, pool, new_account=True)
    if r.get("ok"):
        r["id"] = acc_id
    return r


def login_blocking(acc_id, proxy="", timeout=600, pool=None):
    """阻塞式登录，供命令行使用。"""
    if pool is None:
        from accounts import AccountPool
        pool = AccountPool()
    _run(acc_id, proxy, timeout, pool)
    return status()


if __name__ == "__main__":
    # 用法: python login_camoufox.py <acc_id> [proxy]
    ok, err = available()
    print("camoufox:", "可用" if ok else f"不可用 → {err}")
    if len(sys.argv) < 2:
        print("用法: python login_camoufox.py <acc_id> [proxy]")
        sys.exit(1)
    acc, px = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:7890")
    print(login_blocking(acc, px))
