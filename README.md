# cf-slayer

Cloudflare 放行凭据求解与回放服务。它驱动浏览器通过挑战，收割 `cf_clearance`
饼干和赢得它的那个精确 User-Agent，按域名缓存两者 30 分钟，再用 `curl_cffi`
（Firefox TLS 指纹）回放该凭据。

后端按顺序尝试（`CF_SLAYER_BACKENDS`，默认 `camoufox,pydoll`）。Camoufox
（隐身 Firefox）排第一；pydoll 通过 CDP 驱动本机已安装的 Chrome，缺 Chrome
或依赖包时自动跳过。每次尝试都写进响应的 `attempts` 数组。可选的后台工作线程
在缓存凭据过期前续期。

```
POST /solve  {url}    ──►  {clearance, user_agent, domain, expires_in, cached, attempts[]}
      │                        命中缓存：不启动浏览器，attempts=[]
      └─ 未命中 ─► BackendChain
                   ├─ camoufox: 隐身 Firefox → goto → 轮询 cf_clearance → UA
                   └─ pydoll: 本机 Chrome → 同样的饼干轮询（缺失则跳过）
                        └─► SQLite 缓存（按域名，TTL 30 分钟）

PrewarmWorker (CF_SLAYER_PREWARM_DOMAINS) ──► 在余量内续期记录

GET /replay?url=...    ──►  {status, length, challenge_page, ...}
      └─ 缓存饼干 + 求解时 UA ─► 目标站点
            └─ 403/挑战页 ─► 作废，重解一次，重试一次
```

## 环境要求

- Python 3.12+（在 3.13、Windows 上开发）
- 首次拉取后约 2.5 GB 磁盘空间给打过补丁的 Firefox 与插件
  （下载 1.3 GB，验证机耗时 198 秒）
- 下列命令需要 CMake/Git Bash/PowerShell
- 可选：已安装的 Chrome/Chromium，供 pydoll 回退后端使用。
  会从常见安装路径和 `PATH` 探测，也可设 `CF_SLAYER_CHROME_PATH`。

Camoufox 在 Firefox 的 C++ 层修补指纹（WebGL renderer、canvas、AudioContext、
WebRTC、屏幕几何信息……），因此比原版 Playwright 更能对付 Cloudflare 托管挑战。

## 安装

Git Bash：

```bash
cd cf-slayer
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m camoufox fetch      # 一次性，约 1.3 GB 下载
```

PowerShell：

```powershell
cd cf-slayer
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m camoufox fetch
```

也可以用 `uv`：`uv venv --python 3.13 .venv && uv pip install -r requirements.txt --python .venv/Scripts/python.exe`。

`camoufox fetch` 通过 GitHub API 解析发布版本。若该调用被限流（匿名 IP 每小时
60 次），先导出令牌：

```bash
export GITHUB_TOKEN=$(gh auth token)   # 或个人访问令牌
.venv/Scripts/python -m camoufox fetch
```

用 `.venv/Scripts/python -m camoufox version` 验证已安装的版本。

## 运行

```bash
.venv/Scripts/python -m uvicorn solver.main:app --host 127.0.0.1 --port 8878
```

首次启动时服务生成 bearer 令牌，把它追加到 `.env`，并记录文件路径（令牌本身
永不写日志）。读取方式：

```bash
# Git Bash
TOKEN=$(grep '^CF_SLAYER_AUTH_TOKEN=' .env | cut -d= -f2)

# PowerShell
$TOKEN = (Select-String -Path .env -Pattern '^CF_SLAYER_AUTH_TOKEN=(.+)$').Matches[0].Groups[1].Value
```

Windows 不强制 POSIX `0600`；机器共享时请显式收紧权限：

```powershell
icacls .env /inheritance:r /grant:r "$env:USERNAME:F"
```

### 端点

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| POST | `/solve` | Bearer | 求解并缓存放行凭据 |
| GET | `/replay?url=...` | Bearer | 用缓存凭据抓取目标 |
| GET | `/health` | 无 | `{"state":"ready","browser":"camoufox"}` |
| GET | `/stats` | Bearer | 缓存命中、求解次数、错误、预热错误、平均求解耗时 |

### curl 示例

