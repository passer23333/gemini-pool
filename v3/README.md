# Gemini 账号池 v3

把多个 Google 账号的 Gemini 网页端出图能力，暴露成 OpenAI 兼容的本地接口。
**纯 HTTP 协议层实现，不启动浏览器**；浏览器只在「登录态维护」时登场。

对比 v2：无 Electron、无 Camoufox 常驻、无 1.3GB 浏览器内核依赖，
单张耗时不更快（那是服务端在算），但**零冷启动、可并发、不惧 DOM 改版**，出图质量更高。

---

## 快速开始

推荐双击根目录的 **`start-app.bat`**：它会自动判断情况（已有网关 → 直接开控制台；
否则启动 exe 外壳；exe 起不来 → 退回无托盘的纯网关），就绪后自动打开浏览器。

> **改了代码不需要重新打包 exe。**
> exe 里只有 `app/main.js`（壳）和 Electron 运行时，**不含 `server.py` / `ui.html`** ——
> 验证方式：`runtime\resources\app\` 下只有 `main.js` 和 `package.json` 两个文件。
> 外壳启动时会定位到 `APP_ROOT`（= `v3\`），然后 `spawn python -u <v3>\server.py`，
> 界面也是网关每次请求时从磁盘读 `ui.html`。所以：
>
> | 你改了 | 需要做什么 |
> |---|---|
> | `server.py`、`ui.html`、`*.py` 业务逻辑 | **重启网关**即可（托盘「重启网关」，或关掉重开） |
> | `ui.html` | 浏览器硬刷新一次（Ctrl+F5），不用重启 |
> | `app/main.js`（壳：窗口/托盘/启动逻辑） | 才需要重新打包：`xcopy /E /Y app\* runtime\resources\app\` |

两种方式，任选其一：

### ① 桌面应用（Electron）

双击 `runtime\Gemini账号池v3.exe`（或根目录 `start-app.bat`）

外壳只负责三件事：拉起 Python 网关、显示控制台窗口、提供系统托盘。
关窗口是最小化到托盘（网关继续跑），从托盘菜单「退出」才真正结束。

**启动时不弹「端口被占用」**。端口上已有网关时，外壳先确认对方是不是自家网关
（`/health` 里的 `service` 字段），是就直接接上、静默打开窗口 —— 这跟常规应用
「已经在跑就把窗口叫出来」是一样的。只有端口被**别的程序**占着（比如其它本地服务）才会
征询，并告诉你是哪个进程（PID + 进程名）。

托盘菜单：「打开控制台 / 重启网关 / 停止网关 / 打开产物目录 / 打开日志文件 / 退出」。
网关意外掉线会自动拉回；状态行会标明网关是本次拉起的，还是外部启动的
（外部启动的退出时保留，不会替你关掉命令行里的那个）。

### ② 仅网关（无界面，适合常驻/服务器）

```bat
start_v3.bat
```

然后浏览器打开 <http://127.0.0.1:17890/ui>

---

两种方式的端口同为 `17890`、Key 同为 `sk-geminipool`，
**已配置过的客户端（无限画布等）无需改配置**。端口若被别的本地程序占着，
外壳会提示是哪个进程（PID + 进程名），不会闷声失败。

依赖：Python 3.10+（启动脚本会自动找 `%LOCALAPPDATA%\Programs\Python`，找不到就用 PATH 里的 `python`）+ `requests`；
重新登录另需 `camoufox`。无其他第三方库。

---

## 接口

### 文生图

```bash
curl http://127.0.0.1:17890/v1/images/generations \
  -H "Authorization: Bearer sk-geminipool" -H "Content-Type: application/json" \
  -d '{"prompt":"雪山日落，广角风光摄影","n":1}'
```

### 图生图

multipart：

```bash
curl http://127.0.0.1:17890/v1/images/edits \
  -H "Authorization: Bearer sk-geminipool" \
  -F "prompt=改成水彩风格" -F "image=@ref.jpg"
