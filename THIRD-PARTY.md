# 第三方代码与修改说明

## `vendor/chatdsn/`

- **来源**：[lampts/chatdsn-python](https://github.com/lampts/chatdsn-python)
- **许可**：MIT，原始许可见 `vendor/CHATDSN-LICENSE`
- **状态**：**已被修改**，不是原版。改动如下（对照上游 diff 可复现）：

| 改动 | 位置 | 原因 |
|---|---|---|
| 新增 `from net import BinanceHttp`，HTTP 层改走本项目实现 | `vendor/chatdsn/binance.py` | 需要「直连 / 代理」双通道与退避重试；上游直接用 `requests` |
| 新增 `_sync_server_time()`：用 `/fapi/v1/time` 校准本地时钟偏移 | `vendor/chatdsn/binance.py` | 本机与交易所差 31 秒，签名请求全部 `-1021` |
| 新增 `_dual_side` 探测（单向 / 双向持仓） | `vendor/chatdsn/binance.py` | 双向账户下单必须带 `positionSide`，否则 `-4061` |
| 入场由市价改限价（在 `better_executor.py` 里覆写） | `better_executor.py` | 市价单等于放弃价格优势 |
| 默认杠杆/滑点参数按本项目口径调整 | `vendor/chatdsn/config.py`、`risk.py` | 与「等比复刻」口径一致 |
| 移除 Telegram / 通知等本项目不需要的路径 | 多处 | 减少攻击面 |

**分发注意**：MIT 要求保留版权声明与许可全文，`vendor/CHATDSN-LICENSE`
必须随仓库一起分发。

## 未使用的参考项目

开发期参考过以下公开仓库，**没有复制它们的代码**，仅用于对照接口与
产品形态：

- `butter991011/Binance_Copy` — 未声明许可，未复制代码
- `Diiak/binance-copy-trade-bot` — 未声明许可，未复制代码
- `tpmmthomas/binance-copy-trade-bot` — 未声明许可，未复制代码

这些仓库的本地副本放在 `_refs/`，该目录被 `.gitignore` 排除，**不在
本仓库内**，也不会随本仓库分发。

## 数据

源码中的带单员 ID、账户余额、示例成交流水均为脱敏后的占位值。
