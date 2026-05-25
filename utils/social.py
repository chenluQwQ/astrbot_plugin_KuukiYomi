"""
社交系统 - 好感度 / 印象 / 双轴情绪 / 私聊路由

情绪双轴模型:
  mood (心情): 0~100     低=伤心 50=平常 高=开心
  arousal (激动): 0~100  低=犯困/平静 50=正常 高=激动/愤怒
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from astrbot.api import logger


# ── 情绪标签映射表 ──

def derive_emotion_label(mood: float, arousal: float) -> tuple[str, str]:
    """从双轴数值推导情绪标签和 emoji"""
    if mood >= 70:
        if arousal >= 70:   return "excited", "🤩"    # 兴奋
        if arousal >= 30:   return "happy", "😊"      # 开心
        return "content", "😌"                         # 满足
    if mood >= 30:
        if arousal >= 70:   return "annoyed", "😤"    # 烦躁
        if arousal >= 30:   return "neutral", "😐"    # 平静
        return "sleepy", "😴"                          # 犯困
    # mood < 30
    if arousal >= 70:       return "angry", "😠"      # 愤怒
    if arousal >= 30:       return "sad", "😞"        # 难过
    return "depressed", "😢"                           # 低落


@dataclass
class PersonProfile:
    """某个人的社交档案"""
    user_id: str = ""
    name: str = "未知"
    affection: float = 50.0       # 0~100，无负值
    impression: str = ""           # 一句话印象/关系
    last_interaction: float = 0.0
    interaction_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PersonProfile":
        return cls(
            user_id=data.get("user_id", ""),
            name=data.get("name", "未知"),
            affection=max(0.0, float(data.get("affection", 50.0))),
            impression=data.get("impression", ""),
            last_interaction=float(data.get("last_interaction", 0.0)),
            interaction_count=int(data.get("interaction_count", 0)),
        )

    @property
    def affection_level(self) -> str:
        a = self.affection
        if a >= 80: return "很亲近"
        if a >= 60: return "好感"
        if a >= 40: return "一般"
        if a >= 20: return "不太熟"
        return "陌生"

    def format_for_llm(self) -> str:
        line = f"{self.name}({self.user_id}): 好感={self.affection:.0f}({self.affection_level})"
        if self.impression:
            line += f", 关系: {self.impression}"
        return line


@dataclass
class EmotionState:
    """双轴情绪状态"""
    mood: float = 50.0           # 0=很伤心 50=平常 100=很开心
    arousal: float = 50.0        # 0=犯困 50=正常 100=很激动
    reason: str = ""
    updated_at: float = 0.0
    last_any_interaction: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EmotionState":
        return cls(
            mood=float(data.get("mood", 50.0)),
            arousal=float(data.get("arousal", 50.0)),
            reason=data.get("reason", ""),
            updated_at=float(data.get("updated_at", 0.0)),
            last_any_interaction=float(data.get("last_any_interaction", 0.0)),
        )

    def _clamp(self):
        self.mood = max(0.0, min(100.0, self.mood))
        self.arousal = max(0.0, min(100.0, self.arousal))

    @property
    def label(self) -> str:
        return derive_emotion_label(self.mood, self.arousal)[0]

    @property
    def emoji(self) -> str:
        return derive_emotion_label(self.mood, self.arousal)[1]

    @property
    def is_negative(self) -> bool:
        """心情差且不是在犯困"""
        return self.mood < 30 and self.arousal >= 20

    @property
    def is_sleepy(self) -> bool:
        return self.mood < 70 and self.arousal < 30

    def update(self, mood_delta: float = 0, arousal_delta: float = 0, reason: str = ""):
        """渐进式更新"""
        self.mood += mood_delta
        self.arousal += arousal_delta
        self._clamp()
        if reason:
            self.reason = reason
        self.updated_at = time.time()

    def mark_interaction(self):
        self.last_any_interaction = time.time()

    def format_for_llm(self) -> str:
        label, emoji = derive_emotion_label(self.mood, self.arousal)
        line = f"当前情绪: {emoji} {label} (心情: {self.mood:.0f}/100, 激动: {self.arousal:.0f}/100)"
        if self.reason:
            line += f" — {self.reason}"
        return line

    def check_idle_decay(self, lonely_minutes: int = 30, sleepy_minutes: int = 60):
        """长时间没人理 → mood 降 + arousal 降"""
        if self.last_any_interaction <= 0:
            return False

        idle_min = (time.time() - self.last_any_interaction) / 60.0
        changed = False

        if idle_min >= sleepy_minutes:
            # 很久没人理：心情降，激动度降（犯困）
            target_mood = max(20.0, 50.0 - idle_min * 0.3)
            target_arousal = max(10.0, 50.0 - idle_min * 0.5)
            if self.mood > target_mood or self.arousal > target_arousal:
                self.mood = min(self.mood, target_mood)
                self.arousal = min(self.arousal, target_arousal)
                self.reason = f"已经 {int(idle_min)} 分钟没人理了"
                self.updated_at = time.time()
                changed = True
        elif idle_min >= lonely_minutes:
            # 有点久没人理：心情小降
            target_mood = max(30.0, 50.0 - idle_min * 0.2)
            if self.mood > target_mood:
                self.mood = min(self.mood, target_mood)
                self.reason = f"已经 {int(idle_min)} 分钟没人说话了"
                self.updated_at = time.time()
                changed = True

        return changed

    def natural_decay_toward_neutral(self):
        """情绪自然回归 50/50（缓慢）"""
        elapsed = time.time() - self.updated_at
        rate = elapsed / 600.0  # 每 10 分钟衰减一次

        if abs(self.mood - 50) > 1:
            self.mood += (50 - self.mood) * min(rate * 0.1, 0.3)
        if abs(self.arousal - 50) > 1:
            self.arousal += (50 - self.arousal) * min(rate * 0.1, 0.3)

        self._clamp()
        self.updated_at = time.time()


class SocialSystem:
    """社交系统管理器"""

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._profiles: Dict[str, PersonProfile] = {}
        self._emotion = EmotionState()
        self._group_names: Dict[str, str] = {}
        self._load()

    # ── 好感/印象 ──

    def get_profile(self, user_id: str) -> PersonProfile:
        uid = str(user_id)
        if uid not in self._profiles:
            self._profiles[uid] = PersonProfile(user_id=uid)
        return self._profiles[uid]

    def update_profile(self, user_id: str, *, name: str = None,
                       affection_delta: float = 0, impression: str = None):
        p = self.get_profile(user_id)
        if name:
            p.name = name
        p.affection = max(0.0, min(100.0, p.affection + affection_delta))
        if impression is not None and impression.strip():
            p.impression = impression.strip()
        p.last_interaction = time.time()
        p.interaction_count += 1
        self._save()

    def get_top_affection(self, n: int = 5, exclude: set = None) -> List[PersonProfile]:
        exclude = exclude or set()
        profiles = [p for p in self._profiles.values()
                    if p.interaction_count > 0 and p.user_id not in exclude]
        profiles.sort(key=lambda p: p.affection, reverse=True)
        return profiles[:n]

    def format_profiles_for_llm(self, user_ids: list = None) -> str:
        if user_ids:
            profiles = [self.get_profile(uid) for uid in user_ids]
        else:
            profiles = [p for p in self._profiles.values() if p.interaction_count > 0]
        if not profiles:
            return "(暂无社交记录)"
        profiles.sort(key=lambda p: p.affection, reverse=True)
        return "\n".join(p.format_for_llm() for p in profiles[:20])

    # ── 私聊路由 ──

    def pick_private_target(
        self,
        *,
        configured_targets: List[str] = None,
        enable_affection_route: bool = True,
        umo_lookup: callable = None,
        min_affection: float = 70.0,
    ) -> Optional[str]:
        if configured_targets:
            for t in configured_targets:
                tid = str(t).strip()
                if tid and (not umo_lookup or umo_lookup(tid)):
                    return tid
        if enable_affection_route:
            for p in self.get_top_affection(5):
                if p.affection >= min_affection:
                    if not umo_lookup or umo_lookup(p.user_id):
                        return p.user_id
        return None

    # ── 情绪 ──

    @property
    def emotion(self) -> EmotionState:
        self._emotion.natural_decay_toward_neutral()
        return self._emotion

    def update_emotion(self, mood_delta: float = 0, arousal_delta: float = 0, reason: str = ""):
        self._emotion.update(mood_delta, arousal_delta, reason)
        self._save()

    def mark_interaction(self):
        self._emotion.mark_interaction()
        self._save()

    def check_idle(self, lonely_minutes: int = 30, sleepy_minutes: int = 60) -> bool:
        changed = self._emotion.check_idle_decay(lonely_minutes, sleepy_minutes)
        if changed:
            self._save()
        return changed

    # ── 群名 ──

    def register_group_name(self, group_id: str, group_name: str):
        gid = str(group_id)
        if group_name and self._group_names.get(gid) != group_name:
            self._group_names[gid] = group_name
            self._save()

    def get_group_name(self, group_id: str) -> str:
        return self._group_names.get(str(group_id), f"群{group_id}")

    def format_groups_for_llm(self) -> str:
        if not self._group_names:
            return "(暂无群聊记录)"
        return "\n".join(f"  {gid}: {name}" for gid, name in self._group_names.items())

    # ── 持久化 ──

    def _save(self):
        try:
            data = {
                "profiles": {uid: p.to_dict() for uid, p in self._profiles.items()},
                "emotion": self._emotion.to_dict(),
                "group_names": self._group_names,
            }
            path = os.path.join(self._data_dir, "social.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"[KuukiYomi] 社交保存失败: {e}")

    def _load(self):
        try:
            path = os.path.join(self._data_dir, "social.json")
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for uid, pdata in data.get("profiles", {}).items():
                self._profiles[uid] = PersonProfile.from_dict(pdata)
            if "emotion" in data:
                self._emotion = EmotionState.from_dict(data["emotion"])
            self._group_names = data.get("group_names", {})
        except Exception as e:
            logger.debug(f"[KuukiYomi] 社交加载失败: {e}")