```

也支持 JSON：`{"prompt":"...","image":"data:image/jpeg;base64,..."}` 或 `image` 传 http URL。

### Gemini 原生协议（Infinite Canvas 等客户端）

这类客户端**不走 OpenAI 协议**，而是 Google 官方的 Gemini 协议：拉模型调
`GET {base}/v1beta/models`、鉴权用 `x-goog-api-key` 头。只挂 OpenAI 风格接口的话，
它们会一直「拉取不了模型」。

```bash
# 拉模型（不带 Key 也能拉到，方便客户端自动发现；带错 Key 才 401）
curl http://127.0.0.1:17890/v1beta/models -H "x-goog-api-key: sk-geminipool"

# 出图
curl -X POST http://127.0.0.1:17890/v1beta/models/gemini-image:generateContent \
  -H "x-goog-api-key: sk-geminipool" -H "Content-Type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"雪山日落，广角风光摄影"}]}]}'
```

- 返回的模型清单是 Gemini 官方格式（`models[].name` = `models/xxx`，
  `supportedGenerationMethods` 含 `generateContent`/`streamGenerateContent`）。
- 可用模型：`gemini-image`、`nano-banana`、`nano-banana-2`、
  `gemini-2.5-flash-image(-preview)` —— 全部指向同一个网页端自动模型。
  不列文本/视频模型，避免客户端列出来却调不通。
- 响应为 Gemini 原生结构：`candidates[].content.parts[].inlineData`（图片 base64），
  另附 `_gateway`（用的账号、耗时、产物 URL）便于排查。
- `contents[].parts[]` 里的 `inlineData` 会作为参考图走图生图；
  `generationConfig.candidateCount` 支持 1–4 张并行。
- `:streamGenerateContent?alt=sse` 会按 SSE 一次性推完（出图本身不是流式的）。
- 其它动作（如 `:countTokens`）返回 501。

#### 对接 Infinite Canvas 的注意事项

**必须带跨域支持，否则浏览器层就把请求吞了。** 该客户端界面跑在 Chromium
（`EBWebView`）里，拉模型是**渲染进程内的 `fetch`**，不是服务端调用 —— 同源策略生效：

1. 带 `x-goog-api-key` 自定义头 → 先发 **OPTIONS 预检**；
2. 预检通过后还要校验响应的 `Access-Control-Allow-Origin`。

网关两者都已实现（所有响应带 CORS 头、`OPTIONS` 返回 204 且不鉴权）。此外：

- 协议用 HTTP/1.1 keep-alive（原本是 1.0，每请求后断连，预检+正请求连发时容易被拖住）。
- 它内部把 base URL 规范化后再拼 `/v1beta/models`：只去掉尾部 `/` 和一次尾部 `/v1beta`，
  你填 `.../v1` 会拼成 `/v1/v1beta/models`。网关做了重复前缀折叠，各种写法都通。

填写方式：渠道类型 **Gemini** → Base URL `http://127.0.0.1:17890`
（或 `.../v1beta`，别填 `https`，本机没有证书）→ API Key `sk-geminipool`。

**还拉不到就查 `logs/access.log`**，它记了每个请求的方法、路径、状态码、是否带 Key、Origin：

| access.log 里看到 | 说明 |
|---|---|
| `OPTIONS /v1beta/models -> 204` + `GET ... -> 200` | 网关侧已正常，问题在客户端（渠道类型/模型选择） |
| `GET ... -> 401` | Key 填错或没填（也可以不带 Key，列表允许匿名） |
| 完全没有这一行 | 请求压根没到网关：地址填错、被系统代理劫持、或它没真的发起 |

### 其他

