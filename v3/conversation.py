# -*- coding: utf-8 -*-
"""生图会话（conversation）存档。

协议层直调每次都是「开新对话」：请求载荷里的会话三元组全是空串。网页里那种
「接着上一张图改」的效果因此做不到 —— 用户说「把背景换成蓝色」，模型没有
上一轮的上下文，只能凭空再画一张。

要续接就得记住 Google 返回的 c_/r_/rc_ 三个 id（见 gemini_web.parse_conversation），
下一轮请求带上它们。这三个 id 是**账号私有**的：A 账号的会话不能被 B 账号接，
所以存档里每个会话都记着归属账号，续接时只能派给同一个账号。

为什么落盘：网关重启后内存里的会话就没了，用户点了「继续」却拿到一张全新的图，
是最容易让人困惑的失败。写进 JSON 后重启也能接着聊。

会话是服务端状态，天然会过期（Google 侧回收、账号重登、TTL 到期都算）。
过期不可怕 —— gemini_web 里续接失败会自动退回新对话重发一次，用户不会卡住。
"""
import json
import os
import pathlib
import threading
import time

DEFAULT_TTL_HOURS = 24


def _atomic_write(path, text):
    """tmp + fsync + replace：写一半崩了也不会留下半个 JSON。"""
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class ConversationStore:
    def __init__(self, path, ttl_hours=DEFAULT_TTL_HOURS):
        self.path = pathlib.Path(path)
        self.ttl = max(0.5, float(ttl_hours or DEFAULT_TTL_HOURS)) * 3600
        self._lock = threading.Lock()
        self._data = {}
        self._load()

    # ---------------- 存取 ----------------
    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("conversations"), dict):
                self._data = raw["conversations"]
        except FileNotFoundError:
            self._data = {}
        except Exception:
            # 坏档不值得让网关起不来：丢掉重来，反正会话丢了最多是开个新对话
            self._data = {}

    def _flush(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.path, json.dumps(
                {"updated": int(time.time()), "conversations": self._data},
                ensure_ascii=False, indent=1))
        except Exception:
            pass

    def save(self, conv, account, prompt=""):
        """生图成功后记账。conv 为 gemini_web 返回的 {c,r,rc}。"""
        cid = (conv or {}).get("c")
        if not cid:
            return None
        with self._lock:
            old = self._data.get(cid) or {}
            rec = {"c": cid, "r": conv.get("r") or "", "rc": conv.get("rc") or "",
                   "account": account.get("id") or old.get("account") or "",
                   "email": account.get("email") or old.get("email") or "",
                   "turn": int(old.get("turn") or 0) + 1,
                   "updated": time.time(),
                   "last_prompt": (prompt or "")[:200]}
            self._data[cid] = rec
            self._flush()
            return dict(rec)

    def get(self, cid):
        with self._lock:
            rec = self._data.get(cid)
            return dict(rec) if rec else None

    def latest(self, account_id=None):
        """最近一个会话（可按账号过滤）。过期的直接跳过。"""
        with self._lock:
            now = time.time()
            cands = [r for r in self._data.values()
                     if now - (r.get("updated") or 0) < self.ttl
                     and (not account_id or r.get("account") == account_id)]
            if not cands:
                return None
            return dict(max(cands, key=lambda r: r.get("updated") or 0))

    def list(self, active_only=True):
        with self._lock:
            now = time.time()
            out = [dict(r) for r in self._data.values()
                   if not active_only or now - (r.get("updated") or 0) < self.ttl]
        return sorted(out, key=lambda r: r.get("updated") or 0, reverse=True)

    def delete(self, cid):
        with self._lock:
            existed = self._data.pop(cid, None) is not None
            if existed:
                self._flush()
            return existed

    def clear(self, account_id=None):
        with self._lock:
            if account_id:
                for cid in [k for k, v in self._data.items()
                            if v.get("account") == account_id]:
                    self._data.pop(cid, None)
            else:
                self._data = {}
            self._flush()

    def prune(self):
        """清掉过期会话，避免文件无限增长。"""
        with self._lock:
            now = time.time()
            dead = [k for k, v in self._data.items()
                    if now - (v.get("updated") or 0) >= self.ttl]
            for k in dead:
                self._data.pop(k, None)
            if dead:
                self._flush()
            return len(dead)
