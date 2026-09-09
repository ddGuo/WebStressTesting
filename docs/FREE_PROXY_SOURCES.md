# 🕸 内置免费代理源清单（17 个）

> 覆盖 jhao104/proxy_pool `fetcher/sources` 全部免费源 + 旧 66IP。
> 解析规则参考 jhao104/proxy_pool（MIT）；所有抓取结果仍需过校验器“只保留有效”。
> 实测状态：2026-09-09 从本机网络抽样一轮的结果；不同网络/时段差异很大，
> 爬虫会自动把连续 2 次无效的源冷却禁用 1 小时（自动恢复），无需人工维护。

## 一、实测可用（本轮抓到代理）

| 源 | 类型 | 首页 | 抓取地址 | 解析 | 本轮数量 |
|---|---|---|---|---|---|
| SCDN | JSON/HTML/文本 | https://proxy.scdn.io/ | https://proxy.scdn.io/get_proxies.php?protocol=&country=&per_page=100&page=1 | table_html/data/文本三重兜底 | 200 |
| Geonode | JSON | https://geonode.com/ | https://proxylist.geonode.com/api/proxy-list?filterLastChecked=10&page=1&limit=100&sort_by=lastChecked&sort_type=desc | data[].ip/.port | 100 |
| 89免费代理 | HTML | https://www.89ip.cn/ | https://www.89ip.cn/index_1.html | 表格正则 | 40 |
| Roundproxies | JSON | https://roundproxies.com/free-proxy-list | https://roundproxies.com/api/get-free-proxies/?limit=50&page=1&sort_by=lastChecked&sort_type=desc | data[].ip/.port | 50 |
| FreeVPNNode | HTML+文本 | https://cn.freevpnnode.com | https://cn.freevpnnode.com/free-proxy/ | 表格 + 文本兜底 | 30 |
| 云代理 ip3366 | HTML | http://www.ip3366.net/ | http://www.ip3366.net/free/?stype=1 / ?stype=2 | 表格正则（2 页） | 30 |
| 小幻代理 ihuan | HTML | https://ip.ihuan.me/ | https://ip.ihuan.me/（先取 cookie 再抓） | 表格解析 | 18 |
| 谷德代理 goodips | HTML | https://www.goodips.com/ | https://www.goodips.com/ | 列表解析 | 15 |
| 66代理 daili66 | JSON | https://www.66daili.com/ | https://www.66daili.com/free/list?page=N&size=15 | data[].ip/.port/.protocol | 232 |
| IP动态 ipdongtai | HTML | https://www.ipdongtai.com/ | https://www.ipdongtai.com/free/N | 表格解析（td.kdl-table-cell） | 24 |

## 二、本轮抽样无效（可能反爬/暂时为空，已自动冷却）

| 源 | 类型 | 首页 | 抓取地址 | 解析 | 备注 |
|---|---|---|---|---|---|
| 快代理 kuaidaili | HTML | https://www.kuaidaili.com | https://www.kuaidaili.com/free/inha/1/ 等 | 表格正则（页间 sleep1s） | 反爬较强，需 UA/频率控制 |
| 开心代理 kxdaili | HTML | http://www.kxdaili.com/dailiip.html | http://www.kxdaili.com/dailiip.html / dailiip/2/1.html | 表格解析（2 页） | 可能临时失效 |
| 稻壳代理 docip | JSON | https://www.docip.net/ | https://www.docip.net/data/free.json | data[].ip | 本轮连接被拒 |
| Proxifly | JSON | https://proxifly.dev/ | https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.json | 仅 CN + http | jsdelivr 国内网络偶不可达 |
| 站大爷 zdaye | HTML | https://www.zdaye.com/dayProxy.html | https://www.zdaye.com/free/ → 最新帖详情页 | 分页（页间 sleep5s） | 依赖最新帖时间戳 |
| 66IP（旧） | 文本 | http://www.66ip.cn/ | http://www.66ip.cn/mo.php?tqsl=200 | 文本行解析 | 站点常年不稳 |
| Proxyscrape | JSON | https://proxyscrape.com/ | https://api.proxyscrape.com/v4/free-proxy-list/get?request=get_proxies&proxy_format=protocolipport&format=json&limit=N&skip=M | proxies[]（protocolipport） | 当前网络不可达，连通用后自动生效 |

## 三、快捷操作

```bash
# 自定义白名单（逗号分隔，置空=关闭爬取）
PROXYPOOL_SOURCES=kuaidaili,ip3366,ip89 python -m web_stress_testing.proxypool serve

# 查看某源是否在被冷却禁用（serve 日志 / 心跳里会显示）
```

## 四、说明

- 免费源 = 候选入口，**最终可用性以校验器实测为准**（连通性 + 延迟 + 失败剔除）；
- “无效”不代表永久死掉：冷却 1 小时后自动重试，恢复即重新纳入；
- 生产压测建议以稳定的付费代理 / `--proxy-file` 导入为主，免费源作为补充。
