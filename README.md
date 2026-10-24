[[中文]](README.zh.md)

# Hermes_Soul_patch

> **A continuous, evidence-backed life for an AI companion running on [Hermes Agent](https://github.com/NousResearch/hermes-agent).**

When the owner is silent, she still lives: she browses and searches Xiaohongshu (RedNote) along her own interests, reflects on what she actually read, or rests. She notices how long the owner has been away, can miss them and reach out over Telegram. Under strict owner approval, she can like, favorite and comment on posts. Every experience comes from a real tool call and keeps its source.

Hermes_Soul_patch is a standalone service plus a Hermes lifecycle plugin. Hermes needs no source changes: it makes the model calls, and this service owns persistent state, attention and real MCP actions.

> v0.5 is a full rewrite and replaces the earlier `hermes-companion` prototype (emotion state machine / world simulator).

## Features

- **Interest-led life loop**: on each activity opportunity she chooses to browse, search, reflect, continue a commitment, or rest. Browsing is not a job. Journals, interest changes and things to share are all optional outcomes.
- **Provenance-checked memory**: interests, impressions, relationship beliefs, agreements, boundaries and journals must cite real observations. The model only proposes changes, and the host validates them before committing.
- **Contact awareness**: the host records how long the owner has been silent, excluding sleep hours, as a fact. This can make her miss them and reach out with a short burst of messages.
- **Paced Telegram delivery**: at least 1 hour between bursts, about 2 minutes between messages within a burst, at most 4 messages per burst and 32 per day, plus quiet hours. By default she only writes drafts.
- **Social layer (optional)**: likes, favorites, comments and replies, each `off` / `dry_run` / `live`. A comment or reply is written in an isolated context, then passes hard rule checks and an independent model audit, and **the owner approves it** in the admin page before it is sent.
- **Chat continuity**: the plugin injects relevant life context into the owner's Telegram turns, so in conversation she remembers what she has been doing.
- **Local admin page**: shows current situations, interests, journals, pending expressions and social actions, and lets the owner pause, correct records or export data.

## Layout

| Path | Contents |
| --- | --- |
| `hermes_soul/` | Store, attention loop, contact awareness, delivery, social layer, MCP client, Hermes worker, API, admin page |
| `hermes_plugin/` | Hermes plugin `hermes-soul-bridge` (`pre_llm_call` / `post_llm_call`) |
| `deploy/` | Example config, systemd user unit, docker compose for the Xiaohongshu MCP |
| `tests/` | Unit and integration tests |

SQLite is the single source of truth. The service process is its only writer, and other clients go through the HTTP API or CLI.

## Quick start

Requirements: Linux, Python ≥ 3.12, a working Hermes Agent install (and its venv), Docker. A Telegram bot is optional.

```bash
git clone https://github.com/gejifeng/Hermes_Soul_patch.git ~/Hermes_Soul_patch
cd ~/Hermes_Soul_patch
mkdir -p ~/.config/hermes-soul && chmod 700 ~/.config/hermes-soul
cp deploy/config.example.yaml ~/.config/hermes-soul/config.yaml   # then edit persona, owner ids, paths
python3 -c 'import secrets;print(secrets.token_urlsafe(32))' > ~/.config/hermes-soul/access.token
python3 -c 'import secrets;print(secrets.token_urlsafe(32))' > ~/.config/hermes-soul/mcp.token
printf 'AUTH_TOKEN=%s\n' "$(cat ~/.config/hermes-soul/mcp.token)" > ~/.config/hermes-soul/mcp.env
chmod 600 ~/.config/hermes-soul/*

docker compose -f deploy/compose.yaml up -d                       # Xiaohongshu MCP on 127.0.0.1:18060
cp deploy/hermes-soul.service ~/.config/systemd/user/             # adjust paths inside if needed
systemctl --user daemon-reload && systemctl --user enable --now hermes-soul.service
ln -sfn ~/Hermes_Soul_patch/hermes_plugin ~/.hermes/plugins/hermes-soul-bridge
# add hermes-soul-bridge to plugins.enabled in ~/.hermes/config.yaml, then restart the Hermes gateway
```

Background activity uses its own Hermes profile (`~/.hermes/profiles/hermes-soul/`) for model settings. Optional character files go in `~/.local/share/hermes-soul/identity/SOUL.md` and `USER.md`.

Open <http://127.0.0.1:9120/> and enter the access token. Use "获取登录二维码" in the admin page and scan the code with the Xiaohongshu app to log in.

## Configuration

See [`deploy/config.example.yaml`](deploy/config.example.yaml). Defaults are safe:

- Delivery is `draft` and not authorized.
- The social layer is off.
- When the social layer is enabled, every action kind starts in `dry_run`, which records what would have been done without touching the platform.

Live comments and replies also require `social.private_terms`, a list of words that must never appear publicly. Without it they fall back to `dry_run`. The prompts are currently written in Chinese, and `persona` sets the character's name, how she refers to the owner, and her public interests.

## Safety

- Never commit tokens, cookies, or the database or data directory, since they contain private chats.
- Automating Xiaohongshu may violate its terms of service and can get the account limited or banned. Use a dedicated account and label it as AI-operated.
- Public text is generated without any private memory, and a test enforces this. Publish, delete and undo actions are refused at the MCP layer. Platform warnings, or two consecutive failures, suspend all social actions for 24 hours. Uncertain results are never retried.
- This is a runtime loop with real inputs and persistent continuity. It makes no claim to human consciousness or inner experience.

## Tests

```bash
~/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests
```

## Credits & license

Xiaohongshu MCP by [xpzouying/xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp). Licensed under [MIT](LICENSE).
