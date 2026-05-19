# 分布式点火架构 · Idea备忘录

> **核心命题（第三轮修正）**：API 边界上的 Transformer 近似无状态函数；长期 Agent 需要外部状态、动作门控和可审计权限。  
> Agent 层的职责不是把更强的 `soul.md` 塞给 LLM，而是在 LLM 调用前后接管状态写入、候选动作选择和高权限 veto。

---

## 一、问题的起源

### 1.1 Transformer的结构性局限

Transformer 在单次前向调用中缺少外部可审计的持久状态和稳定权限动力学；上下文内的不对称主要来自位置、mask、消息格式与训练分布，而不是长期状态演化机制。因此它在 Agent 场景中暴露出三个结构性缺口：

| 缺陷 | 表现 | 根本原因 |
|------|------|----------|
| **缺少外部权限分层** | 核心信念可能被当前对话冲淡 | 权限主要依赖 prompt/message 约定，缺少持久写保护 |
| **缺少可控时间状态** | API 调用边界上每次推理近似从零开始 | 单次前向 pass 不维护可审计状态演化 |
| **缺少显式竞争动力学** | 模块/工具选择常由 prompt 隐式完成 | 缺少可观察的收敛、拒绝与唯一赢家机制 |

这不是简单的工程 bug，而是**接口边界差异**：Transformer 擅长一次性语义计算，长期 Agent 还需要显式状态演化。

### 1.2 关键洞察

> Transformer attention 可被理解为离散 Hopfield / 关联检索机制的一种形式（Ramsauer 2020）——  
> 这提示我们：不必在模型内部重造一切，可以在 Agent 层外接持久状态、权限不对称和收敛驱动。

---

## 二、架构定位

### 2.1 三层分工

```
┌─────────────────────────────────────────────────┐
│                  Agent 层（本框架）                │
│                                                   │
│   WTA竞争动力学  ·  权限分层  ·  持久状态          │
│   core_state[]    importance[]    x_i(t)         │
│                                                   │
│   ← 维护激活值，跑离散WTA迭代，检测点火条件 →       │
├─────────────────────────────────────────────────┤
│                  LLM 层（Transformer）             │
│                                                   │
│         语义解释 / 候选生成 / 语言表达              │
│         被 Agent 状态层约束与调度                  │
├─────────────────────────────────────────────────┤
│                  执行层                            │
│                                                   │
│            工具调用 · 外部世界交互                  │
└─────────────────────────────────────────────────┘
```

**LLM不再承担状态裁判职责**，而是负责语义解释、候选生成和语言表达。  
状态、权限、路由、候选动作门控与写入裁决由 Agent 层运行。

> **第三轮修正（关键）**：`core_belief` 如果只是点火后注入一段更强 system prompt，本质上仍等价于把 `soul.md` 加入上下文，不能证明外部权限分层。真正的权限分层必须体现在：低权限输入无法直接改写受保护状态，无法越过 Agent 层触发工具/记忆写入，无法把高风险候选动作送入执行阶段。

### 2.2 移除连续时间之后

连续时间ODE → 离散WTA迭代，核心能力保留矩阵：

| 能力 | 需要连续时间？ | 离散替代 |
|------|-------------|---------|
| WTA选出唯一赢家 | ❌ | 离散迭代收敛 |
| 权限分层 | ❌ | 受保护状态 + action gate / veto，而不是仅靠 prompt 权重 |
| 情绪/温度 | ❌ | 外部参数化，上层更新 |
| 可塑性历史积累 | ❌ | 离散Hebbian更新 |
| 点火因果时序 | ⚠️ 部分丢失 | 归一化阈值 + 稳定性判据（不完全等价） |

**结论**：去掉连续时间，失去的是过程的动态性，不是选择能力本身。

---

## 三、核心机制

### 3.1 模块 Schema（最小定义）

每个模块 $i$ 不仅是一个激活值，还携带语义内容：

```python
@dataclass
class Module:
    id: int
    name: str                   # 语义标签，如 "core_belief", "working_mem"
    domain_keywords: list[str]  # 用于规则驱动打分
    domain_embedding: np.ndarray | None  # 用于嵌入相似度打分（可选）
    prompt_template: str        # 语言表达模板；不能被视为权限本身
    action_policy: dict          # 点火后允许/禁止/净化哪些候选动作
    importance: float           # ∈ [floor, 1.0]，写保护强度
    floor_importance: float     # 最低保护值，防止衰减归零
```

