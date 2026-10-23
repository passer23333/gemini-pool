// Gemini 账号池 v3 — Electron 外壳
//
// 职责边界：这个进程只做三件事 —— 托管 Python 网关子进程、提供窗口、提供系统托盘。
// 所有业务逻辑（账号池、生成、日志、配置）都在 Python 侧的 server.py 里，
// 界面直接以 HTTP 调网关，避免 IPC 层重复实现一套语义。
//
// 注意：v3 的立身之本是「不常驻浏览器内核」。这里的 Electron 窗口只用于承载
// 控制台页面，真正的出图请求走网关的纯 HTTP 协议层，不经过任何浏览器。
const { app, BrowserWindow, Menu, Tray, dialog, shell, ipcMain } = require('electron');
const path = require('path');
const http = require('http');
const net = require('net');
const { spawn, execSync } = require('child_process');
const fs = require('fs');
const { EmbeddedBrowser } = require('./embedded-browser');

// 两种运行形态共用一份 main.js，靠「上级目录里有没有 server.py」自动区分：
//   开发模式: v3/app/main.js                → APP_ROOT = v3
//   打包模式: v3/runtime/resources/app/main.js → APP_ROOT = v3
function resolveRoot() {
  const dev = path.resolve(__dirname, '..');
  if (fs.existsSync(path.join(dev, 'server.py'))) return dev;
  return path.resolve(__dirname, '..', '..', '..');
}

const APP_ROOT = resolveRoot();
const SERVER = path.join(APP_ROOT, 'server.py');
const CONFIG = path.join(APP_ROOT, 'config.json');
const GATEWAY_FILE = path.join(APP_ROOT, 'gateway.json');

// Windows 下打包成 GUI 子系统时 stdout 常常不可见，所以所有日志同时落盘，
// 出问题先看 v3/logs/electron.log。
const LOG_DIR = path.join(APP_ROOT, 'logs');
const LOG_FILE = path.join(LOG_DIR, 'electron.log');
try { fs.mkdirSync(LOG_DIR, { recursive: true }); } catch (e) { /* 已存在 */ }

function log(...a) {
  const line = new Date().toTimeString().slice(0, 8) + ' ' + a.join(' ');
  try { fs.appendFileSync(LOG_FILE, line + '\n', 'utf-8'); } catch (e) { /* 忽略 */ }
  try { console.log(line); } catch (e) { /* 忽略 */ }
}

process.on('uncaughtException', e => log('[v3] 未捕获异常:', e && e.stack || e));
log('[v3] main.js 已加载, APP_ROOT =', APP_ROOT, '| electron =', process.versions.electron);

// Python 解释器优先级：常见安装位置（用环境变量拼，不写死用户名），找不到再退回 PATH。
const PYTHON_CANDIDATES = [
  (process.env.LOCALAPPDATA || '') + '\\Programs\\Python\\Python310\\python.exe',
  (process.env.LOCALAPPDATA || '') + '\\Programs\\Python\\Python311\\python.exe',
  (process.env.LOCALAPPDATA || '') + '\\Programs\\Python\\Python312\\python.exe',
  'python.exe',
  'python',
];

let win = null;
let tray = null;
let engine = null;      // Python 网关子进程
let embedded = null;    // 内嵌 Gemini 工作台（WebContentsView 集合）
let quitting = false;

// 当前网关的来源。区分它很关键：决定了退出时要不要顺手停掉。
//   none    未运行
//   owned   本次由本外壳拉起 —— 退出时必须停
//   adopted 端口上已有网关，本次只是接上 —— 只有对方 owner 也是 electron
//           （即上次崩溃留下的孤儿）才接管收掉，用户手动起的一律保留
let gateway = { mode: 'none', pid: null, owner: null };
// 实际在用的端口。主端口被别的程序占了时会顺延，这里记的是最后真正在用的那个，
// 窗口地址、托盘文案、看门狗都以它为准。
let activePort = readPort();

const sleep = ms => new Promise(r => setTimeout(r, ms));

