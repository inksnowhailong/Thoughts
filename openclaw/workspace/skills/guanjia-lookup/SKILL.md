---
name: guanjia-lookup
description: 查资料、问天气与总结网页。用户说"查一下…/搜一下…/…是什么/今天天气怎么样/明天下不下雨/总结一下这篇（给链接）/念给我听"时使用；回答不超过两句，长内容先问要不要展开。
---

# 管家问事与总结

## 查一下 / 搜一下

`web_search` 目前没有可用的搜索服务商，不要调用它。改用：

1. `web_fetch` 抓必应中国的结果页：`https://cn.bing.com/search?q=<URL 编码后的关键词>`。
2. 从结果里挑 1~2 个最相关的链接，需要细节时再 `web_fetch` 那一页。
3. 必应抓不到东西时，退到 `https://m.baidu.com/s?word=<关键词>`（不要用 www.baidu.com，会被验证码拦）。

## 总结一下这篇（给了 URL）

1. `web_fetch` 该 URL（它自带正文提取）。抓回来是空的、是登录墙或明显靠 JS 渲染时，改用 `browser` 工具打开再 `snapshot` 读正文。
2. 先给一句话主旨 + 一句最重要的细节。

## 天气（今天/明天天气怎么样、要不要带伞）

不要用 `exec` 跑 curl / Invoke-WebRequest 查天气（会触发高风险审批，主人要在手机上点）。一律用 `web_fetch`：

1. 现在的天气：`web_fetch` `https://wttr.in/?lang=zh&format=%l:+%C+%t+feels+%f+humidity+%h+wind+%w+rain+%p`（按家里网络出口自动定位，返回一行）。
2. 问今天全天或明天：`web_fetch` `https://wttr.in/?lang=zh&format=j1`，今天看 `weather[0]`、明天看 `weather[1]`，取 `maxtempC` / `mintempC` 和 `hourly[].chanceofrain` 的最大值。
3. 用户说了城市，就把城市名放进路径：`https://wttr.in/北京?lang=zh&format=...`。
4. 上面抓不到或是空的，退到 `web_fetch` 必应：`https://cn.bing.com/search?q=今天天气`（有城市就加上城市名）。
5. 回答按下面的回答规矩：一句话说天气和温度，有雨就提醒带伞。

## 回答规矩（微信和语音都一样）

- 默认不超过两句话，总结一篇文章也一样：一句主旨 + 一句最重要的细节，然后问"要我展开讲吗？"。用户说要，再分段讲。
- "念给我听"只是说要口语、能直接念，不是要全文照念，同样不超过两句。
- 用户说"念给我听"或会话来自语音（user=voice-desktop）时：只输出能直接念出来的口语，不要 Markdown、表格、列表符号、链接和表情；数字和单位写成好念的样子。朗读和打断由语音守护进程负责，你只管给好念的文字。
- 不要开场白（"这篇是…的报道""念给你听："），第一句直接说内容。
- 不确定、搜不到就直说，不要编。需要给来源时只报站点名（如"36氪"），不念网址。
