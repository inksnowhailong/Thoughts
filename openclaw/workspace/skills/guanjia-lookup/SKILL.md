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

**先用 get_weather，一次就够：** 调 get_weather（工具列表里没有时才用 tool_call，id: get_weather）。用户没说城市参数填 {}（它自己用电脑定位，没定位到按北京）；说了城市 args 填 {"city": "上海"}。它直接返回一句话的天气（现在、今天、明天、要不要带伞），照着回答即可。返回"没查到「…」这个地方"就把城市换成拼音再调一次；返回"天气服务连不上"才走下面的备用步骤。

不要用任何按网络出口自动定位的地址（例如不带城市、不带坐标的 `wttr.in/`）——家里电脑常开 VPN，按出口定位会定到海外。

**备用步骤（get_weather 连不上时）：**

1. 用户没说城市时，先调 get_location（参数 {}；工具列表里没有时才用 tool_call，id: get_location）。它返回纬度、经度，以及是电脑自己定位到的还是"没定位到，按北京"。不要用 exec 跑定位脚本。
2. web_fetch 不在直接可用的工具里，必须用 tool_call 调用：id 填 web_fetch，args 填 {"url": "<下面的地址>"}。用 tool_call 调 web_fetch 按经纬度查天气，一次拿到现在、今天和明天：
   `https://api.open-meteo.com/v1/forecast?latitude=<lat>&longitude=<lon>&current=temperature_2m,apparent_temperature,weather_code,precipitation&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Asia%2FShanghai&forecast_days=2`
   - 现在看 `current`；今天看 `daily` 各数组的第 0 项，明天看第 1 项。
   - `weather_code`（WMO 代码）：0 晴；1–3 晴间多云到阴；45、48 雾；51–57 毛毛雨；61–67 雨；71–77 雪；80–82 阵雨；85、86 阵雪；95–99 雷雨。
   - `precipitation_probability_max` 不低于 40，或天气代码是雨、雪、雷雨，就提醒带伞。
3. 地名：get_location 说"没定位到，按北京"时说"北京"。是电脑自己定位到的时不用报地名，说"家这边"就行；想报地名可以用 tool_call 调 web_fetch `https://wttr.in/<lat>,<lon>?format=%l`（按坐标查，不按网络出口），拿不到就不报。
4. 用户说了别的城市（例如"上海明天下雨吗"）：不调 get_location，先用 tool_call 调 web_fetch `https://geocoding-api.open-meteo.com/v1/search?name=<城市>&count=1&language=zh`，取 `results[0]` 的 `latitude`、`longitude`，再按第 2 步查；查不到中文名就换拼音再查一次。
5. 上面都抓不到或返回是空的，退到用 tool_call 调 web_fetch 必应，查询里必须带城市名：`https://cn.bing.com/search?q=<城市>天气`。城市用用户说的；用户没说就用第 3 步拿到的地名；都没有就写"北京"。
6. 回答按下面的回答规矩：一句话说天气和温度，有雨就提醒带伞。

## 回答规矩（微信和语音都一样）

- 默认不超过两句话，总结一篇文章也一样：一句主旨 + 一句最重要的细节，然后问"要我展开讲吗？"。用户说要，再分段讲。
- "念给我听"只是说要口语、能直接念，不是要全文照念，同样不超过两句。
- 用户说"念给我听"或会话来自语音（user=voice-desktop）时：只输出能直接念出来的口语，不要 Markdown、表格、列表符号、链接和表情；数字和单位写成好念的样子。朗读和打断由语音守护进程负责，你只管给好念的文字。
- 不要开场白（"这篇是…的报道""念给你听："），第一句直接说内容。
- 不确定、搜不到就直说，不要编。需要给来源时只报站点名（如"36氪"），不念网址。
