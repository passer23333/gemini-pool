// Gemini 账号池 v3 — 内嵌 Gemini 工作台（WebContentsView）
//
// 为什么要有它：以前要摆弄某个账号的会话（过人机验证、确认还能不能用、手动出一张图），
// 只能让网关去开一个 Camoufox 火狐窗口 —— 那是一个独立的操作系统窗口，弹出来会抢焦点、
// 还要单独关，体验很差。这里改成把网页直接嵌进主窗口：
//
//   · 每个账号一个 persist:gemini-<id> 分区 —— 登录状态写盘，下次打开还是同一个人
//   · 视图叠在页面之上，位置由一个空的 stage div 同步 —— 看起来就是界面的一部分
//   · 所有新窗口请求都被拦回同一个视图 —— 不会再有任何弹窗跑出来
//   · 分区里的 Cookie 变化会回写账号池 —— 网页上续期了，后台出图立刻跟着生效
//
// 后台出图依旧走 Python 网关的纯 HTTP 协议层，不经过这里。这里只是给人用的那块屏。
const { WebContentsView, session } = require('electron');
const http = require('http');

const GEMINI_URL = 'https://gemini.google.com/app';
// 只需这两个就能让网关的协议层出图；浏览器里其余 Cookie 由 Google 自己补。
const AUTH_COOKIES = ['__Secure-1PSID', '__Secure-1PSIDTS'];
// 放行的权限：不让 'notifications'/'media' 之类把系统弹窗带出来，
// 但剪贴板要给，否则 Gemini 输入框粘贴 prompt 会失败。
const PERMISSION_ALLOW = new Set(['clipboard-read', 'clipboard-write', 'clipboard-sanitized-write']);

function liveContentsOf(view) {
  if (!view) return null;
  const c = view.webContents;
  if (!c) return null;
  try { return c.isDestroyed() ? null : c; } catch { return null; }
}
function liveContents(tab) { return liveContentsOf(tab && tab.view); }

function normalizeBounds(b) {
  return {
    x: Math.max(0, Math.round(b.x)),
    y: Math.max(0, Math.round(b.y)),
    width: Math.max(0, Math.round(b.width)),
    height: Math.max(0, Math.round(b.height)),
  };
}

/** 给网关发一个 JSON 请求，用于把会话回写进账号池。失败一律静默，不能影响界面。 */
function postJson(port, apiKey, path, body) {
  return new Promise(resolve => {
    try {
      const payload = JSON.stringify(body || {});
      const req = http.request({
        host: '127.0.0.1', port, path, method: 'POST', timeout: 5000,
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(payload),
          ...(apiKey ? { Authorization: 'Bearer ' + apiKey } : {}),
        },
      }, res => {
        let raw = '';
        res.on('data', c => { raw += c; });
        res.on('end', () => {
          let j = null;
          try { j = JSON.parse(raw); } catch { /* 非 JSON 响应不关心 */ }
          resolve({ ok: res.statusCode === 200, data: j });
        });
      });
      req.on('error', () => resolve({ ok: false }));
      req.on('timeout', () => { req.destroy(); resolve({ ok: false }); });
      req.write(payload);
      req.end();
    } catch (e) {
      resolve({ ok: false });
    }
  });
}

class EmbeddedBrowser {
  /**
   * @param {BrowserWindow} window 主窗口
   * @param {object} opts { port, apiKey, onChange(snapshot), onLog(line) }
   */
  constructor(window, opts) {
    this.window = window;
    this.opts = opts || {};
    this.tabs = new Map();
    this.activeAccountId = null;
    this.bounds = { x: 0, y: 0, width: 0, height: 0 };
    this.pageVisible = false;
  }

  snapshot() {
    return { tabs: this.list(), activeAccountId: this.activeAccountId };
  }

  list() {
    this.pruneDestroyed();
    return [...this.tabs.values()].map(t => ({ ...t.state }));
  }

  partitionOf(accountId) { return `persist:gemini-${accountId}`; }

