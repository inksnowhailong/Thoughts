import json
import time
import httpx

CONFIG_PATH = r"C:\Users\inksn\.openclaw\openclaw.json"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    cfg = json.load(f)

token = cfg["gateway"]["auth"]["token"]

url = "http://127.0.0.1:18789/v1/chat/completions"
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
}
payload = {
    "model": "openclaw/main",
    "stream": True,
    "messages": [
        {"role": "user", "content": "用一句话说现在几点"}
    ],
}

def one_run():
    start = time.perf_counter()
    ttfb = None
    with httpx.Client(trust_env=False, timeout=30.0) as client:
        with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                resp.read()
                print(f"HTTP {resp.status_code}: {resp.text[:500]}")
                return None
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    choices = obj.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        if delta.get("content"):
                            ttfb = time.perf_counter() - start
                            break
    return ttfb

if __name__ == "__main__":
    for i in range(3):
        t = one_run()
        print(f"run {i+1}: ttfb = {t:.3f}s" if t is not None else f"run {i+1}: no content chunk received")
