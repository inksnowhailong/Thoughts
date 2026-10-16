"""O2 应用索引最小验证：只用 fixture，不读取本机注册表或启动任何应用。"""

import json
import tempfile
from pathlib import Path

from guanjia.app_index import AppCandidate, AppIndex, index_from_apps_json


def check(condition: bool, name: str) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


fixtures = {
    "menu": lambda: [AppCandidate("英雄联盟", "start_menu", r"C:\Menu\League.lnk", ("LOL",), ("LeagueClient.exe",))],
    "paths": lambda: [AppCandidate("Notepad", "app_paths", r"C:\Windows\notepad.exe", ("记事本",), ("notepad.exe",))],
    "apps": lambda: [AppCandidate("网易云音乐", "start_apps", "网易云.AUMID", (), ("cloudmusic.exe",))],
}
index = AppIndex.collect_windows(
    start_menu=fixtures["menu"],
    app_paths=fixtures["paths"],
    start_apps=fixtures["apps"],
    manual_aliases={"英雄联盟": ["撸啊撸"]},
)

check(index.match("lol").kind == "unique", "English alias resolves uniquely")
check(index.match("wangyiyunyinyue").candidates[0].name == "网易云音乐", "pinyin resolves Chinese name")
check(index.match("wyyy").kind == "unique", "pinyin initials resolve Chinese name")
check(index.match("撸啊撸").candidates[0].name == "英雄联盟", "manual alias resolves")
check(index.match("不存在").kind == "not_found", "missing app is explicit")

ambiguous = AppIndex([AppCandidate("Steam", "start_menu"), AppCandidate("Steam Client", "app_paths")])
check(ambiguous.match("ste").kind == "ambiguous", "ambiguous result is not guessed")

league = index.match("英雄联盟").candidates[0]
check(AppIndex.verify(league, process_names=["leagueclient.exe"]).reason == "process", "process verification is pure")
check(AppIndex.verify(league, window_titles=["英雄联盟 客户端"]).reason == "window", "window verification is pure")
check(AppIndex.verify(league).kind == "not_verified", "missing process/window is explicit")

with tempfile.TemporaryDirectory() as tmp:
    apps = Path(tmp) / "apps.json"
    apps.write_text(json.dumps({"apps": {"music": {"names": ["网易云音乐"], "target": r"F:\CloudMusic\cloudmusic.exe"}}}), encoding="utf-8")
    controlled = index_from_apps_json(apps)
    check(controlled.match("wangyiyunyinyue").candidates[0].name == "music", "controlled map supports pinyin fallback")
print("ALL PASS test_app_index")