function resolvePython() {
  for (const p of PYTHON_CANDIDATES) {
    try {
      if (fs.existsSync(p)) return p;
    } catch (e) { /* 非绝对路径会走到下面 */ }
  }
  // 非绝对路径时交给系统解析
  return PYTHON_CANDIDATES.find(p => !path.isAbsolute(p)) || 'python';
}

function readPort() {
  try {
    return parseInt(JSON.parse(fs.readFileSync(CONFIG, 'utf-8')).api_port) || 17890;
  } catch (e) {
    return 17890;
  }
}

function readApiKey() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG, 'utf-8')).api_key || 'sk-geminipool';
  } catch (e) {
    return 'sk-geminipool';
  }
}

function httpJson(port, p, timeout = 1500) {
  return new Promise(resolve => {
    const req = http.get({ host: '127.0.0.1', port, path: p, timeout }, res => {
      let body = '';
      res.on('data', c => { body += c; });
      res.on('end', () => {
        if (res.statusCode !== 200) return resolve(null);
        try { resolve(JSON.parse(body)); } catch (e) { resolve(null); }
      });
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
  });
}

function pingHealth(port) {
  return httpJson(port, '/health').then(h => !!(h && h.status === 'ok'));
}

/** 探测端口上跑的是谁：不通 / 自家网关 / 别人的服务。 */
async function probeGateway(port) {
  const h = await httpJson(port, '/health');
  if (!h || h.status !== 'ok') return { alive: false, ours: false, health: h };
  return {
    alive: true,
    ours: h.service === 'gemini-pool-v3',
    pid: h.pid || null,
    owner: h.owner || null,
    started: h.started || 0,
    port: h.port || null,
  };
}

/** 读网关自报的端口。竞态时（我们探到空端口、它启动时却被抢）只有它自己知道真相。 */
function readGatewayFilePort() {
  try {
    const f = path.join(APP_ROOT, 'gateway.json');
    if (!fs.existsSync(f)) return null;
    const d = JSON.parse(fs.readFileSync(f, 'utf-8'));
    if (d.service !== 'gemini-pool-v3') return null;
    // 只认自己刚拉起的那个进程写的，别拿上一次残留的文件当真
    if (engine && d.pid && d.pid !== engine.pid) return null;
    return d.port || null;
  } catch (e) {
    return null;
  }
}

/** 端口有没有人占（哪怕不是 HTTP 服务也算）。 */
function portInUse(port) {
  return new Promise(resolve => {
    const s = net.connect({ host: '127.0.0.1', port }, () => { s.destroy(); resolve(true); });
    s.on('error', () => resolve(false));
    s.setTimeout(1200, () => { s.destroy(); resolve(false); });
  });
}

/** 查端口占用者的 PID 与进程名，用于给用户一个能看懂的提示。 */
function findPortOwner(port) {
  try {
    const out = execSync('netstat -ano -p TCP', { encoding: 'utf-8', windowsHide: true });
    const pids = [...new Set(
      out.split(/\r?\n/)
        .filter(l => l.includes(`:${port}`) && /LISTENING/i.test(l))
        .map(l => l.trim().split(/\s+/).pop())
        .filter(p => /^\d+$/.test(p) && p !== '0')
    )];
    if (!pids.length) return null;
    let name = '';
    try {
      const t = execSync(`tasklist /FI "PID eq ${pids[0]}" /FO CSV /NH`,
        { encoding: 'utf-8', windowsHide: true });
      const m = t.match(/"([^"]+)"/);
      if (m) name = m[1];
    } catch (e) { /* 进程可能已退出 */ }
    return { pid: parseInt(pids[0], 10), name: name || '未知进程' };
  } catch (e) {
    return null;
  }
}

function killPid(pid) {
  if (!pid) return false;
  try {
    // /T 连子进程一起收：网关拉起的 Camoufox 登录窗口是子进程，不带 /T 的话
    // Python 没了、火狐窗口还留着，照样不算彻底退出。
    execSync(`taskkill /PID ${pid} /F /T`, { windowsHide: true, stdio: 'ignore' });
    return true;
  } catch (e) {
    return false;
  }
}

function pidAlive(pid) {
  if (!pid) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return false; }
}

/**
 * 上次启动留下的身份文件。/health 已经不通、但端口还占着时，它能告诉我们
 * 「这是自家网关僵死了」，而不是笼统地说「被其他程序占用」。
 */
