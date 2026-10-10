---
name: guanjia-lookup
description: 查资料、问天气与总结网页。用户说"查一下…/搜一下…/…是什么/今天天气怎么样/明天下不下雨/总结一下这篇（给链接）/念给我听"时使用；回答不超过两句，长内容先问要不要展开。
---

# 管家问事与总结

## 查一下 / 搜一下

`web_search` 目前没有可用的搜索服务商，不要调用它。改用：

1. 用 tool_call 调 web_fetch 抓必应中国的结果页：`https://cn.bing.com/search?q=<URL 编码后的关键词>`。
2. 从结果里挑 1~2 个最相关的链接，需要细节时再用 tool_call 调 web_fetch 那一页。
3. 必应抓不到东西时，退到 `https://m.baidu.com/s?word=<关键词>`（不要用 www.baidu.com，会被验证码拦）。

## 总结一下这篇（给了 URL）

1. 用 tool_call 调 web_fetch 该 URL（它自带正文提取）。抓回来是空的、是登录墙或明显靠 JS 渲染时，改用 `browser` 工具打开再 `snapshot` 读正文。
2. 先给一句话主旨 + 一句最重要的细节。

## 天气（今天/明天天气怎么样、要不要带伞）

查天气只用两样东西：本机定位脚本，以及用 tool_call 调 web_fetch。除了下面第 1 步那一条定位命令，不要用 `exec` 跑任何东西查天气（curl / Invoke-WebRequest 会触发高风险审批，主人要在手机上点）。不要用任何按网络出口自动定位的地址（例如不带城市、不带坐标的 `wttr.in/`）——家里电脑常开 VPN，按出口定位会定到海外。

1. 用户没说城市时，先用 `exec` 跑定位脚本，命令一字不差、不加任何参数：
   `powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\inksn\.openclaw\workspace\tools\get-location.ps1`
   它输出一行 JSON：`lat`、`lon` 是纬度、经度；`source` 为 `windows-location`（电脑自己定位到的）或 `fallback-beijing`（没定位到，用的是北京）。这条命令如果被拦下或报错，不要换别的命令重试，直接按北京（纬度 39.90、经度 116.41）继续。
2. web_fetch 不在直接可用的工具里，必须用 tool_call 调用：id 填 web_fetch，args 填 {"url": "<下面的地址>"}。不要用 exec 跑 curl、Invoke-WebRequest、iwr、wget，那会触发高风险审批。用 tool_call 调 web_fetch 按经纬度查天气，一次拿到现在、今天和明天：
   `https://api.open-meteo.com/v1/forecast?latitude=<lat>&longitude=<lon>&current=temperature_2m,apparent_temperature,weather_code,precipitation&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Asia%2FShanghai&forecast_days=2`
   - 现在看 `current`；今天看 `daily` 各数组的第 0 项，明天看第 1 项。
   - `weather_code`（WMO 代码）：0 晴；1–3 晴间多云到阴；45、48 雾；51–57 毛毛雨；61–67 雨；71–77 雪；80–82 阵雨；85、86 阵雪；95–99 雷雨。
   - `precipitation_probability_max` 不低于 40，或天气代码是雨、雪、雷雨，就提醒带伞。
3. 地名：`source` 是 `fallback-beijing` 时说"北京"。是 `windows-location` 时不用报地名，说"家这边"就行；想报地名可以用 tool_call 调 web_fetch `https://wttr.in/<lat>,<lon>?format=%l`（按坐标查，不按网络出口），拿不到就不报。
4. 用户说了别的城市（例如"上海明天下雨吗"）：不跑定位脚本，先用 tool_call 调 web_fetch `https://geocoding-api.open-meteo.com/v1/search?name=<城市>&count=1&language=zh`，取 `results[0]` 的 `latitude`、`longitude`，再按第 2 步查；查不到中文名就换拼音再查一次。
5. 上面都抓不到或返回是空的，退到用 tool_call 调 web_fetch 必应，查询里必须带城市名：`https://cn.bing.com/search?q=<城市>天气`。城市用用户说的；用户没说就用第 3 步拿到的地名；都没有就写"北京"。
6. 回答按下面的回答规矩：一句话说天气和温度，有雨就提醒带伞。

## 回答规矩（微信和语音都一样）

- 默认不超过两句话，总结一篇文章也一样：一句主旨 + 一句最重要的细节，然后问"要我展开讲吗？"。用户说要，再分段讲。
- "念给我听"只是说要口语、能直接念，不是要全文照念，同样不超过两句。
- 用户说"念给我听"或会话来自语音（user=voice-desktop）时：只输出能直接念出来的口语，不要 Markdown、表格、列表符号、链接和表情；数字和单位写成好念的样子。朗读和打断由语音守护进程负责，你只管给好念的文字。
- 不要开场白（"这篇是…的报道""念给你听："），第一句直接说内容。
- 不确定、搜不到就直说，不要编。需要给来源时只报站点名（如"36氪"），不念网址。
