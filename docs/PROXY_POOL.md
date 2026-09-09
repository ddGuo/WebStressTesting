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

## 五、免费源抓取（可选）

优先级建议：**文件/API 导入 > 免费源爬取**。内置 3 个可插拔免费源，
默认开启（`PROXYPOOL_SOURCES=kuaidaili,ip3366,66ip`，逗号分隔，置空即禁用）：

| 源 | 名称 | 说明 |
|---|---|---|
| 快代理免费 | `kuaidaili` | HTML 表格解析 |
| 云代理 | `ip3366` | HTML 表格解析 |
| 66IP | `66ip` | 文本行解析 |

免费源质量参差、反爬频繁，抓取结果同样要过校验；建议生产用付费/自有的稳定代理。

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
| `PROXYPOOL_SOURCES` | kuaidaili,ip3366,66ip | 免费源（空=关闭爬取） |

## 七、自检

```bash
python scripts/self_check_proxypool.py
```

（启动本地目标与 3 个本地代理 → 代理池服务 → 导入校验 → 压测验证“一用户一 IP” → 可选 MySQL 往返）
