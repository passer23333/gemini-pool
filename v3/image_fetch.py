# -*- coding: utf-8 -*-
"""图片 URL 规范化与最佳画质下载。

策略来源：voyager-crew/voyager 的 public/fetchInterceptor.js（MAIN world 取图拦截器），
它拦截 Gemini 原生下载请求，把 URL 的尺寸 token 改写成原图再取。

以下三条规则直接沿用：
  1. 尺寸 token 形如 `=sNNN` / `=wNNN` / `=hNNN`，`=s0` 表示原图。
     替换时只动尺寸数字，保留尾随 flag（`=s512-d-I` → `=s0-d-I`）。
  2. 已经是 `=s0` 的不要再改；以 `=d` / `=d-I` 结尾的本身就是原尺寸，也不能追加，
     否则会拼出 `...=d-I?alr=yes-s0` 这种非法 URL。
  3. Gemini 的下载链可能是多跳的：
        /gg-dl/...=d-I  →  文本体（~450B，内容是下一个 URL）
        /rd-gg-dl/...=s0-d-I  →  文本体
        /rd-gg-dl/...=s0-d-I  →  真正的图片字节
     所以拿到非图片的短文本时要把它当成下一跳地址继续跟随，不能直接判失败。

本项目实测补充（voyager 未写明）：
  - `=d-I` 与 `=s0-d-I` 在 1408×768 下体积约为 `=s0` 的 4 倍（778KB vs 164KB），
    即 `-I` 才是低压缩的原始资产。故优先尝试 `-d-I`，失败再退回 `=s0`。

原文混着缩略图，最终按「像素总数优先、字节数次之」择优。
"""
import io
import re

IMAGE_HOSTS = r"(?:[A-Za-z0-9-]+\.)*googleusercontent\.com|[A-Za-z0-9-]+\.ggpht\.com"
IMAGE_PATHS = r"(?:rd-gg-dl|rd-gg|gg-dl)"

# 首选：限定 Gemini 图片路径，避免抓到头像、图标等无关资源
_URL_RE_STRICT = re.compile(
    r"https://(?:%s)/(?:%s)/[^\s\"'\\\)\]]+" % (IMAGE_HOSTS, IMAGE_PATHS), re.I)
# 兜底：路径变了也不至于一张都取不到
_URL_RE_LOOSE = re.compile(
    r"https://[A-Za-z0-9.-]+\.(?:googleusercontent|ggpht)\.com/[^\s\"'\\\)\]]+", re.I)

_SIZE_TOKEN = re.compile(r"=[swh]\d+")
_ORIGINAL = re.compile(r"=s0(?:-[A-Za-z0-9-]+)?(?=[?#]|$)")
_DOWNLOAD_PARAM = re.compile(r"=d(?:-[A-Za-z0-9]+)?(?=[?#]|$)")

MAX_HOPS = 3
MAX_TEXT_HOP_BYTES = 4096


def extract_urls(text):
    """从 StreamGenerate 响应里提取图片候选地址（去重、保序）。"""
    urls = _URL_RE_STRICT.findall(text) or []
    if not urls:
        urls = _URL_RE_LOOSE.findall(text)
    seen, out = set(), []
    for u in urls:
        u = u.rstrip(",;")
        if u not in seen and len(u) > 60:
            seen.add(u)
            out.append(u)
    return out


def _split_query(url):
    base, sep, query = url.partition("?")
    return base, sep, query


def original_url(url):
    """改写为原图尺寸（等价于 voyager 的 replaceWithOriginalSize）。"""
    if _ORIGINAL.search(url) or _DOWNLOAD_PARAM.search(url):
        return url
    base, sep, query = _split_query(url)
    m = _SIZE_TOKEN.search(base)
    if m:
        base = base[:m.start()] + "=s0" + base[m.end():]
    else:
        # 补在路径上，不能补到 query 后面
        base = base + "=s0"
    return base + sep + query


def max_quality_url(url):
    """在 original_url 基础上加 `-I`（低压缩原始资产）。"""
    u = original_url(url)
    if re.search(r"=s0-[A-Za-z0-9-]+(?=[?#]|$)", u):
        return u
    if _DOWNLOAD_PARAM.search(u) and not re.search(r"=d-I(?=[?#]|$)", u):
        return re.sub(r"=d(?=[?#]|$)", "=d-I", u)
    base, sep, query = _split_query(u)
    if _SIZE_TOKEN.search(base) or "=s0" in base:
        return base + "-d-I" + sep + query
    return u


def is_image(data):
    return (data[:3] == b"\xff\xd8\xff"
            or data[:8] == b"\x89PNG\r\n\x1a\n"
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
            or data[:6] in (b"GIF87a", b"GIF89a"))


def pixels(data):
    """读像素总数；失败返回 0。PIL 不存在时退化为 0。"""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return im.size[0] * im.size[1]
    except Exception:
        return 0


def looks_like_next_hop(data):
    """短文本且内容是 http(s) URL —— 说明这是下载链的中间跳。"""
    if len(data) > MAX_TEXT_HOP_BYTES:
        return None
    try:
        t = data.decode("utf-8", "ignore").strip()
    except Exception:
        return None
    m = re.match(r"^(https://[^\s\"']+)$", t)
    return m.group(1) if m else None


def rank_key(data):
    """择优排序：像素优先，字节次之（分辨率相同才比体积=画质）。"""
    return (pixels(data), len(data))