function staleGateway() {
  try {
    const d = JSON.parse(fs.readFileSync(GATEWAY_FILE, 'utf-8'));
    if (d.service !== 'gemini-pool-v3') return null;
    if (!pidAlive(d.pid)) return null;      // 进程已不存在，文件只是残留
    // 再确认这个 PID 确实是 python（避免 PID 被系统复用给别的进程）
    const who = findPortOwner(d.port || readPort());
    if (who && who.pid !== d.pid) return null;
    return d;
  } catch (e) {
    return null;
  }
}

function startEngine(port) {
  const py = resolvePython();
  log('[v3] Python =', py);
  engine = spawn(py, ['-u', SERVER], {
    cwd: APP_ROOT,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1',
           // 让网关在 /health 里自报「我是外壳拉起的」，下次启动才知道能不能接管
           GEMINIPOOL_OWNER: 'electron',
           // 明确指定端口。网关自己也有一套顺延逻辑，这里是双保险：
           // 两边算出来的端口必须一致，否则会出现外壳等 17891、网关听 17892 的错位。
           GEMINIPOOL_PORT: String(port) },
    windowsHide: true,
  });
  // GUI 子系统下 stdout 不可见，网关的输出必须落盘，否则起不来时无从查起
  engine.stdout.on('data', d => log('[engine] ' + String(d).trim()));
  engine.stderr.on('data', d => log('[engine!] ' + String(d).trim()));
  engine.on('exit', code => {
    log('[v3] 网关进程退出，code =', code);
    engine = null;
  });
  engine.on('error', err => log('[v3] 网关进程启动失败:', err.message));
}

async function waitReady(port, ms = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    if (await pingHealth(port)) return true;
    await sleep(400);
  }
  return false;
}

/** 自己拉起一个网关。 */
async function spawnGateway(port) {
  startEngine(port);
  if (!(await waitReady(port, 15000))) {
    // 我们挑这个端口时空着，但网关启动时刚巧被别的程序抢先占了，它自己顺延了。
    // 这时候死等旧端口只会白等 30 秒再报个没用的错 —— 去读它自报的真实端口。
    const real = readGatewayFilePort();
    if (real && real !== port && await waitReady(real, 10000)) {
      log(`[v3] 网关顺延到 ${real}（预期 ${port}），以它自报的为准`);
      port = real;
    } else {
      return { ok: false, reason:
        `未能在 25 秒内连上 http://127.0.0.1:${port}/health。\n\n`
        + '常见原因：Python 环境不可用、config.json 的 api_port 被占用、依赖缺失。\n'
        + `详细日志见：${LOG_FILE}` };
    }
  }
  const p = await probeGateway(port);
  gateway = { mode: 'owned', pid: (p && p.pid) || (engine && engine.pid) || null,
              owner: 'electron' };
  // 网关自己也可能顺延过端口（比如我们挑的这个刚巧被抢先占了）。以它自报的为准。
  if (p && p.port && p.port !== port) {
    log(`[v3] 网关实际监听 ${p.port}（预期 ${port}），以后者为准`);
    port = p.port;
  }
  log('[v3] 网关已就绪 pid=' + gateway.pid + ' port=' + port);
  return { ok: true, port };
}

/** 从 start 起找一个没人占的端口，找不到返回 null。 */
async function findFreePort(start, tries = 50) {
  for (let i = 0; i < tries; i++) {
    const p = start + i;
    if (!(await portInUse(p))) return p;
  }
  return null;
}

/**
 * 确保有网关可用。三种情况都不打扰用户：
 *   1. 端口上已经是自家网关 —— 直接接上（常态）
 *   2. 端口被自家网关的僵死进程占着 —— 自动收掉再用原端口
 *   3. 端口被别的程序占着 —— 自动顺延找一个空端口（比如 MediaPool 占着 17890
 *      时，本应用就退到 17891，而不是弹个框让用户去关别人）
 */
