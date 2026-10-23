# -*- coding: utf-8 -*-
"""v3 协议层客户端：不启动浏览器，直接调 Gemini 网页内部端点。

依据社区逆向实现（foxtwin / gemini_webapi）验证过的真实协议：
    GET  /app                     → 取 SNlM0e / cfb2h / FdrFJe
    POST push.clients6.google.com → 两步可续传上传（图生图）
    POST StreamGenerate           → 生成 / 编辑，响应中取 gg-dl 图片地址
"""
import base64
import json
import os
import random
import re
import time
import uuid

from urllib.parse import quote

from curl_cffi import requests as creq   # 带真实浏览器 TLS/HTTP2 指纹的 HTTP 客户端

from image_fetch import (MAX_HOPS, extract_urls, is_image, looks_like_next_hop,
                         max_quality_url, original_url, rank_key)
from concurrent.futures import ThreadPoolExecutor

APP_URL = "https://gemini.google.com/app"
STREAM_URL = ("https://gemini.google.com/_/BardChatUi/data/"
              "assistant.lamda.BardFrontendService/StreamGenerate")
BATCH_URL = "https://gemini.google.com/_/BardChatUi/data/batchexecute"
UPLOAD_URL = "https://push.clients6.google.com/upload/"
PUSH_ID = "feeds/mcudyrk2a4khkz"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
      "Gecko/20100101 Firefox/140.0")

MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
        ".pdf": "application/pdf", ".svg": "image/svg+xml"}

# 一次最多带几张参考图。这是网页端的上限（再多会被服务端拒绝或直接忽略），
# 不是我们随手定的数；超出的部分直接丢弃并在调用侧提示。
MAX_REFERENCE_IMAGES = 10

MODEL_HEADER = "x-goog-ext-525001261-jspb"

# 网页端模型选择头里的 hash。来源：Gemini 页面配置里 "45739311":"gemini-3-pro-image"
# 紧邻的 "45677899":"1acf3172319789ce"；其余几个来自 foxtwin 的模型注册表。
# 模型选择头映射 —— 实测结论（2026-10-18，见 README「模型切换」一节）：
#
#   免费账号在网页端只有「Gemini 自动」一个模式可选；「3.1 Pro 高级推理」与
#   「4 Argon 复杂任务」都需要付费会员。向服务端发送 Pro 的模型 hash 时，无权限
#   账号会被**静默降级**回默认模型 —— 不报错、照样出图、分辨率也不变，
#   因此无法从响应判断是否真的切换。用非法 hash 得到完全相同的表现，可佐证
#   "静默回退" 而非 "header 生效"。
#
# 故：默认不发送该头。仅当账号确已开通会员、且需要显式指定模型时才启用。
MODEL_HASHES = {
    "gemini-image": "",                     # 不带头，走网页端默认（免费账号唯一可用）
    "nano-banana-pro": "1acf3172319789ce",  # gemini-3-pro-image，需付费会员
    "gemini-3-pro-image": "1acf3172319789ce",
}

# ---------------------------------------------------------------------------
# 思考等级（对话维度的档位，不是生图参数）
#
# 网页端弹层里「低点 / 中等 / 高点」是独立于模型的第二组控件，三档免费账号
# 都能点，但它有两个特性：
#   1. 会话级 —— 不随账号持久化，重开页面回到默认的「低点」；
#   2. 协议层直调每次都是新建会话，所以拿到的永远是最低思考预算。
#
# 生图走的是图片工具通路，与思考预算没有观测上的关联；即便如此，网关仍统一
# 按「高点」发起，不对外暴露其它档位，避免调用方误以为这是可调的生图参数。
#
# 载荷下标定位情况：/app 首屏配置（含 SNlM0e / cfb2h / FdrFJe 及模型 hash 表）
# 中不含任何 thinking 相关符号，档位文案由 JS 弹层动态生成，因此在不做浏览器
# 抓包的前提下无法确认该字段在 92 元素信封里的位置。与其猜一个下标写进去
# （错了会破坏整个请求结构），不如留成策略值：定位到下标后只需填
# THINKING_FIELD_IDX 即可生效。
# ---------------------------------------------------------------------------
THINKING_LEVELS = {"low": 0, "medium": 1, "high": 2}
THINKING_FIELD_IDX = None      # 载荷下标，定位后填入 int
DEFAULT_THINKING = "high"      # 生图会话固定使用高点

