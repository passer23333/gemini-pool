# -*- coding: utf-8 -*-
"""v3 网关：OpenAI 兼容图片接口，多账号轮换 + 自动重试 + 协议层直调。

启动:  python server.py
接口:
    POST /v1/images/generations   文生图 / 带 image 字段即图生图
                                  mode: new（默认，开新对话）| continue（接着上次改图）
    POST /v1/images/edits         图生图（multipart 或 JSON），同样支持 mode
    GET  /v1/models
    GET  /v1/conversations        生图会话列表
    DELETE /v1/conversations[/id] 结束会话（不结束则下次 continue 会接着它）
    POST /v1/videos/generations   不支持（付费会员功能，直接 501）
    GET  /artifacts/<file>
    GET  /health                  账号池健康度
"""
import atexit
import io
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, unquote

from accounts import AccountPool
from gemini_web import GeminiWebClient, GeminiError
import auth
import browser_camoufox
import conversation as conv_store
import login_camoufox

ROOT = pathlib.Path(__file__).parent
ART = ROOT / "artifacts"
ART.mkdir(exist_ok=True)
LOGDIR = ROOT / "logs"
LOGDIR.mkdir(exist_ok=True)
ACCESS_LOG = LOGDIR / "access.log"
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8")) if (ROOT / "config.json").exists() else {}

# 服务身份：Electron 外壳靠 /health 里的 service 字段判断「这是不是自家网关」，
# 从而区分「上次没退干净的自己」和「v2 正在占用端口」这两种完全不同的情况。
SERVICE = "gemini-pool-v3"
# owner 决定外壳退出时要不要顺手停掉它：
#   electron = 由外壳拉起（即便是上次崩溃留下的孤儿，也可以接管并停掉）
#   manual   = 用户自己在命令行起的，外壳只附着、不擅自杀
OWNER = (os.environ.get("GEMINIPOOL_OWNER") or "manual").lower()
GATEWAY_FILE = ROOT / "gateway.json"
STARTED_AT = time.time()