async function ensureGateway(preferred) {
  const probe = await probeGateway(preferred);

  if (probe.alive && probe.ours) {
    gateway = { mode: 'adopted', pid: probe.pid, owner: probe.owner };
    activePort = preferred;
    log('[v3] 已连接现有网关 pid=' + probe.pid + ' owner=' + probe.owner);
    return { ok: true, port: preferred };
  }

  let port = preferred;
  if (probe.alive || await portInUse(preferred)) {
    const stale = probe.alive ? null : staleGateway();
    if (stale) {
      // 自家僵死的网关：直接清理，不值得为它弹窗
      log('[v3] 收掉上次僵死的网关 pid=' + stale.pid);
      killPid(stale.pid);
      await sleep(1500);
      if (await portInUse(preferred)) {
        port = await findFreePort(preferred + 1);
        if (!port) return { ok: false, reason: `端口 ${preferred} 仍未释放，且找不到备用端口。` };
        log('[v3] 端口 ' + preferred + ' 仍未释放，改用 ' + port);
      }
    } else {
      const who = findPortOwner(preferred);
      const desc = who ? `${who.name}（PID ${who.pid}）` : '未知进程';
      port = await findFreePort(preferred + 1);
      if (!port) {
        return { ok: false, reason:
          `端口 ${preferred} 被 ${desc} 占用，且其后 50 个端口也都被占用。` };
      }
      log(`[v3] 端口 ${preferred} 被 ${desc} 占用，自动改用 ${port}`);
    }
  }

  const r = await spawnGateway(port);
  if (r.ok) activePort = r.port || port;
  return { ok: r.ok, port: activePort, reason: r.reason };
}

/** 同步等待。退出流程里没法 await，得有一个不依赖外部命令的阻塞睡眠。 */
function syncSleep(ms) {
  try {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
  } catch (e) { /* 环境不支持就跳过，后面还有强杀兜底 */ }
}

/** 删掉指定 pid 留下的服务发现文件。

 强杀的进程不会走 atexit，文件就留在磁盘上指向一个死端口 —— 下次启动前谁读到它
 谁就连不上（包括外部客户端）。退出时按 pid 精确清理，只删自己杀掉的那个。
 */
function purgeGatewayFiles(pids) {
  if (!pids || !pids.length) return;
  const files = [path.join(APP_ROOT, 'gateway.json')];
  if (process.env.LOCALAPPDATA) {
    files.push(path.join(process.env.LOCALAPPDATA, 'GeminiPool', 'gateway.json'));
  }
  for (const f of files) {
    try {
      if (!fs.existsSync(f)) continue;
      const d = JSON.parse(fs.readFileSync(f, 'utf-8'));
      if (pids.indexOf(d.pid) >= 0) {
        fs.unlinkSync(f);
        log('[v3] 已清理残留的服务发现文件 ' + f);
      }
    } catch (e) { /* 读不出来就别动，留着给下次启动的 purge 逻辑处理 */ }
  }
}

/** 停止网关。只收自己该收的：本次拉起的必停；接管的仅当它本来也是外壳起的。 */
function stopGateway() {
  const killed = [];
  if (engine && !engine.killed) {
    log('[v3] 停止网关进程 pid=' + engine.pid);
    try { engine.kill(); } catch (e) { /* 已退出 */ }
    killed.push(engine.pid);
  }
  if (gateway.mode === 'adopted' && gateway.owner === 'electron' && gateway.pid) {
    log('[v3] 收掉上次遗留的网关 pid=' + gateway.pid);
    if (killPid(gateway.pid)) killed.push(gateway.pid);
  }
  if (gateway.mode === 'adopted' && gateway.owner === 'manual') {
    log('[v3] 网关由外部启动（pid=' + gateway.pid + '），退出时保留');
  }
  // 先给 Python 一点时间走完 atexit 自己收尾；没退干净就 taskkill /F 兜底。
  // 只发信号是不够的 —— 进程卡在 IO 上会活着继续占端口，那就谈不上「彻底退出」。
  syncSleep(600);
  for (const pid of killed) killPid(pid);
  purgeGatewayFiles(killed);
  gateway = { mode: 'none', pid: null, owner: null };
}

