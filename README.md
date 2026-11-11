[[中文]](README.zh.md) · [[Architecture & Design Whitepaper]](docs/DESIGN.html)

# Hermes_Soul_patch

> **A continuous, evidence-backed life and 12D affective-relational runtime for an AI companion on [Hermes Agent](https://github.com/NousResearch/hermes-agent).**

When the owner is silent, she still lives: she browses, searches, and revisits her saved Xiaohongshu (RedNote) posts along her own evolving interests, reflects on what she actually read, or rests. Between turns, a continuous 12-dimensional ODE evolves her vitality, curiosity, Borbély sleep pressure, relational phase transitions (with bistable hysteresis), and long-term attachment plasticity. She tracks cross-day promises on a real calendar, reaches out naturally over Telegram or QQ (OneBot v11 / NapCat), and—under strict safety guards—can like, favorite, and draft owner-approved comments.

Hermes_Soul_patch is a standalone service plus a Hermes lifecycle plugin. Hermes needs no source changes: it makes the model calls, and this service owns persistent state, dynamical evolution, attention, multi-room routing, and real MCP actions.

> **v0.7** upgrades the v0.5 interest-led life loop with a 12D three-timescale coupled ODE, Borbély sleep-energy coupling, cross-day temporal commitment tracking, OpenAI-compatible hybrid semantic recall + Graph Laplacian topic diffusion, multi-room QQ/Telegram routing, natural IM micro-timing ("typing... -> hesitation -> send"), and rich-media post share-back.

## Features

- **12D Three-Timescale Coupled Dynamics (`dynamics.py`)**:
  - **Self-vitality & Borbély sleep-energy compartment**: valence $V$, arousal $A$, curiosity limit-cycle $C$, and homeostatic sleep pressure $H(t)$ ($\tau_w=18.5\text{h}$ wake accumulation, $\tau_s=4.2\text{h}$ sleep recovery, 14:00 post-lunch dip) integrated via 4th-order Runge-Kutta + exact Ornstein-Uhlenbeck micro-fluctuations.
  - **Relational phase compartment with bistable hysteresis**: attachment $S$, slow baseline $\bar{S}$, grievance $G$, outreach impulse $I$, and pride inhibition $W$ drive natural phase transitions (`attached -> missing -> anxious -> angry -> cold -> detached`). Cusp-catastrophe hysteresis prevents instant "one-message amnesia" after prolonged neglect until genuine multi-turn repair occurs.
  - **15-day structural attachment plasticity ($\tau=360\text{h}$)**: kept promises and conflict repairs build security reserve $T_{\text{sec}}$ (widening future grace windows and softening grievance decay), while broken promises or boundary violations elevate sensitivity $B_{\text{sens}}$.
- **Cross-day Temporal Commitment Engine (`life.py`, `contact.py`)**: relative Chinese time expressions ("this weekend", "tomorrow afternoon", "next Friday") bind to concrete `Asia/Shanghai` calendar windows in a protected `commitment` slot (`scheduled -> anticipating -> fulfilled / overdue`), triggering natural anticipation or disappointment without manual reminders.
- **Provenance-checked memory & multi-scale forgetting**: interests, impressions, relationship beliefs, agreements, boundaries, and journals must cite real observations (`user`, `xiaohongshu`, or `host`). Expired feelings, superseded working-memory slots, and 14-day dormant interests naturally recede from active context while preserving full revision history.
- **OpenAI-compatible Hybrid Semantic Recall (`embedding.py`, optional)**: plug in any `/v1/embeddings` endpoint (local `llama-server` / `vLLM` / `Ollama` e.g. `qwen3-embedding-4b`, or cloud APIs) for hybrid lexical + vector recall, near-duplicate interest consolidation, parked-topic wakeup, promise-fulfillment matching, and Graph Laplacian associative topic diffusion.
- **Multi-room QQ (OneBot v11 / NapCat) & Telegram Bridge (`channels.py`)**:
  - *"One companion, multiple rooms"*: owner DMs (`desk` / `private`) merge into the main `house` study room; group chats and guest DMs route to isolated, auto-rotating rooms that never reset owner neglect timers or leak `private_terms`.
  - **Natural IM style & micro-timing**: strips theatrical stage directions and essay formatting into short, conversational bubbles, simulates realistic typing/hesitation cadence (`/set_input_status`), and supports rich-media `[image + caption]` post share-back plus optional `/v1/audio/speech` voice bubbles.
- **Social layer (optional)**: likes, favorites (including browsing back through saved favorites/likes), comments, and replies, each `off` / `dry_run` / `live`. Public comments are drafted in a zero-private-memory sandbox, checked by hard rules + an independent auditor model, and require **explicit owner approval**.

## Layout

| Path | Contents |
| --- | --- |
| `hermes_soul/` | SQLite store, 12D ODE dynamics, commitment & contact engine, hybrid embedding index, attention loop, multi-room QQ/Telegram channels, delivery, social layer, MCP client, Hermes worker, FastAPI & web admin UI |
| `hermes_plugin/` | Hermes plugin `hermes-soul-bridge` (`pre_llm_call` / `post_llm_call` for Telegram & QQ) |
| `deploy/` | Safe example config, systemd user unit, docker compose for Xiaohongshu MCP |
| `docs/DESIGN.html` | Comprehensive architecture & mathematical design whitepaper |
| `tests/` | 95 unit and integration tests covering memory, dynamics, embeddings, QQ multi-room routing, social safety, and v0.7 simulation |

SQLite is the single source of truth. The service process is its only writer, and other clients go through the HTTP API or CLI.

## Quick start

Requirements: Linux, Python ≥ 3.12, a working Hermes Agent install (and its venv), Docker. Telegram, QQ (NapCat / OneBot v11), and local Embedding servers are all optional.

```bash
git clone https://github.com/gejifeng/Hermes_Soul_patch.git ~/Hermes_Soul_patch
cd ~/Hermes_Soul_patch
mkdir -p ~/.config/hermes-soul && chmod 700 ~/.config/hermes-soul
cp deploy/config.example.yaml ~/.config/hermes-soul/config.yaml   # edit persona, owner ids, optional embedding/qq
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

Open <http://127.0.0.1:9120/> and enter the access token to view live 12D dynamics, manage Xiaohongshu / QQ QR logins, review memories, or approve social actions.

## Configuration

See [`deploy/config.example.yaml`](deploy/config.example.yaml). All defaults are safe:

- Delivery defaults to `draft` (`authorized: false`).
- QQ / OneBot integration, Embedding service, and the Social layer all default to `enabled: false`.
- When the social layer is enabled, every action kind starts in `dry_run`. Live comments and replies require `social.private_terms` to be non-empty, otherwise they automatically fall back to `dry_run`.
- `persona` configures the character's name (`name`), how she refers to the owner (`owner`), and her public interests (`interests`).

## Safety

- Never commit tokens, cookies, or the database or data directory, since they contain private chats.
- Automating Xiaohongshu or QQ may violate platform terms of service and risk rate-limiting or account restrictions. Use dedicated accounts and label public profiles as AI-operated.
- Public text is generated without any private memory, and group/guest replies automatically redact `private_terms` and isolate relational state. Publish, delete, and undo actions on Xiaohongshu are refused at the MCP layer.
- This is an engineered runtime with real inputs, persistent provenance, and dynamical continuity. It makes no claim to human consciousness or inner experience.

## Tests

```bash
~/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests
```

## Credits & license

Xiaohongshu MCP by [xpzouying/xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp). Multi-room boundary & local SVG QR concepts inspired by [ssamurias/dsh-fish](https://github.com/ssamurias/dsh-fish). Licensed under [MIT](LICENSE).