# 失败文字特征 → 错误类型（顺序敏感：先具体后宽泛）
FAIL_PATTERNS = [
    ("not_logged_in", ["Sign in", "登录", "重新登录"]),
    ("rate_limited", ["用量", "限额", "quota", "超出", "已用尽", "稍后再试"]),
    ("content_blocked", ["已拒绝生成", "blocked", "不符合", "无法生成此", "policy"]),
    ("gen_failed", ["出了点问题", "Try again", "went wrong", "暂时无法"]),
]


class GeminiError(Exception):
    def __init__(self, kind, message):
        self.kind = kind
        super().__init__(message)


# ---------------------------------------------------------------------------
# 会话（conversation）三元组
#
# StreamGenerate 的响应里每段都带三个 id：
#     c_<hex>    会话 id，一轮对话内不变
#     r_<hex>    本轮回复 id，每轮都变
#     rc_<hex>   本轮候选 id，每轮都变
#
# 请求里把它们填进载荷的 meta 位就能续接上一轮（相当于网页里的「接着问」）：
#     精简信封 [[prompt], null, [c, r, rc]]
#     完整信封 inner[2] = [c, r, rc, null, ...]
# 三个都留空串就是开新对话 —— 也就是本网关以前唯一的工作方式。
#
# 实测（2026-10-21）：带上上一轮的 r_/rc_ 再发「把它改成绿色的」，
# 响应里 c_ 不变、r_ 换新，且正文回显了上一轮的 apple/table —— 上下文确实接上了。
# ---------------------------------------------------------------------------
_CID_RE = re.compile(r"(?<![A-Za-z0-9_])c_[0-9a-f]{6,}")
_RID_RE = re.compile(r"(?<![A-Za-z0-9_])r_[0-9a-f]{6,}")
_RCID_RE = re.compile(r"rc_[0-9a-f]{6,}")


def parse_conversation(text):
    """从 StreamGenerate 响应里取会话三元组；取不到返回 None。

    注意 `c_` 的正则带负向断言，否则会把 `rc_xxx` 里的 `c_xxx` 也抓进来；
    反过来 `r_` 的断言也保证不会误吞 `rc_`。
    """
    if not text:
        return None
    c = _CID_RE.search(text)
    if not c:
        return None
    r = _RID_RE.search(text)
    rc = _RCID_RE.search(text)
    return {"c": c.group(0), "r": r.group(0) if r else "",
            "rc": rc.group(0) if rc else ""}


def conversation_meta(conv):
    """三元组 → 载荷 meta（空 = 新对话）。"""
    conv = conv or {}
    return [conv.get("c") or "", conv.get("r") or "", conv.get("rc") or ""]


def _logged_out_hint(html, url=""):
    """区分「未登录」和「页面改版」——两者都表现为取不到 SNlM0e。

    实测对比（2026-10-21，同一个 gemini.google.com/app，只换 Cookie）：

        标记              已登录页   未登录页
        SNlM0e             有        无        ← 两边唯一的结构差异
        "Sign in"          无        有        ← 唯一能区分两者的可靠信号
        "登录"（中文）      有        无
        cfb2h / FdrFJe     有        有        ← 不能用：未登录页也带
        ServiceLogin       有        有        ← 不能用：登录页里也有登出链接
        accounts.google.com 有       有        ← 同上

    为什么非做不可：未登录访问 /app 时 Google **不跳转**（URL 仍是 /app），
    只给一版带「Sign in」按钮的首页。识别不出来就会被当成「页面结构变了」，
    于是账号被反复重试、健康度一路扣到底，用户只看到「生成失败」——
    真正的病因（没登录）被埋掉了。
    返回空串 = 判不出来（那就仍按页面结构问题处理）。
    """
    if not html:
        return "空响应"
    if "Sign in" in html:
        return "页面带「Sign in」按钮（未登录首页）"
    if len(html) < 20000:               # 正常应用页都在几百 KB，短的多半是壳/错误页
        return "响应只有 %d 字节，不是完整应用页" % len(html)
    return ""