async function restartGateway() {
  log('[v3] 重启网关');
  if (engine && !engine.killed) { try { engine.kill(); } catch (e) { /* 已退出 */ } }
  if (gateway.pid) killPid(gateway.pid);
  gateway = { mode: 'none', pid: null, owner: null };
  await sleep(1500);
  // 走 ensureGateway：万一原来的端口这会儿被别人占了，它会自己顺延
  const r = await ensureGateway(activePort);
  if (!r.ok) {
    dialog.showErrorBox('网关重启失败', r.reason);
    refreshTray();
    return;
  }
  activePort = r.port || activePort;
  // 端口可能换过，reload 会连回旧地址，必须用新的 loadURL
  if (win) win.loadURL(`http://127.0.0.1:${activePort}/ui`);
  refreshTray();
}

/** 用户本来想用的端口。端口顺延后 api_port 会被回写成实际值，只剩这里还记着初衷。 */
function preferredPort() {
  try {
    const c = JSON.parse(fs.readFileSync(CONFIG, 'utf-8'));
    return parseInt(c.preferred_port || c.api_port) || 17890;
  } catch (e) {
    return readPort();
  }
}

function gatewayStateText() {
  // 端口顺延过就得让用户看见：外部客户端里填的是他原本配的那个端口
  const pref = preferredPort();
  const drift = activePort !== pref ? `（${pref} 被占，已顺延）` : '';
  if (gateway.mode === 'owned') return `网关 :${activePort} · 运行中${drift}`;
  if (gateway.mode === 'adopted') {
    return gateway.owner === 'manual'
      ? `网关 :${activePort} · 已连接（外部启动，退出时保留）${drift}`
      : `网关 :${activePort} · 已连接${drift}`;
  }
  return `网关 :${activePort} · 未运行${drift}`;
}

function buildTray() {
  const iconPath = path.join(APP_ROOT, 'assets', 'tray.png');
  tray = new Tray(fs.existsSync(iconPath) ? iconPath : path.join(APP_ROOT, 'icon.png'));
  tray.setToolTip('Gemini 账号池 v3');
  refreshTray();
  tray.on('click', () => { if (win) { win.show(); win.focus(); } });
}

function refreshTray() {
  if (!tray) return;
  // 就两项：显示主窗口、退出。托盘是「我还在后台跑」的提示，不是功能面板 ——
  // 网关状态、产物目录、日志这些在界面里都有，不该在托盘里堆一排入口。
  const menu = Menu.buildFromTemplate([
    { label: '显示主窗口', click: () => { if (win) { win.show(); win.focus(); } } },
    { type: 'separator' },
    { label: '退出', click: () => { quitting = true; app.quit(); } },
  ]);
  tray.setContextMenu(menu);
  // 菜单精简到两项后，状态改由悬浮提示承担（鼠标停在托盘图标上就能看到）
  try { tray.setToolTip('Gemini 账号池 v3 · ' + gatewayStateText()); } catch (e) { /* no-op */ }
}

/**
 * 内嵌工作台：把 Gemini 网页作为一层视图叠在控制台之上。
 * 以前这类操作要靠网关开 Camoufox 独立窗口，每次都弹到系统前台；现在留在应用里。
 */
function setupEmbeddedBrowser(port) {
  embedded = new EmbeddedBrowser(win, {
    port,
    apiKey: readApiKey(),
    onChange: snap => {
      try {
        if (win && !win.isDestroyed() && win.webContents && !win.webContents.isDestroyed()) {
          win.webContents.send('gpool.browser.event', snap);
        }
      } catch (e) { /* 页面正在重载 */ }
    },
    onLog: line => log('[browser] ' + line),
  });

  const eb = embedded;
  // 这里的 handler 抛错会以 rejected promise 回到页面，UI 侧统一 catch 提示
  ipcMain.handle('gpool.browser.open', (_e, a) => eb.open(a));
  ipcMain.handle('gpool.browser.close', (_e, id) => eb.close(id));
  ipcMain.handle('gpool.browser.activate', (_e, id) => eb.activate(id));
  ipcMain.handle('gpool.browser.reload', (_e, id) => eb.reload(id));
  ipcMain.handle('gpool.browser.navigate', (_e, dir) => { eb.navigate(dir); return eb.snapshot(); });
  ipcMain.handle('gpool.browser.navigateTo', (_e, url) => { eb.navigateTo(url); return eb.snapshot(); });
  ipcMain.handle('gpool.browser.setLayout', (_e, o) => {
    eb.setLayout(!!(o && o.visible), (o && o.bounds) || null);
    return true;
  });
  ipcMain.handle('gpool.browser.snapshot', () => eb.snapshot());
  ipcMain.handle('gpool.browser.syncCookies', (_e, id) => eb.syncCookies(id));
  // 托盘只剩「显示主窗口 / 退出」，重启这类操作改由界面触发
  ipcMain.handle('gpool.gateway.restart', async () => {
    await restartGateway();
    return { ok: true, port: activePort };
  });
}

