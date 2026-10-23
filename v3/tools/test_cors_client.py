"""模拟 Infinite Canvas 的渲染进程：从另一个 origin 的页面里用 fetch 拉模型。

它是 Electron + EBWebView，拉模型的代码跑在 Chromium 渲染进程里
（同一 bundle 里出现了 window.location.origin），所以浏览器同源策略完全生效：
没有 CORS 头 / 不处理 OPTIONS 预检，请求会在浏览器层被吞掉，服务端毫无痕迹。

这个脚本同时验证两件事：
  1. 跨域是否通过（拿到 200 + 数据）
  2. 返回的数据过不过得了它自己的过滤条件（supportedGenerationMethods 等）
"""
import io
import json
import pathlib
import socketserver
import tempfile
import threading
import http.server

PAGE_PORT = 5199
BASE = "http://127.0.0.1:17890"
KEY = "sk-geminipool"

JS = r"""
async () => {
  try {
    let r = [], token = "";
    do {
      const u = new URL("%s/v1beta/models");
      if (token) u.searchParams.set("pageToken", token);
      const resp = await fetch(u, { headers: { "x-goog-api-key": "%s" } });
      if (!resp.ok) {
        let body = null;
        try { body = await resp.json(); } catch (e) { body = "(非 JSON)"; }
        return { ok: false, status: resp.status, body };
      }
      const j = await resp.json();
      for (const e of (j.models || [])) {
        const t = (e.name || "").replace(/^models\//, "") || "";
        const a = e.supportedGenerationMethods || [];
        if (t && !/embed|embedding/i.test(t) &&
            (a.includes("generateContent") || a.includes("predictLongRunning") ||
             /^(veo-|imagen-)/i.test(t))) r.push(t);
      }
      token = j.nextPageToken || "";
    } while (token);
    return { ok: true, filterCount: r.length, models: Array.from(new Set(r)).sort() };
  } catch (err) {
    // 浏览器层面的失败（CORS / 网络）会在这里抛出 TypeError: Failed to fetch
    return { error: String(err) };
  }
}
""" % (BASE, KEY)

tmp = pathlib.Path(tempfile.mkdtemp())
(tmp / "index.html").write_text(
    "<meta charset='utf-8'><title>origin-probe</title><body>probe</body>",
    encoding="utf-8")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(tmp), **kw)

    def log_message(self, *a):
        pass


srv = socketserver.TCPServer(("127.0.0.1", PAGE_PORT), Quiet)
threading.Thread(target=srv.serve_forever, daemon=True).start()

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page()
        pg.goto(f"http://127.0.0.1:{PAGE_PORT}/index.html", timeout=30000)
        res = pg.evaluate(JS)
        print("页面 origin : http://127.0.0.1:%d （与网关不同源，会触发 CORS 检查）" % PAGE_PORT)
        print("网关        : %s" % BASE)
        print("-" * 60)
        print(json.dumps(res, ensure_ascii=False, indent=2)[:1500])
        b.close()
finally:
    srv.shutdown()
