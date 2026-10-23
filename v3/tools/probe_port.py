"""探测本项目的网关端口，可选直接打开控制台 —— 给 .bat 启动脚本用。

为什么不用 curl：
  1. 系统里若有 HTTP_PROXY（本机就有），curl 会走代理；对 127.0.0.1 的
     --noproxy 在 cmd.exe 下并不可靠，实测会把活着的网关判成"没起"。
  2. bat 里对探测结果做字符串解析（for /f + 引号）极易踩坑，干脆全放进 Python。

为什么由这里开浏览器：
  bat 里 `start "" "url"` 的引号规则和 for /f 的命令解析会互相干扰。让 Python
  调 webbrowser.open 之后，bat 只需要看退出码（0 = 网关在跑并且已开好网页）。

用法:
  python tools/probe_port.py          只探测，命中打印端口
  python tools/probe_port.py --open   探测到就打开 http://127.0.0.1:<port>/ui

退出码: 0 命中 / 1 没找到。
"""

import json
import pathlib
import socket
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVICE = "gemini-pool-v3"
DEFAULT_PORT = 17890
SPAN = 8               # 与 server.py 的端口顺延范围保持大致一致


def candidates():
    """先信 gateway.json 的端口（漂移后它是唯一权威），再按 config 往下铺。"""
    ports = []
    try:
        gw = json.loads((ROOT / "gateway.json").read_text(encoding="utf-8"))
        if gw.get("service") == SERVICE and gw.get("port"):
            ports.append(int(gw["port"]))
    except Exception:
        pass
    base = DEFAULT_PORT
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        base = int(cfg.get("api_port") or base)
    except Exception:
        pass
    for d in range(SPAN):
        p = base + d
        if p not in ports:
            ports.append(p)
    return ports


def probe(port, timeout=1.2):
    """纯 socket 发一句 GET /health，确认这是本项目的网关。

    绕开所有代理环境变量；Connection: close 让服务端主动断开，避免 keep-alive
    下 recv 一直等到超时。
    """
    s = socket.socket()
    s.settimeout(timeout)
    try:
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return False
        s.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                  b"Connection: close\r\n\r\n")
        buf = b""
        while len(buf) < 65536:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        head, _, body = buf.decode("utf-8", "replace").partition("\r\n\r\n")
        if " 200 " not in head.split("\r\n")[0]:
            return False
        return SERVICE in body
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def main(argv):
    want_open = "--open" in argv
    for p in candidates():
        if not probe(p):
            continue
        url = "http://127.0.0.1:%d/ui" % p
        print("[ok] gateway is up on port %d" % p)
        if want_open:
            print("     opening %s" % url)
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception as e:
                print("[!!] could not open the browser: %s" % e)
                print("     open it manually: %s" % url)
        else:
            print(p)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