function createWindow(port) {
  win = new BrowserWindow({
    width: 1180,
    height: 800,
    minWidth: 960,
    minHeight: 640,
    title: 'Gemini 账号池 v3',
    autoHideMenuBar: true,
    backgroundColor: '#f7f8fa',
    webPreferences: {
      // 页面本身仍然走 HTTP 调网关；preload 只提供内嵌工作台那一小块受控能力
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
    },
  });
  Menu.setApplicationMenu(null);   // 去掉默认菜单栏，观感更接近成品应用
  win.loadURL(`http://127.0.0.1:${port}/ui`);
  win.on('close', e => {
    if (!quitting) {               // 关闭窗口 = 最小化到托盘，网关继续跑
      e.preventDefault();
      win.hide();
    }
  });
  win.on('closed', () => { win = null; });
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
}

/** 网关意外退出时自动拉回，避免托盘还亮着但界面全白。 */
function startEngineWatchdog() {
  setInterval(async () => {
    if (quitting) return;
    if (gateway.mode !== 'owned') return;      // 只管自己拉起的那个
    if (engine && !engine.killed) return;
    // 读 activePort 而不是闭包里的旧值：网关可能顺延过端口，旧值已经失效
    if (await pingHealth(activePort)) return;  // 还在跑，可能只是句柄状态没同步
    log('[v3] 网关已掉线，尝试自动重启');
    const r = await spawnGateway(activePort);
    if (r.ok) {
      activePort = r.port || activePort;
      // 端口可能换过，reload 会连回旧地址，必须用新的 loadURL
      if (win) win.loadURL(`http://127.0.0.1:${activePort}/ui`);
    }
    refreshTray();
  }, 15000);
}

app.on('before-quit', () => {
  if (quitting) return;                 // second-instance 也会走这里，别清理两遍
  quitting = true;
  log('[v3] 正在退出，开始清理');
  if (embedded) { try { embedded.destroyAll(); } catch (e) { /* 已销毁 */ } }
  stopGateway();
  // 托盘图标必须显式销毁：不销毁的话进程虽然退了，Windows 上仍会留一个幽灵图标，
  // 要等鼠标划过才消失 —— 看起来就像应用没退干净。
  if (tray) {
    try { tray.destroy(); } catch (e) { /* 已销毁 */ }
    tray = null;
  }
  log('[v3] 清理完成');
});

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  // 已经在跑了：常规应用的行为是把已有窗口叫出来，而不是报错。
  app.quit();
} else {
  app.on('second-instance', () => { if (win) { win.show(); win.focus(); } });

  app.whenReady().then(async () => {
    const port = readPort();
    log('[v3] ready, port =', port);

    const r = await ensureGateway(port);
    if (!r.ok) {
      if (!r.silent) dialog.showErrorBox('网关启动失败', r.reason);
      app.quit();
      return;
    }
    // r.port 才是真正在用的那个（可能被自动顺延过）
    const used = r.port || activePort;
    if (used !== port) log(`[v3] 主端口 ${port} 不可用，实际使用 ${used}`);

    createWindow(used);
    setupEmbeddedBrowser(used);
    buildTray();
    refreshTray();
    if (gateway.mode === 'owned') startEngineWatchdog();
  });
}

app.on('window-all-closed', () => {
  // 保留托盘常驻：关窗不退出，由托盘菜单显式退出。
  if (process.platform !== 'darwin') { /* no-op：由托盘接管 */ }
});