| 端点 | 说明 |
|---|---|
| `GET /health` | 账号池健康度、在途数（免鉴权）。另含 `service`/`pid`/`owner`/`started`，供外壳判断「端口上跑的是不是自家网关」 |
| `GET /artifacts/<file>` | 取产物（免鉴权，带 TTL 清理） |
| `GET /v1/models` | 模型列表 |
| `GET /v1/accounts` | 账号池明细（需鉴权） |
| `POST /v1/accounts/{add,update,delete,test}` | 账号管理 |
| `POST /v1/login/new` | **新增账号** `{email, proxy}`：开窗登录，cookie 自动写回 |
| `POST /v1/login/start` | 开窗重新登录 `{id}` |
| `GET /v1/login/status` | 重登进度 / camoufox 是否可用 |
| `POST /v1/browser/open` | 打开某账号的可见浏览器窗口（人工验证用） |
| `POST /v1/browser/close` | 关闭该账号窗口，释放 profile 占用 |
| `POST /v1/artifacts/reveal` | 在文件管理器中选中产物 `{name}` |
| `POST /v1/artifacts/open-dir` | 打开产物目录 |
| `GET /v1/logs?type=api\|action&limit=N` | 接口日志 / 行动日志 |
| `POST /v1/logs/clear` | 清空日志 `{which: "all"\|"api"\|"action"}` |
| `GET` / `POST /v1/config` | 读取 / 保存 `config.json` |
| `POST /v1/videos/generations` | **不支持**（付费会员功能，固定返回 501） |

返回体里每个 `data[]` 都带 `account`（实际用的账号）、`elapsed`（秒）、`size`（字节），
并发时可以直接看出请求分摊到了哪个账号。

---

## 账号

- 存储：`pool.db`（SQLite）。只保存两个关键 cookie：`__Secure-1PSID`、`__Secure-1PSIDTS`。
- 添加：控制台点**「用浏览器登录添加」** —— 会打开一个独立浏览器，你在里面正常登录
  Google，登录成功后 cookie 自动写入账号池并启用。这是推荐方式。
- 备用添加：折叠的「手工粘贴 Cookie（高级）」，仅在无法开窗时用（远程服务器等）。
  （旧版的「从 v2 导入」已随旧版本目录一并移除，账号早已迁移完毕。）
- 调度：占用式轮换（谁在途少给谁，全忙排队），失败自动换号重试，指数退避冷却 30s / 2min / 10min。

### cookie 失效怎么办

Google 会静默吊销会话。发现方式有两种：

1. 生成时报 `not_logged_in` —— 账号被自动标记 `login_required`，控制台该行「重新登录」按钮高亮。
2. 后台巡检 —— `check_interval` 到点对空闲账号做一次轻量引导，提前发现（默认 30 分钟）。

点「重新登录」会弹出一个 Camoufox 窗口：

- 使用持久化 profile（`profiles/account_<id>`），已有会话通常免密。
- 登录成功后导出 cookies，**先用协议层实测一次再写库**，验证不过就不覆盖，避免写进一堆死 cookie。
- 验证通过后自动把账号恢复为 `active` 并清零失败统计。
- **新增账号时若没登录成功**（超时 / 直接关窗 / 出错），占位账号会自动删除，不留死账号。
- 检测到窗口被关闭立即结束，不再空等到超时。

### 「重新登录」与「浏览器」按钮的关系

两者都要打开 Camoufox，且**共用同一个 `profiles/account_<id>`**，
而 Firefox profile 带独占锁 —— 同一账号不能同时进行。因此做了统一互斥：

| 操作 | 另一操作进行中 | 结果 |
|---|---|---|
| 点「浏览器」 | 该账号正在登录 | 409「该账号正在登录中，请等登录结束再开窗」 |
| 点「重新登录」 | 该账号窗口开着 | 409「该账号的浏览器窗口还开着，请先关闭它再重新登录」 |
| 新增账号 | 任意登录窗口在跑 | 409「已有登录窗口在运行，请先完成或关闭它」 |

控制台上：登录中该行「重新登录」置灰；窗口开着时按钮变成「关闭窗口」。
（原「账号浏览器」标签页与账号池重复，已删除，开窗入口收进账号池页内。）

---

## 账号隔离

多账号能否安全并存，取决于四个维度：