def normalize_refs(images, default_mime=None):
    """把各种写法的参考图统一成 [(bytes, filename, mime), ...]。

    收 bytes / (bytes, name) / (bytes, name, mime) 及其列表，空项跳过；
    超过 MAX_REFERENCE_IMAGES 的部分丢弃。mime 按扩展名推断，推断不出
    才用 default_mime —— 不能对所有图都用同一个（jpg 传成 png 会被拒）。
    """
    if isinstance(images, (bytes, bytearray)):
        images = [(bytes(images), "ref.png")]
    elif isinstance(images, tuple):
        images = [images]

    out = []
    for i, it in enumerate(images or [], 1):
        if isinstance(it, (bytes, bytearray)):
            it = (bytes(it), "ref%d.png" % i)
        it = list(it)
        data = it[0] if it else b""
        if not data:
            continue
        name = (it[1] if len(it) > 1 and it[1] else "ref%d.png" % i)
        mt = (it[2] if len(it) > 2 and it[2] else None)
        if not mt:
            mt = MIME.get(os.path.splitext(name)[1].lower(), default_mime or "image/png")
        out.append((data, name, mt))
    return out[:MAX_REFERENCE_IMAGES]


def _b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _pctx():
    # protobuf: field1(len7){ field2(len5){ 5 random bytes } }
    return base64.b64encode(bytes([0x0A, 0x07, 0x12, 0x05]) + os.urandom(5)).decode()