def discovery_path():
    """外部客户端读的那份服务发现文件的路径。

    项目目录下的 gateway.json 是给外壳判断「端口上的是不是自家人」用的；但外部
    调用方（Infinite Canvas 之类）并不知道本项目装在哪个盘，不可能去翻项目目录。
    所以额外在 %LOCALAPPDATA%\\GeminiPool\\ 下留一份 —— 路径固定，谁都能读。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    try:
        d = pathlib.Path(base) / "GeminiPool"
        d.mkdir(parents=True, exist_ok=True)
        return d / "gateway.json"
    except Exception:
        return None


def preferred_port():
    """用户心里那个「本来该用的端口」。

    端口顺延后会把实际端口回写进 api_port，于是「配置值」就不再是用户的初衷了。
    另存一份 preferred_port，才能一直回答「你想用的那个端口现在通了吗」。
    """
    return int(CFG.get("preferred_port") or CFG.get("api_port") or 17890)


def pid_alive(pid):
    """进程还在不在。判断不了时一律当活着 —— 宁可不清，也别误删别人的文件。"""
    try:
        pid = int(pid)
    except Exception:
        return True
    if pid <= 0:
        return True
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            # PROCESS_QUERY_LIMITED_INFORMATION（0x1000）：只要句柄能开就说明进程还在
            h = k.OpenProcess(0x1000, False, pid)
            if h:
                k.CloseHandle(h)
                return True
            return False
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return True


def purge_stale_discovery():
    """清掉上一个被强杀的实例留下的发现文件。

    进程都没了，文件里的端口就是个死地址 —— 外部客户端照着它连会静默失败，
    不如让它读不到，至少能明确知道「服务没起来」。
    """
    for f in (GATEWAY_FILE, discovery_path()):
        if not f:
            continue
        try:
            p = pathlib.Path(f)
            if not p.exists():
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("service") == SERVICE and not pid_alive(d.get("pid")):
                p.unlink()
        except Exception:
            pass


def gateway_info():
    """一次启动的全部对接信息：端口顺延后，这里是唯一的权威来源。"""
    pref = preferred_port()
    return {"service": SERVICE, "pid": os.getpid(), "port": PORT,
            "base_url": f"http://127.0.0.1:{PORT}",
            "api_key": API_KEY,
            "owner": OWNER, "started": STARTED_AT,
            "config_port": int(CFG.get("api_port") or 17890),
            "preferred_port": pref,
            "drifted": PORT != pref}


def write_gateway_file():
    """写下身份文件 + 对外服务发现文件（两份内容一致）。"""
    data = json.dumps(gateway_info(), ensure_ascii=False)
    for f in (GATEWAY_FILE, discovery_path()):
        if not f:
            continue
        try:
            pathlib.Path(f).write_text(data, encoding="utf-8")
        except Exception:
            pass


def remove_gateway_file():
    """只有「自己写的那份」才删，避免误删继任者留下的文件。"""
    for f in (GATEWAY_FILE, discovery_path()):
        if not f:
            continue
        try:
            p = pathlib.Path(f)
            if not p.exists():
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("pid") == os.getpid():
                p.unlink()
        except Exception:
            pass

PROXY = CFG.get("proxy") or ""
# 端口优先级：外壳指定的环境变量 > config.json。
# 外壳在「主端口被别人占了」时会挑一个空端口传过来，避免两边各猜各的。
PORT = int(os.environ.get("GEMINIPOOL_PORT") or CFG.get("api_port") or 17890)
# 端口冲突时最多往后顺延多少个。50 已经足够覆盖「主端口被别的池子占了」这类情况。
PORT_FALLBACK_TRIES = 50
API_KEY = CFG.get("api_key") or "sk-geminipool"
MAX_ATTEMPTS = int(CFG.get("max_attempts") or 3)
QUEUE_WAIT = float(CFG.get("queue_wait") or 120)      # 全忙时最长排队秒数
# 单账号在途上限。网页端同一个账号开多个标签页可以各发各的请求，说明并发不是
# 账号级硬限制，故默认放开到 3；真遇到「200 但没图」的限流表现时再调回 1 或加账号。
# 即便被限流，run_job 也会自动换号重试，不会直接失败。
MAX_PER_ACCOUNT = int(CFG.get("max_per_account") or 3)
IMG_SIZE = CFG.get("image_size") or "s0"               # s0=原图, w1024=1024宽
# 保存格式：original = 原始 JPEG（Google 给的就是这个，体积小）
#           png      = 转无损 PNG（体积约 1.9 倍，后续编辑/印刷不二次压缩）
# 注意：转 PNG **不会增加细节**，分辨率完全一样，只是不再走有损编码。
SAVE_FORMAT = (CFG.get("save_format") or "original").lower()
# 思考等级属于「对话」维度的档位，不是生图参数。网关统一按高点发起，
# 且不在任何对外接口/控制台暴露其它档位（见 gemini_web.py 文件头注释）。
THINKING = (CFG.get("thinking_level") or "high").lower()
# 生图会话保留时长（小时）。超过就当作已失效，continue 会自动开新对话。
CONV_TTL_HOURS = float(CFG.get("conversation_ttl_hours") or 24)
# 一次最多几张参考图。网页端的上限就是 10，多传的服务端不会用，这里直接截断。
MAX_REF_IMAGES = int(CFG.get("max_reference_images") or 10)

pool = AccountPool(max_per_account=MAX_PER_ACCOUNT)
convs = conv_store.ConversationStore(ROOT / "conversations.json", CONV_TTL_HOURS)

MODELS = [
    {"id": "gemini-image", "object": "model", "available": True,
     "description": "图片生成（网页端「Gemini 自动」模型，不发送模型选择头）"},
    {"id": "nano-banana-2", "object": "model", "available": True,
     "description": "同 gemini-image（别名）"},
    {"id": "nano-banana-pro", "object": "model", "available": False,
     "requires_paid": True,
     "description": "需要付费会员。免费账号发送该模型头会被服务端静默降级为自动模型，"
                    "出图正常但不确定是否真的切换；无会员时请用 gemini-image"},
]


# ---- Gemini 原生协议（/v1beta/models + :generateContent）----
# 客户端（如 Infinite Canvas）走的是 Google 官方 Gemini 协议，不是 OpenAI 兼容协议：
#   GET  {base}/v1beta/models                       头 x-goog-api-key / ?key=
#   POST {base}/v1beta/models/{model}:generateContent
# 本网关只有「Gemini 网页端图片生成」一种能力，所以只暴露图片模型，
# 不列文本/视频模型 —— 列了用户点上去只会失败。
GEMINI_MODEL_ALIASES = [
    ("gemini-image", "Gemini 图片（网页端自动模型）"),
    ("nano-banana", "Nano Banana（同 gemini-image）"),
    ("nano-banana-2", "Nano Banana 2（同 gemini-image）"),
    ("gemini-2.5-flash-image", "Gemini 2.5 Flash Image"),
    ("gemini-2.5-flash-image-preview", "Gemini 2.5 Flash Image Preview"),
]


def _gemini_model_entry(mid, desc):
    return {
        "name": "models/" + mid,
        "baseModelId": mid,
        "version": "001",
        "displayName": desc,
        "description": desc,
        "inputTokenLimit": 32768,
        "outputTokenLimit": 32768,
        "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
    }


GEMINI_MODELS = [_gemini_model_entry(m, d) for m, d in GEMINI_MODEL_ALIASES]


def norm_api_path(p):
    """容忍 base URL 里多填了 /v1 或 /v1beta。

    客户端是把 `/v1beta/models` 直接拼在用户填的 base 后面的，
    用户填 `http://host/v1beta` 就会请求成 `/v1beta/v1beta/models`。
    这类重复前缀在这里折叠掉，省得为了填法反复来回试。
    """
    for _ in range(3):
        new = re.sub(r"^/v1(?=/v1/)", "", p)
        new = re.sub(r"^/v1beta(?=/v1beta/)", "", new)
        new = re.sub(r"^/v1(?=/v1beta/)", "", new)
        if new == p:
            break
        p = new
    return p


def mime_of(b):
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}.get(sniff_ext(b), "image/png")


def sniff_ext(b):
    if b[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def save_artifact(data):
    """落盘产物。

    SAVE_FORMAT=png 时转无损 PNG（体积 ~1.9x，像素与 JPEG 完全一致，
    适合拿去二次编辑/印刷；不提升清晰度）。
    """
    ext = sniff_ext(data)
    if SAVE_FORMAT == "png" and ext != ".png":
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.mode else "RGB")
            buf = io.BytesIO()
            im.save(buf, "PNG", optimize=True)
            data, ext = buf.getvalue(), ".png"
        except Exception as e:
            print(f"[artifact] 转 PNG 失败，保留原格式: {type(e).__name__}: {e}")
    name = f"gemini_{int(time.time())}_{uuid.uuid4().hex[:6]}{ext}"
    (ART / name).write_bytes(data)
    return name, len(data)


def list_artifacts(limit=300):
    """产物列表（画廊用），按修改时间倒序。"""
    items = []
    for f in ART.glob("*"):
        try:
            if f.is_file():
                st = f.stat()
                items.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
        except Exception:
            pass
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:limit]


def reveal_in_file_manager(path):
    """在系统文件管理器里定位文件（Windows 为选中该文件）。

    仅对本机有效：网关通常与浏览器/Electron 在同一台机器上，
    所以从 Python 侧调 explorer 就能把窗口弹到用户眼前。
    """
    p = pathlib.Path(path).resolve()
    try:
        if sys.platform == "win32":
            # explorer 的 /select, 必须与路径连写成同一个参数，否则会当成两个参数忽略
            subprocess.Popen(["explorer", f"/select,{p}"])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p.parent)])
        return True
    except Exception as e:
        print(f"[reveal] 打开文件管理器失败: {type(e).__name__}: {e}")
        return False


def open_dir_in_file_manager(path):
    p = pathlib.Path(path).resolve()
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(p)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True
    except Exception as e:
        print(f"[opendir] 打开目录失败: {type(e).__name__}: {e}")
        return False


def parse_multipart(content_type, body):
    """返回 {字段名: [(filename, bytes), ...]}。

    同名多个文件必须都留着 —— 一次传多张参考图时它们字段名相同（都是 image），
    老实现直接覆盖，结果永远只剩最后一张。
    """
    msg = BytesParser(policy=email_policy).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    out = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        out.setdefault(name, []).append(
            (part.get_filename(), part.get_payload(decode=True) or b""))
    return out


def form_images(form):
    """从 multipart 里取出全部参考图。

    同名字段（image 出现多次）、image[]、images 都收，按文档顺序。
    """
    keys = ("image", "image[]", "images", "image_file", "file", "files")
    vals = []
    for k, items in (form or {}).items():
        if str(k).strip().lower() in keys:
            vals.extend(it[1] for it in items if it[1])
    return vals


def decode_image_ref(ref):
    """支持 图片二进制 / data URL / http URL / 裸 base64。返回 (bytes, filename)。

    解不出来返回 (None, None) 让调用方跳过 —— 多张参考图里有一张坏了，
    应该忽略它继续出图，而不是让整个请求 400。
    """
    import base64 as _b

    if not ref:
        return None, None
    # multipart 取出来的是已经解好的二进制，别再当 base64 解一次（会整批失败）；
    # 反过来，裸 base64 的 bytes 要先解出来才是真图。用魔数区分。
    if isinstance(ref, (bytes, bytearray)):
        b = bytes(ref)
        if looks_like_image(b):
            return b, "ref" + sniff_ext(b)
        try:
            d = _b.b64decode(b, validate=False)
            if looks_like_image(d):
                return d, "ref" + sniff_ext(d)
        except Exception:
            pass
        # 都认不出来也照原样交上去，让服务端判断，别在这里自作主张丢掉
        return b, "ref" + sniff_ext(b)
    if isinstance(ref, str) and ref.startswith("data:"):
        head, _, b64 = ref.partition(",")
        ext = "png" if "png" in head else "jpg"
        try:
            return _b.b64decode(b64), f"ref.{ext}"
        except Exception:
            return None, None
    if isinstance(ref, str) and ref.startswith(("http://", "https://")):
        import requests
        r = requests.get(ref, timeout=60, proxies={"http": PROXY, "https": PROXY} if PROXY else None)
        r.raise_for_status()
        ext = os.path.splitext(urlparse(ref).path)[1] or ".png"
        return r.content, f"ref{ext}"
    try:
        return _b.b64decode(ref), "ref.png"
    except Exception:
        return None, None


def looks_like_image(b):
    """按魔数判断一段字节是不是图片（用于区分「已解码的图」和「裸 base64」）。"""
    if not b:
        return False
    if b[:3] == b"\xff\xd8\xff":
        return True
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return True
    if b[:4] in (b"GIF8", b"II*\x00", b"MM\x00*"):
        return True
    return False


def collect_refs(values):
    """参考图字段 → [(bytes, filename), ...]。

    单个（data URL / http URL / 裸 base64）或它们的数组都收，空列表即文生图。
    """
    if not values:
        return []
    if not isinstance(values, (list, tuple)):
        values = [values]
    out = []
    for v in values:
        d, fn = decode_image_ref(v)
        if d:
            out.append((d, fn))
    return out


def cap_refs(refs):
    """超过上限就截断并记一条日志：多传的图服务端不会用，报错不如出图。"""
    if len(refs) <= MAX_REF_IMAGES:
        return refs, 0
    dropped = len(refs) - MAX_REF_IMAGES
    pool.log_action("ref.truncated", f"参考图 {len(refs)} 张，超出上限，已丢弃 {dropped} 张")
    return refs[:MAX_REF_IMAGES], dropped


def err_code(kind):
    """生图失败类型 → HTTP 状态码。

    auth_required 必须是 401 而不是 503：503 在调用方眼里等于「稍后重试」，
    可登录态的问题重试一万次也不会自己好 —— 只有人去登录一次才行。
    返回 401 才能让客户端明确知道「这是凭证问题，不是服务忙」。
    """
    return {"no_account": 503, "auth_required": 401}.get(kind, 500)


def run_job(fn, exclude=(), model="", only_account="", conversation=None):
    """按账号轮换执行 fn(client)，直到成功或用尽尝试次数。

    调度要点：用占用式 reserve（计入在途数）而非单纯取最久未用，
    否则并发请求会同时挑到同一个账号。

    only_account：续接会话时该会话是哪个账号的就得还发给哪个账号 ——
    c_/r_/rc_ 是账号私有的，换号就接不上（表现为拿到一张完全无关的图，
    或者直接失败）。所以续接请求不参与轮换，拿不到这个账号就直接报错。

    conversation：续接用的三元组，会透传给协议层客户端。
    """
    tried = set(exclude)
    last = None
    acc = None          # 必须在循环外初始化：reserve 超时返回 None 时 break，
    skipped = []        # 登录态不过关被跳过的账号（最后要给调用方一句人话）
    if only_account:
        acc = pool.get(only_account)
        if not acc or acc["status"] != "active":
            raise GeminiError("no_account", "该会话所属账号已不可用，请开新对话")
        if not auth.usable(acc):
            raise GeminiError("auth_required",
                              "该会话所属账号登录态不可用，请到「账号池」重新登录")
        if not pool.reserve_id(only_account, wait=QUEUE_WAIT):
            raise GeminiError("no_account", "该会话所属账号正忙，请稍后重试或开新对话")
        lock = pool.acc_lock(acc["id"])
        t0 = time.time()
        try:
            with lock:
                cli = GeminiWebClient(acc["psid"], acc["psidts"], acc["proxy"] or PROXY,
                                      size=IMG_SIZE, model=model, thinking=THINKING,
                                      conversation=conversation)
                data = fn(cli)
            pool.report(acc["id"], True, time.time() - t0)
            auth.mark_result(acc["id"], True, pool=pool)
            return data, acc, time.time() - t0
        except GeminiError as e:
            if e.kind == "not_logged_in":
                auth.mark_result(acc["id"], False, "not_logged_in", pool=pool, force=True)
                pool.log_action("login.revoked", acc.get("email") or acc["id"])
            else:
                pool.report(acc["id"], False, time.time() - t0, f"{e.kind}: {e}")
            raise
        except Exception as e:
            pool.report(acc["id"], False, time.time() - t0, f"{type(e).__name__}: {e}")
            raise
        finally:
            pool.release(acc["id"])
    for _ in range(MAX_ATTEMPTS):   # 而 break 仍会走 finally，不判空会 TypeError
        # 先非阻塞探一下：一个候选都没有时 reserve 会死等 QUEUE_WAIT（默认 120s），
        # 客户端明明该立刻知道「没有可用账号」，却要干等两分钟才收到报错。
        if not pool.pick(exclude=tried):
            break
        acc = pool.reserve(exclude=tried, wait=QUEUE_WAIT)
        if not acc:
            break
        # 登录态闸门：凭据缺失/脏/已吊销的账号根本不值得发一次请求 ——
        # 发也是必失败，还会把账号的健康度和冷却一起打下去（真正的问题
        # 是「没登录」，不是「这个账号不稳定」）。所以这里直接跳过并记账。
        if not auth.usable(acc):
            skipped.append(acc.get("email") or acc["id"])
            tried.add(acc["id"])
            pool.release(acc["id"])
            acc = None
            continue
        lock = pool.acc_lock(acc["id"])
        t0 = time.time()
        try:
            with lock:
                cli = GeminiWebClient(acc["psid"], acc["psidts"], acc["proxy"] or PROXY,
                                      size=IMG_SIZE, model=model, thinking=THINKING)
                data = fn(cli)
            pool.report(acc["id"], True, time.time() - t0)
            auth.mark_result(acc["id"], True, pool=pool)
            return data, acc, time.time() - t0
        except GeminiError as e:
            last = e
            if e.kind == "not_logged_in":
                # cookie 被吊销：这是「登录态问题」不是「账号不稳定」，
                # 所以刻意不 report(False) —— 不扣健康度、不进冷却，
                # 否则重登成功后账号还要背着旧账慢慢恢复。
                auth.mark_result(acc["id"], False, "not_logged_in", pool=pool, force=True)
                pool.log_action("login.revoked", acc.get("email") or acc["id"])
                skipped.append(acc.get("email") or acc["id"])
                tried.add(acc["id"])
                continue
            pool.report(acc["id"], False, time.time() - t0, f"{e.kind}: {e}")
            if e.kind in ("rate_limited", "content_blocked"):
                tried.add(acc["id"])
                continue
        except Exception as e:
            last = e
            pool.report(acc["id"], False, time.time() - t0, f"{type(e).__name__}: {e}")
        finally:
            if acc:
                pool.release(acc["id"])
                acc = None
    # 登录态原因导致的「没有可用账号」要单独说清楚，否则用户只会看到
    # 一句「没有可用账号」，不知道该去登录还是该等限流结束。
    if skipped:
        raise GeminiError("auth_required",
                          "以下账号登录态不可用（未登录 / 凭据失效），"
                          "请到「账号池」点「重新登录」或在「账号工作台」登录一次："
                          + "、".join(skipped[:6])
                          + (f" 等 {len(skipped)} 个" if len(skipped) > 6 else ""))
    if isinstance(last, GeminiError):
        raise last
    # 登录态造成的「没有可用账号」一律按 401 报（见 err_code）：
    # 它们和上面 skipped 是同一类问题，不该因为账号已经被停用就换成 503。
    needs_login = [a["email"] for a in pool.all() if a["status"] == "login_required"]
    if needs_login:
        raise GeminiError("auth_required",
                          "所有账号登录态已失效，请到「账号池」点「重新登录」"
                          "或在「Gemini 工作台」登录一次：" + "、".join(needs_login))
    raise GeminiError("no_account", f"没有可用账号（{last}）" if last else "没有可用账号")


def run_batch(fn, n, model="", only_account="", conversation=None):
    """n>1 时并行生成（受账号数限制）。

    续接会话时 n>1 没有意义（多个并发回复会把同一个会话的 r_/rc_ 搅乱），
    所以带 only_account 时强制只发一张。
    """
    if n <= 1 or only_account:
        return [run_job(fn, model=model, only_account=only_account,
                        conversation=conversation)]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(n, 4)) as ex:
        return list(ex.map(lambda _: run_job(fn, model=model), range(n)))


def parse_conv_mode(req):
    """从请求里解析「新对话 / 继续对话」以及要续接的会话 id。

    三种写法都认，方便不同客户端：
        {"mode": "continue"}
        {"conversation_id": "c_xxx"}
        {"conversation": "c_xxx"}
    没写就是 new —— 与改造前的行为一致，老客户端不受影响。
    """
    mode = str(req.get("mode") or "").strip().lower()
    cid = str(req.get("conversation_id") or req.get("conversation") or "").strip()
    if cid.lower() in ("new", "continue"):      # 有人会把 mode 写进 conversation
        mode, cid = cid.lower(), ""
    if mode not in ("new", "continue"):
        mode = "continue" if cid else "new"
    return mode, cid


def resolve_conversation(mode, cid):
    """决定这一发要不要续接。返回 (conv 三元组, 绑定的账号 id, 实际模式)。

    要续接却找不到可用会话时**退化成新对话**，不报错：
    用户点了「继续」但会话已过期/被删，开张新图总比失败好，
    只是在结果里把 mode 如实标成 new，让他知道这一发没接上。
    """
    if mode != "continue":
        return None, "", "new"
    rec = convs.get(cid) if cid else convs.latest()
    if not rec or not rec.get("c"):
        return None, "", "new"
    return ({"c": rec.get("c") or "", "r": rec.get("r") or "",
             "rc": rec.get("rc") or ""}, rec.get("account") or "", "continue")


def do_generate(prompt, refs=None, n=1, model="", mode="new", cid=""):
    """一次生图（文生图/图生图都走这里），顺带维护会话存档。

    refs 是 [(bytes, filename), ...]，最多 MAX_REF_IMAGES 张；空即文生图。
    返回 (results, conv_info)。conv_info 里带本次实际用的模式、会话 id、
    第几轮，以及是否因续接失败而退回新对话 —— 这几项都要回到调用方，
    否则用户无法判断「继续」到底生效没有。
    """
    refs, dropped = cap_refs(list(refs or []))
    conv, only_acc, real_mode = resolve_conversation(mode, cid)
    holder = {}

    def job(cli):
        holder["cli"] = cli
        return cli.img2img(prompt, refs) if refs else cli.text2img(prompt)

    results = run_batch(job, max(1, min(int(n or 1), 4)), model=model,
                        only_account=only_acc, conversation=conv)
    cli = holder.get("cli")
    info = {"mode": real_mode, "conversation_id": (cli.conv or {}).get("c", "") if cli else "",
            "turn": 0, "fell_back": bool(cli and cli.fell_back),
            "references": len(refs), "refs_dropped": dropped}
    if cli and cli.conv:
        rec = convs.save(cli.conv, results[0][1], prompt)
        info["conversation_id"] = rec["c"] if rec else (cli.conv or {}).get("c", "")
        info["turn"] = rec["turn"] if rec else 1
    if info["fell_back"]:
        info["mode"] = "new"
        pool.log_action("conv.fallback", "会话续接失败，已按新对话重发")
    return results, info


class Handler(BaseHTTPRequestHandler):
    server_version = "GeminiPoolV3/1.0"
    # HTTP/1.1 + keep-alive：默认是 HTTP/1.0（每请求后断连），浏览器/WebView
    # 在短时间内连发预检与真实请求时会被拖慢甚至复用失败。所有响应都已带
    # Content-Length，切到 1.1 是安全的。
    protocol_version = "HTTP/1.1"

    # ---------- 跨域 ----------
    # 为什么必须有：Infinite Canvas 这类客户端的界面跑在 Chromium（它的 AppData 下
    # 是 EBWebView）里，下拉框是**页面里的 fetch** 直连网关。浏览器同源策略会先发
    # OPTIONS 预检，再校验响应头；网关不做这两件事，请求在浏览器层就被拦掉，
    # 根本进不到下面的任何路由 —— 表现就是「拉不到模型」且服务端毫无痕迹。
    CORS_HEADERS = ("Authorization, Content-Type, x-goog-api-key, x-goog-api-client, "
                    "x-goog-user-project, x-requested-with, accept, origin, cache-control")

    def _cors(self):
        # file:// / Electron 自定义协议是 opaque origin，浏览器会送 Origin: null，
        # 此时回显 "null" 反而会被拒；这种情况以及没有 Origin 时一律给 `*`。
        # 我们的鉴权走请求头而非 cookie，不存在带凭证场景，`*` 是安全的。
        origin = self.headers.get("Origin") or "*"
        allow = origin if origin.lower().startswith(("http://", "https://")) else "*"
        self.send_header("Access-Control-Allow-Origin", allow)
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD")
        self.send_header("Access-Control-Allow-Headers", self.CORS_HEADERS)
        self.send_header("Access-Control-Expose-Headers",
                         "Content-Length, Content-Type, Date, Server")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        # 预检：必须放行且不鉴权（浏览器发 OPTIONS 时不带自定义头的实际值）
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    # ---------- 访问日志 ----------
    # 只有落到文件才能事后判断「请求到底有没有到达网关」。
    def log_request(self, code="-", size="-"):
        try:
            keyed = bool(self.headers.get("Authorization")
                         or self.headers.get("x-goog-api-key")
                         or self._query_key())
            line = "%s %s %s -> %s | key=%s | origin=%s | ua=%s\n" % (
                time.strftime("%m-%d %H:%M:%S"),
                self.command, self.path, code,
                "yes" if keyed else "no",
                self.headers.get("Origin") or "-",
                (self.headers.get("User-Agent") or "-")[:70])
            with ACCESS_LOG.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def log_message(self, fmt, *args):
        # 控制台保留一行即可，避免 ThreadingHTTPServer 的默认噪音刷屏
        print(f"[{time.strftime('%H:%M:%S')}] {fmt % args}")

    # ---------- 工具 ----------
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, kind, msg):
        # 同时照顾两种客户端：OpenAI 读 error.message，
        # Gemini 原生读 error.message + error.code/status
        self._json(code, {"error": {"message": msg, "type": kind,
                                    "code": code,
                                    "status": {400: "INVALID_ARGUMENT",
                                               401: "UNAUTHENTICATED",
                                               404: "NOT_FOUND",
                                               500: "INTERNAL",
                                               501: "UNIMPLEMENTED",
                                               503: "UNAVAILABLE"}.get(code, kind)}})

    def _authed(self):
        """鉴权：OpenAI 客户端用 Bearer，Gemini 原生客户端用 x-goog-api-key 或 ?key=。"""
        h = self.headers.get("Authorization", "")
        if h == f"Bearer {API_KEY}":
            return True
        if self.headers.get("x-goog-api-key") == API_KEY:
            return True
        return self._query_key() == API_KEY

    def _query_key(self):
        try:
            q = parse_qs(urlparse(self.path).query)
        except Exception:
            return ""
        return (q.get("key") or [""])[0]

    def _authed_soft(self):
        """模型列表这类只读接口：没带凭证就放行，带了就必须是对的。

        Gemini 系客户端拉模型清单时常常不带 key，卡死在鉴权上就没法对接了。
        """
        has = (self.headers.get("Authorization")
               or self.headers.get("x-goog-api-key")
               or self._query_key())
        return True if not has else self._authed()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # ---------- 路由 ----------
    def do_GET(self):
        p = norm_api_path(urlparse(self.path).path)
        if p.startswith("/artifacts/"):
            f = ART / pathlib.Path(unquote(p)).name
            if not f.exists():
                return self._err(404, "not_found", "文件不存在")
            data = f.read_bytes()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "image/jpeg" if f.suffix == ".jpg" else f"image/{f.suffix.lstrip('.')}")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return
        if p in ("/", "/ui", "/ui/"):
            f = ROOT / "ui.html"
            if not f.exists():
                return self._err(404, "not_found", "缺少 ui.html")
            body = f.read_bytes()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p == "/health":
            # port 带上真实监听端口：端口自动漂移后，这是告诉外部「我到底在哪」的权威来源
            return self._json(200, {"status": "ok", "proxy": PROXY,
                                    "inflight": pool.inflight(),
                                    "service": SERVICE, "pid": os.getpid(),
                                    "owner": OWNER, "started": STARTED_AT,
                                    "port": PORT, "base_url": f"http://127.0.0.1:{PORT}",
                                    # 需要人去登录的账号 id：客户端拿到非空就该提示用户，
                                    # 而不是反复重试（重试多少次都不会自己好）
                                    "need_login": [a["id"] for a in auth.need_login_accounts(pool)],
                                    "accounts": pool.snapshot()})
        if p == "/v1/models":
            return self._json(200, {"object": "list", "data": MODELS})
        if p == "/v1beta/models":
            if not self._authed_soft():
                return self._err(401, "auth", "API Key 无效")
            return self._json(200, {"models": GEMINI_MODELS})
        if p.startswith("/v1beta/models/"):
            if not self._authed_soft():
                return self._err(401, "auth", "API Key 无效")
            mid = unquote(p[len("/v1beta/models/"):]).strip("/")
            mid = mid[7:] if mid.startswith("models/") else mid
            entry = next((m for m in GEMINI_MODELS if m["baseModelId"] == mid), None)
            if not entry:
                return self._err(404, "not_found", f"未知模型 {mid}")
            return self._json(200, entry)
        if p == "/v1/accounts":
            if not self._authed():
                return self._err(401, "auth", "API Key 无效")
            return self._json(200, {"accounts": pool.snapshot(), "inflight": pool.inflight()})
        if p == "/v1/conversations":
            if not self._authed():
                return self._err(401, "auth", "API Key 无效")
            q = parse_qs(urlparse(self.path).query)
            aid = (q.get("account") or [""])[0]
            convs.prune()
            return self._json(200, {"conversations": convs.list(),
                                    "ttl_hours": round(convs.ttl / 3600, 1),
                                    "latest": convs.latest(aid) or None})
        if p == "/v1/accounts/secret":
            # 仅供本机控制台（内嵌工作台）恢复网页会话用。
            # 返回的是可直接登录 Gemini 的凭据，所以除 Key 之外再加一层本机限制：
            # 网关一旦被转发到局域网，这个接口也不会跟着泄漏出去。
            if not self._authed():
                return self._err(401, "auth", "API Key 无效")
            peer = (self.client_address or ("", 0))[0]
            if peer not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                return self._err(403, "local_only", "该接口只允许本机调用")
            q = parse_qs(urlparse(self.path).query)
            acc = pool.get((q.get("id") or [""])[0])
            if not acc:
                return self._err(404, "not_found", "账号不存在")
            # 带上登录态摘要：工作台据此决定「带凭据进去」还是「先清掉再让用户重登」。
            # 摘要里没有凭据明文，只有判定结果和排查用的细节。
            d = auth.from_row(acc)
            d.pop("psid", None)
            d.pop("psidts", None)
            return self._json(200, {"id": acc["id"], "email": acc["email"],
                                    "psid": acc["psid"], "psidts": acc["psidts"],
                                    "proxy": acc["proxy"] or PROXY, "auth": d})
        if p == "/v1/accounts/auth":
            if not self._authed():
                return self._err(401, "auth", "API Key 无效")
            return self.auth_diagnose()
        if p == "/v1/accounts/auth/backups":
            if not self._authed():
                return self._err(401, "auth", "API Key 无效")
            return self.auth_backups()
        if p == "/v1/login/status":
            if not self._authed():
                return self._err(401, "auth", "API Key 无效")
            ok, err = login_camoufox.available()
            return self._json(200, {"available": ok, "reason": err,
                                    "windows": browser_camoufox.open_windows(),
                                    "status": login_camoufox.status()})
        if p == "/v1/logs":
            if not self._authed():
                return self._err(401, "auth", "API Key 无效")
            q = parse_qs(urlparse(self.path).query)
            which = (q.get("type") or ["api"])[0]
            try:
                limit = max(1, min(int((q.get("limit") or [200])[0]), 1000))
            except Exception:
                limit = 200
            if which == "action":
                return self._json(200, {"type": "action",
                                        "items": pool.recent_actions(limit)})
            return self._json(200, {"type": "api",
                                    "items": pool.recent_stats(limit)})
        if p == "/v1/artifacts":
            if not self._authed():
                return self._err(401, "auth", "API Key 无效")
            return self._json(200, {"artifacts": list_artifacts(),
                                    "dir": str(ART.resolve())})
        if p == "/v1/config":
            if not self._authed():
                return self._err(401, "auth", "API Key 无效")
            # runtime 段自报真实端口：漂移后 config 里的 api_port 会被回写成实际值，
            # 但 UI 仍要能回答「用户本来想用的端口现在通了吗」，所以两个都给。
            return self._json(200, {"config": CFG, "runtime": {
                "port": PORT, "base_url": f"http://127.0.0.1:{PORT}",
                "preferred_port": preferred_port(),
                "drifted": PORT != preferred_port(),
                "discovery_file": str(discovery_path() or ""),
            }})
        self._err(404, "not_found", "not found")

    def do_DELETE(self):
        p = norm_api_path(urlparse(self.path).path)
        if not self._authed():
            return self._err(401, "auth", "API Key 无效")
        # 结束会话 = 下次 continue 不再接它。不结束的话「继续」会一直挂在
        # 最近一次对话上，用户想换个主题却总被旧上下文带偏。
        if p == "/v1/conversations":
            n = len(convs.list())
            convs.clear()
            pool.log_action("conv.clear", f"结束全部会话（{n} 个）")
            return self._json(200, {"ok": True, "deleted": n})
        if p.startswith("/v1/conversations/"):
            cid = unquote(p[len("/v1/conversations/"):]).strip("/")
            if not convs.delete(cid):
                return self._err(404, "not_found", "会话不存在或已过期")
            pool.log_action("conv.delete", cid)
            return self._json(200, {"ok": True, "deleted": 1, "conversation_id": cid})
        self._err(404, "not_found", "not found")

    def do_POST(self):
        p = norm_api_path(urlparse(self.path).path)
        if not self._authed():
            return self._err(401, "auth", "API Key 无效")
        if p == "/v1/images/generations":
            return self.handle_generate()
        if p == "/v1/images/edits":
            return self.handle_edit()
        if p.startswith("/v1beta/models/"):
            # 注意别写成 ":generateContent" in p —— streamGenerateContent 的冒号后
            # 是 "stream…"，子串匹配会漏掉流式接口。
            if ":generateContent" in p or ":streamGenerateContent" in p:
                return self.handle_gemini_content(p)
            return self._err(501, "not_supported",
                             "本网关只支持 :generateContent / :streamGenerateContent（出图），"
                             "该动作未实现")
        if p == "/v1/videos/generations":
            # 视频生成是 Gemini 付费会员功能，免费账号不可用，本网关不提供该能力。
            return self._err(501, "not_supported",
                             "视频生成需付费会员，本网关不支持；"
                             "出图请用 /v1/images/generations 或 /v1/images/edits")
        if p == "/v1/accounts/add":
            return self.accounts_add()
        if p == "/v1/accounts/update":
            return self.accounts_update()
        if p == "/v1/accounts/delete":
            return self.accounts_delete()
        # v2 迁移入口已移除（旧版本目录已清理，账号早已迁移完毕）。
        # 新增账号统一走 /v1/login/new（浏览器开窗登录）。
        if p == "/v1/accounts/test":
            return self.accounts_test()
        if p == "/v1/accounts/auth/repair":
            return self.auth_repair()
        if p == "/v1/accounts/auth/restore":
            return self.auth_restore()
        if p == "/v1/login/new":
            return self.login_new()
        if p == "/v1/login/start":
            return self.login_start()
        if p == "/v1/browser/open":
            return self.browser_open()
        if p == "/v1/browser/close":
            return self.browser_close()
        if p == "/v1/config":
            return self.config_save()
        if p == "/v1/logs/clear":
            return self.logs_clear()
        if p == "/v1/artifacts/reveal":
            return self.artifacts_reveal()
        if p == "/v1/artifacts/open-dir":
            return self.artifacts_open_dir()
        self._err(404, "not_found", "not found")

    # ---------- 账号管理 ----------
    def accounts_add(self):
        """{email, cookie:"整条 Cookie 头"} 或 {email, psid, psidts}

        还有第三种：{placeholder:true} —— 先占个位，Cookie 稍后由工作台在网页里
        登录成功后回写。这样新增账号也不必再开一个 Camoufox 窗口。
        """
        try:
            req = json.loads(self._body() or b"{}")
            psid, psidts = req.get("psid"), req.get("psidts", "")
            if req.get("cookie"):
                got = AccountPool.parse_cookie_string(req["cookie"])
                psid = psid or got.get("__Secure-1PSID")
                psidts = psidts or got.get("__Secure-1PSIDTS", "")
            placeholder = bool(req.get("placeholder"))
            if not psid and not placeholder:
                return self._err(400, "bad_request", "缺少 __Secure-1PSID")
            # 建号前先验凭据：脏的 __Secure-1PSID 一旦入库，账号就成了
            # 「看起来能用、其实每次都失败」的死账号，排查成本极高。
            if not placeholder:
                d = auth.diagnose_creds(auth.clean(psid), auth.clean(psidts))
                if d["state"] in ("missing", "malformed"):
                    return self._err(400, "bad_request", d["message"])
            if placeholder:
                psid = psid or ""      # 库里 psid 是 NOT NULL，占位用空串
            acc_id = pool.upsert(req.get("email", "") or f"acc_{uuid.uuid4().hex[:8]}",
                                 "", "", req.get("proxy", ""))
            if placeholder:
                pool.update(acc_id, status="login_required", login_state="missing",
                            login_msg=auth.MSGS["missing"])
            else:
                auth.save(acc_id, psid, psidts, source="api:add", pool=pool,
                          email=req.get("email", ""))
                pool.update(acc_id, status="active")
            pool.log_action("account.add", req.get("email", "") or acc_id)
        except Exception as e:
            return self._err(400, "bad_request", f"添加失败: {e}")
        self._json(200, {"id": acc_id, "accounts": pool.snapshot()})

    def accounts_update(self):
        try:
            req = json.loads(self._body() or b"{}")
            if not req.get("id"):
                return self._err(400, "bad_request", "缺少 id")
            aid = req.pop("id")
            if req.get("cookie"):
                got = AccountPool.parse_cookie_string(req.pop("cookie"))
                if got.get("__Secure-1PSID"):
                    req["psid"] = got["__Secure-1PSID"]
                if got.get("__Secure-1PSIDTS"):
                    req["psidts"] = got["__Secure-1PSIDTS"]
            if req.pop("reset_stats", False):
                pool.reset_stats(aid)
            # 凭据单独走 auth.save：它是唯一写入口，带清洗/校验/备份，
            # 坏值会被挡在这里 —— 工作台网页同步时尤其重要（页面里登出、
            # 或者切到另一个 Google 账号，同步回来的是空值/别人的凭据）。
            if "psid" in req or "psidts" in req:
                acc0 = pool.get(aid)
                if not acc0:
                    return self._err(404, "not_found", "账号不存在")
                new_psid = req.pop("psid", acc0.get("psid") or "")
                new_ts = req.pop("psidts", acc0.get("psidts") or "")
                if (acc0.get("psid") or "").strip() and auth.clean(new_psid) \
                        and auth.clean(new_psid) != auth.clean(acc0.get("psid")):
                    pool.log_action("login.switched",
                                    (acc0.get("email") or aid) + " 凭据被替换（网页里可能换了账号）")
                res = auth.save(aid, new_psid, new_ts, source="api:update", pool=pool,
                                email=acc0.get("email") or "")
                if not res.get("ok"):
                    return self._err(400, "bad_request", res.get("message", "凭据写入被拒绝"))
            if req:
                pool.update(aid, **req)
                pool.log_action("account.update", aid + " " + ",".join(sorted(req)))
        except Exception as e:
            return self._err(400, "bad_request", f"更新失败: {e}")
        self._json(200, {"ok": True, "accounts": pool.snapshot()})

    def accounts_delete(self):
        try:
            req = json.loads(self._body() or b"{}")
            if not req.get("id"):
                return self._err(400, "bad_request", "缺少 id")
            pool.remove(req["id"])
            pool.log_action("account.delete", req["id"])
        except Exception as e:
            return self._err(400, "bad_request", f"删除失败: {e}")
        self._json(200, {"ok": True, "accounts": pool.snapshot()})

    def accounts_test(self):
        """登录态体检：对该账号做一次引导，看 cookies 是否还有效。

        本地已经能判死的（没凭据 / 凭据是脏的）就不再发请求 —— 明知道会失败
        还去打一次 Gemini，除了污染日志没有意义，直接把结论告诉用户更省事。
        """
        try:
            req = json.loads(self._body() or b"{}")
            acc = pool.get(req.get("id") or "")
            if not acc:
                return self._err(404, "not_found", "账号不存在")
        except Exception as e:
            return self._err(400, "bad_request", f"请求解析失败: {e}")
        t0 = time.time()
        pre = auth.from_row(acc)
        if not pre["usable"]:
            return self._json(200, {
                "ok": False, "skipped": True, "state": pre["state"],
                "level": pre["level"], "error": pre["message"],
                "detail": pre["detail"], "actions": pre["actions"],
                "elapsed": 0, "accounts": pool.snapshot()})
        try:
            cli = GeminiWebClient(acc["psid"], acc["psidts"], acc["proxy"] or PROXY)
            cli.tokens(force=True)
            ok, err = True, ""
        except GeminiError as e:
            ok, err = False, f"{e.kind}: {e}"
        except Exception as e:
            ok, err = False, f"{type(e).__name__}: {e}"
        # 把实测结果记进登录态：成功清零失败计数，未登录累计到阈值才判死
        kind = ""
        if not ok and "not_logged_in" in err:
            kind = "not_logged_in"
        auth.mark_result(acc["id"], ok, kind, pool=pool, force=True)
        pool.log_action("account.test", (acc.get("email") or acc["id"])
                        + (" 通过" if ok else " 失败：" + err[:80]))
        self._json(200, {"ok": ok, "error": err, "elapsed": round(time.time() - t0, 1),
                         "state": auth.from_row(pool.get(acc["id"]) or acc)["state"],
                         "accounts": pool.snapshot()})

    # ---------- 登录态诊断 / 自愈 ----------
    def _auth_target(self, acc_id):
        acc = pool.get(acc_id or "")
        if not acc:
            return None, self._err(404, "not_found", "账号不存在")
        return acc, None

    def auth_diagnose(self):
        """GET /v1/accounts/auth?id= —— 单账号登录态诊断（不联网，秒回）。

        不给 id 就返回全池概览：谁现在拿去生图一定失败，一眼看全。
        """
        q = parse_qs(urlparse(self.path).query)
        acc_id = (q.get("id") or [""])[0]
        if not acc_id:
            return self._json(200, {
                "need_login": [a["id"] for a in auth.need_login_accounts(pool)],
                "accounts": pool.snapshot()})
        acc, err = self._auth_target(acc_id)
        if err:
            return err
        d = auth.from_row(acc)
        d.pop("psid", None)
        d.pop("psidts", None)
        snap = auth.read_store(acc_id, quarantine=False)
        d["store"] = {"has": bool(snap["psid"]), "saved_at": snap["saved_at"],
                      "from": snap["from"], "state": snap["state"],
                      "quarantined": snap["quarantined"], "error": snap["error"]}
        d["backups"] = auth.list_backups(acc_id)
        return self._json(200, {"id": acc_id, "auth": d})

    def auth_backups(self):
        """GET /v1/accounts/auth/backups?id= —— 列出可回滚的历史凭据。"""
        q = parse_qs(urlparse(self.path).query)
        acc, err = self._auth_target((q.get("id") or [""])[0])
        if err:
            return err
        return self._json(200, {"id": acc["id"], "backups": auth.list_backups(acc["id"])})

    def auth_repair(self):
        """POST {id} —— 自愈：清洗脏凭据 / 补回 PSIDTS / 从备份换一份可用的。"""
        try:
            req = json.loads(self._body() or b"{}")
        except Exception as e:
            return self._err(400, "bad_request", f"请求解析失败: {e}")
        acc, err = self._auth_target(req.get("id"))
        if err:
            return err
        res = auth.repair(acc["id"], pool=pool)
        pool.log_action("login.repair", (acc.get("email") or acc["id"]) + " → "
                        + str(res.get("action") or res.get("state") or ""))
        return self._json(200, dict(res, id=acc["id"], accounts=pool.snapshot()))

    def auth_restore(self):
        """POST {id, backup?} —— 回滚到上一次可用的凭据（不给 backup 就挑最新的可用那份）。"""
        try:
            req = json.loads(self._body() or b"{}")
        except Exception as e:
            return self._err(400, "bad_request", f"请求解析失败: {e}")
        acc, err = self._auth_target(req.get("id"))
        if err:
            return err
        res = auth.restore(acc["id"], req.get("backup"), pool=pool)
        pool.log_action("login.restore", (acc.get("email") or acc["id"]) + " → "
                        + str(res.get("restored_from") or res.get("error") or ""))
        return self._json(200, dict(res, id=acc["id"], accounts=pool.snapshot()))

    def login_new(self):
        """{email, proxy} —— 新增账号的主流方式：开窗让用户在浏览器里登录。

        成功后 cookie 自动写回并置 active；关窗/超时/失败则占位账号自动删除。
        """
        ok, err = login_camoufox.available()
        if not ok:
            return self._err(503, "no_camoufox", f"当前解释器没有 camoufox：{err}")
        try:
            req = json.loads(self._body() or b"{}")
        except Exception as e:
            return self._err(400, "bad_request", f"请求解析失败: {e}")
        r = login_camoufox.login_new_async(
            req.get("email", ""), (req.get("proxy") or "").strip(),
            timeout=int(req.get("timeout") or 600), pool=pool)
        if not r.get("ok"):
            return self._err(409, "busy", r.get("error", "已有登录窗口在运行"))
        return self._json(200, {"ok": True, "id": r["id"],
                                "status": login_camoufox.status()})

    def login_start(self):
        """{id} —— 后台开 Camoufox 窗口重登，进度用 GET /v1/login/status 轮询。"""
        ok, err = login_camoufox.available()
        if not ok:
            return self._err(503, "no_camoufox", f"当前解释器没有 camoufox：{err}")
        try:
            req = json.loads(self._body() or b"{}")
            acc = pool.get(req.get("id") or "")
            if not acc:
                return self._err(404, "not_found", "账号不存在")
        except Exception as e:
            return self._err(400, "bad_request", f"请求解析失败: {e}")
        # 与「浏览器」按钮互斥：两者共用同一个 profile，同时开会抢 Firefox 的锁
        if acc["id"] in browser_camoufox.open_windows():
            return self._err(409, "busy", "该账号的浏览器窗口还开着，请先关闭它再重新登录")
        if acc["id"] in login_camoufox.busy_profiles():
            return self._err(409, "busy", "该账号正在登录中")
        r = login_camoufox.login_async(acc["id"], acc["proxy"] or PROXY,
                                       timeout=int(req.get("timeout") or 600), pool=pool)
        if not r.get("ok"):
            pool.log_action("login.start_failed", r.get("error", "busy"))
            return self._err(409, "busy", r.get("error", "已有登录窗口在运行"))
        pool.log_action("login.start", acc["email"] or acc["id"])
        self._json(200, {"ok": True, "id": acc["id"], "status": login_camoufox.status()})

    def browser_open(self):
        """打开某账号的可见浏览器窗口（用于人工验证/查看会话）。"""
        ok, err = login_camoufox.available()
        if not ok:
            return self._err(503, "no_camoufox", f"当前解释器没有 camoufox：{err}")
        try:
            req = json.loads(self._body() or b"{}")
            acc = pool.get(req.get("id") or "")
            if not acc:
                return self._err(404, "not_found", "账号不存在")
        except Exception as e:
            return self._err(400, "bad_request", f"请求解析失败: {e}")
        # 与登录流程互斥：登录窗口开着时再开「浏览器」会抢同一个 profile 的锁
        st = login_camoufox.status()
        if st.get("running") and st.get("account") == acc["id"]:
            return self._err(409, "busy", "该账号正在登录中，请等登录结束再开窗")
        r = browser_camoufox.open_async(acc["id"], acc["proxy"] or PROXY,
                                        keep=int(req.get("keep") or 3600), pool=pool)
        if not r.get("ok"):
            return self._err(409, "busy", r.get("error", "窗口已打开"))
        pool.log_action("browser.open", acc["email"] or acc["id"])
        return self._json(200, {"ok": True, "id": acc["id"]})

    def browser_close(self):
        """{id} —— 关闭该账号的浏览器窗口，释放 profile 占用。"""
        try:
            req = json.loads(self._body() or b"{}")
            acc_id = req.get("id") or ""
        except Exception as e:
            return self._err(400, "bad_request", f"请求解析失败: {e}")
        if not acc_id:
            return self._err(400, "bad_request", "缺少 id")
        ok = browser_camoufox.request_close(acc_id)
        pool.log_action("browser.close", acc_id)
        return self._json(200, {"ok": ok, "windows": browser_camoufox.open_windows()})

    # ---------- 配置 / 日志 ----------
    def config_save(self):
        """保存 config.json，并热更新不需要重启的项。"""
        global CFG, PROXY, IMG_SIZE, THINKING, MAX_PER_ACCOUNT, MAX_ATTEMPTS, QUEUE_WAIT, SAVE_FORMAT
        global CONV_TTL_HOURS
        try:
            req = json.loads(self._body() or b"{}")
        except Exception as e:
            return self._err(400, "bad_request", f"请求解析失败: {e}")
        if not isinstance(req, dict):
            return self._err(400, "bad_request", "配置必须是对象")
        allowed = {"proxy", "api_port", "api_key", "max_attempts",
                   "max_per_account", "queue_wait", "image_size", "save_format",
                   "thinking_level", "artifacts_ttl_days", "check_interval",
                   "conversation_ttl_hours"}
        incoming = {k: v for k, v in req.items() if k in allowed}
        if not incoming:
            return self._err(400, "bad_request", "没有可保存的字段")
        # 端口是否真被改动，要和「改动前的配置值」比，不能和运行时 PORT 比：
        # 端口漂移后两者本就不等，否则用户没动端口也会被判成「需重启」。
        old_port = int(CFG.get("api_port") or 17890)
        merged = {**CFG, **incoming}
        if "api_port" in incoming:
            # 用户主动改端口 = 这就是他现在想要的端口，把「初衷」一并更新，
            # 免得界面一直提示「本想用 X」。
            merged["preferred_port"] = int(incoming["api_port"])
        try:
            (ROOT / "config.json").write_text(
                json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            return self._err(500, "io_error", f"写入失败: {e}")
        CFG = merged
        PROXY = merged.get("proxy") or ""
        IMG_SIZE = merged.get("image_size") or "s0"
        SAVE_FORMAT = (merged.get("save_format") or "original").lower()
        THINKING = (merged.get("thinking_level") or "high").lower()
        MAX_PER_ACCOUNT = int(merged.get("max_per_account") or 3)
        MAX_ATTEMPTS = int(merged.get("max_attempts") or 3)
        QUEUE_WAIT = float(merged.get("queue_wait") or 120)
        CONV_TTL_HOURS = float(merged.get("conversation_ttl_hours") or 24)
        convs.ttl = max(0.5, CONV_TTL_HOURS) * 3600
        pool.max_per_account = MAX_PER_ACCOUNT
        restart = "api_port" in incoming and int(incoming["api_port"]) != old_port
        pool.log_action("config.save", ",".join(sorted(incoming)))
        return self._json(200, {"ok": True, "config": CFG,
                                "runtime": {"port": PORT,
                                            "base_url": f"http://127.0.0.1:{PORT}"},
                                "restart_required": bool(restart)})

    # ---------- 产物定位 ----------
    def artifacts_reveal(self):
        """{name} —— 在系统文件管理器里选中该产物。"""
        try:
            req = json.loads(self._body() or b"{}")
            # 只取 basename，避免 ../ 穿越到产物目录之外
            name = pathlib.Path(req.get("name") or "").name
        except Exception as e:
            return self._err(400, "bad_request", f"请求解析失败: {e}")
        if not name:
            return self._err(400, "bad_request", "缺少 name")
        f = ART / name
        if not f.exists():
            return self._err(404, "not_found", "文件不存在，可能已被清理")
        ok = reveal_in_file_manager(f)
        return self._json(200, {"ok": ok, "path": str(f.resolve())})

    def artifacts_open_dir(self):
        """打开产物目录本身。"""
        ok = open_dir_in_file_manager(ART)
        return self._json(200, {"ok": ok, "dir": str(ART.resolve())})

    def logs_clear(self):
        """清空接口日志 / 行动日志。"""
        try:
            req = json.loads(self._body() or b"{}")
            which = req.get("which") or "all"
        except Exception:
            which = "all"
        if which not in ("all", "api", "action"):
            return self._err(400, "bad_request", "which 必须是 all|api|action")
        pool.clear_logs(which)
        pool.log_action("logs.clear", which)
        return self._json(200, {"ok": True, "which": which})

    # ---------- 业务 ----------
    def handle_generate(self):
        raw = self._body()
        ctype = self.headers.get("Content-Type", "")
        n, model = 1, ""
        req = {}
        try:
            if "multipart/form-data" in ctype:
                form = parse_multipart(ctype, raw)
                prompt = (form.get("prompt", [(None, b"")])[0][1] or b"").decode("utf-8", "ignore")
                refs = collect_refs(form_images(form))
                if form.get("model"):
                    model = (form["model"][0][1] or b"").decode("utf-8", "ignore")
                req = {k: (v[0][1] or b"").decode("utf-8", "ignore")
                       for k, v in form.items() if k in ("mode", "conversation_id", "conversation")}
            else:
                req = json.loads(raw or b"{}")
                prompt = req.get("prompt", "")
                n = req.get("n", 1)
                model = req.get("model") or ""
                refs = collect_refs(req.get("images") or req.get("image"))
        except Exception as e:
            return self._err(400, "bad_request", f"请求解析失败: {e}")

        if not prompt:
            return self._err(400, "bad_request", "prompt 不能为空")

        mode, cid = parse_conv_mode(req)
        try:
            results, info = do_generate(prompt, refs, n=n, model=model,
                                        mode=mode, cid=cid)
        except GeminiError as e:
            return self._err(err_code(e.kind), e.kind, str(e))

        host = self.headers.get("Host") or f"127.0.0.1:{PORT}"
        data = []
        for out, acc, el in results:
            name, nbytes = save_artifact(out)
            data.append({"url": f"http://{host}/artifacts/{name}",
                         "account": acc["email"], "elapsed": round(el, 1), "size": nbytes})
        self._json(200, {"created": int(time.time()), "data": data,
                         "mode": info["mode"], "conversation_id": info["conversation_id"],
                         "turn": info["turn"], "fell_back": info["fell_back"],
                         "references": info["references"],
                         "refs_dropped": info["refs_dropped"]})

    def handle_edit(self):
        raw = self._body()
        ctype = self.headers.get("Content-Type", "")
        prompt, model, refs = "", "", []
        req = {}
        try:
            if "multipart/form-data" in ctype:
                form = parse_multipart(ctype, raw)
                prompt = (form.get("prompt", [(None, b"")])[0][1] or b"").decode("utf-8", "ignore")
                refs = collect_refs(form_images(form))
                if form.get("model"):
                    model = (form["model"][0][1] or b"").decode("utf-8", "ignore")
                req = {k: (v[0][1] or b"").decode("utf-8", "ignore")
                       for k, v in form.items() if k in ("mode", "conversation_id", "conversation")}
            else:
                req = json.loads(raw or b"{}")
                prompt = req.get("prompt", "")
                model = req.get("model") or ""
                refs = collect_refs(req.get("images") or req.get("image"))
        except Exception as e:
            return self._err(400, "bad_request", f"请求解析失败: {e}")

        if not prompt:
            return self._err(400, "bad_request", "prompt 不能为空")
        if not refs:
            return self._err(400, "bad_request", "缺少 image 字段")

        mode, cid = parse_conv_mode(req)
        try:
            results, info = do_generate(prompt, refs, model=model, mode=mode, cid=cid)
        except GeminiError as e:
            return self._err(err_code(e.kind), e.kind, str(e))

        out, acc, el = results[0]
        name, nbytes = save_artifact(out)
        host = self.headers.get("Host") or f"127.0.0.1:{PORT}"
        self._json(200, {"created": int(time.time()),
                         "data": [{"url": f"http://{host}/artifacts/{name}",
                                   "account": acc["email"], "elapsed": round(el, 1),
                                   "size": nbytes}],
                         "mode": info["mode"], "conversation_id": info["conversation_id"],
                         "turn": info["turn"], "fell_back": info["fell_back"],
                         "references": info["references"],
                         "refs_dropped": info["refs_dropped"]})

    # ---------- Gemini 原生协议 ----------
    def handle_gemini_content(self, p):
        """POST /v1beta/models/{model}:generateContent（或 :streamGenerateContent）

        把 Gemini 原生请求翻译成文生图/图生图，再用 Gemini 原生的响应格式回。
        本网关只出图，所以文本请求也当成提示词去生成图。
        """
        import base64 as _b64

        tail = p.split("/v1beta/models/", 1)[1]
        mid, _, action = tail.partition(":")
        mid = unquote(mid).strip("/")
        if mid.startswith("models/"):
            mid = mid[7:]
        stream = action.startswith("streamGenerateContent")

        try:
            req = json.loads(self._body() or b"{}")
        except Exception as e:
            return self._err(400, "bad_request", f"请求解析失败: {e}")

        prompt, refs = self._gemini_parts(req)
        if not prompt:
            return self._err(400, "bad_request",
                             "缺少提示词：contents[].parts[].text")

        job = (lambda c: c.img2img(prompt, refs)) if refs else (
            lambda c: c.text2img(prompt))
        try:
            n = self._gemini_candidate_count(req)
            results = run_batch(job, n)
        except GeminiError as e:
            return self._err(err_code(e.kind), e.kind, str(e))

        host = self.headers.get("Host") or f"127.0.0.1:{PORT}"
        chunks = []
        for out, acc, el in results:
            name, nbytes = save_artifact(out)
            chunks.append({
                "candidates": [{
                    "content": {"role": "model",
                                "parts": [{"inlineData": {"mimeType": mime_of(out),
                                                          "data": _b64.b64encode(out)
                                                                  .decode("ascii")}}]},
                    "finishReason": "STOP",
                    "index": 0,
                }],
                "usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 0,
                                  "totalTokenCount": 0},
                "modelVersion": mid,
                # Gemini 客户端会忽略不认识的字段，带上便于排查
                "_gateway": {"account": acc["email"], "elapsed": round(el, 1),
                             "size": nbytes,
                             "url": f"http://{host}/artifacts/{name}"},
            })

        if stream:
            return self._sse(chunks)
        # 非流式：多张就把候选拼在一起（Gemini 的 candidateCount 语义）
        merged = chunks[0]
        if len(chunks) > 1:
            merged = {"candidates": [c["candidates"][0] for c in chunks],
                      "usageMetadata": chunks[0]["usageMetadata"],
                      "modelVersion": mid,
                      "_gateway": [c["_gateway"] for c in chunks]}
        return self._json(200, merged)

    @staticmethod
    def _gemini_candidate_count(req):
        try:
            n = int(((req.get("generationConfig") or {}).get("candidateCount")) or 1)
        except Exception:
            n = 1
        return max(1, min(n, 4))

    @staticmethod
    def _gemini_parts(req):
        """从 Gemini 请求体里取出提示词与全部内联图（最多 MAX_REF_IMAGES 张）。"""
        import base64 as _b64

        texts, refs = [], []
        contents = req.get("contents")
        if isinstance(contents, dict):
            contents = [contents]
        for c in (contents or []):
            for part in (c.get("parts") or []):
                if part.get("text"):
                    texts.append(part["text"])
                blob = part.get("inlineData") or part.get("inline_data")
                if blob:
                    try:
                        d = _b64.b64decode(blob.get("data") or "")
                        mt = blob.get("mimeType") or blob.get("mime_type") or "image/png"
                        if d:
                            refs.append((d, "ref%d.%s" % (len(refs) + 1,
                                                          mt.split("/")[-1] or "png")))
                    except Exception:
                        pass
        si = req.get("systemInstruction") or req.get("system_instruction")
        if isinstance(si, dict):
            for part in (si.get("parts") or []):
                if part.get("text"):
                    texts.insert(0, part["text"])
        return "\n".join(texts).strip(), refs[:MAX_REF_IMAGES]

    def _sse(self, chunks):
        """:streamGenerateContent?alt=sse —— 出图是一次性的，这里一次性推完。"""
        body = "".join("data: " + json.dumps(c, ensure_ascii=False) + "\n\n"
                       for c in chunks).encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def cleanup_artifacts(days=7):
    """启动时清理过期产物，避免无限增长。"""
    import time as _t
    cut = _t.time() - days * 86400
    n = 0
    for f in ART.glob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cut:
                f.unlink(); n += 1
        except Exception:
            pass
    return n


def watchdog(interval):
    """登录态守护：定时对空闲账号做一次轻量引导，cookie 被吊销就标记待重登。

    Google 吊销会话是静默的——只有下次生成时才报错。这个线程把发现时机
    从「用户等 30s 后失败」提前到「后台每半小时巡检」，无人值守才成立。
    """
    while True:
        time.sleep(interval)
        for a in pool.all():
            if a["status"] != "active" or a["cooldown_till"] > time.time():
                continue
            busy = pool.inflight().get(a["id"], 0) > 0
            if busy:
                continue
            # 本地已经能判死的（没凭据 / 脏凭据 / 已吊销）不必再打一次网络
            if not auth.usable(a):
                continue
            try:
                GeminiWebClient(a["psid"], a["psidts"], a["proxy"] or PROXY).tokens(force=True)
                auth.mark_result(a["id"], True, pool=pool)
            except GeminiError as e:
                if e.kind == "not_logged_in":
                    # 不 force：单次失败可能只是代理绕路/网络抖动，连续 REVOKE_AFTER
                    # 次才真的判死 —— 误杀的代价（好账号被停用、要人重登一次）
                    # 比晚半小时发现高得多。
                    r = auth.mark_result(a["id"], False, "not_logged_in", pool=pool)
                    if r.get("state") == "revoked":
                        pool.log_action("login.revoked", a["email"] or a["id"])
                        print(f"[watchdog] {a['email']} 登录态已失效，标记为待重登")
            except Exception:
                pass


class AlreadyRunning(Exception):
    """端口上已经有一个本项目的网关在跑 —— 这是「别重复启动」，不是端口冲突。"""


def port_busy(port, timeout=0.6):
    """这个端口上有没有人在听。

    用 connect 判断而不是 bind —— Windows 上各家都开 SO_REUSEADDR，两个进程
    绑同一个端口可能都「成功」（实测过：第二个实例照样绑上了 17890，然后
    两个网关抢同一份 traffic）。所以先连一下才靠谱。
    """
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def _read_headers(sock, limit=65536):
    buf = b""
    while len(buf) < limit:
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if b"\r\n\r\n" in buf:
            break
    return buf


def probe_self(port, timeout=1.2):
    """端口上跑的是不是自家网关。

    刻意不用 urllib/requests：它们会读系统代理环境变量，把这种本机探测劫持到
    代理上再返回 502。这里直接发一个最原始的 HTTP 请求问 /health。
    网关是 HTTP/1.1 keep-alive，读完必须按 Content-Length 收手，
    否则会一直等到 socket 超时才返回，那会误判成「不是自家网关」。
    """
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                  b"Connection: close\r\n\r\n")
        head = _read_headers(s)
        i = head.find(b"\r\n\r\n")
        if i < 0:
            s.close()
            return False
        need = 0
        for line in head[:i].split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                try:
                    need = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    need = 0
                break
        body = head[i + 4:]
        while len(body) < need:
            try:
                chunk = s.recv(max(4096, need - len(body)))
            except OSError:
                break
            if not chunk:
                break
            body += chunk
        try:
            s.close()
        except OSError:
            pass
    except OSError:
        return False
    try:
        return json.loads(body.decode("utf-8", "ignore")).get("service") == SERVICE
    except Exception:
        return False


def start_http_server(preferred):
    """监听 preferred；被别的程序占着就自动顺延到下一个空端口。

    两种「占着」要分开处理：
      · 上面跑的是自家网关 → 已经有一个实例了，不该再开第二个（否则会出现两个
        网关各写一份 gateway.json，外壳接管的 pid 到底是谁就说不清了）
      · 上面跑的是别的程序 → 这才是冲突，换个端口继续，不打扰用户
    """
    last = None
    for n in range(PORT_FALLBACK_TRIES):
        port = preferred + n
        # 先连一下确认没人占，再 bind。顺序不能反 —— 见 port_busy 的注释。
        if port_busy(port):
            if probe_self(port):
                raise AlreadyRunning(port)
            last = f"{port} 已被其他程序占用"
            print(f"[v3] 端口 {port} 被其他程序占用，自动改用 {port + 1}", file=sys.stderr)
            continue
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), Handler), port
        except OSError as e:
            last = e
            # 极少见：探测时空着，bind 时被抢了，继续往后试
            if not (getattr(e, "errno", None) in (10048, 98, 13)
                    or "in use" in str(e).lower() or "denied" in str(e).lower()):
                raise
            print(f"[v3] 端口 {port} 刚被抢占，自动改用 {port + 1}", file=sys.stderr)
    raise RuntimeError(f"从 {preferred} 起连续 {PORT_FALLBACK_TRIES} 个端口都不可用（最后: {last}）")


if __name__ == "__main__":
    removed = cleanup_artifacts(int(CFG.get("artifacts_ttl_days") or 7))
    if removed:
        print(f"[v3] 已清理 {removed} 个过期产物")
    try:
        httpd, PORT = start_http_server(PORT)
    except AlreadyRunning as e:
        # 已经有一个网关在服务了，这次启动没有意义 —— 当作成功退出，别吓唬用户
        print(f"[v3] 端口 {e.args[0]} 上已有一个本项目的网关在运行，不再重复启动。", file=sys.stderr)
        sys.exit(0)
    except Exception as e:
        print(f"[v3] 无法启动: {e}", file=sys.stderr)
        sys.exit(1)
    # 端口粘性：顺延后把真实端口固化回 config.json。
    # 不固化的话每次启动都可能落在不同端口上（这次 17891、下次 17890），外部客户端
    # 配一次就失效一次 —— 那比端口冲突本身更烦人。固化后端口只在首次冲突时变一次。
    pref = preferred_port()
    if int(CFG.get("api_port") or 17890) != PORT or CFG.get("preferred_port") != pref:
        CFG["preferred_port"] = pref
        CFG["api_port"] = PORT
        try:
            (ROOT / "config.json").write_text(
                json.dumps(CFG, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[v3] 端口已固定为 {PORT}（本想用 {pref}），已写入 config.json。",
                  file=sys.stderr)
        except Exception as e:
            print(f"[v3] 端口 {PORT} 未能写回 config.json（下次启动可能再漂）: {e}",
                  file=sys.stderr)
    # 身份文件与 /health 都写真实端口，外壳和外部客户端才能接上（漂移后它是唯一权威）
    purge_stale_discovery()
    write_gateway_file()
    atexit.register(remove_gateway_file)
    print(f"[v3] 账号数 = {len(pool.all())}   代理 = {PROXY or '(直连)'}   图片尺寸 = {IMG_SIZE}")
    print(f"[v3] 监听 http://127.0.0.1:{PORT}  API Key = {API_KEY}")
    if PORT != pref:
        print(f"[v3] 注意：{pref} 被别的程序占用，本次改用 {PORT}。外部客户端请填 "
              f"http://127.0.0.1:{PORT}（或读 {discovery_path()}）。", file=sys.stderr)
    wi = float(CFG.get("check_interval") or 0)
    if wi > 0:
        threading.Thread(target=watchdog, args=(wi,), daemon=True).start()
        print(f"[v3] 登录态巡检已开启，间隔 {int(wi)}s")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[v3] 已停止")