```bash
# 求解
curl -s -X POST http://127.0.0.1:8878/solve \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://target.example/","want":"clearance"}'

# -> {"clearance":"...","user_agent":"Mozilla/5.0 ... Firefox/156.0",
#     "domain":"target.example","expires_in":1789,"cached":false,"solve_seconds":14.2,
#     "attempts":[{"backend":"camoufox","ok":true,"error":null,"seconds":14.2}]}

# 回放（使用缓存饼干 + 求解时的 UA）
curl -s "http://127.0.0.1:8878/replay?url=https://target.example/" \
  -H "Authorization: Bearer $TOKEN"

# -> {"url":"https://target.example/","domain":"target.example","status":200,
#     "length":202117,"challenge_page":false,"replay_seconds":1.34,"resolved":false}

# 健康检查与统计
curl -s http://127.0.0.1:8878/health
curl -s http://127.0.0.1:8878/stats -H "Authorization: Bearer $TOKEN"
```

`/solve` 只接受 `want: "clearance"`，其他值返回 `BAD_REQUEST`。命中缓存时
`attempts: []`。失败时同构数组出现在 `error.attempts` 内，每个已运行或被跳过的
后端一条记录。

## 错误

所有失败均为 JSON：

```json
{"error": {"code": "SOLVE_TIMEOUT", "message": "cf_clearance did not appear within 90s"}}
```

| 代码 | HTTP | 含义 |
|---|---|---|
| `AUTH_MISSING` | 401 | 缺少或错误的 `Authorization: Bearer <token>` |
| `BAD_REQUEST` | 400 | 网址错误、JSON 错误、不支持的 `want` |
| `SOLVE_TIMEOUT` | 504 | 在求解/挑战预算（120 秒 / 90 秒）内没等到 `cf_clearance` |
| `SOLVE_FAILED` | 502 | 浏览器已启动但挑战未通过，或引擎崩溃 |
| `UPSTREAM_403` | 502 | 自动重解一次后回放仍被挑战拦截 |

## 硬性规则（为何有效）

`cf_clearance` 饼干只有回放上下文一致才有意义。服务强制能强制的部分，其余明确
记录：

1. **同一出口 IP。** 饼干绑定求解时的 IP。v1 不支持代理，所以求解与回放必须
   在同一主机/连接上运行。
2. **同一 User-Agent。** 回放发送从浏览器会话收割的精确 UA；缓存把饼干与 UA
   作为一个整体保存。回放时改动任一都会静默 403。
3. **约 30 分钟 TTL。** 缓存 TTL 上限 1800 秒，饼干自身的 `expires` 更早时以它为
   准。过期行在读取时删除。
4. **按域名隔离。** 缓存键是小写主机名。`a.example.com` 的凭据永不用于
   `b.example.com`，回放客户端会拒绝域名不匹配。

若回放看到 403/503 或正文里的 Cloudflare 中间页，服务作废该条目，求解一次，
再重试一次。二次失败返回 `UPSTREAM_403`。对无缓存凭据的域名调用 `/replay`
会先求解再回放（同样只重试一次）。

## 配置

`pydantic-settings` 从环境或 `.env` 读取 `CF_SLAYER_*`；见
[`.env.example`](.env.example)。要点：`CF_SLAYER_PORT`（8878）、
`CF_SLAYER_TTL_SECONDS`（1800）、`CF_SLAYER_SOLVE_TIMEOUT`（120）、
`CF_SLAYER_CHALLENGE_TIMEOUT`（90）、`CF_SLAYER_HEADLESS`（true）、
`CF_SLAYER_BACKENDS`（`camoufox,pydoll`）、`CF_SLAYER_CHROME_PATH`（自动）、
`CF_SLAYER_PREWARM_DOMAINS`（关闭）、`CF_SLAYER_PREWARM_MARGIN`（300）、
`CF_SLAYER_PREWARM_INTERVAL`（60）。

## 后端

`CF_SLAYER_BACKENDS` 是有序列表。每个后端每个请求拿到同样的超时预算；首个成功
者胜出，尝试记录保留每一次。