| 维度 | 状态 | 说明 |
|---|---|---|
| Cookie | ✅ | 每账号独立存两个关键 cookie，每次请求独立 Session，互不串号 |
| 登录 Profile | ✅ | `profiles/account_<id>` 持久化，各账号指纹独立 |
| 出口 IP | ⚠️ 需手动配 | 账号「独立代理」留空则跟随全局；填上即走独立出口 |
| 请求指纹 | ✅ | `curl_cffi` 的 `impersonate="firefox"`，TLS + HTTP/2 与真实 Firefox 对齐 |

### 为什么"请求指纹"这一栏关键

v3 主链路是纯 HTTP、不启动浏览器 —— 这带来性能，也带来风险：
`requests` 的 TLS 指纹不匹配任何真实浏览器，且只支持 HTTP/1.1、没有 HTTP/2
指纹，在风控视角里就是脚本。所以这里没有用 `requests`，而是换成 `curl_cffi`
的 `impersonate="firefox"` 来对齐指纹。它依然是 HTTP 客户端而非浏览器，
零冷启动、可并发的优势不受影响。

### 配独立出口

控制台「账号池」每行有「代理」列；编辑账号时填入即可，例如
`http://127.0.0.1:7891`。留空 = 跟随全局 `proxy`。

---

## 配置 `config.json`

> 首次使用：复制 `config.example.json` 为 `config.json` 再改。
> `config.json` 含你的 api_key 与代理地址，已在 `.gitignore` 中排除，不会提交。

| 键 | 默认 | 说明 |
|---|---|---|
| `proxy` | `http://127.0.0.1:7890` | Clash 混合端口；留空则直连 |
| `api_port` / `api_key` | 17890 / `sk-geminipool` | 沿用旧版默认值，已配置的客户端不用改 |
| `max_attempts` | 3 | 单请求最多换几个账号 |
| `max_per_account` | 3 | 单账号在途上限；若出现「200 但没图」调回 1 |
| `thinking_level` | `high` | 生成会话的思考等级，固定高点，不对外暴露选项 |
| `queue_wait` | 120 | 全忙时最长排队秒数 |
| `check_interval` | 1800 | 登录态巡检间隔，0 = 关闭 |
| `artifacts_ttl_days` | 7 | 产物保留天数，启动时清理 |
| `save_format` | `original` | `original` = Google 原始 JPEG；`png` = 转无损 PNG（体积约 1.9 倍） |

---

## 出图质量：为什么是 `-d-I`

Gemini 的图片地址是 `https://lh3.googleusercontent.com/gg-dl/<id>`，**不带尺寸参数只给缩略图**。
尺寸 token 形如 `=sNNN` / `=wNNN` / `=hNNN`，`=s0` 表示原图。实测同一张 1408×768：

| 后缀 | 体积 |
|---|---|
| 无 | 57KB（且只有 512×279） |
| `=s0` | 164KB |
| `=d` | 201KB |
| `=s0-d-I` / `=d-I` | **778KB** |

`-d-I` 是同分辨率下低压缩的原始资产。所以取图逻辑是：每个候选生成 `-d-I` 与 `=s0` 两个变体，
**并发**下载，按「像素总数优先、字节数次之」择优；`-d-I` 不可用时自动退回 `=s0`。

改写规则（移植自 voyager-crew/voyager 的 `fetchInterceptor.js`）：

- 只替换尺寸数字，保留尾随 flag：`=s512-d-I` → `=s0-d-I`
- 已经是 `=s0`、或带 `=d` / `=d-I` 的不动（否则会拼出 `...=d-I?alr=yes-s0` 这种非法 URL）
- 下载链可能是多跳的（第一跳返回一段 ~450B 文本，内容是下一跳地址），需要跟随，不能当失败

### 为什么浏览器插件下载的是 2MB，这里是 900KB

**不是清晰度差异，是编码格式差异。** 同一张图、同样的 1408×768 像素：

