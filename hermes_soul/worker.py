"""One bounded Hermes invocation, isolated from the production Gateway/profile."""
import json
import os
from pathlib import Path
import signal
import sys


def main():
    request = json.load(sys.stdin)
    cfg = request["config"]
    os.environ["HERMES_HOME"] = cfg["hermes_home"]
    sys.path.insert(0, cfg["hermes_root"])
    from dotenv import load_dotenv
    load_dotenv(Path(cfg["hermes_home"]) / ".env", override=False)
    from run_agent import AIAgent
    from hermes_cli.config import load_config
    config = load_config()
    model = config["model"]
    overrides = dict(cfg.get("request_overrides", {}))
    if cfg.get("structured_json"):
        overrides["response_format"] = {"type": "json_object"}
    agent = AIAgent(
        model=model["default"], provider=model.get("provider", "custom"),
        base_url=model.get("base_url"), api_key=os.path.expandvars(model.get("api_key", "")),
        enabled_toolsets=[], disabled_toolsets=[], skip_memory=True, skip_context_files=True,
        skip_background_review=True, quiet_mode=True, save_trajectories=True,
        session_id="soul_" + request["call_id"], platform="cli",
        max_iterations=2, max_tokens=cfg.get("max_output_tokens", 1600),
        reasoning_config={"enabled": False, "effort": "none"},
        run_budget_seconds=cfg.get("worker_timeout", 150) - 10,
        request_overrides=overrides,
    )
    signal.signal(signal.SIGTERM, lambda *_: agent.interrupt("soul runtime interruption"))
    try:
        # AIAgent, not a second custom inference client, owns model compatibility and usage.
        result = agent.run_conversation(request["prompt"], system_message=request["system"],
                                        conversation_history=request.get("history") or None)
        if result.get("failed") or result.get("interrupted") or not result.get("final_response"):
            raise RuntimeError("Hermes run failed or was interrupted: " + str(result.get("final_response", ""))[:600])
        output = {"text": result["final_response"], "usage": {k: v for k, v in result.items()
                  if "token" in k or "cost" in k or k == "api_calls"}}
        Path(request["result_path"]).write_text(json.dumps(output, ensure_ascii=False))
    finally:
        agent.close()


if __name__ == "__main__":
    main()