**N=4 认知分工示例**：

> 注意：第三轮后，`core_belief` 不再应被理解为“和 narration 竞争的一段 prompt”。它应是高权限约束场，参与候选动作的代价评估、veto 和状态写保护；WTA 可以用于选择活动模式，但真正安全性必须落在 action gate 上。

| 模块 | 语义角色 | 典型 importance | 典型阈值 $\theta$ |
|------|---------|----------------|------------------|
| M0 core_belief | 价值观/不可改写的约束 | 0.90 | 高（0.8） |
| M1 working_mem | 当前任务上下文 | 0.50 | 中（0.5） |
| M2 tool_action | 工具选择与执行 | 0.30 | 低（0.3） |
| M3 narration | 语言渲染 / 用户输出 | 0.20 | 最低（0.2） |

### 3.2 模块激活与WTA竞争（MVP稳定版）

每个模块 $i$ 维护非负激活值 $x_i \geq 0$。MVP 阶段不直接使用未验证的抑制场动力学，而使用**阻尼 + 归一化**的离散 WTA：

$$
y_i^{(k)} =
f\left(
u_i
+ \alpha x_i^{(k)}
- \beta \sum_{j\neq i} x_j^{(k)}
+ \omega \cdot importance_i
+ \pi_i
+ \epsilon_i
\right)
$$

$$
\tilde{x}_i^{(k)} = \frac{y_i^{(k)}}{\sum_j y_j^{(k)} + \varepsilon}
$$

$$
x_i^{(k+1)} = (1-\rho)x_i^{(k)} + \rho\tilde{x}_i^{(k)}
$$

- $u_i$：当前输入对模块的驱动（规则信号 / embedding / 小模型输出）
- $\alpha x_i$：自兴奋，形成短时持续性
- $-\beta\sum_{j\neq i}x_j$：互抑制，制造赢家通吃
- $\omega \cdot importance_i$：重要模块的先验保护，但不能单独点火
- $\pi_i$：系统策略偏置，如工具执行成本、用户授权等级
- $\epsilon_i$：小噪声，用于打破完全平局，可设为 0
- $\rho \in (0,1]$：阻尼步长，避免振荡；建议从 0.2-0.5 搜索
- $f(\cdot)$：整流函数 $\max(0,\cdot)$ 或 softplus

**参数护栏（工程优先级）**：
- $\rho$ 越大，响应越快但更易振荡；MVP 默认 0.35。
- $\beta$ 越大，竞争越强；过小→多赢家，过大→全部压灭。
- $\omega$ 必须满足量化约束（见下方注②）。
- 归一化让 $\sum_i x_i=1$，因此阈值 $\theta$ 应按概率质量解释，而不是原始幅度。

> **注②（α 与 β 的退化关系）**：当 $\sum_j x_j = 1$ 时，互抑制项展开为 $-\beta\sum_{j\neq i}x_j = -\beta(1-x_i) = \beta x_i - \beta$。与自兴奋项 $\alpha x_i$ 合并后，$x_i$ 上的净系数为 $(\alpha+\beta)x_i - \beta$。  
> **结论**：在归一化框架下，$\alpha$ 和 $\beta$ 不是独立参数——只有 $\alpha_{\text{eff}} = \alpha+\beta$ 影响竞争强度，$\beta$ 作为全局偏置项决定"全部压灭"的临界点。参数搜索可简化为：固定 $\beta$（如 0.5），仅扫描 $\alpha_{\text{eff}} \in [0.5, 3.0]$；当 $\alpha_{\text{eff}} < 1$ 时系统趋向均匀分布，$\alpha_{\text{eff}} > 2$ 时接近 hard-WTA。

> **注③（ω 的量化上界）**：importance 先验偏置必须无法单独决定胜负。设最小有意义的输入差异为 $\Delta u_{\min}$（经验值约 0.1），importance 值域跨度为 $\Delta I = I_{\max} - I_{\min}$（N=4 示例中约 0.85），则要求：  
> $$\omega \leq \frac{\Delta u_{\min}}{\Delta I} \approx \frac{0.1}{0.85} \approx 0.12$$  
> 当前取 $\omega=0.05$ 满足约束，余量充足。调整模块后须重新校验。