class GeminiWebClient:
    def __init__(self, psid, psidts="", proxy="", timeout=180, size="s0", model="",
                 thinking=DEFAULT_THINKING, conversation=None):
        self.psid, self.psidts, self.proxy, self.timeout = psid, psidts, proxy, timeout
        self.size = size or "s0"     # 图片尺寸后缀：s0=原图, w1024=1024宽
        self.model = model or ""
        self.model_hash = MODEL_HASHES.get(self.model, "")
        # 续接用的会话三元组；None = 本次开新对话。每轮成功后由响应自动刷新。
        self.conv = dict(conversation) if conversation else None
        self.fell_back = False       # 续接失败、已自动退回新对话重发
        # 对外不接受思考等级：只有「高点」一个值，其余档位不在接口/界面暴露。
        self.thinking = THINKING_LEVELS.get((thinking or "").lower(),
                                            THINKING_LEVELS[DEFAULT_THINKING])
        # 用 curl_cffi 而非 requests：后者只有 HTTP/1.1 且 TLS 指纹不匹配任何真实
        # 浏览器，在风控眼里就是脚本。curl_cffi 的 impersonate 会一次性对齐
        # TLS(JA3/JA4) + HTTP/2(Akamai) + 浏览器 header 三者。
        self.s = creq.Session(impersonate="firefox")
        self.s.trust_env = False          # 关键：屏蔽环境里其他 HTTPS_PROXY
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        # 刻意不覆盖 User-Agent：impersonate 自带的 UA 与 TLS 指纹是配套的，
        # 手动改会造成「TLS 像 Firefox、UA 却是另一个版本」的自相矛盾。
        self.s.headers.update({"Accept-Language": "zh-CN,zh;q=0.9",
                               "Origin": "https://gemini.google.com",
                               "Referer": "https://gemini.google.com/"})
        self.s.cookies.set("__Secure-1PSID", psid, domain="google.com", path="/")
        if psidts:
            self.s.cookies.set("__Secure-1PSIDTS", psidts, domain="google.com", path="/")
        self._tk = None
        self._tk_at = 0.0

    # ---------------- 引导 ----------------
    def tokens(self, force=False):
        if self._tk and not force and time.time() - self._tk_at < 600:
            return self._tk
        r = self.s.get(APP_URL, timeout=60, allow_redirects=True)
        if "accounts.google.com" in r.url or "/signin" in r.url:
            raise GeminiError("not_logged_in", "账号未登录（cookies 失效）")
        html = r.text
        g = lambda k: (re.search(r'"%s"\s*:\s*"(.*?)"' % k, html) or [None, None])[1]
        at = g("SNlM0e")
        if not at:
            if "CookieMismatch" in r.url or "32050" in r.url:
                raise GeminiError("not_logged_in", "cookie 冲突（错误 32050）")
            # 绝大多数「取不到 SNlM0e」其实是未登录，不是页面改版：
            # 未登录访问 /app 时 Google **不跳转**，直接给一版带「登录」按钮的
            # 首页（URL 仍是 gemini.google.com）。以前这种情况被当成
            # bootstrap_failed，于是账号被反复重试、健康度被一路扣到底，
            # 而真正该做的是提示去登录。用页面里的登录特征词来区分。
            why = _logged_out_hint(html, r.url)
            if why:
                raise GeminiError("not_logged_in", "账号未登录（%s）" % why)
            raise GeminiError("bootstrap_failed",
                              "未能提取 SNlM0e，页面结构可能已变"
                              + ("（HTTP %d）" % r.status_code if r.status_code >= 400 else ""))
        self._tk = {"at": at, "bl": g("cfb2h"), "sid": g("FdrFJe")}
        self._tk_at = time.time()
        return self._tk

    def warmup(self):
        """网页端在上传/生成前的 ESY5D 预热，失败不影响主流程。"""
        try:
            tk = self.tokens()
            p = {"at": tk["at"],
                 "f.req": json.dumps([[["ESY5D", json.dumps([[[["bard_activity_enabled"]]]]), None, "generic"]]])}
            if tk.get("bl"):
                p["bl"] = tk["bl"]
            if tk.get("sid"):
                p["f.sid"] = tk["sid"]
            self.s.post(f"{BATCH_URL}?rpcids=ESY5D&source-path=%2Fapp&hl=zh-CN"
                        f"&_reqid={random.randint(10**6, 10**7)}&rt=c", data=p,
                        headers={"content-type": "application/x-www-form-urlencoded;charset=utf-8",
                                 "x-same-domain": "1",
                                 "x-goog-ext-525001261-jspb": "[1,null,null,null,null,null,null,null,[4]]",
                                 "x-goog-ext-73010989-jspb": "[0]"}, timeout=30)
        except Exception:
            pass

    # ---------------- 上传 ----------------
    def upload(self, data, filename, mime):
        base = {"push-id": PUSH_ID, "x-tenant-id": "bard-storage", "x-client-pctx": _pctx(),
                "User-Agent": UA, "Referer": "https://gemini.google.com/",
                "Origin": "https://gemini.google.com"}
        r1 = self.s.post(UPLOAD_URL, headers={**base,
                         "content-type": "application/x-www-form-urlencoded;charset=utf-8",
                         "x-goog-upload-protocol": "resumable", "x-goog-upload-command": "start",
                         "x-goog-upload-header-content-length": str(len(data)),
                         "x-goog-upload-header-content-type": mime},
                         data=f"File name: {filename}".encode(), timeout=60)
        uid = r1.headers.get("x-guploader-uploadid")
        if not r1.ok or not uid:
            raise GeminiError("upload_failed", f"上传初始化失败 HTTP {r1.status_code}")
        r2 = self.s.post(f"{UPLOAD_URL}?upload_id={quote(uid)}&upload_protocol=resumable",
                         headers={**base,
                                  "content-type": "application/x-www-form-urlencoded;charset=utf-8",
                                  "x-goog-upload-command": "upload, finalize",
                                  "x-goog-upload-offset": "0"},
                         data=data, timeout=120)
        rid = r2.text.strip()
        if not r2.ok or not rid:
            raise GeminiError("upload_failed", f"上传失败 HTTP {r2.status_code}")
        return rid

    # ---------------- 载荷 ----------------
    @staticmethod
    def _freq_simple(prompt, meta=None):
        """文生图：精简格式（实测最稳）。meta 为会话三元组，空则开新对话。"""
        return json.dumps([None, json.dumps([[prompt], None, conversation_meta(meta)],
                                            ensure_ascii=False)],
                          ensure_ascii=False)

    @staticmethod
    def _freq_full(prompt, files=None, lang="zh-CN", thinking=None, meta=None):
        """92 元素信封：带附件或强制走图片工具时使用。"""
        ref = [[[f[0], 1, None, f[2]], f[1], None, None, None, None, None, None, [0]]
               for f in (files or [])] or None
        m = conversation_meta(meta)
        inner = [None] * 92
        inner[0] = [prompt, 0, None, ref, None, None, 0]
        inner[1] = [lang]
        inner[2] = [m[0], m[1], m[2], None, None, None, None, None, None, ""]
        inner[3] = "!" + _b64url(os.urandom(1910))
        inner[4] = uuid.uuid4().hex
        inner[6] = [1]; inner[7] = 1
        inner[10] = 1; inner[11] = 0
        inner[17] = [[0]]; inner[18] = 0
        inner[27] = 1; inner[30] = [4]; inner[41] = [1]; inner[49] = 14; inner[53] = 0
        inner[59] = str(uuid.uuid4()).upper(); inner[61] = []; inner[68] = 2
        inner[79] = 1; inner[91] = 0
        # 思考等级档位（会话级、不持久化）。下标未定位前不写入，见文件头注释。
        if thinking is not None and THINKING_FIELD_IDX is not None:
            inner[THINKING_FIELD_IDX] = [thinking]
        return json.dumps([None, json.dumps(inner, ensure_ascii=False)], ensure_ascii=False)

    # ---------------- 请求 ----------------
    def _post(self, f_req):
        tk = self.tokens()
        p = {"at": tk["at"], "f.req": f_req}
        if tk.get("bl"):
            p["bl"] = tk["bl"]
        if tk.get("sid"):
            p["f.sid"] = tk["sid"]
        headers = {"content-type": "application/x-www-form-urlencoded;charset=utf-8",
                   "x-same-domain": "1"}
        if self.model_hash:
            # 17 元素图片生成版模型头（与网页端一致），用于指定模型
            headers[MODEL_HEADER] = json.dumps(
                [1, None, None, None, self.model_hash, None, None, 0, [4, 5, 6, 8],
                 None, None, 1, None, None, 1, None, uuid.uuid4().hex.upper()],
                separators=(",", ":"))
        return self.s.post(f"{STREAM_URL}?hl=zh-CN&rt=c&_reqid={random.randint(10**6, 10**7)}",
                           data=p, timeout=self.timeout, headers=headers)

    @staticmethod
    def _images(text):
        return extract_urls(text)

    @staticmethod
    def _classify(text):
        low = text.lower()
        for kind, pats in FAIL_PATTERNS:
            if any(p.lower() in low for p in pats):
                return kind
        return "no_image"

    @staticmethod
    def _valid(data):
        return is_image(data)

    def _download(self, url, hop=0):
        """下载一个地址。返回图片字节；命中下载链的文本跳转时跟随下一跳。"""
        try:
            data = self.s.get(url, timeout=120).content
        except Exception:
            return None
        if is_image(data):
            return data
        nxt = looks_like_next_hop(data)
        if nxt and hop < MAX_HOPS:
            # 中间跳给的地址同样要改写尺寸，否则跟随回来还是缩略图
            return self._download(max_quality_url(nxt), hop + 1)
        return None

    def _finish(self, r):
        if r.status_code == 401:
            raise GeminiError("not_logged_in", "账号会话已失效")
        if r.status_code == 429:
            raise GeminiError("rate_limited", "请求过于频繁")
        # 每轮成功都要记下新的 r_/rc_：续接下一轮时用的是「上一轮」的 id，
        # 不刷新就会一直拿第一轮的 id 去接，接的是同一个老节点。
        conv = parse_conversation(r.text)
        if conv:
            self.conv = conv
        urls = extract_urls(r.text)
        if not urls:
            raise GeminiError(self._classify(r.text), "响应中没有图片")

        # 每个候选准备两个变体：低压缩原图（-d-I）与标准原图（=s0）。
        # 前者画质高但偶尔不可用，后者做兜底；并发取，不额外耗时。
        jobs, seen = [], set()
        for u in urls[:4]:
            for cand in (max_quality_url(u), original_url(u)):
                if cand not in seen:
                    seen.add(cand)
                    jobs.append(cand)

        best, best_url = b"", None
        with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as ex:
            for url, data in zip(jobs, ex.map(self._download, jobs)):
                if data and rank_key(data) > rank_key(best):
                    best, best_url = data, url
        if not best:
            raise GeminiError("bad_image", "候选图片均无法下载")
        self.last_image_url = best_url
        return best

    # ---------------- 对外 ----------------
    def clear_conversation(self):
        """放弃续接，下一轮开新对话。"""
        self.conv = None

    def _with_fallback(self, fn):
        """带续接降级地跑一次生图。

        续接只在会话还活着时成立；对话被服务端回收、或换了个账号，
        原来的 r_/rc_ 就接不上了。这种失败不重试就等于「点了继续却一直报错」，
        所以这里清空会话、按新对话重发一次，并把 fell_back 标出来让上层告知用户。
        登录态/限流不重试 —— 换了模式也一样失败，重发只是浪费额度。
        """
        try:
            return fn(self.conv)
        except GeminiError as e:
            if not self.conv or e.kind in ("not_logged_in", "rate_limited",
                                           "content_blocked"):
                raise
            self.clear_conversation()
            self.fell_back = True
            return fn(None)

    def text2img(self, prompt):
        self.warmup()

        def once(conv):
            try:
                img = self._finish(self._post(self._freq_simple(prompt, conv)))
                if img:
                    return img
            except GeminiError as e:
                if e.kind in ("not_logged_in", "rate_limited"):
                    raise
            # 兜底：强制图片工具信封再试一次
            return self._finish(self._post(
                self._freq_full(prompt, thinking=self.thinking, meta=conv)))
        return self._with_fallback(once)

    def img2img(self, prompt, images, mime=None):
        """参考图生图，最多 MAX_REFERENCE_IMAGES 张。

        images 三种写法都收（老的「单张 + 文件名」调用不用改）：
            bytes                         单张，文件名 ref.png
            (bytes, filename[, mime])     单张，指定文件名
            [(bytes, filename[, mime]), ...]   多张
        """
        self.warmup()
        items = normalize_refs(images, mime)
        if not items:
            return self.text2img(prompt)

        def once(conv):
            # 续接降级会重跑 once，届时要重新上传：服务端那边上一轮的附件
            # 只在被引用时才有效，而新对话信封引用的是新上传的 id。
            files = [(self.upload(d, fn, mt), fn, mt) for d, fn, mt in items]
            return self._finish(self._post(
                self._freq_full(prompt, files, thinking=self.thinking, meta=conv)))
        return self._with_fallback(once)
