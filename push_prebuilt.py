"""把本地预生成的早报 prebuilt/<日期>.md 上传到 GitHub 仓库，供云端定时任务读取发送。"""
import os
import sys
import json
import base64
import urllib.request
import urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO = "zjh-fyy/daily-ai-digest"


def load_github_token() -> str:
    """按优先级获取 GitHub Token：环境变量 -> .env -> .gh_token.json"""
    token = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if token:
        return token

    env_file = BASE_DIR / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k in ("GITHUB_TOKEN", "GH_TOKEN") and v:
                    return v
        except Exception:
            pass

    gh_token_file = BASE_DIR / ".gh_token.json"
    if gh_token_file.exists():
        try:
            data = json.loads(gh_token_file.read_text(encoding="utf-8"))
            token = data.get("access_token") or data.get("token") or ""
            if token:
                return token
        except Exception:
            pass

    raise RuntimeError("未检测到有效的 GitHub Token，请在环境变量、.env 或 .gh_token.json 中配置 GITHUB_TOKEN")


def api(url, token, data=None, method=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "daily-ai-digest",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def pull_remote(token):
    """从仓库拉回 prebuilt/ 下的早报与 history_seen.json（云端生成的早报只存在于仓库），以仓库为准"""
    local_dir = BASE_DIR / "prebuilt"
    local_dir.mkdir(exist_ok=True)
    listing = api(f"https://api.github.com/repos/{REPO}/contents/prebuilt", token) or []
    for item in listing:
        name = item.get("name", "")
        if not (name.endswith(".md") or name == "history_seen.json"):
            continue
        meta = api(item["url"], token)
        if not meta or "content" not in meta:
            continue
        remote_bytes = base64.b64decode(meta["content"])
        local_fp = local_dir / name
        if name == "history_seen.json" and local_fp.exists():
            try:
                local_data = json.loads(local_fp.read_text(encoding="utf-8"))
                remote_data = json.loads(remote_bytes.decode("utf-8"))
                merged = local_data.get("entries", {})
                merged.update(remote_data.get("entries", {}))
                local_fp.write_text(json.dumps({"entries": merged}, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"已合并 prebuilt/{name}")
            except Exception as e:
                print(f"合并 history_seen.json 失败: {e}")
            continue
        if local_fp.exists() and local_fp.read_bytes() == remote_bytes:
            continue
        local_fp.write_bytes(remote_bytes)
        print(f"已拉取 prebuilt/{name}")


def main():
    token = load_github_token()
    if "--pull" in sys.argv:
        pull_remote(token)
        return
    files = sorted((BASE_DIR / "prebuilt").glob("*.md"))
    if not files:
        print("没有可上传的预生成早报")
        sys.exit(1)
    targets = [(fp, f"prebuilt/{fp.name}") for fp in files[-3:]]
    history_fp = BASE_DIR / "prebuilt" / "history_seen.json"
    if history_fp.exists():
        targets.append((history_fp, "prebuilt/history_seen.json"))
    if "--code" in sys.argv:
        targets.append((BASE_DIR / "main.py", "main.py"))
        targets.append((BASE_DIR / ".github" / "workflows" / "daily_digest.yml", ".github/workflows/daily_digest.yml"))
        targets.append((BASE_DIR / "push_prebuilt.py", "push_prebuilt.py"))
        targets.append((BASE_DIR / "README.md", "README.md"))
    for fp, rel in targets:
        url = f"https://api.github.com/repos/{REPO}/contents/{rel}"
        body = {"message": f"update {rel}", "content": base64.b64encode(fp.read_bytes()).decode(), "branch": "main"}
        old = api(url, token)
        if old and "sha" in old:
            body["sha"] = old["sha"]
        api(url, token, body, "PUT")
        print(f"已上传 {rel}")


if __name__ == "__main__":
    main()