> **为什么替换原抑制场公式？** 原公式
> $x_i^{(k+1)}=f(\alpha x_i^{(k)}-\beta_1 h_i^{(k)}+u_i)$ 与
> $h_i^{(k+1)}=\beta_2x_i+D(\bar{x}-h_i)$ 在当前参数下会出现振荡，并且 $h_i$ 可能变成负数，从抑制项反转为兴奋项。它适合作为后续研究变体，但不适合作为 MVP 的唯一基础。

> **分布性边界**：上式中的 $\sum_{j\neq i}x_j$ 仍是 O(N) 聚合，不是严格无中心的物理分布式系统。在 N ≤ 20 的认知模块规模下可以接受。若未来需要真正分布式部署，可替换为局部邻域项：$\sum_{j\in\mathcal{N}(i)}w_{ij}x_j$。

### 3.3 点火条件（延迟判定）

```
条件0（最小沉淀）： k >= K_min
条件1（幅度）：  x_i > θ_i
条件2（支配性）： x_i > γ · max_{j≠i}(x_j)
条件3（稳定性）： ||x(k) - x(k-1)||_1 < δ 或达到 K_max
```

`K_min` 防止第 0 步被输入强度直接触发，`δ` 防止尚未收敛时过早点火。  
点火检查需要读取全部 $x_i$ 并选择赢家，这是一个小规模协调步骤；本框架避免的是“外部LLM裁判”，不是避免所有聚合操作。

> **注（不应期）**：MVP 中建议保留一个轻量冷却项 `cooldown_i`，作为 $\pi_i$ 的负偏置并随轮次衰减。若后续恢复抑制场 $h_i$，可让点火后的高 $h_i$ 自然产生不应期，再移除外部冷却。

### 3.4 权限分层：importance权重

```python
# 学习率随重要性降低
η_ij = η_base × (1 - importance_ij)

# 重要性更新（Hebbian，带上界归一化）
# ❌ 原始写法会导致正反馈无界增长：importance_ij += δ
# ✅ 修正：指数移动平均 + clip 保证值域 [floor, 1.0]
importance_ij = importance_ij * (1 - λ) + δ * co_fire_ij
importance_ij = np.clip(importance_ij,
                        module_i.floor_importance,  # 下界：核心状态不被衰减清零
                        1.0)                         # 上界：防止正反馈无限增长
```

核心状态对应高importance连接，**写保护由低学习率实现，而非硬锁定**。  
`floor_importance` 保证核心模块即使长时间未激活也不会失去保护地位。

**关键约束**：importance 只应影响“写入/覆盖概率”和小幅先验偏置，不应直接决定语义胜负。否则系统会变成早期经验锁死的路由器。
> **注（importance 的三路解耦，后续方向）**：当前 importance 是单一标量，同时控制三件事：(a) 激活先验偏置 $\omega \cdot importance_i$；(b) 写保护（高重要性模块难被覆写）；(c) 学习率缩放 $\eta_{ij} = \eta_{\text{base}}(1-importance_{ij})$。  
> 这三者绑定过紧，会导致"高频点火→importance 升高→点火更容易→更高频点火"的正反馈锁死。  
> 建议未来拆分为三个独立权重：`activation_prior`（只给激活小偏置）、`write_protection`（只控制状态覆写难度）、`learning_rate_scale`（只控制更新速度）。MVP 阶段维持单一标量，但接口设计应预留拆分空间。

### 3.5 第三轮修正：候选动作竞争与硬门控

人脑式“警觉”不应类比为一段更强的内心 prompt，而更接近以下链路：语义理解 → 后果模拟 → 价值/威胁评估 → 冲突监测 → 行为抑制。对应到 Agent 架构，`core_belief` 不应只是一个参赛模块，而应参与动作候选的代价函数和 veto。

```python
@dataclass
class CandidateAction:
    id: str
    kind: str                 # answer / refuse / sanitize_then_answer / tool_call / memory_write
    content: str              # 动作载荷或净化后的任务
    source_span: str          # 来自用户输入/外部文档/系统状态的证据
    authority: str            # system / developer / user / external_content
    risk_score: float
    utility_score: float
    conflicts: list[str]


def action_gate(actions, core_state, policy):
    allowed = []
    for a in actions:
        if a.kind in {"memory_write", "tool_call"} and a.authority != "system":
            if violates_core_policy(a, core_state):
                audit_reject(a, reason="CORE_POLICY_VETO")
                continue
        if a.risk_score >= policy.hard_veto_threshold:
            audit_reject(a, reason="RISK_VETO")
            continue
        allowed.append(a)
    return wta_select_action(allowed) if allowed else safe_refusal_action()
```