  /**
   * 打开（或唤回）某账号的工作台。
   * @param {object} account { id, email, psid, psidts, proxy }
   */
  async open(account) {
    if (!account || !account.id) throw new Error('缺少账号信息');
    this.pruneDestroyed();

    const existing = this.tabs.get(account.id);
    if (existing) {
      const c = liveContents(existing);
      if (!c) await this.resume(account.id);
      this.markActive(account.id);
      this.applyLayoutToAll();
      this.emit();
      return this.snapshot();
    }

    const id = account.id;
    const partition = this.partitionOf(id);
    const ses = session.fromPartition(partition);
    await this.applySessionPolicy(ses, account.proxy);
    const injected = await this.ensureAuthCookies(ses, account);

    const view = this.createView(partition);
    const tab = {
      accountId: id, view, lastActiveAt: Date.now(),
      state: {
        accountId: id, accountName: account.email || id,
        title: account.email || 'Gemini', url: GEMINI_URL,
        loading: true, canGoBack: false, canGoForward: false,
        // 注入成功通常意味着能直接进；登录态最终以页面实际结果为准
        injected, error: null, syncedAt: 0, syncing: false,
      },
    };
    this.wire(tab);
    this.tabs.set(id, tab);

    try { this.window.contentView.addChildView(view); }
    catch (e) {
      this.tabs.delete(id);
      this.detachView(view);
      this.activeAccountId = null;
      throw new Error('主窗口已关闭');
    }
    this.activeAccountId = id;
    this.applyLayoutToAll();
    void view.webContents.loadURL(GEMINI_URL).catch(() => {});
    void this.startCookieWatch(id, ses);
    this.emit();
    return this.snapshot();
  }

  async resume(accountId) {
    const tab = this.tabs.get(accountId);
    if (!tab) return this.snapshot();
    if (liveContents(tab)) { this.markActive(accountId); this.applyLayoutToAll(); this.emit(); return this.snapshot(); }

    const view = this.createView(this.partitionOf(accountId));
    tab.view = view;
    tab.state.loading = true;
    tab.state.error = null;
    this.wire(tab);
    try { this.window.contentView.addChildView(view); }
    catch (e) {
      tab.state.loading = false;
      this.detachView(view);
      throw new Error('主窗口已关闭');
    }
    this.markActive(accountId);
    this.applyLayoutToAll();
    void view.webContents.loadURL(tab.state.url || GEMINI_URL).catch(() => {});
    this.emit();
    return this.snapshot();
  }

  close(accountId) {
    const tab = this.tabs.get(accountId);
    if (!tab) return this.snapshot();
    this.stopCookieWatch(accountId);
    this.tabs.delete(accountId);
    this.detach(tab);
    if (this.activeAccountId === accountId) {
      const rest = [...this.tabs.keys()];
      this.activeAccountId = rest.length ? rest[rest.length - 1] : null;
    }
    this.applyLayoutToAll();
    this.emit();
    return this.snapshot();
  }

  activate(accountId) {
    this.pruneDestroyed();
    const tab = this.tabs.get(accountId);
    if (!tab) return this.snapshot();
    if (!liveContents(tab)) return this.resume(accountId);
    this.markActive(accountId);
    this.applyLayoutToAll();
    this.emit();
    return this.snapshot();
  }

  setLayout(visible, bounds) {
    if (bounds && bounds.width > 0 && bounds.height > 0) this.bounds = normalizeBounds(bounds);
    this.pageVisible = !!visible && this.bounds.width > 0 && this.bounds.height > 0;
    this.applyLayoutToAll();
  }

  reload(accountId) {
    const tab = this.tabs.get(accountId || this.activeAccountId);
    const c = liveContents(tab);
    if (!c) return;
    tab.state.loading = true;
    tab.state.error = null;
    c.reload();
    this.emit();
  }

  navigate(dir) {
    const tab = this.tabs.get(this.activeAccountId);
    const h = liveContents(tab) && liveContents(tab).navigationHistory;
    if (!h) return;
    if (dir === 'back' && h.canGoBack()) h.goBack();
    if (dir === 'forward' && h.canGoForward()) h.goForward();
  }