- `camoufox` — 打过补丁的 Firefox，主力求解器。
- `pydoll` — 通过 DevTools 协议驱动本机 Chrome（不用 WebDriver）。缺 `pydoll`
  包或 Chrome 时后端自报不可用，链会跳过它而不是失败。`--user-agent` 取自已安装
  Chrome 的版本，否则 Chrome 会自报 `HeadlessChrome`，Cloudflare 视之为机器人
  信号。

未知名称会记录日志并被忽略。若无后端可用，启动以清晰的 `RuntimeError` 失败。
链失败返回 `SOLVE_FAILED`（全部超时则为 `SOLVE_TIMEOUT`），并在
`error.attempts` 中给出逐后端错误。

## 预热

把 `CF_SLAYER_PREWARM_DOMAINS` 设为逗号分隔的域名或完整 URL
（`steamdb.info,https://example.com/`）。服务运行期间，后台工作线程检查每条
缓存记录，缺失或剩余不足 `CF_SLAYER_PREWARM_MARGIN` 秒时重新求解。两轮之间
休眠 `CF_SLAYER_PREWARM_INTERVAL`，跟踪进行中的域名以免并发重复求解，失败计入
`/stats` 的 `prewarm_errors` 而不中断循环。预热绕过缓存读取（`force=True`），
因此仍有效但临近过期的凭据会被真正续期。

## Go 客户端（v4）

`client-go/` 是热路径的原生实现：只用 Go 标准库，无 CGO、无第三方依赖，
要求 Go 1.23+。

```bash
cd client-go
go test ./...
go build -o demo.exe ./examples/demo

# 单实例
CF_SLAYER_AUTH_TOKEN=... ./demo.exe -url https://target.example/ -n 5

# 多 worker：按域名粘性 + 故障摘除
CF_SLAYER_SERVICES=http://127.0.0.1:8878,http://127.0.0.1:8879 \
CF_SLAYER_AUTH_TOKEN=... ./demo.exe -url https://target.example/ -n 20
```

- `Client`：`Solve` 调 `/solve`，按域名缓存在内存（`sync.Map`），TTL 取
  `expires_in`，并在 80% 处后台续期；命中缓存不产生任何 HTTP。`Do` 注入
  `cf_clearance` 与求解时的精确 User-Agent；遇 403 作废缓存、重解一次、重试
  一次，仍是 403 就原样返回。请求体不可重放时（无 `GetBody`）不重试。
- `Pool`：`CF_SLAYER_SERVICES` 为逗号分隔列表。一致性哈希让同一域名粘住同一
  worker（服务端按域缓存因此才能命中）。连续 3 次失败（连接拒绝/5xx/超时）
  即从哈希环摘除；后台每 5 秒 `GET /health`，连续 2 次健康后自动回归。
  `Status()` 暴露 `{URL, State, ConsecutiveFails}`。全部摘除时返回
  `ErrNoWorkers`，不做无限重试。
- 测试用 `httptest` 假服务，覆盖缓存命中、提前续期、403 恰好重解一次、连续
  3 次 500 摘除、探针恢复、哈希稳定性，以及日志不含凭据。
- 令牌只从 `CF_SLAYER_AUTH_TOKEN` 读取；凭据与 UA 从不写日志。

v4 本地实测（`127.0.0.1` 假目标服务加两个真实 Python worker，**不是**真实
Cloudflare 站点）：Python `/replay` 10 次中位数约 11.1 ms（7.7–12.6 ms）；
Go 客户端回放 10 次中位数约 1.1 ms（0.6–1.8 ms）。差别来自绕开 FastAPI 与
Python 请求栈，只代表本机热路径开销。

## 多 worker 运行时（v4）

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run-workers.ps1
```

默认启动 2 个 uvicorn（8878、8879），共享同一个 `cache.sqlite3`（WAL）。
`-Count`、`-BasePort`、`-CachePath`、`-Token` 可调；`-NoWait` 只打印 PID。
令牌不会被打印。

跨进程缓存已验证：worker A 对 `127.0.0.1` 假目标完成一次真实 Camoufox 求解
（34.8 秒）后，worker B 对同一域名 `/solve` 返回 `cached=true`、
`expires_in=1796`，用时 153 ms。共享依赖 SQLite WAL，不依赖进程内状态；
v4 没有改动存储层，也未观察到写锁阻塞。

## Turnstile 令牌（v5）

`POST /solve` 现在有两条路：`want=clearance`（默认）与 `want=turnstile`。

```bash
curl -s http://127.0.0.1:8878/solve \
  -H "Authorization: Bearer $CF_SLAYER_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"want":"turnstile","url":"https://target.example/login","sitekey":"0x4AAAAAAA..."}'
