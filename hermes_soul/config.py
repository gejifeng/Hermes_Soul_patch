from pathlib import Path
import json
import os
import re
import yaml


def load_config(path=None):
    path = Path(path or os.environ.get("HERMES_SOUL_CONFIG", Path.home() / ".config/hermes-soul/config.yaml"))
    data = yaml.safe_load(path.read_text())
    data["config_path"] = str(path)
    for key in ("home", "hermes_root", "hermes_home"):
        data[key] = str(Path(data[key]).expanduser().resolve())
    # Resolving a venv's Python symlink bypasses pyvenv.cfg and loses its packages.
    data["python"] = str(Path(data["python"]).expanduser().absolute())
    for section, key in (
        (data, "access_token_file"),
        (data, "mcp_token_file"),
        (data.get("delivery") or {}, "token_file"),
        (data.get("social") or {}, "persona_file"),
        (data.get("embedding") or {}, "api_key_file"),
    ):
        if section.get(key):
            section[key] = str(Path(section[key]).expanduser())
    data.setdefault("interval_seconds", 720)
    data.setdefault("idle_max_seconds", 720)
    data.setdefault("max_calls_per_day", 500)
    data.setdefault("worker_timeout", 150)
    data.setdefault("max_browse_steps", 3)
    data.setdefault("decision_min_seconds", 1800)
    data.setdefault("decision_max_seconds", 7200)
    data.setdefault("expression_review_min_seconds", 21600)
    data.setdefault("delivery", {"mode": "draft", "authorized": False})
    data.setdefault("embedding", {"enabled": False})
    from .schedule import sleep_until
    sleep_until(data)  # Validate an enabled schedule before starting the service.
    return data


def parse_object(text):
    text = re.sub(r"<\|DELIM_\d+\|>|<PRESIDIO_[A-Z_]+>", "", text.strip())
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("模型必须返回 JSON 对象")
    return value
