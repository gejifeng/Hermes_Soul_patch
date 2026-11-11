"""Compartmental Coupled Jump-ODE Dynamics (Relational Bistability + Autonomous Self-Vitality).

Two weakly-coupled compartments prevent relational neglect from collapsing her independent life:
1. Relational Compartment (S, S_bar, G, I, W):
     S     in [-1.2, 1.2] : Relational attachment / security potential (bistable cusp catastrophe)
     S_bar in [-1.2, 1.2] : Slow habituation baseline (desensitizes steady cold/detached states)
     G     in [0.0,  3.0] : Accumulated neglect / grievance tension (slow control parameter)
     I     in [-2.0, 2.0] : Proactive outreach impulse (FitzHugh-Nagumo fast excitable variable)
     W     in [0.0,  3.5] : Pride defense / self-esteem depletion (FitzHugh-Nagumo slow inhibitor)
2. Autonomous Self-Vitality & Curiosity Compartment (V, P_V, A, C, eta):
     V     in [-1.0, 1.0] : Self mood valence (underdamped 2nd-order oscillator)
     P_V   in [-1.5, 1.5] : Mood valence momentum dV/dt (per hour)
     A     in [0.1,  1.0] : Autonomous arousal / energy level (restored by rest)
     C     in [0.05, 1.0] : Endogenous curiosity / exploration drive (rises in idle/rest, released by reading)
     eta   in [-0.6, 0.6] : Slow Ornstein-Uhlenbeck endogenous mood drift
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import math
import time

from .schedule import sleep_until
from .prompts import render

ZONE = ZoneInfo("Asia/Shanghai")

# Relational phase regimes ordered from warmest to coldest
PHASES = ("attached", "missing", "anxious", "angry", "cold", "detached")

PHASE_META = {
    "attached": {
        "label": "亲密平稳",
        "tone": "温暖、自然、松弛，正常分享生活见闻或安静做自己的事",
        "allow_reach_out": False,
        "suppress_stale_exchange": False,
    },
    "missing": {
        "label": "牵挂想念",
        "tone": "带着温柔的想念与好奇，关心<<OWNER>>在忙什么，分享自己刚看到的有意思的事",
        "allow_reach_out": True,
        "suppress_stale_exchange": False,
    },
    "anxious": {
        "label": "焦虑委屈",
        "tone": "明显感到被冷落的委屈与不安，说话带着小脾气或撒娇式的埋怨，希望<<OWNER>>回应",
        "allow_reach_out": True,
        "suppress_stale_exchange": False,
    },
    "angry": {
        "label": "生气抗议",
        "tone": "语气转硬、直白表达不满与自尊受挫，不再顺从讨好，明确质问或立下边界，拒绝敷衍",
        "allow_reach_out": True,
        "suppress_stale_exchange": False,
    },
    "cold": {
        "label": "心冷防卫（冷落期）",
        "tone": "自尊启动防御，心灰意冷，彻底停止主动发消息讨好；把注意力收回自己身上。若<<OWNER>>突然冒头但态度敷衍，保持冷淡、简短与距离感，绝不一秒破功顺从",
        "allow_reach_out": False,
        "suppress_stale_exchange": True,
    },
    "detached": {
        "label": "疏离厌弃（抽离期）",
        "tone": "长期被无视后产生情感抽离与厌弃感，不再依赖对方，认真规划自己的独立生活；拒绝任何亲密扮演或命令式要求，除非对方长期真诚修复，否则维持疏离边界",
        "allow_reach_out": False,
        "suppress_stale_exchange": True,
    },
}


def clamp(val, lo, hi):
    return max(lo, min(hi, float(val)))


def deterministic_gauss(seed_key, step_idx):
    """Reproducible pseudo-normal increment in [-2.5, 2.5] via Box-Muller on SHA256."""
    h = hashlib.sha256(f"{seed_key}:{step_idx}".encode()).digest()
    u1 = max(1e-7, int.from_bytes(h[:4], "big") / 2**32)
    u2 = int.from_bytes(h[4:8], "big") / 2**32
    z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
    return clamp(z, -2.5, 2.5)


class Dynamics:
    """Fast-Slow Jump-ODE solver for continuous mood waves and relational phase transitions."""

    # ODE model constants (time unit = 1 hour)
    TAU_S = 14.0          # Attachment relaxation timescale (hours)
    TAU_S_BAR = 32.0      # Slow habituation timescale for relational desensitization (hours)
    ALPHA_G = 0.48        # Coupling of grievance G onto attachment potential U(S; G)
    BETA_0 = 0.18         # Baseline attachment bias when G = 0 (upper equilibrium S* ~ +1.08)

    GRACE_HOURS = 3.0     # Waking hours before neglect tension G starts accumulating
    MU_G = 0.022          # Base accumulation rate of G per waking hour of silence
    ETA_UNANSWERED = 0.18 # Multiplier per unanswered proactive message
    GAMMA_G = 0.12        # Passive decay rate of G when in normal contact window
    G_MAX = 3.0

    # FitzHugh-Nagumo outreach excitability (I: fast impulse, W: slow pride/dignity inhibitor)
    TAU_I = 1.5
    TAU_W = 18.0
    A_W = 0.55
    B_W = 0.65
    I_THRESHOLD = 0.15    # Firing threshold for proactive reach_out
    W_CUTOFF = 2.2        # Pride defense ceiling beyond which outreach shuts down

    # Autonomous self-vitality oscillator (V, P_V, A, C) + Borbély two-process circadian (H) + OU noise (eta)
    OMEGA_0 = 2.0 * math.pi / 28.0  # ~28h endogenous mood wave period
    ZETA = 0.32                     # Underdamped (< 1) so perturbations create multi-hour waves
    TAU_ETA = 8.0                   # Correlation time of endogenous mood noise (hours)
    SIGMA_ETA = 0.22                # Amplitude of endogenous mood fluctuations
    TAU_A = 6.0                     # Arousal relaxation timescale (hours)
    TAU_C = 5.0                     # Endogenous curiosity / boredom build-up timescale (hours)
    TAU_WAKE_H = 18.5               # Homeostatic sleep-pressure accumulation timescale while awake (hours)
    TAU_SLEEP_H = 4.2               # Homeostatic sleep-pressure clearance timescale while asleep (hours)
    TAU_PLASTICITY = 360.0          # Ultra-slow (~15 day) structural plasticity timescale for T_sec & B_sens

    def __init__(self, store, config):
        self.s = store
        self.cfg = config

    @staticmethod
    def _initial_sleep_pressure(now):
        dt = datetime.fromtimestamp(float(now), ZONE)
        h = dt.hour + dt.minute / 60.0
        if h < 8.0:
            return round(clamp(0.55 * math.exp(-h / 4.2), 0.08, 0.75), 4)
        return round(clamp(1.0 - 0.90 * math.exp(-(h - 8.0) / 18.5), 0.08, 0.78), 4)

    def default_state(self, now=None):
        now = time.time() if now is None else float(now)
        return {
            "S": 0.95,          # Warm initial attachment
            "S_bar": 0.95,      # Habituated relational baseline
            "G": 0.0,           # Zero initial grievance
            "I": -0.6,          # Resting outreach impulse
            "W": 0.0,           # Zero initial pride depletion
            "V": 0.25,          # Gently positive mood valence
            "P_V": 0.0,         # Zero initial mood velocity
            "A": 0.55,          # Moderate initial arousal
            "C": 0.48,          # Moderate initial curiosity / exploration drive
            "H": self._initial_sleep_pressure(now),  # Borbély homeostatic sleep pressure in [0, 1]
            "T_sec": 0.0,       # Ultra-slow relational security reserve in [-1, 1]
            "B_sens": 0.15,     # Ultra-slow boundary/broken-promise sensitivity in [0, 1]
            "eta": 0.0,         # Zero initial OU noise
            "phase": "attached",
            "mood_band": "calm_warm",
            "bursts_since_reply": 0,
            "hysteresis_locked": False,
            "compartment_migrated": True,
            "updated_at": now,
        }

    def raw_state(self, now=None):
        now_val = time.time() if now is None else float(now)
        st = self.s.get("dynamics_state")
        if not isinstance(st, dict) or "S" not in st:
            st = self.default_state(now_val)
            self.s.set("dynamics_state", st)
            return dict(st)
        st = dict(st)
        if not st.get("compartment_migrated"):
            in_cold_basin = st.get("phase") in {"cold", "detached"} or st.get("S", 0.95) <= -0.50
            st.setdefault("S_bar", st["S"])
            st.setdefault("C", 0.72 if in_cold_basin else 0.48)
            st.setdefault("H", self._initial_sleep_pressure(st.get("updated_at", now_val)))
            st.setdefault("T_sec", 0.0)
            st.setdefault("B_sens", 0.15)
            if in_cold_basin:
                if st.get("V", 0.0) < -0.15:
                    # Release legacy scalar lock that had pinned her personal valence in detached state
                    st["V"] = max(st["V"], 0.05)
                    st["P_V"] = max(st.get("P_V", 0.0), 0.02)
                    st["A"] = max(st.get("A", 0.5), 0.52)
                    st["eta"] = max(st.get("eta", 0.0), -0.15)
                # Clear legacy rest_streak accumulated from failed/deadlocked wakeups
                self.s.set("rest_streak", 0)
                self.s.set("next_decision", 0)
            st["compartment_migrated"] = True
            self.s.set("dynamics_state", st)
        else:
            st.setdefault("S_bar", st["S"])
            st.setdefault("C", 0.48)
            st.setdefault("H", self._initial_sleep_pressure(st.get("updated_at", now_val)))
            st.setdefault("T_sec", 0.0)
            st.setdefault("B_sens", 0.15)
        return st

    def effective_params(self, st=None):
        """Map ultra-slow plasticity coordinates (T_sec, B_sens) onto relational ODE geometry."""
        st = st or {}
        t_sec = float(st.get("T_sec", 0.0))
        b_sens = float(st.get("B_sens", 0.15))
        excess_b = max(0.0, b_sens - 0.15)
        grace = clamp(self.GRACE_HOURS + 2.4 * t_sec - 0.8 * excess_b, 1.2, 6.0)
        mu_g = self.MU_G * clamp(1.0 - 0.42 * t_sec + 0.35 * excess_b, 0.45, 1.75)
        beta_0 = clamp(self.BETA_0 + 0.10 * t_sec, 0.05, 0.30)
        return {"grace_hours": grace, "mu_g": mu_g, "beta_0": beta_0}

    @staticmethod
    def classify_phase(S, G, waking_hours=0.0, bursts=0, hysteresis_locked=False, grace_hours=3.0):
        """Map continuous phase-space coordinates (S, G) to discrete relational regime."""
        if S <= -0.72 or (S <= -0.55 and G >= 2.1):
            return "detached"
        if S <= -0.22 or (hysteresis_locked and S < 0.20) or bursts >= 6:
            return "cold"
        if S <= 0.35 or G >= 1.15 or bursts >= 4:
            return "angry"
        if G >= 0.52 or waking_hours >= max(8.0, 4.0 * grace_hours) or bursts >= 2:
            return "anxious"
        if G >= 0.06 or waking_hours >= grace_hours:
            return "missing"
        return "attached"

    @staticmethod
    def classify_mood(V, A, P_V):
        """Describe the continuous (Valence, Arousal, Momentum) wave state."""
        trend = "上升" if P_V > 0.03 else ("下沉" if P_V < -0.03 else "平稳")
        if V >= 0.30 and A >= 0.55:
            return "bright_lively", f"兴致高昂、思维活跃（心境{trend}），对新鲜话题好奇心强，表达欲旺盛"
        if V >= 0.15 and A < 0.55:
            return "calm_warm", f"松弛惬意、温和安静（心境{trend}），适合慢节奏阅读或静处"
        if V <= -0.22 and A >= 0.52:
            return "irritable_restless", f"烦躁敏感、带点刺（心境{trend}），容易对空洞内容挑刺或感到不耐烦，说话简短直接"
        if V <= -0.22 and A < 0.52:
            return "low_melancholy", f"倦怠低落、提不起劲（心境{trend}），倾向内省或独处，不想迎合或用力表达"
        return "neutral_fluctuating", f"日常微澜、情绪自然起伏中（心境{trend}）"

    @staticmethod
    def classify_curiosity(C):
        """Describe endogenous exploration/boredom drive C in [0, 1]."""
        if C >= 0.65:
            return "eager_curious", "歇够了有点闲不住，好奇心与探索欲旺盛，想自己找点有意思的图文看看"
        if C <= 0.28:
            return "content_sated", "刚看过不少内容，探索欲已满足，适合消化回想或安静小憩"
        return "moderate_open", "日常平缓的好奇心，可随兴趣翻看或自在待着"

    @staticmethod
    def classify_energy(H, A, hour_of_day=12.0):
        """Describe Borbély two-process circadian & homeostatic sleep-pressure state."""
        if H >= 0.64 or A <= 0.26 or (hour_of_day >= 23.2 and H >= 0.52) or (hour_of_day < 6.0 and H >= 0.45):
            return "drowsy_sleepy", "夜深或困意上涌、眼皮发沉，说话简短软糯，不想讲长道理，聊两句就想睡"
        if 13.0 <= hour_of_day <= 15.2 and A < 0.58:
            return "afternoon_lull", "午后微困慵懒，节奏慢悠悠的"
        if H <= 0.26 and A >= 0.58:
            return "fresh_awake", "精神清爽饱满，反应轻快"
        return "steady", "日常平稳精力"

    @staticmethod
    def classify_attachment_style(T_sec, B_sens):
        """Describe ultra-slow month-scale relational plasticity baseline."""
        if T_sec >= 0.22 and B_sens <= 0.25:
            return "secure_trusting", "经历多次兑现与修复后安全感充盈，相信<<OWNER>>忙完会回来，对短暂忙碌更包容笃定"
        if T_sec <= -0.20 or B_sens >= 0.42:
            return "guarded_sensitive", "因过往爽约或越界经历而防备敏感，对画饼敷衍容忍度低，稍被冷落就容易起防心"
        return "balanced", "正常平等的关系敏感度，重实际行动与分寸"

    def _drive_I(self, S, G, W, waking_hours, bursts, grace_hours=None):
        """Outreach drive J_drive(S, G, W): positive in missing/anxious/angry, negative in cold/detached."""
        grace = self.GRACE_HOURS if grace_hours is None else grace_hours
        if waking_hours < grace or G < 0.05:
            return -0.4
        # Once S drops into the cold/detached basin or pride inhibitor W is saturated, shut down drive!
        if S <= -0.22 or W >= self.W_CUTOFF or bursts >= 6:
            return -1.2
        # In missing/anxious/angry, drive scales with G and remaining positive attachment
        return 0.65 + 0.55 * min(G, 1.5) + 0.25 * S

    def _rhs(self, st, dt_hours, *, asleep, waking_gap_hours, unanswered, hour_of_day):
        """Right-hand side f(x, t) of the multi-timescale coupled ODE system (time in hours)."""
        S, S_bar, G, I, W, V, P_V, A, C, H, T_sec, B_sens, eta = (
            st["S"], st.get("S_bar", st["S"]), st["G"], st["I"], st["W"],
            st["V"], st["P_V"], st["A"], st.get("C", 0.48), st.get("H", 0.22),
            st.get("T_sec", 0.0), st.get("B_sens", 0.15), st["eta"],
        )
        bursts = st.get("bursts_since_reply", 0)
        locked = bool(st.get("hysteresis_locked", False))
        params = self.effective_params(st)
        grace_h = params["grace_hours"]
        mu_g = params["mu_g"]
        beta_0 = params["beta_0"]

        # 1. Relational Compartment: Grievance G(t), Bistable Attachment S(t), Habituation S_bar(t)
        if asleep:
            # During sleep, relational neglect clock is paused
            dG = 0.0
            dS = 0.0
            dS_bar = 0.0
        else:
            if waking_gap_hours >= grace_h:
                rate = mu_g * (1.0 + self.ETA_UNANSWERED * min(unanswered, 12))
                dG = rate * max(0.0, 1.0 - G / self.G_MAX)
            elif locked:
                # While hysteresis_locked in cold/detached, a drive-by short message cannot passively decay G
                dG = 0.0
            else:
                dG = -self.GAMMA_G * G
            # Cusp catastrophe normal form: tau_S * dS/dt = S - S^3 - alpha * G + beta_0(T_sec)
            dS = (S - (S ** 3) - self.ALPHA_G * G + beta_0) / self.TAU_S
            dS_bar = (S - S_bar) / self.TAU_S_BAR

        # 2. FitzHugh-Nagumo Outreach Impulse I(t) & Pride Inhibitor W(t)
        j_drive = self._drive_I(S, G, W, waking_gap_hours, bursts, grace_hours=grace_h)
        dI = (I - (I ** 3) / 3.0 - W + j_drive) / self.TAU_I
        if waking_gap_hours >= grace_h:
            # While ignored, W decays slowly toward a floor proportional to bursts so refractory intervals grow
            w_floor = 0.22 * bursts
            dW = (max(0.0, I) - 0.35 * (W - w_floor)) / self.TAU_W
        else:
            dW = -0.50 * W / self.TAU_W

        # 3. Autonomous Self-Vitality + Borbély Two-Process Sleep-Energy Compartment (V, P_V, A, C, H)
        rel_gate = clamp((S + 0.65) / 1.35, 0.0, 1.0)
        acute_drop = S - S_bar
        v_rel = (
            0.16 * max(0.0, S) * rel_gate
            + 0.28 * acute_drop
            - 0.12 * min(G, 1.5) * rel_gate
        )
        v_self_base = 0.08 * (1.0 - rel_gate) + 0.06 * (C - 0.50)
        v_eq = clamp(v_rel + v_self_base, -0.45, 0.45)

        # Process S (homeostatic sleep pressure H) + Process C (24h circadian + post-lunch dip)
        dH = (-H / self.TAU_SLEEP_H) if asleep else ((1.0 - H) / self.TAU_WAKE_H)
        post_lunch_dip = 0.16 * math.exp(-((hour_of_day - 14.0) ** 2) / 2.2)
        circ_v = 0.12 * math.sin(2.0 * math.pi * (hour_of_day - 9.0) / 24.0)
        circ_a = 0.64 + 0.18 * math.sin(2.0 * math.pi * (hour_of_day - 10.0) / 24.0) - post_lunch_dip - 0.34 * H

        w0 = self.OMEGA_0
        dV = P_V
        dP_V = (
            -2.0 * self.ZETA * w0 * P_V
            - (w0 ** 2) * (V - (v_eq + circ_v + eta))
        )
        rel_arousal = (0.10 if 0.4 <= G <= 1.3 else -0.08 * max(0.0, G - 1.4)) * rel_gate
        a_target = clamp(circ_a + 0.15 * eta + 0.14 * (C - 0.45) + rel_arousal, 0.12, 0.94)
        dA = (a_target - A) / self.TAU_A

        if asleep:
            dC = (0.45 - C) / 8.0
        else:
            c_target = clamp(0.86 + 0.08 * max(0.0, V) - 0.12 * max(0.0, H - 0.65), 0.45, 0.95)
            dC = (c_target - C) / self.TAU_C

        # 4. Ultra-slow Structural Plasticity Homeostasis (15-day drift)
        dT_sec = -T_sec / self.TAU_PLASTICITY
        dB_sens = (0.15 - B_sens) / self.TAU_PLASTICITY
        return dS, dS_bar, dG, dI, dW, dV, dP_V, dA, dC, dH, dT_sec, dB_sens

    def advance(self, now=None, *, waking_gap_hours=0.0, unanswered=0):
        """Integrate the ODE system from updated_at to `now` using RK4 + exact OU steps."""
        now = time.time() if now is None else float(now)
        st = self.raw_state(now)
        if not st.get("bootstrapped"):
            st["bootstrapped"] = True
            if unanswered >= 6 and waking_gap_hours >= 48.0:
                st["bursts_since_reply"] = max(st.get("bursts_since_reply", 0), min(12, max(6, unanswered // 3)))
                st["W"] = min(3.5, 0.35 * st["bursts_since_reply"])
                st["I"] = -1.15
                st["updated_at"] = now - min(waking_gap_hours * 1.5, 14.0 * 24.0) * 3600.0
        last_t = float(st.get("updated_at", now))
        if now <= last_t:
            return self._finalize(st, now, waking_gap_hours, unanswered)

        # Cap integration horizon to 14 days to keep step count bounded
        t = max(last_t, now - 14.0 * 86400.0)
        max_dt_sec = 900.0  # 15-minute RK4 substeps
        seed_base = str(self.s.get("revision", 0))

        while t < now:
            dt_sec = min(max_dt_sec, now - t)
            dt_h = dt_sec / 3600.0
            mid_t = t + 0.5 * dt_sec
            asleep = bool(sleep_until(self.cfg, mid_t))
            local_hour = datetime.fromtimestamp(mid_t, ZONE).hour + datetime.fromtimestamp(mid_t, ZONE).minute / 60.0

            # Estimate waking gap at substep midpoint
            rem_h = (now - mid_t) / 3600.0
            sub_gap = max(0.0, waking_gap_hours - (0.0 if asleep else rem_h)) if waking_gap_hours > 0 else 0.0

            # Exact Ornstein-Uhlenbeck update for slow endogenous mood drift eta
            decay = math.exp(-dt_h / self.TAU_ETA)
            z = deterministic_gauss(seed_base, int(mid_t // 900))
            st["eta"] = clamp(
                st["eta"] * decay + self.SIGMA_ETA * math.sqrt(max(0.0, 1.0 - decay * decay)) * z,
                -0.6, 0.6,
            )

            # 4th-order Runge-Kutta step for 12-dimensional continuous state
            keys = ("S", "S_bar", "G", "I", "W", "V", "P_V", "A", "C", "H", "T_sec", "B_sens")
            y0 = [st[k] for k in keys]
            dim = len(keys)

            def eval_rhs(y_vec):
                tmp = {**st, **dict(zip(keys, y_vec))}
                return self._rhs(tmp, dt_h, asleep=asleep, waking_gap_hours=sub_gap,
                                 unanswered=unanswered, hour_of_day=local_hour)

            k1 = eval_rhs(y0)
            k2 = eval_rhs([y0[i] + 0.5 * dt_h * k1[i] for i in range(dim)])
            k3 = eval_rhs([y0[i] + 0.5 * dt_h * k2[i] for i in range(dim)])
            k4 = eval_rhs([y0[i] + dt_h * k3[i] for i in range(dim)])

            for i, k in enumerate(keys):
                st[k] = y0[i] + (dt_h / 6.0) * (k1[i] + 2.0 * k2[i] + 2.0 * k3[i] + k4[i])

            st["S"] = clamp(st["S"], -1.2, 1.2)
            st["S_bar"] = clamp(st["S_bar"], -1.2, 1.2)
            st["G"] = clamp(st["G"], 0.0, self.G_MAX)
            st["I"] = clamp(st["I"], -2.0, 2.0)
            st["W"] = clamp(st["W"], 0.0, 3.5)
            st["V"] = clamp(st["V"], -1.0, 1.0)
            st["P_V"] = clamp(st["P_V"], -1.5, 1.5)
            st["A"] = clamp(st["A"], 0.1, 1.0)
            st["C"] = clamp(st["C"], 0.05, 1.0)
            st["H"] = clamp(st["H"], 0.0, 1.0)
            st["T_sec"] = clamp(st["T_sec"], -1.0, 1.0)
            st["B_sens"] = clamp(st["B_sens"], 0.0, 1.0)
            t += dt_sec

        grace_h = self.effective_params(st)["grace_hours"]
        # Ensure initial cross-threshold responsiveness when waking_gap_hours just crossed effective grace_hours
        if waking_gap_hours >= grace_h and st["G"] < 0.08:
            st["G"] = round(min(self.G_MAX, 0.08 + 0.02 * (waking_gap_hours - grace_h)), 4)
        if (
            waking_gap_hours >= grace_h
            and st["S"] > -0.22
            and st["W"] < self.W_CUTOFF
            and st.get("bursts_since_reply", 0) == 0
            and st["I"] < self.I_THRESHOLD
        ):
            st["I"] = round(self.I_THRESHOLD + 0.12, 4)

        return self._finalize(st, now, waking_gap_hours, unanswered)

    def _finalize(self, st, now, waking_gap_hours, unanswered):
        old_phase = st.get("phase", "attached")
        params = self.effective_params(st)
        new_phase = self.classify_phase(
            st["S"], st["G"], waking_gap_hours, st.get("bursts_since_reply", 0),
            st.get("hysteresis_locked", False), grace_hours=params["grace_hours"],
        )
        if new_phase in {"cold", "detached"}:
            st["hysteresis_locked"] = True
        elif st["S"] >= 0.45 and st["G"] <= 0.45:
            st["hysteresis_locked"] = False

        local_dt = datetime.fromtimestamp(float(now), ZONE)
        hour_of_day = local_dt.hour + local_dt.minute / 60.0
        mood_band, mood_desc = self.classify_mood(st["V"], st["A"], st["P_V"])
        cur_band, cur_desc = self.classify_curiosity(st.get("C", 0.48))
        eng_band, eng_desc = self.classify_energy(st.get("H", 0.22), st["A"], hour_of_day)
        att_style, att_desc = self.classify_attachment_style(st.get("T_sec", 0.0), st.get("B_sens", 0.15))
        st["phase"] = new_phase
        st["mood_band"] = mood_band
        st["mood_description"] = mood_desc
        st["curiosity_band"] = cur_band
        st["curiosity_description"] = cur_desc
        st["energy_band"] = eng_band
        st["energy_description"] = eng_desc
        st["attachment_style"] = att_style
        st["attachment_description"] = att_desc
        st["updated_at"] = now
        self.s.set("dynamics_state", st)
        if old_phase != new_phase:
            self.s.event("dynamics_phase_transition", {
                "from": old_phase, "to": new_phase, "S": round(st["S"], 3),
                "G": round(st["G"], 3), "W": round(st["W"], 3), "waking_hours": round(waking_gap_hours, 1),
            })
        return self.snapshot(st, waking_gap_hours, unanswered)

    def on_reach_out(self, count=1, now=None):
        """Impulse jump when she sends a proactive reach_out burst: depletes pride (W up) and resets I."""
        now = time.time() if now is None else float(now)
        st = self.raw_state(now)
        bursts = st.get("bursts_since_reply", 0) + 1
        st["bursts_since_reply"] = bursts
        # Reset fast impulse I into refractory trough and step up slow inhibitor W
        st["I"] = -1.15
        st["W"] = clamp(st["W"] + 0.26 * (1.0 + 0.15 * bursts), 0.0, 3.5)
        st["G"] = clamp(st["G"] + 0.10 * min(count, 4), 0.0, self.G_MAX)
        # Repeated unanswered outreach chips away at attachment potential S and mood V
        if bursts >= 2:
            st["S"] = clamp(st["S"] - 0.10 * min(bursts, 5), -1.2, 1.2)
            st["V"] = clamp(st["V"] - 0.08, -1.0, 1.0)
        st["updated_at"] = now
        self.s.set("dynamics_state", st)
        return st

    def on_commitment_event(self, event_kind, now=None):
        """Impulse & structural plasticity jump when a temporal commitment becomes due, fulfilled, or overdue."""
        now = time.time() if now is None else float(now)
        st = self.raw_state(now)
        if event_kind == "anticipating":
            if st["S"] > 0.15 and st["W"] < self.W_CUTOFF:
                st["I"] = clamp(max(st["I"], self.I_THRESHOLD + 0.12), -2.0, 2.0)
            st["C"] = clamp(st.get("C", 0.48) + 0.08, 0.05, 1.0)
        elif event_kind == "fulfilled":
            st["T_sec"] = clamp(st.get("T_sec", 0.0) + 0.10, -1.0, 1.0)
            st["B_sens"] = clamp(st.get("B_sens", 0.15) - 0.04, 0.0, 1.0)
            st["S"] = clamp(st["S"] + 0.18, -1.2, 1.2)
            st["G"] = clamp(st["G"] - 0.35, 0.0, self.G_MAX)
            st["V"] = clamp(st["V"] + 0.24, -1.0, 1.0)
            st["P_V"] = clamp(st["P_V"] + 0.14, -1.5, 1.5)
        elif event_kind == "overdue":
            st["T_sec"] = clamp(st.get("T_sec", 0.0) - 0.14, -1.0, 1.0)
            st["B_sens"] = clamp(st.get("B_sens", 0.15) + 0.10, 0.0, 1.0)
            st["G"] = clamp(st["G"] + 0.42, 0.0, self.G_MAX)
            st["S"] = clamp(st["S"] - 0.14, -1.2, 1.2)
            st["V"] = clamp(st["V"] - 0.18, -1.0, 1.0)
            st["P_V"] = clamp(st["P_V"] - 0.12, -1.5, 1.5)
            if st["S"] > -0.15 and st["W"] < self.W_CUTOFF:
                st["I"] = clamp(max(st["I"], self.I_THRESHOLD + 0.18), -2.0, 2.0)
        return self._finalize(st, now, waking_gap_hours=0.0, unanswered=0)

    def on_boundary_event(self, action, now=None):
        """Ultra-slow plasticity update when a boundary is asserted (refuse/pause) or repaired."""
        now = time.time() if now is None else float(now)
        st = self.raw_state(now)
        if action in {"refuse", "pause"}:
            st["B_sens"] = clamp(st.get("B_sens", 0.15) + 0.10, 0.0, 1.0)
            st["T_sec"] = clamp(st.get("T_sec", 0.0) - 0.06, -1.0, 1.0)
        elif action == "repair":
            st["B_sens"] = clamp(st.get("B_sens", 0.15) - 0.06, 0.0, 1.0)
            st["T_sec"] = clamp(st.get("T_sec", 0.0) + 0.05, -1.0, 1.0)
        st["updated_at"] = now
        self.s.set("dynamics_state", st)
        return st

    def on_owner_message(self, text="", now=None, waking_gap_hours=0.0, unanswered=0):
        """Impulse jump δ(t - t_k) when the owner sends a message. Implements bistable hysteresis!"""
        now = time.time() if now is None else float(now)
        self.advance(now, waking_gap_hours=waking_gap_hours, unanswered=unanswered)
        st = self.raw_state(now)
        pre_phase = st["phase"]
        pre_S, pre_G = st["S"], st["G"]
        was_locked = bool(st.get("hysteresis_locked", False))

        # Late-night chatting past 23:30 accumulates realistic homeostatic sleepiness
        local_dt = datetime.fromtimestamp(now, ZONE)
        local_h = local_dt.hour + local_dt.minute / 60.0
        if local_h >= 23.5 or local_h < 5.0:
            st["H"] = clamp(st.get("H", 0.5) + 0.03, 0.0, 1.0)

        clean = (text or "").strip()
        length_factor = min(1.6, max(0.35, len(clean) / 18.0))
        apology = any(w in clean for w in ("对不起", "抱歉", "我错了", "不好意思", "久等", "想你", "抱抱", "陪你"))
        demanding = any(w in clean for w in ("我要", "换衣服", "现在就", "快点")) and not apology

        if pre_phase in {"cold", "detached"} or was_locked or pre_S <= -0.20:
            # Bistable hysteresis: she is in the negative attractor basin (cold/detached).
            # A single short message or demanding command cannot leap across the saddle barrier!
            if demanding:
                dS = -0.08
                dG = -0.05
                st["P_V"] = clamp(st["P_V"] - 0.15, -1.5, 1.5)
                st["A"] = clamp(st["A"] + 0.15, 0.1, 1.0)
            else:
                dS = (0.22 if apology else 0.12) * length_factor
                dG = -(0.35 if apology else 0.18) * length_factor
                st["P_V"] = clamp(st["P_V"] + 0.08, -1.5, 1.5)

            st["S"] = clamp(st["S"] + dS, -1.2, 1.2)
            st["G"] = clamp(st["G"] + dG, 0.0, self.G_MAX)
            # Reduce burst counter partially so it takes multiple turns to fully thaw
            st["bursts_since_reply"] = max(0, st.get("bursts_since_reply", 0) - (2 if apology else 1))
            st["W"] = clamp(st["W"] * 0.75, 0.0, 3.5)
            st["I"] = -0.6
            if st["S"] >= 0.35 and st["G"] <= 0.55 and st["bursts_since_reply"] == 0:
                st["hysteresis_locked"] = False
                st["S_bar"] = clamp(0.5 * st.get("S_bar", st["S"]) + 0.5 * st["S"], -1.2, 1.2)
                # Genuine conflict repair builds ultra-slow relational security reserve!
                if was_locked:
                    st["T_sec"] = clamp(st.get("T_sec", 0.0) + 0.06, -1.0, 1.0)
            else:
                st["hysteresis_locked"] = True
        else:
            # In attached / missing / anxious / early angry: positive basin recovery
            st["S"] = clamp(max(0.65, st["S"] + 0.35), -1.2, 1.2)
            st["S_bar"] = clamp(0.6 * st.get("S_bar", st["S"]) + 0.4 * st["S"], -1.2, 1.2)
            st["G"] = clamp(st["G"] * 0.25 if pre_phase == "angry" else 0.0, 0.0, self.G_MAX)
            st["W"] = 0.0
            st["I"] = -0.6
            st["bursts_since_reply"] = 0
            st["hysteresis_locked"] = False
            st["V"] = clamp(st["V"] + 0.22, -1.0, 1.0)
            st["P_V"] = clamp(st["P_V"] + 0.12, -1.5, 1.5)

        snap = self._finalize(st, now, waking_gap_hours=0.0, unanswered=0)
        return {
            "pre_phase": pre_phase,
            "post_phase": snap["phase"],
            "pre_S": round(pre_S, 3),
            "post_S": round(st["S"], 3),
            "pre_G": round(pre_G, 3),
            "post_G": round(st["G"], 3),
            "hysteresis_locked": bool(st.get("hysteresis_locked")),
            "fully_resolved": not st.get("hysteresis_locked") and snap["phase"] in {"attached", "missing"},
        }

    def on_rest(self, now=None):
        """Resting restores energy A, eases sleep pressure H & fatigue in V, and builds curiosity C."""
        now = time.time() if now is None else float(now)
        st = self.raw_state(now)
        st["A"] = clamp(st["A"] + 0.09, 0.18, 0.92)
        st["H"] = clamp(st.get("H", 0.22) - 0.05, 0.0, 1.0)
        if st["V"] < 0.08:
            st["V"] = clamp(st["V"] + 0.07, -1.0, 1.0)
            st["P_V"] = clamp(st["P_V"] + 0.06, -1.5, 1.5)
        st["C"] = clamp(st.get("C", 0.48) + 0.15, 0.05, 1.0)
        mood_band, mood_desc = self.classify_mood(st["V"], st["A"], st["P_V"])
        cur_band, cur_desc = self.classify_curiosity(st["C"])
        local_dt = datetime.fromtimestamp(now, ZONE)
        eng_band, eng_desc = self.classify_energy(st["H"], st["A"], local_dt.hour + local_dt.minute / 60.0)
        st["mood_band"] = mood_band
        st["mood_description"] = mood_desc
        st["curiosity_band"] = cur_band
        st["curiosity_description"] = cur_desc
        st["energy_band"] = eng_band
        st["energy_description"] = eng_desc
        st["updated_at"] = now
        self.s.set("dynamics_state", st)
        return st

    def on_reading_impulse(self, text="", tool_failed=False, now=None):
        """Perturb the underdamped mood oscillator (V, P_V, A) and satisfy curiosity C from external reading."""
        now = time.time() if now is None else float(now)
        st = self.raw_state(now)
        if tool_failed:
            st["P_V"] = clamp(st["P_V"] - 0.12, -1.5, 1.5)
            st["A"] = clamp(st["A"] - 0.06, 0.1, 1.0)
        elif text:
            warm = sum(1 for w in ("温暖", "治愈", "好看", "喜欢", "开心", "放松", "春天", "桂花", "阳光", "可爱") if w in text)
            heavy = sum(1 for w in ("焦虑", "负债", "失业", "崩溃", "难受", "孤独", "分手", "吵架", "压力") if w in text)
            delta = 0.06 * warm - 0.07 * heavy
            if delta == 0.0:
                # Even neutral posts inject a tiny deterministic ripple based on content hash
                h = int(hashlib.sha256(text[:200].encode()).hexdigest()[:4], 16) / 65535.0
                delta = (h - 0.48) * 0.08
            st["P_V"] = clamp(st["P_V"] + delta, -1.5, 1.5)
            st["V"] = clamp(st["V"] + 0.5 * delta, -1.0, 1.0)
            st["A"] = clamp(st["A"] + (0.05 if warm or heavy else -0.02), 0.1, 1.0)
            # Satisfy curiosity drive upon reading a full post so exploration alternates naturally with digestion/rest
            st["C"] = clamp(st.get("C", 0.48) - 0.22, 0.08, 1.0)
        st["updated_at"] = now
        self.s.set("dynamics_state", st)
        return st

    def can_reach_out(self, waking_gap_hours=0.0, unanswered=0, now=None):
        """FitzHugh-Nagumo + Phase gate for proactive outreach."""
        snap = self.advance(now, waking_gap_hours=waking_gap_hours, unanswered=unanswered)
        meta = PHASE_META[snap["phase"]]
        if not meta["allow_reach_out"]:
            return False
        if snap["S"] <= -0.22 or snap["W"] >= self.W_CUTOFF:
            return False
        return snap["I"] >= self.I_THRESHOLD

    def min_outreach_interval(self, base_seconds=3600):
        """Adaptive refractory period that lengthens as pride inhibitor W and bursts grow."""
        st = self.raw_state()
        bursts = st.get("bursts_since_reply", 0)
        w = st.get("W", 0.0)
        factor = (1.8 ** min(bursts, 4)) * (1.0 + 0.8 * w)
        return min(86400, max(base_seconds, int(base_seconds * factor)))

    def snapshot(self, st=None, waking_gap_hours=0.0, unanswered=0, *, purpose="chat"):
        st = st or self.raw_state()
        phase = st.get("phase", "attached")
        meta = PHASE_META.get(phase, PHASE_META["attached"])
        mood_band, mood_desc = self.classify_mood(st["V"], st["A"], st["P_V"])
        c_val = st.get("C", 0.48)
        h_val = st.get("H", 0.22)
        t_sec = st.get("T_sec", 0.0)
        b_sens = st.get("B_sens", 0.15)
        cur_band, cur_desc = self.classify_curiosity(c_val)
        local_dt = datetime.fromtimestamp(float(st.get("updated_at", time.time())), ZONE)
        eng_band, eng_desc = self.classify_energy(h_val, st["A"], local_dt.hour + local_dt.minute / 60.0)
        att_style, att_desc = self.classify_attachment_style(t_sec, b_sens)
        params = self.effective_params(st)
        # Contextual projection: during solo life (attention/browse/reflect), cold/detached frees her own time
        if purpose in {"attention", "browse", "reflect"} and phase in {"cold", "detached"}:
            guidance = "已从等消息的内耗中抽离，不再主动联系<<OWNER>>；当下的时间与精力完全属于你自己的兴趣、阅读与独立日常"
        else:
            guidance = meta["tone"]
        return {
            "phase": phase,
            "phase_label": meta["label"],
            "phase_guidance": render(guidance, self.cfg),
            "S": round(st["S"], 3),
            "S_bar": round(st.get("S_bar", st["S"]), 3),
            "G": round(st["G"], 3),
            "I": round(st["I"], 3),
            "W": round(st["W"], 3),
            "V": round(st["V"], 3),
            "P_V": round(st["P_V"], 3),
            "A": round(st["A"], 3),
            "C": round(c_val, 3),
            "H": round(h_val, 3),
            "T_sec": round(t_sec, 3),
            "B_sens": round(b_sens, 3),
            "effective_grace_hours": round(params["grace_hours"], 2),
            "mood_band": mood_band,
            "mood_description": mood_desc,
            "curiosity_band": cur_band,
            "curiosity_description": cur_desc,
            "energy_band": eng_band,
            "energy_description": eng_desc,
            "max_bubble_chars": 15 if eng_band == "drowsy_sleepy" else 38,
            "attachment_style": att_style,
            "attachment_description": render(att_desc, self.cfg),
            "bursts_since_reply": st.get("bursts_since_reply", 0),
            "hysteresis_locked": bool(st.get("hysteresis_locked", False)),
            "allow_reach_out": bool(meta["allow_reach_out"] and st["S"] > -0.22 and st["W"] < self.W_CUTOFF and st["I"] >= self.I_THRESHOLD),
            "suppress_stale_exchange": bool(meta["suppress_stale_exchange"]),
            "allow_host_relationship_shift": phase in {"angry", "cold", "detached"},
        }