| 来源 | 格式 | 体积 | 分辨率 |
|---|---|---|---|
| 本网关（`original`） | JPEG（Google 原始资产） | ~900KB | 1408×768 |
| voyager 插件下载 | **PNG（去水印后重编码）** | ~1.8–2MB | 1408×768 |
| 本网关（`save_format=png`） | PNG（无损转码） | ~1.7MB | 1408×768 |

依据：voyager 的 `src/pages/content/watermarkRemover/index.ts` 里
`processedCanvas.toDataURL('image/png')` —— 它把图解码到 canvas、擦掉水印，
再用 **PNG 无损**输出。PNG 不压缩，所以体积约为 JPEG 的 1.9 倍。

实测：把本网关一张 938KB 的 JPEG 用 PIL 转成 PNG，得到 **1766KB**，分辨率不变 ——
这个数字与你看到的 2MB 完全对得上。

**结论**：拿不到"更大的分辨率"。`=s0` / `-d-I` 已经是 Google 网页端能给的原始尺寸上限，
voyager 也只做 `=s0` 改写，不做放大。想要大文件就往 `save_format=png` 走
（好处是后续二次编辑不再叠加一次 JPEG 压缩损失，代价是磁盘占用翻倍）。

---

## 画廊与产物

- 点击缩略图放大；放大后可直接「在文件夹中显示」。
- 每个缩略图右下角有「位置」按钮 —— 在系统文件管理器里**选中**该文件。
- 顶部「打开产物文件夹」直接打开 `artifacts/` 目录。
- 这两个动作由 Python 侧调系统文件管理器完成（Windows `explorer /select,`、
  macOS `open -R`、Linux `xdg-open`），所以只在**网关与界面同机**时有效；
  远程访问时会弹在服务器那台机器上。

---

## 模型切换（已定论：免费账号无第二个模型可切）

网页端「模式选择器」弹层实际是**两组控件**：

**① 模型（有 `data-mode-id`，付费项被 Google 显式标记 `upsell_` 前缀）**

| 模型 | mode-id | 免费账号 |
|---|---|---|
| 自动 Gemini 3 模型 | `cf41b0e0dd7d53e5` | ✅ 唯一可用（本网关默认，不发送模型选择头） |
| 3.1 Pro 高级推理 | `upsell_MODE_CATEGORY_PRO` | ❌ 付费引导 |
| 4 Argon 复杂任务 | `upsell_MODE_CATEGORY_ULTRA` | ❌ 付费引导（ULTRA 档） |

**② 思考等级（无 mode-id，是独立档位，不是模型）**

| 档位 | 说明 | 免费账号 |
|---|---|---|
| 低点 · 快速高效 | 网页端默认档 | ✅ 可用 |
| 中等 · 深度适中 | | ✅ 可用 |
| 高点 · 超级详尽 | **本网关固定值** | ✅ 可用 |

**结论**：免费账号下**模型切换不成立** —— 不是协议有 bug，而是**没有第二个模型可选**。
思考等级虽然三档免费可点（无 `upsell_` 标记），但它是**对话维度的档位，不是生图参数**：

- 生图走的是图片工具通路，与思考预算没有观测上的关联；
- 它是**会话级**的，不随账号持久化，网页端重开就回到「低点」；协议层直调每次
  都是新建会话，因此天然落在默认档。

**网关的处理**：统一按「高点」发起（`config.json` 的 `thinking_level`），
且**不在接口和控制台暴露其它档位** —— 避免调用方把它误当成可调的生图参数。

> 载荷下标未定位：`/app` 首屏配置里没有任何 thinking 相关符号，档位文案由 JS 弹层
> 动态生成。不做浏览器抓包就确认不了它在 92 元素信封里的位置，与其猜下标（猜错会
> 破坏整个请求结构），不如留成策略常量 `THINKING_FIELD_IDX`，定位后填入即可生效。

围绕这个头做过三轮验证，结论一致：

