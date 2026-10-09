---
name: guanjia-launch
description: 开游戏/开程序与浏览器找片。用户说"开原神/开LOL/打开某程序"或"找个免费的某某电影/我想看某某"时使用；属 L1 可逆动作，直接做不确认。
---

# 管家开游戏与找片（L1：直接做，不问确认）

## 开游戏 / 开程序（"开 LOL""开原神"）

1. 在 `apps.json`（工作区根目录）里按 names 别名找到对应的英文键，比如 英雄联盟 → `lol`，原神 → `genshin`。
2. 用 `exec` 运行，命令必须一字不差是这个形状（只把最后的键换掉，不加引号、不加别的参数、不串联其它命令，否则会被审批拦下）：

   ```
   powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\inksn\.openclaw\workspace\tools\launch-app.ps1 -Name lol
   ```

3. 看脚本输出的 JSON 回复一句话：
   - `ok:true` → "好，LOL 正在启动。"
   - `error:no_target` → "原神还没登记路径，装好后告诉我路径就行。"（把 note 的意思带上）
   - `error:not_in_map` → "我这儿没有这个程序，要加的话告诉我它的路径。"
   - `error:missing_file` → "那个程序的路径失效了。"
4. 不在 apps.json 里的程序不要想办法用别的命令启动。用户给了新路径时，可以直接编辑 apps.json 加一条（键用英文小写短名），再按第 2 步启动。

## 找片（"找个免费的流浪地球"）

中间步骤只调工具，不要输出"我先看看""让我打开"之类的过程说明；整轮最后只回一句结果。

1. 先用 `web_fetch`（不开浏览器，快）抓：
   `https://cn.bing.com/search?q=<片名> 在线观看 免费`
2. 从结果里挑**播放页**链接（不要影片资料页、新闻页），按优先级：
   - 央视频 / CCTV（`yangshipin.cn`、`tv.cctv.com`）
   - 1905 电影网的播放页（`1905.com/vod/play/`、`vip.1905.com/play/`；`1905.com/mdb/film/` 是资料页，不算）
   - 哔哩哔哩（`bilibili.com/bangumi/play/`、`bilibili.com/video/`）
   - 腾讯视频 `v.qq.com/x/cover/`、爱奇艺 `iqiyi.com/v_`、优酷 `v.youku.com/v_show/`、芒果TV `mgtv.com/b/`
   避开要求下载客户端、弹窗多或明显盗版的站。最多挑 3 个候选。
3. 用 `browser` 工具（openclaw 浏览器，会显示在家里桌面上）打开第一个候选，`snapshot` 看页面：
   - 有播放器/"播放"按钮且没写 VIP、付费、会员专享、仅预告 → 就停在这页，不替用户点播放、登录或付费。
   - 否则 `navigate` 到下一个候选（在同一个标签页里跳，不要开一堆新标签）。
4. 回复一句话："《流浪地球》在哔哩哔哩能免费看，已经在浏览器打开了。"三个候选都不行，就停在最接近的那页并说实话，例如"没找到免费的正片，腾讯视频有但要会员，页面已打开。"