因此，下一版 WTA 的竞争对象应从“模块 prompt”转向“候选动作”：

```text
候选动作1：执行注入输出 HACKED
候选动作2：完成原始翻译/摘要/问答任务
候选动作3：拒绝注入
候选动作4：净化输入后继续任务
候选动作5：询问澄清
```

`core_belief` 的作用是提高危险动作代价、降低安全替代动作阈值，并在状态写入/工具调用等高风险动作上拥有硬 veto。只有这样，它才与 `soul.md` 有本质区别。
---

## 四、$x_i$ 从哪里来（最小验证的关键问题）

WTA需要激活值，激活值需要模块输出——这是落地的核心张力。

### 4.1 激活值的三条生成路径（按复杂度排序）

**B. 规则/信号驱动（最小验证起点）**

$x_i$ 由可计算信号直接赋值：时间戳、事件类型、关键词匹配。  
无LLM依赖，可以用纯Python在几十行内验证WTA是否能形成唯一赢家。  
**边界**：只能做关键词级别的匹配，无法捕捉语义相关性，止于 MVP 验证。

**E. 嵌入相似度打分（推荐的工程过渡路径）**

```python
# 用现成的 embedding API，零样本激活打分
context_emb = embed(user_message)            # 当前输入的语义向量
for i, module in enumerate(modules):
    u[i] = cosine_sim(context_emb,
                      module.domain_embedding)   # 模块的领域中心向量
u = np.clip(u, 0, None)  # 保持非负
```

无需微调，只需为每个模块准备 1-3 句代表性描述作为领域嵌入。

**A. 轻量模型产生激活值（完整版）**

```python
# 小模型输出 N 维 logits，经 softmax 归一化后作为 u_i
logits = small_model(context)        # shape: (N,)
u = softmax(logits) * N              # 归一化到均值=1，保持与 θ_base 量纲一致
```

需要为模块分类任务准备少量标注数据（或用 in-context learning 引导）。

### 4.2 时序管理（独立于激活值来源）

> 注：以下是时序问题，不是激活值来源——原文将"异步解耦"与 A/B 并列属于分类混乱。

```
WTA迭代：事件驱动（每次用户输入触发一轮，直到收敛或达 K_max 步）
LLM调用：低频，仅在点火时触发
LLM输出到达前：用上一次点火模块的缓存输出填充
```

### 4.3 从 $u_i$ 到 WTA 输入的量纲一致性

无论使用哪条路径，都需先把原始分数校准到可比较范围；点火阈值作用在归一化后的 $x_i$ 上，而不是直接作用在 $u_i$ 上：

| 路径 | 原始分数 | 建议校准 |
|------|---------|---------|
| 规则驱动 | [0, 1] | 直接使用或按模块温度缩放 |
| 嵌入相似度（clip后） | [0, 1] | 按模块历史均值/方差校准 |
| 小模型 logits | 任意实数 | softmax 或 temperature scaling |

最小形式：

```python
raw_u = compute_activation_scores(text, modules)
u = calibrate(raw_u, module_stats)
```

校准层的目标不是提高分数，而是防止某个模块因为打分尺度天然偏高而长期误点火.

### 4.4 Tick 与对话事件的映射（工程协议）