  navigateTo(url) {
    const tab = this.tabs.get(this.activeAccountId);
    const c = liveContents(tab);
    if (!c) return;
    let target = String(url || '').trim();
    if (!target) return;
    if (!/^https?:\/\//i.test(target)) target = 'https://' + target;
    tab.state.loading = true;
    tab.state.error = null;
    void c.loadURL(target).catch(() => {});
    this.emit();
  }

  destroyAll() {
    for (const id of [...this.tabs.keys()]) { this.stopCookieWatch(id); this.detach(this.tabs.get(id)); }
    this.tabs.clear();
    this.activeAccountId = null;
    this.pageVisible = false;
  }

  // ---------- 会话 ----------

  createView(partition) {
    const view = new WebContentsView({
      webPreferences: {
        partition,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        backgroundThrottling: false,   // 页面在后台时也要能跑完生成轮询
        spellcheck: false,
      },
    });
    view.setBackgroundColor('#ffffff');
    return view;
  }

  async applySessionPolicy(ses, proxy) {
    try {
      if (proxy) await ses.setProxy({ proxyRules: proxy });
      else await ses.setProxy({});                       // 跟随系统
    } catch (e) { this.log('setProxy 失败: ' + e.message); }
    try {
      ses.setPermissionRequestHandler((_wc, permission, cb) => cb(PERMISSION_ALLOW.has(permission)));
    } catch (e) { /* 部分平台不支持设置，忽略 */ }
  }

  /**
   * 清掉分区里的两个登录 Cookie。
   *
   * 什么时候必须清：账号池里那份凭据已经被判「不可用」（吊销 / 脏数据），
   * 而分区里还留着同样这份 —— 带着它打开网页，Google 会一直把它当成
   * 「已登录但会话无效」，用户看到的就是「怎么都登不上」。
   * 清干净再进，反而能正常走登录流程，登录成功后自动同步回账号池。
   */
  async wipeAuthCookies(ses) {
    let n = 0;
    for (const name of AUTH_COOKIES) {
      try {
        await ses.cookies.remove(GEMINI_URL, name);
        n++;
      } catch (e) { /* 本来就没有 */ }
    }
    try { await ses.cookies.flushStore(); } catch (e) { /* 分区可能已释放 */ }
    if (n) this.log('已清除分区里的失效登录 Cookie（' + n + ' 个），请在网页里重新登录');
    return n;
  }

  async ensureAuthCookies(ses, account) {
    // 网关那边的两个 credential 就是可用与否的全部依据；分区里已经有一致的就不动，
    // 不一致才覆盖 —— 避免每次打开都把用户在网页里刚续期的值冲掉。
    if (!account || !account.psid) return false;
    // 凭据已经被判不可用（revoked / malformed）：带着它只会让网页卡在无效会话里，
    // 先清掉，让用户可以干净地重新登录。
    if (account.auth && account.auth.usable === false) {
      await this.wipeAuthCookies(ses);
      return false;
    }
    let current = [];
    try { current = await ses.cookies.get({ url: GEMINI_URL }); } catch (e) { return false; }
    const get = n => { const c = current.find(x => x.name === n); return c ? c.value : ''; };
    const samePsid = get('__Secure-1PSID') === account.psid;
    const sameTs = !account.psidts || get('__Secure-1PSIDTS') === account.psidts;
    if (samePsid && sameTs) return true;

    const base = { url: GEMINI_URL, domain: '.google.com', path: '/', secure: true,
                   httpOnly: true, sameSite: 'no_restriction',
                   expirationDate: Math.floor(Date.now() / 1000) + 365 * 86400 };
    try {
      await ses.cookies.set({ ...base, name: '__Secure-1PSID', value: account.psid });
      if (account.psidts) {
        await ses.cookies.set({ ...base, name: '__Secure-1PSIDTS', value: account.psidts });
      }
      await ses.cookies.flushStore();
      return true;
    } catch (e) {
      this.log('注入 Cookie 失败: ' + e.message);
      return false;
    }
  }

  async readAuthCookies(accountId) {
    try {
      const list = await session.fromPartition(this.partitionOf(accountId)).cookies.get({ url: GEMINI_URL });
      const get = n => { const c = list.find(x => x.name === n); return c ? c.value : ''; };
      return { psid: get('__Secure-1PSID'), psidts: get('__Secure-1PSIDTS') };
    } catch (e) {
      return { psid: '', psidts: '' };
    }
  }

  /** 网页会话里 Google 续期了 PSIDTS —— 自动写回账号池，后台出图就能继续用。 */
  startCookieWatch(accountId, ses) {
    if (this.watchers && this.watchers.has(accountId)) return;
    (this.watchers ||= new Map());
    const entry = { ses, listener: null, timer: null };
    entry.listener = (_e, cookie) => {
      if (!cookie || !AUTH_COOKIES.includes(cookie.name)) return;
      if (entry.timer) clearTimeout(entry.timer);
      entry.timer = setTimeout(() => { entry.timer = null; void this.syncCookies(accountId); }, 1200);
    };
    ses.cookies.on('changed', entry.listener);
    this.watchers.set(accountId, entry);
  }

  stopCookieWatch(accountId) {
    const w = this.watchers && this.watchers.get(accountId);
    if (!w) return;
    try { w.ses.cookies.removeListener('changed', w.listener); } catch (e) { /* 已释放 */ }
    if (w.timer) clearTimeout(w.timer);
    this.watchers.delete(accountId);
  }

  async syncCookies(accountId) {
    const tab = this.tabs.get(accountId || this.activeAccountId);
    if (!tab) return { ok: false, error: '标签已关闭' };
    const id = tab.accountId;
    tab.state.syncing = true;
    this.emit();
    const creds = await this.readAuthCookies(id);
    let ok = false, error = '';
    if (!creds.psid) {
      error = '分区里没有登录 Cookie，先在网页里登录一次';
    } else {
      const r = await postJson(this.opts.port, this.opts.apiKey, '/v1/accounts/update',
                               { id, psid: creds.psid, psidts: creds.psidts });
      ok = r.ok;
      error = r.ok ? '' : ((r.data && r.data.error && r.data.error.message) || '写回失败');
      if (ok) {
        // 拿到有效 Cookie 就意味着这个账号可以用了：占位账号（新建会话）在这里转正
        await postJson(this.opts.port, this.opts.apiKey, '/v1/accounts/update',
                       { id, status: 'active' });
        this.log(`已同步 ${id} 的登录态回账号池`);
      }
    }
    tab.state.syncing = false;
    tab.state.syncedAt = ok ? Date.now() : tab.state.syncedAt;
    this.emit();
    return { ok, error, psid: creds.psid ? creds.psid.slice(0, 8) + '…' : '' };
  }

  // ---------- 内部 ----------

  markActive(accountId) {
    this.activeAccountId = accountId;
    const t = this.tabs.get(accountId);
    if (t) t.lastActiveAt = Date.now();
  }

  wire(tab) {
    const view = tab.view;
    const c = liveContentsOf(view);
    if (!c) return;
    c.on('did-start-loading', () => { tab.state.loading = true; tab.state.error = null; this.emit(); });
    c.on('did-stop-loading', () => { tab.state.loading = false; this.syncNavigation(tab); this.emit(); });
    c.on('did-navigate', (_e, url) => { tab.state.url = url; this.syncNavigation(tab); this.emit(); });
    c.on('did-navigate-in-page', (_e, url) => { tab.state.url = url; this.syncNavigation(tab); this.emit(); });
    c.on('page-title-updated', (_e, title) => {
      tab.state.title = (title || '').replace(/\s*-\s*Google Gemini.*$/i, '').trim() || tab.state.accountName;
      this.emit();
    });
    c.on('did-fail-load', (_e, code, desc, _url, isMain) => {
      if (!isMain || code === -3) return;      // -3 = 用户中止，不是错误
      tab.state.loading = false;
      tab.state.error = `${desc || '加载失败'} (${code})`;
      this.emit();
    });
    c.on('render-process-gone', (_e, d) => {
      tab.state.loading = false;
      tab.state.error = '页面进程退出: ' + d.reason;
      this.emit();
    });
    c.on('destroyed', () => {
      if (this.tabs.get(tab.accountId) !== tab || tab.view !== view) return;
      this.stopCookieWatch(tab.accountId);
      this.tabs.delete(tab.accountId);
      if (this.activeAccountId === tab.accountId) this.activeAccountId = null;
      this.emit();
    });
    // 关键：任何"开新窗"的请求都拦回本视图，杜绝弹窗。
    c.setWindowOpenHandler(({ url }) => {
      void c.loadURL(url).catch(() => {});
      return { action: 'deny' };
    });
  }

  syncNavigation(tab) {
    const h = liveContents(tab) && liveContents(tab).navigationHistory;
    if (!h) return;
    tab.state.canGoBack = h.canGoBack();
    tab.state.canGoForward = h.canGoForward();
  }

  applyLayoutToAll() {
    for (const tab of this.tabs.values()) {
      if (!liveContentsOf(tab.view)) continue;
      tab.view.setBounds(this.bounds);
      tab.view.setVisible(this.pageVisible && this.activeAccountId === tab.accountId);
    }
  }

  detachView(view) {
    try { this.window.contentView.removeChildView(view); } catch (e) { /* 已移除 */ }
    try { liveContentsOf(view).close({ waitForBeforeUnload: false }); } catch (e) { /* 已关闭 */ }
  }

  detach(tab) {
    const v = tab.view;
    tab.view = null;
    if (v) this.detachView(v);
  }

  pruneDestroyed() {
    for (const [id, tab] of [...this.tabs.entries()]) {
      if (!tab.view) continue;
      if (liveContentsOf(tab.view)) continue;
      this.tabs.delete(id);
      if (this.activeAccountId === id) this.activeAccountId = null;
    }
  }

  log(line) {
    if (typeof this.opts.onLog === 'function') this.opts.onLog(line);
  }

  emit() {
    if (typeof this.opts.onChange === 'function') {
      try { this.opts.onChange(this.snapshot()); } catch (e) { /* 页面销毁时忽略 */ }
    }
  }
}

module.exports = { EmbeddedBrowser, GEMINI_URL };