```

```json
{
  "want": "turnstile",
  "token": "0.xxxxx…",
  "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) …",
  "domain": "target.example",
  "solve_seconds": 8.4,
  "cached": false
}
```

- `sitekey` 必填，格式为 `^0x[0-9a-zA-Z]{20,}$`；缺失或非法直接
  `BAD_REQUEST`，浏览器不会启动。URL 在启动前完成绝对 http(s) 校验。我们
  无法离线确认 sitekey 注册在哪个域名，因此不做注册校验；传错域名会让 CF
  组件自己失败。
- **一次性**：令牌首次使用即被消费。服务端不读也不写 SQLite；客户端不缓存、
  不提前续期。每次 `/solve` 都是一次全新的浏览器求解，`cached` 恒为
  `false`。
- 令牌绑定求解会话的 User-Agent；回放时必须同时使用返回的 `user_agent`。
- 后端链只尝试实现了令牌能力的后端；不支持的后端记入 `attempts` 并跳过。
  全部不支持时返回 `SOLVE_FAILED`，错误体同样带 `attempts`。
- Go 客户端：`SolveTurnstile(ctx, url, sitekey) (Token, error)`，不缓存、不
  续期；`Do` 仍只用于 clearance 回放。
- 统计新增 `tokens_solved`、`token_errors`，与 clearance 的计数分开。

诚实说明：**没有在真实 Cloudflare 站点上验证成功**。原因：官方 always-pass
测试 sitekey 以 `1x` 开头（`1x00000000000000000000AA`），而本服务的契约只接受
`0x` 开头的正式 sitekey；该请求会被 `BAD_REQUEST` 提前拒绝。上述行为由 84 个
离线 Python 测试与 20 个 Go 测试覆盖；浏览器路径只用假页面验证了组件加载、
轮询、关闭顺序与超时。

## 已验证

2026-11-12 真实站点 E2E，目标 `https://steamdb.info/`（Cloudflare 托管），
Camoufox v156.0.1-beta.36，Python 3.13.16。完整记录见
[`docs/e2e-v1.md`](docs/e2e-v1.md)。

| 步骤 | 结果 | 实测 |
|---|---|---|
| `POST /solve`（冷） | 200, `cached=false` | 求解 12.94 秒，HTTP 12.98 秒 |
| `POST /solve`（热） | 200, `cached=true` | HTTP 0.0038 秒 |
| `GET /replay` | 200, `challenge_page=false` | 194,161 字节，0.265 秒 |

v2 验收（2026-11-12，同一机器，仅离线/本地）：

| 检查 | 结果 |
|---|---|
| 完整离线套件 | 70 通过，ruff 干净（v1 的 36 项不变） |
| 仅 pydoll 且 `CF_SLAYER_CHROME_PATH` 指向不存在路径 | 502 `SOLVE_FAILED`，`attempts[0].backend="pydoll"`，错误里含该路径；启动不受影响 |
| 预热对接本地发饼干服务器，`TTL=30 秒`，间隔 5 秒 | 18.5 秒内 2 次续期，`cached_domains=1`，`prewarm_errors=0`；消费方 `/solve` 返回 `cached=true`，剩余 28 秒，`cache_hits=1` |
| v4：`go test ./...` | 15 个离线测试通过（无浏览器、无网络） |
| v4：跨进程缓存 | worker A 求解 34.8 秒后，worker B 同域 `/solve` 返回 `cached=true`，耗时 153 ms |
| v4：demo 中途杀 worker | 回放持续 200；池状态第 68 次由 `healthy -> down`，重启 worker 后第 150 次由 `down -> healthy` |
| v5：`pytest` | 84 个离线测试通过（新增 14 个 Turnstile 用例） |
| v5：`go test ./...` | 20 个离线测试通过（新增 5 个令牌用例） |

