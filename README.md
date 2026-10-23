# llm-api-compat-check

换 `base_url` 之前，用 5 个请求把「OpenAI 兼容」这四个字验一遍。

不依赖 `openai` SDK，只用 Python 标准库直接打 HTTP —— 目的就是看见 SDK 帮你藏起来的那一层：真实 URL、状态码、流的首尾、错误体的形状。

---

## 它检查什么

| # | 检查项 | 失败的典型表现 |
|---|---|---|
| 1 | `GET /models` 鉴权与清单 | 401 / 403，或返回 0 个模型 |
| 2 | 非流式对话 + usage 完整性 | 通但 `usage` 缺字段，账单对不上 |
| 3 | 流式 + `[DONE]` 结束标志 | 客户端卡住到超时 |
| 4 | `tools` / function calling | 参数被静默忽略，模型只回文本 |
| 5 | 错误体形状 | 200 里包 `error`，重试逻辑失效 |

---

## 用法

```bash
python3 compat_check.py \
  --base-url https://api.example.com/v1 \
  --api-key sk-xxx \
  --model gpt-4o-mini
```

可选参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--timeout` | 60 | 单请求超时（秒）。长思考模型建议 180+ |

输出：

```
base_url = https://api.example.com/v1
model    = gpt-4o-mini
---------------------------------------------------------------------
1. /models 鉴权+清单              PASS  HTTP 200 · 模型 87 个
2. 非流式对话                      PASS  HTTP 200 · 1.2s · 回复 pong · usage 缺 无
3. 流式 + 结束标志                  PASS  HTTP 200 · chunk 24 · [DONE] 有
4. tools / function calling   PASS  HTTP 200 · tool_calls 有
5. 错误形状                       PASS  4xx + error 体（正常）
---------------------------------------------------------------------
结果：5/5 通过
```

退出码：`0` 全通过，`1` 有失败项 —— 可以直接挂在 CI 或上线前脚本里。

---

## 五个检查背后的原理

**1. base_url 拼接**：OpenAI SDK 的规则是 `base_url + "/chat/completions"`，不会自动补 `/v1`。少写 `/v1`，请求就打到 `https://host/chat/completions`，多数网关 404。Base 带尾斜杠还会拼出 `//`，严格路由同样 404。

**2. 鉴权头**：SDK 只发 `Authorization: Bearer`。Azure 要 `api-key`，Anthropic 原生要 `x-api-key`，一些自建网关要 query 参数。跨协议调用时，401 的错误文案经常只写「invalid api key」，很容易误判成密钥错误。

**3. 模型名**：聚合网关常加 `vendor/model` 前缀或做别名。最坏的情况不是 404，而是「智能匹配」到别的模型 —— 花 A 的钱跑 B 的模型。脚本会对比请求名与响应里的 `model` 字段。

**4. 流式**：三处会坏 —— 缺 `[DONE]`（客户端等到超时）、`usage` 不在流里（计费统计为 0，需要 `stream_options.include_usage`）、代理层 `proxy_buffering` 把 SSE 缓冲成一次性响应。

**5. 错误形状**：有的网关统一回 200，失败信息放在 body 的 `error`/`code` 字段里。SDK 认为成功，解析出空 `choices`，你的 `except` 永远不触发。

---

## 它不检查什么

- **长上下文阶梯价、缓存命中价**：属于计价口径，不在兼容性范围内
- **参数是否真的生效**（`temperature`、`response_format`）：需要多次采样对比，见另一篇《多模型 API 成本对比》里的说明
- **图片 / 视频 / 音频接口**：只覆盖文本 chat completions 这一条主路径
- **并发与限流表现**：单请求探测，压测是另一件事

---

## 目录

```
compat_check.py     主脚本，零依赖
```

---

## 关于

由 [LONG AI](https://ai.dijinlong.com/) 维护。我们做的是一个**多卖家报价**的大模型 API 平台：
同一个模型由不同卖家各自挂价，调用方按卖家报价付费，一个密钥、OpenAI 兼容。

这几个脚本最早是内部工具 —— 平台上几百个模型的计价口径要对齐，各家的「OpenAI 兼容」也要逐个验，
后来整理成能独立跑的版本放出来。

这套东西一共三个仓库，并列放在同账号下：

- [`llm-price-compare`](../llm-price-compare) —— 把不同计价口径归一化成一张可比的表
- [`llm-api-compat-check`](../llm-api-compat-check) —— OpenAI 兼容层的 5 项体检
- [`llm-scheduler-demo`](../llm-scheduler-demo) —— 多货源派单：健康分 + 熔断 + 带探索的加权选择
