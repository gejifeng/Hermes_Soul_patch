import argparse
import json
from pathlib import Path
import httpx

from .config import load_config


def main():
    parser = argparse.ArgumentParser(description="Hermes Soul 自主生活运行层")
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("status")
    sub.add_parser("login")
    for name in ("pause", "resume", "wake", "stop", "new", "reset-budget"):
        sub.add_parser(name)
    p = sub.add_parser("chat")
    p.add_argument("text")
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.command == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(cfg), host=cfg.get("host", "127.0.0.1"), port=cfg.get("port", 9120))
        return
    headers = {"Authorization": "Bearer " + Path(cfg["access_token_file"]).read_text().strip()}
    url = f"http://127.0.0.1:{cfg.get('port', 9120)}"
    with httpx.Client(timeout=cfg["worker_timeout"] + 30, trust_env=False, headers=headers) as client:
        if args.command == "status":
            result = client.get(url + "/api/state")
            result.raise_for_status()
            print(json.dumps(result.json()["status"], ensure_ascii=False, indent=2))
            return
        if args.command == "login":
            result = client.get(url + "/api/xhs/status")
        elif args.command == "chat":
            result = client.post(url + "/api/chat", json={"text": args.text})
        else:
            result = client.post(url + "/api/control/" + args.command)
        result.raise_for_status()
        print(json.dumps(result.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