1. **响应元数据不含模型标识** —— 原始 `StreamGenerate` 响应里搜不到模型名或 hash，无法从返回判定。
2. **非法 hash 与 Pro hash 表现完全相同** —— 塞 `deadbeefdeadbeef` 照样 200、照样出图、分辨率不变。
   这说明服务端对无权限/未知 hash 是**静默降级**，不是报错。
3. **完整复刻浏览器头也无差异** —— 用抓包得到的真实头结构
   （能力列表 `[4,5,6,8,16,19,20]`、构建号、时间戳）与完全不带头对照，
   两者出图均为 1408×768，体积相当。这个头对输出分辨率没有影响，属纯冗余。

因此 `/v1/models` 里 `nano-banana-pro` 标为 `available: false`，`MODEL_HASHES` 保留但仅作
「已开通会员时可启用」的入口，默认不发送。**无会员时请一律使用 `gemini-image`。**

> 若日后开通会员：把 `config.json` 外的 `model` 参数传 `nano-banana-pro` 即可试验，
> 但仍建议先用同一提示词对比一次，不要假定生效。

---

## 出图质量：人像的短板在哪

用复杂人像提示词（手部特写 + 皮肤质感 + 环境光）实测自动模型的表现：

- ✅ 构图、光影、材质、皮肤皱纹、眼镜反光都到位
- ⚠️ **手指是短板** —— 握持动作中相邻手指会粘连、指节糊成一块，放大到 2× 可辨

这是自动模型的固有天花板，换 Pro（若开通）会有明显改善。免费的缓解手段是
在提示词里明确手部姿态（如「右手拇指与食指捏住刻刀，其余三指自然弯曲」），
比笼统的「双手握着刻刀」更容易画对。

---

## 已知限制

- **视频生成不支持**：属 Gemini 付费会员功能，免费账号不可用。网关保留该路由
  只为给出明确原因，固定返回 501，不会尝试调用。
- **单账号并发上限默认 3**。网页端同一账号开多个标签页可以各发各的请求，说明
  并发并非账号级硬限制，故默认放开。少数情况下仍会出现「HTTP 200 但响应里没图」
  的限流表现 —— 此时 `run_job` 会自动换号重试，不会直接失败；若频繁出现，
  把 `max_per_account` 调回 1 或增加账号数。

---

## 目录结构

```
v3/
├── app/                Electron 外壳源码（改窗口/托盘/启动逻辑改这里）
├── runtime/            可分发应用：exe + 运行时 + resources/app
├── server.py           网关：OpenAI 兼容接口 + 账号池调度
├── gemini_web.py       协议层客户端（curl_cffi，带浏览器指纹）
├── image_fetch.py      图片地址改写与择优下载（-d-I / =s0）
├── accounts.py         账号池：SQLite、健康度、冷却、轮换
├── login_camoufox.py   cookie 失效时开窗重登
├── browser_camoufox.py 账号浏览器窗口（人工验证用）
├── ui.html             控制台页面（7 个视图）
├── config.json         配置
├── pool.db             账号库
├── artifacts/          生成产物
└── profiles/           浏览器 profile（登录态）
```

改完外壳源码后同步到运行时：

```bat
xcopy /E /Y app\* runtime\resources\app\
```

---

## 排障

**代理明明是通的，Python 却一直 502**
系统里有 `HTTPS_PROXY=127.0.0.1:13744`（沙箱代理）会抢走请求。客户端已设 `trust_env=False`，
如果你的环境有别的代理变量，同样需要屏蔽。

**被甩到错误页 32050 / CookieMismatch**
cookie 塞太多了。只带 `__Secure-1PSID` + `__Secure-1PSIDTS` 两个即可，全塞会冲突。

**重登时抛 `NotInstalledGeoIPExtra`**
不要给 Camoufox 传 `geoip=True`，它需要额外装 `camoufox[geoip]`，且是进入上下文阶段才报错。
本模块已移除该参数。

**`persistent_context=True` 时没有 `.contexts`**
Camoufox 在这种模式下直接返回 `BrowserContext` 而不是 `Browser`，取上下文要分支处理。