pydoll 后端本身已对 `https://example.com` 实跑（Chrome 157 启动、读饼干、
读 `navigator.userAgent`）；尚未通过真实 Cloudflare 挑战，因此视作回退而非替代。

## 测试

```bash
.venv/Scripts/python -m pytest     # 84 个离线测试，无浏览器，无网络
.venv/Scripts/ruff check .
cd client-go && go test ./...      # 20 个 Go 离线测试，仅标准库
```

引擎测试注入假浏览器工厂；API 测试注入假后端并 monkeypatch `solver.main.回放`。
链与预热测试用桩后端和临时 SQLite 缓存。任何测试都不访问受 Cloudflare 保护的
站点；本机无 Chrome 时 pydoll 探测测试跳过。

## 项目结构

```
solver/
  main.py      FastAPI 应用 + 服务：鉴权、缓存/求解/回放编排、统计
  engine.py    后端协议 + 尝试记录 + Camoufox后端
  chain.py     后端链：有序回退、跳过不可用、汇总尝试
  pydoll_backend.py  Pydoll后端：CDP 驱动本机 Chrome、Chrome/UA 探测
  prewarm.py   预热器：按余量的续期循环
  cache.py     SQLite缓存：WAL、TTL、线程安全、按域名
  config.py    pydantic-settings + 首启生成令牌写入 .env
  errors.py    错误码与 HTTP 状态映射
client/
  replay.py    curl_cffi 回放（impersonate="firefox"）+ 挑战页检测
client-go/
  client.go    Go 客户端：求解、按域缓存、80% 提前续期、403 自愈
  pool.go      一致性哈希、失败摘除、健康探针、池状态
  examples/demo/main.go  演示 CLI（solve → N 次回放 → 池状态）
scripts/
  run-workers.ps1  多 worker 启动器（共享同一个 SQLite 缓存）
tests/
  test_cache.py
  test_engine_offline.py
  test_api.py
  test_replay.py
  test_chain.py
  test_pydoll.py
  test_prewarm.py
```

## 限制（v2）

- 不产出 Turnstile / reCAPTCHA / AWS WAF 令牌。
- pydoll 是尽力而为：它改写 UA，但原版 Chrome 没有 Camoufox 那类指纹补丁，
  可能被更狠地挑战。默认链中 Camoufox 始终第一。
- 预热是进程内、基于间隔的，不是持久调度器：随应用启动、每轮执行、随进程重启。
  每次续期都是一次完整求解，消耗与请求相同的后端预算。
- 没有代理池与出口固定；绑定 IP 的凭据仅在求解主机上可用。
- 每次求解一个浏览器进程（无池化）；并发求解会扇出多个浏览器，不排队。
- `/health` 在应用起来后即报 `ready`，不探测浏览器二进制（必须先跑过
  `python -m camoufox fetch`，pydoll 还需要 Chrome）。可用性由链在每次求解时检查。
- 未安装浏览器时 Camoufox 会在 fetch 时查询 GitHub 发布元数据。若该调用被限流，
  `fetch` 报 "Synced 0 versions"，求解以 `SOLVE_FAILED` 失败；导出
  `GITHUB_TOKEN` 可解除限制（Camoufox 会转发给 GitHub API）。

## 路线图（v3 推迟、v4 已交付 Go 侧）

v3 的代理池按计划推迟：当前出口 IP 干净，且没有代理资源。

- **FlareSolverr 后端。** 可选的 HTTP 后端，对接 FlareSolverr 实例；同一
  `Backend` 协议，排在 Camoufox 之后。
- **Rust cfsolve 后端。** 纯协议求解器，作为最终回退。
- **代理池。** 按域名粘性出口并带健康评分，使求解与回放共用同一 IP；让服务可在
  求解主机之外使用。

## 说明：标识符也是中文

内部实现（模块内变量、函数、类、测试名、日志、异常消息）全部使用中文标识符，
这是刻意的整活。公开契约保持 ASCII 不变：JSON 键、错误码、环境变量名、SQL
表/列名、端点路径、HTTP 头、`配置` 的字段名（pydantic 环境映射依赖它们）、
模块路径与 ASGI 属性 `app`。因此现有客户端与首次运行行为不受影响。

## 许可证

MIT。
