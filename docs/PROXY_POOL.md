# 🛰 内置代理池 ProxyPool

> 参照 [jhao104/proxy_pool](https://github.com/jhao104/proxy_pool) 架构自建：
> **抓取(Crawler) → 校验(Validator) → 存储(MySQL) → API**，核心原则是**只保留有效代理**。

- 存储：本机 MySQL（库 `proxy_pool`，用户 `proxy_pool`，密码 `123456`，自动建表）
- API：默认 `:5010`，接口兼容 jhao104/proxy_pool
- 压测接入：`--proxy-api` / `--proxy-file` / `--proxy`，粘性模式 = **一个用户一个 IP**
- 语言：中文

---

## 一、快速开始

```bash
# 1. 启动常驻服务（调度 + 校验 + 抓取 + API）
python -m web_stress_testing.proxypool serve
# 可选环境变量覆盖：
#   PROXYPOOL_DB_URL=sqlite:///C:/tmp/pool.db   无 MySQL 时用 SQLite
#   PROXYPOOL_API_PORT=5010  PROXYPOOL_TEST_URL=http://127.0.0.1:8000/health

# 2. 导入代理文件（每行 ip:port 或 protocol://ip:port，入库先校验）
python -m web_stress_testing.proxypool --db-url PROXYPOOL_DB_URL import proxies.txt

# 3. 查看池状态
python -m web_stress_testing.proxypool status

# 4. 压测时使用代理池（每个用户粘性绑定一个 IP）
python -m web_stress_testing https://example.com -u 100 -d 60 --yes     --proxy-api http://127.0.0.1:5010
```

> 全局参数（`--db-url` 等）放在子命令之前：
> `python -m web_stress_testing.proxypool --db-url sqlite:///C:/tmp/pool.db import proxies.txt`

## 二、只保留有效 IP 的三层机制

1. **入库先校验**：`import` / `POST /add` / 爬虫抓到的代理，先经校验器验证再保留；
2. **定时重测打分**：校验成功 `score+1`（上限 10），失败 `score-1`；
   `score < 0` 或连续失败 ≥3 次 → **立即删除**；
3. **过期清理**：超过 TTL（默认 600s）未通过校验的代理会被过滤（`/get` 等不返回）并定期清理。

校验器默认对 `PROXYPOOL_TEST_URL`（默认 `http://www.baidu.com`）发探测请求，
记录延迟与连通性；可配置 `PROXYPOOL_IP_API` 进一步检测出口 IP（匿名性）。

## 三、API 参考（http://127.0.0.1:5010）

| 接口 | 说明 |
|---|---|
| `GET /get?count=N` | 随机返回 N 个有效代理（默认 1） |
| `GET /get_all` | 返回全部有效代理 |
| `GET /count` | 有效代理数量 |
| `GET /get_status` | 池统计（total/valid/invalid） |
| `GET /pop` | 取一个并从池中删除（一次性使用） |
| `GET /delete?ip=&port=` | 删除指定代理 |
| `GET /add?ip=&port=&protocol=` | 手动入库（立即校验，仅保留有效） |
| `GET /healthz` | 存活检查 |

返回示例：
```json
{ "count": 2, "data": [ {"ip": "1.2.3.4", "port": 8080, "protocol": "http", "score": 3, "latency_ms": 180} ] }
```

## 四、压测接入参数

| 参数 | 说明 |
|---|---|
| `--proxy http://ip:port` | 单代理（最简单） |
| `--proxy-file proxies.txt` | 代理文件（每行一个，不经过数据库，直接使用+本地统计） |
| `--proxy-api http://127.0.0.1:5010` | 对接代理池服务（推荐；池缺代理时自动再拉取） |
| `--proxy-sticky` / `--no-proxy-sticky` | 粘性：每个用户固定一个代理 IP（默认开） |

- **一用户一 IP**：用户创建时分配一个代理并保持到结束；目标站点看到的出口 IP 稳定。
- **故障换绑**：某个代理连接失败时，该用户自动换用池内其他健康代理（不影响整体测试）。
- **统计**：`report.json` 的 `proxy_pool` 字段 + HTML 报告“代理池”表格
  （每个代理的成功/失败/平均延迟/状态）。

## 五、免费源抓取（默认开启全部 17 个）

优先级建议：**文件/API 导入 > 免费源爬取**。内置源覆盖
[jhao104/proxy_pool fetcher/sources](https://github.com/jhao104/proxy_pool/tree/master/fetcher/sources)
的全部免费源（解析规则参考其实现，MIT），默认开启，可用
`PROXYPOOL_SOURCES=kuaidaili,ip3366`（逗号分隔）自定义白名单，置空即关闭爬取：

| 源 | 名称 | 类型 | 抓取方式 |
|---|---|---|---|
| 快代理 | `kuaidaili` | HTML | 表格正则（页间 sleep 1s） |
| 云代理 | `ip3366` | HTML | 表格正则（2 页） |
| 89免费代理 | `ip89` | HTML | 表格正则 |
| 开心代理 | `kxdaili` | HTML | 表格解析（2 页） |
| 66代理 | `daili66` | JSON | `www.66daili.com/free/list?page=N&size=15`（网页版接口） |
| 稻壳代理 | `docip` | JSON | `docip.net/data/free.json` |
| FreeVPNNode | `freevpnnode` | HTML+文本 | 表格 + 文本兜底 |
| Geonode | `geonode` | JSON | proxylist API（limit=100） |
| 谷德代理 | `goodips` | HTML | 列表解析 |
| 小幻代理 | `ihuan` | HTML | 表格解析（先取 cookie） |
| Proxifly | `proxifly` | JSON | jsdelivr 数据（仅 CN+http） |
| Roundproxies | `roundproxies` | JSON | RoundAPI（limit=50） |
| SCDN | `scdn` | JSON | table_html/data/文本三重兜底 |
| 站大爷 | `zdaye` | HTML | 最新帖分页（页间 sleep 5s） |
| 66IP（旧） | `66ip` | 文本 | 文本行解析 |
| IP动态 | `ipdongtai` | HTML | 表格解析（td.kdl-table-cell，自动翻页） |
| Proxyscrape | `proxyscrape` | JSON | v4 API（protocolipport，skip 分页） |

### 自代理爬取（防反爬）+ 坏代理兜底

抓取代理站时，爬虫会**轮换使用代理池内校验通过的有效代理作为转发出口**，
避免本机/服务器 IP 被代理站反爬封禁；池为空或
`PROXYPOOL_CRAWL_USE_POOL=0` 时自动退化为直连。

免费代理质量参差，因此抓取请求做了健壮性处理：

- **逐请求多代理尝试**：单个 URL 最多尝试 `PROXYPOOL_CRAWL_PROXY_ATTEMPTS`
  （默认 2）个池内代理，全部失败后**直连兜底**；
- **坏代理自动弃用**：某代理连续 2 次转发失败（超时/4xx-5xx/连接错误/空响应），
  自动弃用 600 秒，避免坏代理拖垮整个源的抓取；已发现概率比用户之前的日志低很多。

### 并发 + 自动翻页（加速）

- **源间并发**：`PROXYPOOL_CRAWL_CONCURRENCY`（默认 5）个源同时抓取；
- **自动翻页**：每源最多翻 `PROXYPOOL_MAX_PAGES`（默认 3）页
  （kuaidaili / ip89 / geonode / roundproxies / scdn / freevpnnode /
  zdaye 等；kuaidaili、zdaye 对页间做了限速 sleep，避免被封锁）。

### 无效源自动过滤

爬虫对每个源维护健康状态：**连续 2 次抓取失败（网络异常或 0 条结果）→
自动进入 3600 秒冷却禁用**，不再反复请求无效网站；冷却到期后自动重试，
恢复后重新纳入抓取。各源实时状态可通过 `serve` 日志或 `source_status()` 查看
（报告中也会输出“已自动过滤”的源列表）。

免费源质量参差、反爬频繁，抓取结果同样要过校验；建议生产用付费/自有的稳定代理。

完整清单（首页/抓取地址/实测状态）见 [`FREE_PROXY_SOURCES.md`](FREE_PROXY_SOURCES.md)。

## 六、配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROXYPOOL_DB_URL` | 无 | `mysql://user:pass@host/db` 或 `sqlite:///path`；默认本机 MySQL |
| `PROXYPOOL_API_HOST/PORT` | 0.0.0.0 / 5010 | API 监听 |
| `PROXYPOOL_TEST_URL` | http://www.baidu.com | 校验连通性目标 |
| `PROXYPOOL_IP_API` | 空 | 出口 IP 探测接口（可选） |
| `PROXYPOOL_TIMEOUT` | 6 | 单代理校验超时（秒） |
| `PROXYPOOL_CONCURRENCY` | 200 | 校验并发数 |
| `PROXYPOOL_FAIL_THRESHOLD` | 3 | 连续失败删除阈值 |
| `PROXYPOOL_TTL` | 600 | 校验有效期（秒） |
| `PROXYPOOL_CHECK_INTERVAL` | 60 | 增量校验周期（秒） |
| `PROXYPOOL_CRAWL_INTERVAL` | 1800 | 爬取周期（秒） |
| `PROXYPOOL_SOURCES` | 全部 17 个（见第五节） | 免费源白名单（空=关闭爬取） |
| `PROXYPOOL_CRAWL_USE_POOL` | 1 | 抓代理站时用池内有效 IP 转发（0=直连） |
| `PROXYPOOL_MAX_PAGES` | 3 | 每源自动翻页上限 |
| `PROXYPOOL_CRAWL_CONCURRENCY` | 5 | 源间并发抓取数 |
| `PROXYPOOL_CRAWL_PROXY_ATTEMPTS` | 2 | 每请求最多尝试几个池内代理（失败后直连兜底） |

## 七、自检

```bash
python scripts/self_check_proxypool.py
```

（启动本地目标与 3 个本地代理 → 代理池服务 → 导入校验 → 压测验证“一用户一 IP” → 可选 MySQL 往返）