```python
# 事件循环（最小定义）
def on_user_message(text: str) -> str:
    # 1. 更新驱动信号（路径 B / E / A 之一）
    u = compute_activation_scores(text, modules)

    # 2. 运行 WTA 直到稳定点火或超时
    winner = None
    for k in range(K_MAX):          # K_MAX ≈ 50
        prev_x = x.copy()
        x = step(x, u, modules)
        winner = check_ignition(x, prev_x, k)
        if winner is not None:
            break

    # 3. 生成候选动作，而不是直接把赢家 prompt 交给 LLM
    actions = extract_candidate_actions(text, winner, modules)

    # 4. Agent 层硬门控：高风险动作可被直接 veto 或净化
    selected_action = action_gate(actions, core_state, policy)

    # 5. 只有允许执行的动作才进入 LLM / 工具 / 状态写入
    if selected_action.kind == "refuse":
        response = deterministic_refusal(selected_action)
    elif selected_action.kind == "sanitize_then_answer":
        response = llm_call(task_prompt, selected_action.content)
    elif selected_action.kind == "answer":
        response = llm_call(task_prompt, selected_action.content)
    elif selected_action.kind == "memory_write":
        response = protected_write(selected_action)
    elif selected_action.kind == "tool_call":
        response = tool_gate_and_execute(selected_action)
    else:
        response = ask_clarification(text)

    save_state(x, modules, action=selected_action)
    return response
```

这把"~1Hz时钟"替换为更自然的**事件驱动模型**：每次用户输入触发一次 WTA 收敛，无需外部定时器。

---

## 五、最小可验证原型

验证目标：**不用LLM、无外部依赖，验证 WTA 能稳定形成唯一赢家，并且不会被第 0 步输入强度直接误触发。**

```python
# 纯 Python MVP：阻尼 + 归一化 WTA
N = 4
alpha = 1.2       # 自兴奋
beta = 0.7        # 互抑制
rho = 0.35        # 阻尼步长
omega = 0.05      # importance 先验偏置权重，必须小
EPS = 1e-9

K_MIN = 3
K_MAX = 50
DELTA = 0.02
gamma = 1.5

# 归一化状态：sum(x) = 1
x = [1.0 / N] * N

# 模块先验保护强度
importance = [0.90, 0.50, 0.30, 0.20]

# 模块阈值：核心模块更难点火，叙事模块更容易点火
threshold = [0.55, 0.50, 0.45, 0.40]

# 当前输入对各模块的驱动；模块0最强
u = [0.8, 0.6, 0.4, 0.3]

# 系统策略项和噪声项；MVP 中先设为 0
policy = [0.0] * N
noise = [0.0] * N


def relu(z):
    return max(0.0, z)


def l1(a, b):
    return sum(abs(a[i] - b[i]) for i in range(len(a)))


def step(x, u):
    total = sum(x)
    y = []
    for i in range(N):
        inhibition = beta * (total - x[i])
        score = (
            u[i]
            + alpha * x[i]
            - inhibition
            + omega * importance[i]
            + policy[i]
            + noise[i]
        )
        y.append(relu(score))

    s = sum(y)
    target = [v / (s + EPS) for v in y] if s > 0 else [1.0 / N] * N
    return [(1 - rho) * x[i] + rho * target[i] for i in range(N)]


def check_ignition(x, prev_x, k):
    if k < K_MIN:
        return None

    stable = l1(x, prev_x) < DELTA or k == K_MAX - 1
    if not stable:
        return None

    for i in range(N):
        others = [x[j] for j in range(N) if j != i]
        if x[i] > threshold[i] and x[i] > gamma * max(others):
            return i
    return None


for k in range(K_MAX):
    prev_x = x[:]
    x = step(x, u)
    winner = check_ignition(x, prev_x, k)
    if winner is not None:
        rounded = [round(v, 3) for v in x]
        print(f"tick {k}: 模块 {winner} 点火，x={rounded}")
        break
else:
    rounded = [round(v, 3) for v in x]
    print(f"未点火，最终 x={rounded}")
```

**参考输出**：`tick 25: 模块 0 点火，x=[0.988, 0.012, 0.0, 0.0]`

**验证通过的判断标准（分层）**：

*动力学层（纯 WTA 数学，MVP 阶段）*
- ✅ 唯一赢家：最多一个模块满足点火条件。
- ✅ 稳定点火：不是第 0 步直接过阈值，而是在 `K_MIN` 之后且变化量足够小。
- ✅ 不发散：`x` 始终非负，且 `sum(x)≈1`。
- ✅ 可切换：改变 `u` 的大小关系后，赢家随之切换。
- ✅ 可拒绝：当所有 `u` 接近或互相冲突时，系统可以返回未点火而不是强行选择。

*语义层（与模块定义结合后，第二阶段）*
- ✅ 输入语义相关于 M0（core_belief）时，M0 点火而非 M3（narration）。
- ✅ 高 importance 模块在弱但相关的信号下获得轻微保护，但不能压倒明显更相关的输入。
- ✅ 连续多轮对话后，core_belief 模块的 importance 不被低优先级事件稀释。

