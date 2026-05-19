# hermes-companion (v0.2 — emotion + heartbeat + inference)

实现 [core.md](../core_idea/core.md) 中除"sender 隔离 (v0.3)"以外的全部功能：

| 能力 | 机制 | 文件 |
|---|---|---|
| 时间感知 | `pre_llm_call` 注入 | [companion/time_context.py](companion/time_context.py) |
| 情感状态注入 | `pre_llm_call` 读 EMOTION_STATE.md → user message | [companion/emotion_state.py](companion/emotion_state.py) |
| 工具失败下调 valence | `post_tool_call` | [plugin/hooks.py](plugin/hooks.py) |
| 情感自动推断 (v0.4) | `post_llm_call` → 后台线程 → `agent.auxiliary_client.call_llm` | [companion/emotion_inference.py](companion/emotion_inference.py) |
| 主动消息 — 策略 1 | 插件内后台线程 + `ctx.inject_message()`（CLI/TUI 无延迟） | [plugin/__init__.py](plugin/__init__.py) |
| 主动消息 — 策略 2 | 独立进程 → 队列文件 + `fcntl.flock` → `pre_llm_call` drain | [companion/heartbeat.py](companion/heartbeat.py) |
| Slash 命令 | `/mood`  `/mood-set`  `/heartbeat` | [plugin/commands.py](plugin/commands.py) |

> 主动消息触发逻辑：`arousal > HERMES_COMPANION_AROUSAL_THRESHOLD`（默认 0.75）
> 或每天 09:00 一次早安。Gateway 模式下 `inject_message` 不可用时自动降级到队列。

## 安装

```bash
ln -s "$PWD/plugin" ~/.hermes/plugins/hermes-companion
hermes plugins enable hermes-companion
hermes plugins list      # 确认 enabled
```

## 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `HERMES_TIMEZONE` | 系统本地 | 时间标签时区。**优先级**：`hermes_time` 模块解析（与 Hermes 主程序一致）> `HERMES_TIMEZONE` > `~/.hermes/config.yaml: timezone` > 系统时区。Hermes 不可导入时走 env/config 兜底。参考 [hermes-time_perception-extension](https://github.com/gejifeng/hermes-time_perception-extension) |
| `HERMES_COMPANION_INFERENCE` | `1` | `0` 关闭情感自动推断 |
| `HERMES_COMPANION_INFERENCE_INTERVAL` | `60` | 两次推断最小间隔（秒） |
| `HERMES_COMPANION_HEARTBEAT` | `1` | `0` 关闭插件内 heartbeat 线程 |
| `HERMES_COMPANION_HEARTBEAT_INTERVAL` | `300` | 心跳检查间隔（秒） |
| `HERMES_COMPANION_AROUSAL_THRESHOLD` | `0.75` | 触发主动消息的 arousal 阈值 |
| `HERMES_HOME` | `~/.hermes` | 所有状态文件根路径 |

## Slash 命令

```text
/mood                         显示当前情感状态
/mood-set 0.6 0.3 content "完成了一个困难问题"
/heartbeat                    显示队列状态
/heartbeat push 测试一条主动消息
```

## 数据文件（运行时位于 `$HERMES_HOME/`）

- `EMOTION_STATE.md` — JSON 块，被 `pre_llm_call` 每 turn 读取
- `companion_pending.txt` — 主动消息队列（策略 2 降级用）

## 独立 heartbeat 进程（可选，与插件内线程二选一）

```bash
python -m companion.heartbeat &
# 关掉插件内线程避免重复：
export HERMES_COMPANION_HEARTBEAT=0
```

## 主动消息的两条路径

```text
Strategy 1 (推荐, CLI/TUI):
  plugin thread → ctx.inject_message(content, role="user")
    └─ idle:    排为下一输入
    └─ running: 中断当前 turn 并插入
    └─ gateway: 返回 False → 自动 fallback 到 enqueue()

Strategy 2 (降级, Gateway / 外部进程):
  external proc → enqueue() → companion_pending.txt
                                  ↓ 下一次用户输入
  pre_llm_call → drain_pending() → 注入到 user message
```

Gateway 定时推送另有原生 `hermes cron --deliver` 路径（零代码，见 core.md §6.2）。

## 测试

```bash
cd hermes-companion
python -m pytest tests/ -v
```

零外部依赖：所有 Hermes 接口在测试中被 mock，可在干净 Python 环境运行。

