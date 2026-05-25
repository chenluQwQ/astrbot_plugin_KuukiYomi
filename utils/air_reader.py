"""
读空气 - 小模型判断引擎

核心流程：
1. 每条群消息先过规则层（关键词、@、冷却）
2. 规则层通过后，调小模型做结构化判断
3. 小模型返回 JSON：要不要回复、私聊谁、情绪变化、好感变化
4. 根据判断结果路由：群回复 / 私聊 / 沉默
"""

from __future__ import annotations

import json
import re
import random
import time
from typing import Dict

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


def extract_json(text: str) -> dict:
    """从模型返回的文本中稳健提取 JSON"""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"无法提取 JSON: {text[:200]}")


class AirReader:
    """读空气决策器"""

    def __init__(self):
        self._last_reply_ts: Dict[str, float] = {}
        self._in_progress: Dict[str, bool] = {}

    @staticmethod
    def _chat_key(platform: str, chat_id: str) -> str:
        return f"{platform}:{chat_id}"

    def is_cooling_down(self, platform: str, chat_id: str, cooldown_seconds: int) -> bool:
        key = self._chat_key(platform, chat_id)
        return (time.time() - self._last_reply_ts.get(key, 0)) < cooldown_seconds

    def mark_replied(self, platform: str, chat_id: str):
        self._last_reply_ts[self._chat_key(platform, chat_id)] = time.time()

    def is_busy(self, platform: str, chat_id: str) -> bool:
        return self._in_progress.get(self._chat_key(platform, chat_id), False)

    def set_busy(self, platform: str, chat_id: str, busy: bool = True):
        self._in_progress[self._chat_key(platform, chat_id)] = busy

    # ── 规则层 ──

    def should_trigger(
        self,
        event: AstrMessageEvent,
        *,
        probability: float = 0.15,
        keywords: list = None,
        blacklist_keywords: list = None,
        private_keywords: list = None,
        cooldown_seconds: int = 30,
        bot_name: str = "",
        bot_id: str = "",
    ) -> tuple[bool, bool]:
        """
        返回: (should_trigger, is_private_keyword_hit)
        """
        message_text = (event.message_str or "").strip()
        if not message_text:
            return False, False

        platform = event.get_platform_name()
        chat_id = event.get_group_id() or event.get_sender_id()
        if not chat_id:
            return False, False

        if blacklist_keywords:
            for kw in blacklist_keywords:
                if kw and kw in message_text:
                    return False, False

        if self.is_busy(platform, chat_id):
            return False, False

        is_at_me = self._check_at_me(event, bot_id)

        # bot名字命中 = 等同 @，不受冷却限制
        is_name_hit = bool(bot_name and bot_name in message_text)

        if is_at_me or is_name_hit:
            return True, False

        # 私聊关键词（不受冷却限制）
        if private_keywords:
            for entry in private_keywords:
                kw = str(entry).split(":")[0].strip() if ":" in str(entry) else str(entry)
                if kw and kw in message_text:
                    return True, True

        # 普通关键词（不受冷却限制）
        if keywords:
            for kw in keywords:
                if kw and kw in message_text:
                    return True, False

        # 只有随机触发才受冷却限制
        if self.is_cooling_down(platform, chat_id, cooldown_seconds):
            return False, False

        if random.random() < probability:
            return True, False

        return False, False

    @staticmethod
    def _check_at_me(event: AstrMessageEvent, bot_id: str) -> bool:
        try:
            if not hasattr(event, "message_obj") or not event.message_obj:
                return False
            if not hasattr(event.message_obj, "message"):
                return False
            from astrbot.api.message_components import At
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    if str(getattr(comp, "qq", "")) == bot_id:
                        return True
        except Exception:
            pass
        return False

    # ── 小模型 prompt ──

    @staticmethod
    def build_judge_prompt(
        *,
        chat_history_text: str,
        current_message: str,
        sender_name: str,
        sender_id: str,
        bot_name: str = "",
        group_name: str = "",
        group_id: str = "",
        emotion_text: str = "",
        social_text: str = "",
        groups_text: str = "",
        persona_summary: str = "",
        is_private_keyword_hit: bool = False,
        private_keywords_config: list = None,
        topic_interests_config: list = None,
        custom_judge_prompt: str = "",
    ) -> str:

        pk_info = ""
        if private_keywords_config:
            pk_lines = []
            for entry in private_keywords_config:
                parts = str(entry).split(":", 1)
                if len(parts) == 2:
                    pk_lines.append(f"  当聊到「{parts[0].strip()}」→ 考虑私聊通知 {parts[1].strip()}")
            if pk_lines:
                pk_info = "\n私聊触发规则:\n" + "\n".join(pk_lines)

        topic_info = ""
        if topic_interests_config:
            ti_lines = []
            for entry in topic_interests_config:
                parts = str(entry).split(":", 1)
                if len(parts) == 2:
                    ti_lines.append(f"  「{parts[0].strip()}」话题 → 考虑通知 {parts[1].strip()}")
            if ti_lines:
                topic_info = "\n话题通知规则（语义匹配，不需要精确出现关键词）:\n" + "\n".join(ti_lines)

        prompt = f"""你是群聊 AI 的决策系统。根据以下信息判断如何回应最新消息。

## 角色设定
{persona_summary or "智能助手"}

## 当前状态
{emotion_text or "情绪: 正常"}

## 社交关系
{social_text or "(暂无)"}

## 已知群聊
{groups_text or "(暂无)"}

## 当前场景
群: {group_name}({group_id})
发送者: {sender_name}({sender_id})

## 最近聊天记录
{chat_history_text}

## 最新消息
{sender_name}: {current_message}
{pk_info}
{topic_info}

{custom_judge_prompt}

以 JSON 回复:
```json
{{
    "scores": {{
        "reply_need": 5,
        "share_value": 5,
        "topic_match": 5,
        "emotional_weight": 5,
        "timing": 5
    }},
    "action": "reply/silent/private",
    "private_target": "QQ号(仅private时)",
    "private_content": "为什么要私聊对方、聊什么话题(仅private时，简短描述即可)",
    "mood_delta": 0,
    "arousal_delta": 0,
    "emotion_reason": "简短原因",
    "affection_updates": {{"发送者QQ号": 1}},
    "reasoning": "判断理由"
}}
```

scores 说明（每项 0~10）:
- reply_need: 这条消息和bot相关吗？（直接提问/聊到bot话题=高，和bot完全无关的闲聊=低，有趣的话题=中）
- share_value: 值得分享给别人吗？（有趣/重要/和某人相关=高）
- topic_match: 和某人感兴趣的话题匹配度
- emotional_weight: 情感触发强度（求助/吐槽/分享心情/开玩笑=中~高，纯信息=低）
- timing: 时机合适吗？（话题正热=高，已经聊完换话题了=低）

action 说明:
- reply: 在群里回复（内容由主模型生成）
- silent: 沉默
- private: 私聊某人（吐槽/报告/不方便在群里说的话）

mood_delta: 心情变化（-10~+10，正=开心 负=伤心）
arousal_delta: 激动度变化（-10~+10，正=激动 负=平静）
affection_updates: 好感变化 0~5（不能为负）

**只回复 JSON，不要包含其他内容！**"""

        return prompt

    # ── 解析判断结果 ──

    @staticmethod
    def parse_judge_result(text: str) -> dict:
        try:
            data = extract_json(text)
        except ValueError as e:
            logger.warning(f"[KuukiYomi] 小模型返回解析失败: {e}")
            return {"action": "silent", "reasoning": f"解析失败: {e}", "overall": 0}

        action = data.get("action", "silent")
        if action not in ("reply", "silent", "private"):
            action = "silent"

        # 评分提取
        raw_scores = data.get("scores") or {}
        score_keys = ["reply_need", "share_value", "topic_match", "emotional_weight", "timing"]
        scores = {}
        for k in score_keys:
            try:
                scores[k] = max(0.0, min(10.0, float(raw_scores.get(k, 5))))
            except (TypeError, ValueError):
                scores[k] = 5.0

        # 加权综合分（均衡分配，避免单项主导）
        weights = {"reply_need": 0.25, "share_value": 0.15, "topic_match": 0.15,
                   "emotional_weight": 0.25, "timing": 0.2}
        overall = sum(scores[k] * weights[k] for k in score_keys)

        aff_updates = {}
        raw_aff = data.get("affection_updates") or {}
        if isinstance(raw_aff, dict):
            for uid, delta in raw_aff.items():
                try:
                    aff_updates[str(uid)] = max(0.0, min(5.0, float(delta)))
                except (TypeError, ValueError):
                    pass

        try:
            mood_d = max(-10.0, min(10.0, float(data.get("mood_delta", 0))))
        except (TypeError, ValueError):
            mood_d = 0.0
        try:
            arousal_d = max(-10.0, min(10.0, float(data.get("arousal_delta", 0))))
        except (TypeError, ValueError):
            arousal_d = 0.0

        return {
            "scores": scores,
            "overall": overall,
            "action": action,
            "private_target": str(data.get("private_target", "")),
            "private_content": str(data.get("private_content", "")),
            "mood_delta": mood_d,
            "arousal_delta": arousal_d,
            "emotion_reason": str(data.get("emotion_reason", "")),
            "affection_updates": aff_updates,
            "reasoning": str(data.get("reasoning", "")),
        }

    @staticmethod
    def contains_no_response(text: str, tag: str = "<NO_RESPONSE>") -> bool:
        return tag in text

    # ── 回复后评估 prompt（更新好感/印象/情绪） ──

    @staticmethod
    def build_post_reply_prompt(
        *,
        sender_name: str,
        sender_id: str,
        user_message: str,
        bot_reply: str,
        current_affection: float,
        current_impression: str,
        current_emotion: str,
    ) -> str:
        return f"""你是 AI 的内部评估系统。刚才有一段对话发生了，请评估变化。

## 对话
用户 {sender_name}({sender_id}) 说: {user_message}
你回复了: {bot_reply}

## 当前状态
对 {sender_name} 的好感: {current_affection:.0f}/100
对 {sender_name} 的印象: {current_impression or "暂无"}
{current_emotion}

以 JSON 回复:
```json
{{
    "affection_delta": 0,
    "impression": "一句话印象/关系",
    "mood_delta": 0,
    "arousal_delta": 0,
    "emotion_reason": "简短原因"
}}
```

规则:
- affection_delta: 好感变化 0~5（正常 +1，被夸 +2~3，被骂不减保持 0，帮忙 +3~5）
- impression: 一句话关系描述（如"主人，很关心我""群里的话痨"）
- mood_delta: 心情变化 -10~+10（被夸开心+，被骂伤心-，正常聊天小+）
- arousal_delta: 激动变化 -10~+10（吵架+，安慰-，正常聊天接近0）

**只回复 JSON！**"""

    @staticmethod
    def parse_post_reply_result(text: str) -> dict:
        try:
            data = extract_json(text)
        except ValueError:
            return {}

        try:
            delta = max(0.0, min(5.0, float(data.get("affection_delta", 0))))
        except (TypeError, ValueError):
            delta = 0.0
        try:
            mood_d = max(-10.0, min(10.0, float(data.get("mood_delta", 0))))
        except (TypeError, ValueError):
            mood_d = 0.0
        try:
            arousal_d = max(-10.0, min(10.0, float(data.get("arousal_delta", 0))))
        except (TypeError, ValueError):
            arousal_d = 0.0

        return {
            "affection_delta": delta,
            "impression": str(data.get("impression", "")),
            "mood_delta": mood_d,
            "arousal_delta": arousal_d,
            "emotion_reason": str(data.get("emotion_reason", "")),
        }
