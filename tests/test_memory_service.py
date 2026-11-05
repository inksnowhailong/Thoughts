"""Phase 4：Memory Service（检索/钳制）+ bridge /internal/memory/search + 导入脚本链。"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guanjia.bridge_service import BridgeApp, make_bridge_server  # noqa: E402
from guanjia.memory.contracts import MemoryCandidate, MemorySource, RetentionLevel  # noqa: E402
from guanjia.memory.service import MemoryService, clamp_content  # noqa: E402


def check(condition: bool, name: str) -> None:
    print(("PASS " if condition else "FAIL ") + name, flush=True)
    if not condition:
        raise SystemExit(1)


def candidate(key: str, content: str, *, importance: int = 5, people: tuple[str, ...] = ()) -> MemoryCandidate:
    return MemoryCandidate(
        key=key, content=content, memory_type="topic",
        source=MemorySource.SYSTEM, confidence=0.9,
        importance=importance, retention=RetentionLevel.LONG_TERM,
        people=people,
    )


with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)

    # 1) clamp_content：按码点截断
    check(clamp_content("a" * 600) == "a" * 500, "clamp_content caps at 500 chars")
    check(clamp_content("  你好  ") == "你好", "clamp_content strips whitespace")

    # 2) MemoryService：写入 + 检索 + 包内钳制
    db = tmp_path / "memory.sqlite3"
    service = MemoryService(db)
    service.write(candidate("topic/fridge", "冰箱顶上有三个纸箱", importance=6))
    service.write(candidate("topic/sofa", "沙发上有黄色和绿色抱枕", importance=7, people=("老婆",)))
    long_content = "长记忆" * 300  # 900 字
    service.write(candidate("topic/long", long_content, importance=8))
    packet = service.search("抱枕")
    check(len(packet.records) == 1 and packet.records[0].key == "topic/sofa", "search matches by keyword")
    packet = service.search("记忆", limit=5)
    top = packet.records[0]
    check(top.key == "topic/long" and len(top.content) == 500, "search clamps content to 500 chars")
    packet = service.search("抱枕", people=("老婆",))
    check(len(packet.records) == 1, "people filter applies")
    packet = service.search("不存在的词xyz")
    check(len(packet.records) == 0, "no match returns empty packet")
    # 多关键词打分：房间布局 + 冰箱 两个词命中 room-layout 记录
    service.write(candidate("topic/room", "房间布局：沙发在右侧客厅区，冰箱在中左区，电脑桌在最左。", importance=7))
    packet = service.search("房间布局 冰箱")
    check(len(packet.records) >= 1 and packet.records[0].key == "topic/room",
          "multi-keyword scoring hits room layout record")
    wire = MemoryService.packet_to_wire(packet)
    check("records" in wire and "query" in wire, "packet_to_wire shape")
    service.close()

    # 3) bridge /internal/memory/search 端到端（HTTP）
    app = BridgeApp(data_dir=tmp_path / "bridge-data")
    server = make_bridge_server(app, port=0, token="tok-test")
    port = server.server_address[1]
    threading_server = server
    import threading

    threading.Thread(target=threading_server.serve_forever, daemon=True).start()
    try:
        app.memory.write(candidate("topic/test", "测试记忆条目"))
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/internal/memory/search",
            data=json.dumps({"query": "测试", "limit": 3}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer tok-test"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        check(body["ok"] is True and body["records"][0]["content"] == "测试记忆条目",
              "bridge memory search returns matching record")
        # 参数校验
        bad = urllib.request.Request(
            f"http://127.0.0.1:{port}/internal/memory/search",
            data=json.dumps({"query": 123}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer tok-test"},
            method="POST",
        )
        try:
            urllib.request.urlopen(bad, timeout=5)
            check(False, "bad query rejected")
        except urllib.error.HTTPError as exc:
            check(exc.code == 400, "bad query rejected with 400")
    finally:
        threading_server.shutdown()
        threading_server.server_close()
        app.store.close()
        app.memory.close()

    # 4) 导入脚本：collect_candidates 契约（key 前缀/来源/钳制）
    sys.path.insert(0, str(ROOT / "scripts"))
    import import_openclaw_memory as importer  # noqa: E402

    importer.WORKSPACE = tmp_path / "workspace"
    ws = importer.WORKSPACE
    (ws / "memory" / "topics").mkdir(parents=True, exist_ok=True)
    (ws / "USER.md").write_text("# USER\n\n- Always 用中文回复主人。\n" * 1, encoding="utf-8")
    (ws / "memory" / "topics" / "room-layout.md").write_text(
        "# 房间布局\n\n沙发在右侧，冰箱在中左。\n", encoding="utf-8")
    candidates = importer.collect_candidates()
    keys = {c["key"] for c in candidates}
    check(any(k.startswith("user/") for k in keys), "USER.md candidates keyed user/")
    check(any(k.startswith("topic/room-layout") for k in keys), "topics candidates keyed topic/<name>")
    check(all(c["source"] == "system" for c in candidates), "import source is system")
    check(all(len(c["content"]) <= 500 for c in candidates), "import content clamped")

print("ALL PASS test_memory_service")