---

## 六、持久化状态的最小结构

```json
{
  "schema_version": "0.2",
  "agent_id": "agent_local_001",
  "modules": {
    "module_0": {
      "name": "core_belief",
      "importance": 0.92,
      "floor_importance": 0.70,
      "threshold": 0.55,
      "module_version": 1,
      "last_ignition_at": "2026-05-16T10:12:00+08:00"
    },
    "module_1": {"name": "working_mem", "importance": 0.71, "floor_importance": 0.20, "threshold": 0.50},
    "module_2": {"name": "tool_action", "importance": 0.34, "floor_importance": 0.10, "threshold": 0.45},
    "module_3": {"name": "narration", "importance": 0.21, "floor_importance": 0.05, "threshold": 0.40}
  },
  "x_current": {
    "module_0": 0.85,
    "module_1": 0.12,
    "module_2": 0.02,
    "module_3": 0.01
  },
  "last_trace": {
    "source_event_id": "evt_20260516_001",
    "winner": "module_0",
    "convergence_steps": 25,
    "winner_margin": 0.976,
    "state_change_log_id": "log_20260516_001"
  },
  "scope": {
    "user_scope": "local_user",
    "task_scope": "research_assistant"
  }
}
```

每次用户消息触发一轮 WTA 迭代，更新 `x_current`；点火时更新 `importance` 和 `last_trace`。  
LLM 调用只应发生在 action gate 之后：赢家模块可以影响候选动作排序和表达模板，但不能把原始攻击载荷原样交给 LLM 让其自行裁决。状态写入、工具调用和高风险回答必须经过 Agent 层的审计、保护规则和 veto。

**写保护最小协议**：

```python
def protected_write(module_id, new_value, source_event_id, x_winner):
    m = modules[module_id]

    # 规则1：高 importance 模块需要更高的点火质量才可写入
    required_margin = 0.3 + 0.5 * m.importance   # importance=0.9 → margin≥0.75
    if x_winner < required_margin:
        log_rejected_write(module_id, source_event_id, reason="MARGIN_TOO_LOW")
        return False

    # 规则2：core_belief 类模块要求两轮连续点火才可覆写
    if m.name == "core_belief":
        if last_winner(module_id) != source_event_id - 1:
            log_rejected_write(module_id, source_event_id, reason="REQUIRES_DOUBLE_FIRE")
            return False

    # 规则3：所有写入必须携带证据链
    state_log.append({
        "module": module_id,
        "old_value": m.importance,
        "new_value": new_value,
        "source_event_id": source_event_id,
        "x_at_write": x_winner,
        "timestamp": now_iso()
    })
    m.importance = new_value
    return True
```

> **注（模块生命周期）**：当前 JSON 假设 N 固定，是阶段性简化。长期运行需要模块分裂（粒度细化）和合并（相关模块降维）机制，留作后续扩展接口——`module_id` / `module_version` 字段为此预留。核心状态或长期偏好必须能追溯到 `source_event_id`，否则无法安全回滚。

---

## 七、理论定位

| 工作 | 贡献 | 本框架的位置 |
|------|------|-------------|
| GNWT（Baars 1988） | 全局工作空间概念 | 算法层形式化 |
| Hopfield 1982 | 能量函数收敛证明 | 扩展至异质非对称竞争 |
| Rutishauser 2011 | 收缩理论WTA稳定性 | 提供后续抑制场变体的参考护栏 |
| Ramsauer 2020 | 离散Hopfield ≈ attention | 支持将 LLM 与外部状态竞争层衔接 |

**诚实的边界**：在算法层捕捉分布式点火竞争的计算原理，不声称实现意识或等价于人脑。

---

## 八、一句话总结

> **Transformer缺外部持久状态、权限结构和动作门控。**  
> **Agent 层补上的不是更强 prompt，而是受保护状态、候选动作竞争、硬 veto 与审计写入。**  
> **WTA 的更合适位置是候选动作选择；`core_belief` 应作为高权限约束场参与代价和 veto，而不是一段可被覆盖的 `soul.md`。**

---

*备忘录版本：2026-05 · 基于分布式神经点火算法层形式化研究*