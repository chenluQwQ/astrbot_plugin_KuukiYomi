"""
读空气 (KuukiYomi) - AstrBot 智能群聊感知插件

功能：
1. 对话缓存：群聊/私聊分桶缓存
2. 读空气：小模型判断 → 群回复 / 私聊 / 沉默
3. 社交系统：好感度 / 印象 / 情绪状态
4. LLM 工具：主动发消息、跨群读缓存、通讯录
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime
from typing import Optional

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.star import StarTools
    HAS_STARTOOLS = True
except ImportError:
    HAS_STARTOOLS = False

try:
    from astrbot.api import llm_tool
    HAS_LLM_TOOL = True
except ImportError:
    def llm_tool(*args, **kwargs):
        def decorator(func):
            return func
        return decorator
    HAS_LLM_TOOL = False

from .utils import ConversationCache, CachedMessage, AirReader, SocialSystem, MemoryBridge
from .utils.air_reader import extract_json
from .utils.cross_group import CrossGroupHandler


@register(
    "kuukiyomi",
    "chenluQwQ",
    "读空气 - 智能群聊感知：小模型读空气、私聊路由、好感/情绪系统",
    "0.2.0",
    "https://github.com/chenluQwQ/astrbot_plugin_KuukiYomi",
)
class KuukiYomi(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        data_dir = self._get_data_dir()

        # 缓存
        cache_cfg = self.cfg.get("cache_settings") or {}
        self.cache = ConversationCache(
            group_maxlen=int(cache_cfg.get("group_cache_size", 200)),
            private_maxlen=int(cache_cfg.get("private_cache_size", 100)),
            persist_dir=data_dir,
            persist_interval=int(cache_cfg.get("persist_interval_seconds", 60)),
        )

        # 读空气引擎
        self.air = AirReader()

        # 社交系统
        self.social = SocialSystem(data_dir)

        # 记忆桥接（联动晨露记忆插件(astrbot_plugin_shuangxu_memory)，未安装时自动降级）
        self.mem_bridge = MemoryBridge(
            astrbot_data_dir=os.path.join(os.getcwd(), "data")
        )

        # UMO 注册表
        self._umo_registry: dict = {}
        self._load_umo_registry()

        # 自动通讯录
        self._contacts: dict = {}
        self._load_contacts()

        # 全局主动私聊冷却
        self._last_private_send_ts: float = 0

        # 跨群扫描
        self._cross_group = CrossGroupHandler(self)

        # 今日主动聊天话题记录（防重复）
        self._proactive_topic_log: list = []
        self._load_proactive_log()

        # 私聊上下文注入：target_id → [fake_tool_call_messages]
        self._pending_private_ctx: dict = {}
        self._load_pending_ctx()

        # 监听模式：group_id → {"until": float, "remaining": int}
        self._listening_groups: dict = {}

        # 最近私聊发送记录：user_id → timestamp（用于群聊注入私聊上下文）
        self._recent_private_sends: dict = {}

        # 缓存的人格摘要（给 scheduler 用，因为 scheduler 没有 event）
        self._cached_persona: str = ""


    # ── 生命周期 ──

    async def initialize(self):
        self.cache.start_persist_loop()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

        # 启动时尝试加载人设
        await self._load_persona_standalone()
        if self._cached_persona:
            logger.info(f"[KuukiYomi] 插件已启动 ✨ 人设已加载（{len(self._cached_persona)}字）")
        else:
            logger.warning("[KuukiYomi] 插件已启动 ✨ ⚠️ 未能加载人设，主动消息将不带人设生成，等待首条群消息触发后缓存")

        if HAS_LLM_TOOL:
            tools_cfg = self.cfg.get("tools_settings") or {}
            if tools_cfg.get("enable_send_message", True):
                self.context.activate_llm_tool("kuuki_send_message")
                self.context.activate_llm_tool("kuuki_list_contacts")
            if tools_cfg.get("enable_read_cache", True):
                self.context.activate_llm_tool("kuuki_read_cache")
            if tools_cfg.get("enable_forward_history", True):
                self.context.activate_llm_tool("kuuki_forward_history")

    async def terminate(self):
        if hasattr(self, '_scheduler_task') and not self._scheduler_task.done():
            self._scheduler_task.cancel()
        await self.cache.stop()
        self.cache.force_save()
        self._save_umo_registry()
        self._save_contacts()
        self._save_pending_ctx()
        self._save_proactive_log()
        logger.info("[KuukiYomi] 插件已停止，缓存已保存")

    # ── 辅助方法 ──

    def _get_data_dir(self) -> str:
        if HAS_STARTOOLS:
            p = str(StarTools.get_data_dir() / "kuukiyomi")
        else:
            p = os.path.join(os.getcwd(), "data", "plugin_data", "kuukiyomi")
        os.makedirs(p, exist_ok=True)
        return p

    def _cfg_group(self, group: str, key: str, default=None):
        g = self.cfg.get(group)
        return g.get(key, default) if isinstance(g, dict) else default

    def _is_group_enabled(self, group_id: str) -> bool:
        gf = self.cfg.get("group_filter") or {}
        gid = str(group_id).strip()
        blocked = {str(g).strip() for g in gf.get("blocked_groups", []) if str(g).strip()}
        if gid in blocked:
            return False
        if gf.get("enable_all_groups", False):
            return True
        enabled = {str(g).strip() for g in gf.get("enabled_groups", []) if str(g).strip()}
        return gid in enabled

    def _get_bot_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_self_id() or "")
        except Exception:
            return ""

    def _get_bot_name(self, event: AstrMessageEvent) -> str:
        return self._cfg_group("air_reading", "bot_name", "")

    def _is_curfew(self) -> bool:
        """宵禁时段不主动发消息"""
        adv = self.cfg.get("advanced") or {}
        start = adv.get("curfew_start", "")
        end = adv.get("curfew_end", "")
        if not start or not end:
            return False
        try:
            now = datetime.now().strftime("%H:%M")
            if start <= end:
                return start <= now <= end
            else:  # 跨午夜，如 23:00~07:00
                return now >= start or now <= end
        except Exception:
            return False

    def _get_user_last_message_ts(self, user_id: str) -> float:
        """获取某用户最后一次发消息的时间戳（遍历所有缓存桶）"""
        latest = 0.0
        uid = str(user_id)
        try:
            for key in self.cache.get_all_keys():
                platform, chat_type, chat_id = self.cache.parse_key(key)
                # 私聊：直接取该用户的桶
                if chat_type == "private" and chat_id == uid:
                    msgs = self.cache.get_recent(platform, True, chat_id, 5)
                    for m in msgs:
                        if not m.is_bot and m.timestamp > latest:
                            latest = m.timestamp
                # 群聊：看这个用户在群里的最新发言
                elif chat_type == "group":
                    msgs = self.cache.get_recent(platform, False, chat_id, 30)
                    for m in msgs:
                        if not m.is_bot and m.sender_id == uid and m.timestamp > latest:
                            latest = m.timestamp
        except Exception:
            pass
        return latest

    # ── 伪造 tool_call 注入 ──

    FAKE_CALL_PREFIX = "kuuki_ctx_"

    def _build_fake_tool_call(self, group_name: str, group_id: str,
                               history_text: str, bot_message: str,
                               target_name: str) -> list:
        """构造 fake_tool_call 消息对，注入私聊上下文"""
        call_id = self.FAKE_CALL_PREFIX + uuid.uuid4().hex[:12]

        content_parts = [f"来自群「{group_name}」({group_id}) 的最近聊天记录：",
                         history_text]
        if bot_message:
            content_parts.append(f"\n你主动找 {target_name} 说了：{bot_message}")

        assistant_msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "kuuki_read_group_context",
                    "arguments": json.dumps({"group": group_name}, ensure_ascii=False),
                }
            }]
        }

        tool_msg = {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "kuuki_read_group_context",
            "content": "\n".join(content_parts),
        }

        return [assistant_msg, tool_msg]

    @staticmethod
    def _clean_old_fake_calls(contexts: list) -> int:
        """从 contexts 中清除旧的 kuuki fake_tool_call"""
        to_remove = []
        for i, msg in enumerate(contexts):
            if not isinstance(msg, dict):
                continue
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                if isinstance(tc, dict) and str(tc.get("id", "")).startswith("kuuki_ctx_"):
                    to_remove.append(i)
                    break
            if msg.get("role") == "tool" and str(msg.get("tool_call_id", "")).startswith("kuuki_ctx_"):
                to_remove.append(i)

        for i in sorted(set(to_remove), reverse=True):
            contexts.pop(i)
        return len(to_remove)

    # ── UMO 注册表 ──

    def _register_umo(self, chat_id: str, umo: str):
        if chat_id and umo:
            key = str(chat_id)
            if self._umo_registry.get(key) != umo:
                self._umo_registry[key] = umo
                self._save_umo_registry()

    def _lookup_umo(self, target_id: str) -> str | None:
        return self._umo_registry.get(str(target_id))

    def _save_umo_registry(self):
        try:
            path = os.path.join(self._get_data_dir(), "umo_registry.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._umo_registry, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"[KuukiYomi] UMO 保存失败: {e}")

    def _load_umo_registry(self):
        try:
            path = os.path.join(self._get_data_dir(), "umo_registry.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    self._umo_registry = json.load(f)
        except Exception:
            pass

    # ── 通讯录 ──

    def _register_contact(self, user_id: str, name: str):
        if user_id and name:
            key = str(user_id)
            if self._contacts.get(key) != name:
                self._contacts[key] = name
                self._save_contacts()

    def _save_contacts(self):
        try:
            path = os.path.join(self._get_data_dir(), "contacts.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._contacts, f, ensure_ascii=False)
        except Exception:
            pass

    def _load_contacts(self):
        try:
            path = os.path.join(self._get_data_dir(), "contacts.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    self._contacts = json.load(f)
        except Exception:
            pass

    # ── pending context 持久化 ──

    def _save_pending_ctx(self):
        try:
            path = os.path.join(self._get_data_dir(), "pending_ctx.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._pending_private_ctx, f, ensure_ascii=False)
        except Exception:
            pass

    def _load_pending_ctx(self):
        try:
            path = os.path.join(self._get_data_dir(), "pending_ctx.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    self._pending_private_ctx = json.load(f)
        except Exception:
            pass

    # ── 主动聊天话题日志（防重复） ──

    def _save_proactive_log(self):
        try:
            path = os.path.join(self._get_data_dir(), "proactive_log.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._proactive_topic_log, f, ensure_ascii=False)
        except Exception:
            pass

    def _load_proactive_log(self):
        try:
            path = os.path.join(self._get_data_dir(), "proactive_log.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    self._proactive_topic_log = json.load(f)
            # 启动时清理非今天的记录
            self._clean_proactive_log()
        except Exception:
            self._proactive_topic_log = []

    def _clean_proactive_log(self):
        """清理非今天的记录"""
        today = datetime.now().strftime("%Y-%m-%d")
        before = len(self._proactive_topic_log)
        self._proactive_topic_log = [
            e for e in self._proactive_topic_log if e.get("date") == today
        ]
        if len(self._proactive_topic_log) != before:
            self._save_proactive_log()

    def _log_proactive_topic(self, target_name: str, content: str, source: str = "idle"):
        """记录一条主动聊天话题"""
        self._clean_proactive_log()
        self._proactive_topic_log.append({
            "ts": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "target": target_name,
            "content": content,
            "source": source,  # idle / emotion / cross_group
        })
        self._save_proactive_log()

    def _get_today_proactive_summary(self, target_name: str = "") -> str:
        """获取今天主动聊天的摘要文本，供生成 prompt 参考"""
        self._clean_proactive_log()
        if not self._proactive_topic_log:
            return ""

        entries = self._proactive_topic_log
        if target_name:
            entries = [e for e in entries if e.get("target") == target_name]

        if not entries:
            return ""

        lines = ["【今天已主动聊过的话题（不要重复）】"]
        for e in entries:
            t = e.get("target", "?")
            c = e.get("content", "")[:80]
            src = {"idle": "闲聊", "emotion": "情绪倾诉", "cross_group": "话题通知"}.get(e.get("source", ""), "主动")
            lines.append(f"  - 对 {t}（{src}）: {c}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════
    #  群消息处理：缓存 + 读空气 + 小模型判断
    # ══════════════════════════════════════════════════════════

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=900)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self.cfg.get("enable", True):
            return

        platform = event.get_platform_name()
        group_id = event.get_group_id()
        if not group_id:
            return

        message_text = (event.message_str or "").strip()
        if not message_text:
            return

        sender_id = event.get_sender_id() or "unknown"
        sender_name = event.get_sender_name() or "未知"

        # ── 缓存消息 ──
        self.cache.append(platform, False, group_id, CachedMessage(
            sender_id=sender_id,
            sender_name=sender_name,
            content=message_text,
            timestamp=time.time(),
            is_bot=False,
        ))

        # ── 注册 UMO + 通讯录 + 群名 ──
        umo = event.unified_msg_origin or ""
        if umo:
            self._register_umo(group_id, umo)
        self._register_contact(sender_id, sender_name)

        # 尝试抓群名
        try:
            raw = getattr(event.message_obj, "raw_message", None) or {}
            if isinstance(raw, dict):
                gname = raw.get("group_name", "")
            else:
                gname = getattr(raw, "group_name", "") if raw else ""
            if gname:
                self.social.register_group_name(group_id, gname)
        except Exception:
            pass

        # 标记有人在聊天（用于空闲检测）
        self.social.mark_interaction()

        # ── 读空气：是否触发判断 ──
        if not self._is_group_enabled(group_id):
            return

        air_cfg = self.cfg.get("air_reading") or {}
        if not air_cfg.get("enable_air_reading", True):
            return

        # 宵禁
        if self._is_curfew():
            return

        # 跳过已经被 @ 唤醒的消息（让正常流程处理）
        if event.is_at_or_wake_command:
            return

        should, is_pk_hit = self.air.should_trigger(
            event,
            probability=float(air_cfg.get("trigger_probability", 0.15)),
            keywords=air_cfg.get("keywords") or [],
            blacklist_keywords=air_cfg.get("blacklist_keywords") or [],
            private_keywords=air_cfg.get("private_keywords") or [],
            cooldown_seconds=int(air_cfg.get("cooldown_seconds", 30)),
            bot_name=self._get_bot_name(event),
            bot_id=self._get_bot_id(event),
        )

        # ── 监听模式：bot 刚回复或关键词未回复后，强制触发判断 ──
        is_listening = False
        listen_info = self._listening_groups.get(group_id)
        if listen_info:
            if time.time() > listen_info["until"] or listen_info["remaining"] <= 0:
                # 过期或次数用完，清除
                self._listening_groups.pop(group_id, None)
            else:
                is_listening = True
                listen_info["remaining"] -= 1
                if not should:
                    should = True
                    logger.debug(f"[KuukiYomi] 监听模式触发 | 群={group_id} | 剩余={listen_info['remaining']}")

        if not should:
            return

        # 检查是否关键词触发
        is_keyword_hit = False
        msg_text = (event.message_str or "").strip()
        for kw in (air_cfg.get("keywords") or []):
            if kw and kw in msg_text:
                is_keyword_hit = True
                break

        # ── 调小模型判断 ──
        self.air.set_busy(platform, group_id, True)
        try:
            await self._judge_and_route(event, air_cfg, is_pk_hit,
                                        is_listening=is_listening,
                                        is_keyword_hit=is_keyword_hit)
        except Exception as e:
            logger.error(f"[KuukiYomi] 判断异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
        finally:
            self.air.set_busy(platform, group_id, False)

    async def _judge_and_route(self, event: AstrMessageEvent, air_cfg: dict, is_pk_hit: bool,
                               *, is_listening: bool = False, is_keyword_hit: bool = False):
        """调小模型判断 → 根据结果路由"""

        # 获取小模型 provider
        judge_provider_name = self._cfg_group("air_reading", "judge_provider_name", "")
        if not judge_provider_name:
            logger.debug("[KuukiYomi] 未配置小模型 provider，跳过判断")
            return

        try:
            judge_provider = self.context.get_provider_by_id(judge_provider_name)
            if not judge_provider:
                logger.warning(f"[KuukiYomi] 找不到 provider: {judge_provider_name}")
                return
        except Exception as e:
            logger.error(f"[KuukiYomi] 获取 provider 失败: {e}")
            return

        platform = event.get_platform_name()
        group_id = event.get_group_id()
        sender_id = event.get_sender_id() or "unknown"
        sender_name = event.get_sender_name() or "未知"

        # 构建判断 prompt
        history_count = int(air_cfg.get("history_count", 20))
        history_text = self.cache.format_recent_for_llm(platform, False, group_id, history_count)

        # 人格摘要（简短版给小模型）
        persona_summary = await self._get_persona_summary(event)

        # 社交信息：优先用记忆插件的画像，否则用内置社交系统
        if self.mem_bridge.available:
            # 收集最近聊天参与者
            recent_msgs = self.cache.get_recent(platform, False, group_id, history_count)
            participant_ids = list({m.sender_id for m in recent_msgs if not m.is_bot})
            social_text = self.mem_bridge.format_social_context_for_judge(participant_ids)
            # 确保当前发言人有详细信息
            sender_detail = self.mem_bridge.format_user_for_judge(sender_id, sender_name)
            if sender_detail and sender_id not in social_text:
                social_text = sender_detail + "\n" + social_text
        else:
            social_text = self.social.format_profiles_for_llm()

        judge_prompt = AirReader.build_judge_prompt(
            chat_history_text=history_text,
            current_message=(event.message_str or "").strip(),
            sender_name=sender_name,
            sender_id=sender_id,
            bot_name=self._get_bot_name(event),
            group_name=self.social.get_group_name(group_id),
            group_id=group_id,
            emotion_text=self.social.emotion.format_for_llm(),
            social_text=social_text,
            groups_text=self.social.format_groups_for_llm(),
            persona_summary=persona_summary,
            is_private_keyword_hit=is_pk_hit,
            private_keywords_config=air_cfg.get("private_keywords") or [],
            topic_interests_config=air_cfg.get("topic_interests") or [],
            custom_judge_prompt=air_cfg.get("custom_judge_prompt", ""),
        )

        # 调小模型
        max_retries = int(air_cfg.get("judge_max_retries", 2)) + 1
        result = None

        for attempt in range(max_retries):
            try:
                llm_resp = await judge_provider.text_chat(
                    prompt=judge_prompt,
                    contexts=[],
                )
                content = llm_resp.completion_text.strip()
                logger.debug(f"[KuukiYomi] 小模型返回: {content[:200]}")

                result = AirReader.parse_judge_result(content)
                break
            except Exception as e:
                logger.warning(f"[KuukiYomi] 小模型调用失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    return

        if not result:
            return

        action = result["action"]
        overall = result.get("overall", 5.0)
        scores = result.get("scores", {})
        logger.info(f"[KuukiYomi] 判断: action={action}, overall={overall:.1f}, "
                    f"scores={scores}, reasoning={result.get('reasoning', '')[:50]}")

        # ── 更新社交系统（无论是否通过阈值都更新） ──
        self.social.update_emotion(
            mood_delta=result.get("mood_delta", 0),
            arousal_delta=result.get("arousal_delta", 0),
            reason=result.get("emotion_reason", ""),
        )
        for uid, delta in result.get("affection_updates", {}).items():
            name = self._contacts.get(uid, "")
            self.social.update_profile(uid, name=name, affection_delta=delta)

        # ── 阈值过滤：分数太低直接沉默 ──
        threshold = float(self._cfg_group("air_reading", "score_threshold", 3.5))
        # 小模型主动给出 action 时适当降低阈值（尊重模型判断）
        # private 更宽松：模型专门选了目标，说明判断了话题相关性
        if action == "private":
            effective_threshold = threshold * 0.6
        elif action == "reply":
            effective_threshold = threshold * 0.8
        else:
            effective_threshold = threshold
        if overall < effective_threshold and action != "silent":
            logger.debug(f"[KuukiYomi] 分数 {overall:.1f} < 阈值 {effective_threshold:.1f}，覆盖为沉默")
            action = "silent"

        # ── 路由 ──
        if action == "reply":
            event.is_at_or_wake_command = True
            event.set_extra("kuukiyomi_triggered", True)
            event.set_extra("kuukiyomi_sender_name", sender_name)
            event.set_extra("kuukiyomi_sender_id", sender_id)
            event.set_extra("kuukiyomi_history", history_text)
            self.air.mark_replied(platform, group_id)
            logger.info(f"[KuukiYomi] ✨ 触发群回复 | 群={group_id} | {sender_name}({sender_id}) | overall={overall:.1f}")

        elif action == "private":
            # 全局私聊冷却检查（单位：分钟，兼容旧秒数配置）
            raw_pm = int(self._cfg_group("advanced", "private_message_cooldown", 5))
            pm_cooldown = raw_pm * 60 if raw_pm <= 300 else raw_pm
            if hasattr(self, '_last_private_send_ts') and (time.time() - self._last_private_send_ts) < pm_cooldown:
                logger.debug(f"[KuukiYomi] 私聊冷却中，跳过")
            else:
                target = result.get("private_target", "")
                hint = result.get("private_content", "")  # 小模型给的只是话题/原因
                if target and hint:
                    target_name = self._contacts.get(target, target)
                    group_name = self.social.get_group_name(group_id)

                    # 先读私聊缓存，判断是否合适发送
                    private_history = ""
                    try:
                        platform = event.get_platform_name()
                        priv_msgs = self.cache.get(platform, True, str(target))
                        if priv_msgs:
                            recent = priv_msgs[-10:]
                            private_history = "\n".join(
                                f"[{m.sender_name}] {m.content}" for m in recent
                            )
                    except Exception:
                        pass

                    # 用主模型 + 人设生成实际内容（带私聊上下文）
                    content = await self._generate_private_content(
                        target_name=target_name,
                        hint=hint,
                        group_name=group_name,
                        history_text=history_text,
                        private_history=private_history,
                    )

                    if content:
                        fake_msgs = self._build_fake_tool_call(
                            group_name=group_name,
                            group_id=group_id,
                            history_text=history_text,
                            bot_message=content,
                            target_name=target_name,
                        )
                        self._pending_private_ctx[str(target)] = {
                            "messages": fake_msgs, "ts": time.time(),
                            "bot_message": content, "rounds_left": 6
                        }
                        self._save_pending_ctx()
                        await self._do_private_send(target, content)
                        logger.info(f"[KuukiYomi] 💌 私聊 → {target}: {content[:50]} | overall={overall:.1f}")

        else:
            logger.debug(f"[KuukiYomi] 😶 沉默 | overall={overall:.1f}")

        # ── 监听模式激活 ──
        group_id = event.get_group_id()
        if group_id and not is_listening:  # 避免监听模式自我循环
            listen_count = int(air_cfg.get("post_listen_count", 5))
            listen_minutes = float(air_cfg.get("post_listen_minutes", 2))

            if action == "reply" and air_cfg.get("enable_post_reply_listen", True):
                self._listening_groups[group_id] = {
                    "until": time.time() + listen_minutes * 60,
                    "remaining": listen_count,
                }
                logger.debug(f"[KuukiYomi] 🎧 进入监听模式（回复后）| 群={group_id} | {listen_count}条/{listen_minutes}分钟")

            elif action == "silent" and is_keyword_hit and air_cfg.get("enable_keyword_listen", True):
                self._listening_groups[group_id] = {
                    "until": time.time() + listen_minutes * 60,
                    "remaining": listen_count,
                }
                logger.debug(f"[KuukiYomi] 🎧 进入监听模式（关键词未回复）| 群={group_id} | {listen_count}条/{listen_minutes}分钟")

    async def _generate_private_content(self, *, target_name: str, hint: str,
                                         group_name: str, history_text: str,
                                         private_history: str = "") -> str:
        """用主模型 + 人设生成私聊内容，会参考私聊历史判断是否合适"""
        try:
            main_provider = self.context.get_using_provider()
            if not main_provider:
                return hint  # fallback 用小模型的原文

            persona = self._cached_persona or ""
            emotion = self.social.emotion.format_for_llm()

            private_ctx = ""
            if private_history:
                private_ctx = f"\n【你和 {target_name} 的私聊记录】（这是你们之前私下聊过的内容，请结合这些上下文回复）\n{private_history}\n"

            gen_prompt = f"""【群聊「{group_name}」最近的对话】（这是群里大家在聊的内容，你从这里产生了想私聊的念头）
{history_text}
{private_ctx}
你想私聊 {target_name}，原因：{hint}
{emotion}

先判断现在私聊是否合适（比如：最近在吵架、闹不愉快、对方明确不想聊等情况就不要发）。
如果不合适，只输出 <SKIP>。
如果合适，结合群聊话题和你们之前私聊的上下文，写一条简短的私聊消息（1~2句话），用中文，语气符合你的性格，不要使用emoji或颜文字。
只输出消息内容或 <SKIP>，不要其他任何东西。"""

            full_prompt = f"【你的人设】\n{persona}\n\n【任务】\n{gen_prompt}" if persona else gen_prompt
            resp = await main_provider.text_chat(prompt=full_prompt, contexts=[])
            content = resp.completion_text.strip()

            if content and "<SKIP>" in content:
                logger.info(f"[KuukiYomi] 主模型判断不适合私聊 {target_name}，跳过")
                return ""

            if content and len(content) < 300:
                return content
            return hint  # 生成失败 fallback
        except Exception as e:
            logger.debug(f"[KuukiYomi] 主模型生成私聊内容失败: {e}")
            return hint

    async def _do_private_send(self, target_id: str, content: str) -> bool:
        """执行私聊发送 + 存入缓存 + 更新全局冷却。返回是否成功发送。"""
        # 全局私聊冷却兜底（防止多路径同时触发）
        raw_pm = int(self._cfg_group("advanced", "private_message_cooldown", 5))
        pm_cooldown = raw_pm * 60 if raw_pm <= 300 else raw_pm
        if (time.time() - self._last_private_send_ts) < pm_cooldown:
            logger.debug(f"[KuukiYomi] _do_private_send: 全局冷却中，跳过 → {target_id}")
            return False

        umo = self._lookup_umo(str(target_id))
        if not umo:
            logger.warning(f"[KuukiYomi] 私聊失败: {target_id} 无 UMO 记录")
            return False
        try:
            chain = MessageChain().message(content)
            await self.context.send_message(umo, chain)
            # 更新全局私聊冷却
            self._last_private_send_ts = time.time()
            # 记录最近私聊对象（用于群聊注入私聊上下文）
            self._recent_private_sends[target_id] = time.time()
            # 存进缓存，对方回复时能看到上文
            self.cache.append("default", True, target_id, CachedMessage(
                sender_id="bot",
                sender_name="bot",
                content=content,
                timestamp=time.time(),
                is_bot=True,
            ))
            return True
        except Exception as e:
            logger.error(f"[KuukiYomi] 私聊发送异常: {e}")
            return False

    async def _get_persona_summary(self, event: AstrMessageEvent) -> str:
        """获取人格摘要（简短版给小模型），同时缓存完整人格给主模型用"""
        try:
            persona_mgr = getattr(self.context, "persona_manager", None)
            if not persona_mgr:
                return ""
            umo = event.unified_msg_origin
            persona = await persona_mgr.get_default_persona_v3(umo=umo)
            prompt = persona.get("prompt", "") if persona else ""
            # 缓存完整人格（给 scheduler 的主模型生成用）
            if prompt:
                self._cached_persona = prompt
            # 截取前 300 字给小模型（省 token）
            if len(prompt) > 300:
                return prompt[:300] + "..."
            return prompt
        except Exception:
            return ""

    async def _load_persona_standalone(self):
        """独立加载人设（不依赖 event），启动时和调度器中使用"""
        if self._cached_persona:
            return  # 已有缓存，不重复加载

        persona_mgr = getattr(self.context, "persona_manager", None)
        if not persona_mgr:
            logger.debug("[KuukiYomi] persona_manager 不可用")
            return

        # 尝试多种方式获取默认人设
        prompt = ""
        try:
            # 方式1: 不带 umo 获取全局默认人设
            persona = await persona_mgr.get_default_persona_v3(umo=None)
            prompt = persona.get("prompt", "") if persona else ""
        except Exception:
            pass

        if not prompt:
            try:
                # 方式2: 空字符串 umo
                persona = await persona_mgr.get_default_persona_v3(umo="")
                prompt = persona.get("prompt", "") if persona else ""
            except Exception:
                pass

        if not prompt:
            try:
                # 方式3: 旧版 API
                persona = await persona_mgr.get_default_persona()
                prompt = persona.get("prompt", "") if persona else ""
            except Exception:
                pass

        if not prompt:
            try:
                # 方式4: 直接读属性
                personas = getattr(persona_mgr, "personas", None) or {}
                for p in personas.values():
                    if isinstance(p, dict) and p.get("prompt"):
                        prompt = p["prompt"]
                        break
            except Exception:
                pass

        if prompt:
            self._cached_persona = prompt
            logger.info(f"[KuukiYomi] 独立加载人设成功（{len(prompt)}字）")
        else:
            # 兜底：使用配置中的备用人设
            fallback = self._cfg_group("air_reading", "fallback_persona", "")
            if fallback:
                self._cached_persona = fallback
                logger.info(f"[KuukiYomi] 使用备用人设（{len(fallback)}字）")

    def _pick_private_target_with_bridge(
        self,
        configured_targets: list = None,
        enable_affection_route: bool = True,
        min_affection: float = 70.0,
    ) -> Optional[str]:
        """选择私聊目标：优先记忆插件好感度，降级到内置社交系统"""
        # 1. 配置的固定目标
        if configured_targets:
            for t in configured_targets:
                tid = str(t).strip()
                if tid and self._lookup_umo(tid):
                    return tid

        if not enable_affection_route:
            return None

        # 2. 记忆插件可用 → 用四维关系的好感度
        if self.mem_bridge.available:
            top = self.mem_bridge.get_top_favorability(5)
            # 记忆插件好感度是 1-10，映射：7/10 ≈ 70/100
            min_fav = min_affection / 10.0
            for r in top:
                uid = r.get("user_id", "")
                fav = r.get("favorability", 5.0)
                if fav >= min_fav and self._lookup_umo(uid):
                    return uid

        # 3. 降级到内置社交系统
        return self.social.pick_private_target(
            enable_affection_route=True,
            umo_lookup=self._lookup_umo,
            min_affection=min_affection,
        )

    # ── 主模型请求注入 ──

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """注入上下文：读空气场景 + 私聊时用 fake_tool_call 注入群聊来源"""
        if not req:
            return

        # 先清除旧的 kuuki fake_tool_call
        if hasattr(req, "contexts") and req.contexts:
            cleaned = self._clean_old_fake_calls(req.contexts)
            if cleaned:
                logger.debug(f"[KuukiYomi] 清理了 {cleaned} 条旧 fake_tool_call")

        # 读空气触发时：注入群聊上下文 + 发送者信息（不修改 system_prompt）
        if event.get_extra("kuukiyomi_triggered"):
            s_name = event.get_extra("kuukiyomi_sender_name") or ""
            s_id = event.get_extra("kuukiyomi_sender_id") or ""
            history = event.get_extra("kuukiyomi_history") or ""
            emotion_note = self.social.emotion.format_for_llm()

            # 全部走 fake_tool_call 注入 contexts（下次自动清理）
            import uuid
            ctx_id = f"kuuki_ctx_{uuid.uuid4().hex[:8]}"
            if not hasattr(req, "contexts") or req.contexts is None:
                req.contexts = []

            note = f"本次是你主动参与群聊的，不是用户叫你。你正在回复 {s_name}({s_id}) 的消息。回复应自然随意，称呼正确。{emotion_note}"
            history_section = f"\n\n【群聊最近的对话记录】\n{history}" if history else ""
            ctx_content = f"{note}{history_section}"

            fake_call = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": ctx_id,
                    "type": "function",
                    "function": {
                        "name": "read_group_chat",
                        "arguments": "{}"
                    }
                }]
            }
            fake_result = {
                "role": "tool",
                "tool_call_id": ctx_id,
                "content": ctx_content
            }
            # 插到最后一条 user 消息前面
            insert_pos = len(req.contexts)
            for i in range(len(req.contexts) - 1, -1, -1):
                if isinstance(req.contexts[i], dict) and req.contexts[i].get("role") == "user":
                    insert_pos = i
                    break
            req.contexts.insert(insert_pos, fake_call)
            req.contexts.insert(insert_pos + 1, fake_result)

            if s_name and s_id and hasattr(req, "prompt") and req.prompt:
                req.prompt = f"[{s_name}({s_id}) 说] {req.prompt}"

        # ── 私聊→群聊桥接：最近私聊过的用户在群里发言时，注入私聊上下文 ──
        if not event.is_private_chat() and hasattr(req, "contexts"):
            air_cfg = self.cfg.get("air_reading") or {}
            bridge_count = int(air_cfg.get("private_group_context_count", 5))
            bridge_minutes = float(air_cfg.get("private_group_context_minutes", 20))
            if bridge_count > 0:
                sender_id = str(event.get_sender_id() or "")
                if sender_id and sender_id in self._recent_private_sends:
                    elapsed = time.time() - self._recent_private_sends[sender_id]
                    if elapsed < bridge_minutes * 60:
                        platform = event.get_platform_name()
                        priv_msgs = self.cache.get_recent(platform, True, sender_id, bridge_count)
                        if not priv_msgs:
                            priv_msgs = self.cache.get_recent("default", True, sender_id, bridge_count)
                        if priv_msgs:
                            import uuid
                            ctx_id = f"kuuki_ctx_{uuid.uuid4().hex[:8]}"
                            if not hasattr(req, "contexts") or req.contexts is None:
                                req.contexts = []
                            sender_name = event.get_sender_name() or sender_id
                            priv_text = "\n".join(m.format_for_llm() for m in priv_msgs)
                            fake_call = {
                                "role": "assistant", "content": "",
                                "tool_calls": [{"id": ctx_id, "type": "function",
                                    "function": {"name": "read_private_chat", "arguments": "{}"}}]
                            }
                            fake_result = {
                                "role": "tool", "tool_call_id": ctx_id,
                                "content": f"【你和 {sender_name} 最近的私聊记录】（{int(elapsed/60)}分钟前私聊过，请结合这些上下文）\n{priv_text}"
                            }
                            insert_pos = len(req.contexts)
                            for i in range(len(req.contexts) - 1, -1, -1):
                                if isinstance(req.contexts[i], dict) and req.contexts[i].get("role") == "user":
                                    insert_pos = i
                                    break
                            req.contexts.insert(insert_pos, fake_call)
                            req.contexts.insert(insert_pos + 1, fake_result)
                    else:
                        # 过期清理
                        self._recent_private_sends.pop(sender_id, None)

        # 私聊时：有 pending 上下文就注入（持续到轮次用完或超时）
        if event.is_private_chat() and hasattr(req, "contexts"):
            sender_id = event.get_sender_id()
            if not sender_id:
                return

            sid = str(sender_id)
            if sid in self._pending_private_ctx:
                pending = self._pending_private_ctx[sid]
                fake_msgs = pending.get("messages", pending) if isinstance(pending, dict) else pending
                ts = pending.get("ts", 0) if isinstance(pending, dict) else 0
                bot_said = pending.get("bot_message", "") if isinstance(pending, dict) else ""
                rounds_left = pending.get("rounds_left", 0) if isinstance(pending, dict) else 0

                # 超时或轮次用完 → 清除
                ctx_timeout = float(self._cfg_group("advanced", "private_context_timeout", 1800))
                if rounds_left <= 0 or (ts > 0 and (time.time() - ts) > ctx_timeout):
                    self._pending_private_ctx.pop(sid, None)
                    self._save_pending_ctx()
                    return

                # 确保 contexts 列表存在
                if not hasattr(req, "contexts") or req.contexts is None:
                    req.contexts = []

                # 每轮都一样注入，跟第一轮完全相同
                import copy
                inject_msgs = copy.deepcopy(fake_msgs)

                if ts > 0:
                    elapsed = time.time() - ts
                    if elapsed < 60:
                        time_note = "（刚才发的）"
                    elif elapsed < 3600:
                        time_note = f"（{int(elapsed / 60)} 分钟前发的）"
                    else:
                        time_note = f"（{int(elapsed / 3600)} 小时前发的）"
                    for msg in inject_msgs:
                        if isinstance(msg, dict) and msg.get("role") == "tool":
                            msg["content"] = time_note + "\n" + msg.get("content", "")

                if bot_said:
                    inject_msgs.append({"role": "assistant", "content": bot_said})

                # 插到最后一条 user 消息前面
                insert_pos = len(req.contexts)
                for i in range(len(req.contexts) - 1, -1, -1):
                    if isinstance(req.contexts[i], dict) and req.contexts[i].get("role") == "user":
                        insert_pos = i
                        break
                for j, fm in enumerate(inject_msgs):
                    req.contexts.insert(insert_pos + j, fm)

                # 扣轮次
                pending["rounds_left"] = rounds_left - 1
                self._save_pending_ctx()
                logger.debug(f"[KuukiYomi] 📎 私聊上下文注入 → {sid} | 剩余轮次={rounds_left - 1}")

    # ── 回复后评估：更新好感/印象/情绪 ──

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        """回复后调小模型评估好感/印象/情绪变化"""
        if not self.cfg.get("enable", True):
            return

        # 只处理群聊回复
        if event.is_private_chat():
            return

        judge_provider_name = self._cfg_group("air_reading", "judge_provider_name", "")
        if not judge_provider_name:
            return

        # 获取回复内容
        try:
            from astrbot.api.message_components import Plain
            result = event.get_result()
            if not result or not result.chain:
                return
            bot_text = "".join(c.text for c in result.chain if isinstance(c, Plain)).strip()
            if not bot_text:
                return
        except Exception:
            return

        sender_id = event.get_sender_id() or ""
        sender_name = event.get_sender_name() or "未知"
        user_msg = (event.message_str or "").strip()
        if not sender_id or not user_msg:
            return

        # 异步执行评估（不阻塞回复发送）
        asyncio.create_task(self._post_reply_evaluate(
            judge_provider_name, sender_id, sender_name, user_msg, bot_text
        ))

    async def _post_reply_evaluate(self, provider_name: str, sender_id: str,
                                    sender_name: str, user_msg: str, bot_reply: str):
        """回复后异步评估"""
        try:
            provider = self.context.get_provider_by_id(provider_name)
            if not provider:
                return

            profile = self.social.get_profile(sender_id)
            prompt = AirReader.build_post_reply_prompt(
                sender_name=sender_name,
                sender_id=sender_id,
                user_message=user_msg,
                bot_reply=bot_reply,
                current_affection=profile.affection,
                current_impression=profile.impression,
                current_emotion=self.social.emotion.format_for_llm(),
            )

            llm_resp = await provider.text_chat(prompt=prompt, contexts=[])
            result = AirReader.parse_post_reply_result(llm_resp.completion_text.strip())

            if not result:
                return

            # 更新好感和印象
            self.social.update_profile(
                sender_id,
                name=sender_name,
                affection_delta=result.get("affection_delta", 0),
                impression=result.get("impression"),
            )

            # 更新情绪
            self.social.update_emotion(
                mood_delta=result.get("mood_delta", 0),
                arousal_delta=result.get("arousal_delta", 0),
                reason=result.get("emotion_reason", ""),
            )

            logger.debug(f"[KuukiYomi] 回复后评估: {sender_name} 好感+{result.get('affection_delta', 0):.0f}, "
                         f"印象={result.get('impression', '')[:30]}, "
                         f"mood_d={result.get('mood_delta', 0):+.0f} arousal_d={result.get('arousal_delta', 0):+.0f}")

        except Exception as e:
            logger.debug(f"[KuukiYomi] 回复后评估失败: {e}")

    # ── 调度器：空闲检测 + 情绪驱动 ──

    async def _scheduler_loop(self):
        """后台调度循环：空闲衰减 + 主动消息（委托给各 handler）"""
        _last_idle_check = 0
        try:
            while True:
                await asyncio.sleep(120)

                if not self.cfg.get("enable", True):
                    continue

                # 如果人设还没加载成功，持续尝试
                if not self._cached_persona:
                    await self._load_persona_standalone()

                # 空闲衰减（每 30 分钟做一次就行）
                if (time.time() - _last_idle_check) >= 1800:
                    adv = self.cfg.get("advanced") or {}
                    lonely_min = int(adv.get("lonely_minutes", 30))
                    sleepy_min = int(adv.get("sleepy_minutes", 60))
                    self.social.check_idle(lonely_min, sleepy_min)
                    _last_idle_check = time.time()

                # 每日清理主动话题日志
                self._clean_proactive_log()

                if self._is_curfew():
                    continue

                air_cfg = self.cfg.get("air_reading") or {}

                # ── 跨群扫描 ──
                await self._cross_group.try_fire(air_cfg)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[KuukiYomi] 调度器异常: {e}")

    # ══════════════════════════════════════════════════════════
    #  私聊消息：缓存
    # ══════════════════════════════════════════════════════════

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        if not self.cfg.get("enable", True):
            return
        message_text = (event.message_str or "").strip()
        if not message_text:
            return

        platform = event.get_platform_name()
        sender_id = event.get_sender_id()
        if not sender_id:
            return

        self.cache.append(platform, True, sender_id, CachedMessage(
            sender_id=sender_id,
            sender_name=event.get_sender_name() or "未知",
            content=message_text,
            timestamp=time.time(),
            is_bot=False,
        ))

        umo = event.unified_msg_origin or ""
        if umo and sender_id:
            self._register_umo(sender_id, umo)
            self._register_contact(sender_id, event.get_sender_name() or "未知")

    # ── Bot 自身消息缓存 ──

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        try:
            result = event.get_result()
            if not result or not result.chain:
                return

            from astrbot.api.message_components import Plain
            text = "".join(c.text for c in result.chain if isinstance(c, Plain)).strip()
            if not text:
                return

            platform = event.get_platform_name()
            is_private = event.is_private_chat()
            chat_id = event.get_sender_id() if is_private else event.get_group_id()
            if not chat_id:
                return

            self.cache.append(platform, is_private, chat_id, CachedMessage(
                sender_id=self._get_bot_id(event) or "bot",
                sender_name="bot",
                content=text,
                timestamp=time.time(),
                is_bot=True,
            ))

            if not is_private:
                self.air.mark_replied(platform, chat_id)

        except Exception as e:
            logger.debug(f"[KuukiYomi] 保存 bot 消息失败: {e}")

    # ══════════════════════════════════════════════════════════
    #  LLM 工具
    # ══════════════════════════════════════════════════════════

    @llm_tool(name="kuuki_list_contacts")
    async def tool_list_contacts(self, event: AstrMessageEvent):
        '''查看通讯录。在你想给某人发消息但不知道对方 ID 时使用。

        Args:
        '''
        lines = []
        manual_list = self._cfg_group("tools_settings", "contacts") or []
        if manual_list:
            lines.append("【手动配置的联系人】")
            for entry in manual_list:
                if ":" in str(entry):
                    name, cid = str(entry).split(":", 1)
                    has = "✓" if self._lookup_umo(cid.strip()) else "✗"
                    lines.append(f"  {name.strip()} → {cid.strip()} ({has})")

        if self._contacts:
            lines.append("【自动记录的联系人】")
            for cid, name in list(self._contacts.items())[:50]:
                has = "✓" if self._lookup_umo(cid) else "✗"
                lines.append(f"  {name} → {cid} ({has})")

        group_keys = [k for k in self._umo_registry if "GroupMessage" in self._umo_registry.get(k, "")]
        if group_keys:
            lines.append("【已知群聊】")
            for gid in group_keys:
                gname = self.social.get_group_name(gid)
                lines.append(f"  {gname} → {gid} ✓")

        return "\n".join(lines) if lines else "通讯录为空。"

    @llm_tool(name="kuuki_send_message")
    async def tool_send_message(self, event: AstrMessageEvent, target_id: str, content: str):
        '''主动给指定的群或人发送一条消息。发送前会自动读取目标窗口的最近上下文。

        Args:
            target_id(string): 目标群号或用户 ID
            content(string): 要发送的消息内容
        '''
        tools_cfg = self.cfg.get("tools_settings") or {}
        allowed = tools_cfg.get("allowed_send_targets") or []
        if allowed and str(target_id) not in [str(a) for a in allowed]:
            return f"安全限制：目标 {target_id} 不在允许列表中。"

        umo = self._lookup_umo(str(target_id))
        if not umo:
            return f"找不到 {target_id} 的会话记录，无法发送。"

        # 读取目标窗口最近上下文
        platform = event.get_platform_name()
        recent_msgs = self.cache.get_recent(platform, True, str(target_id), 10)
        if not recent_msgs:
            recent_msgs = self.cache.get_recent(platform, False, str(target_id), 10)
        context_text = ""
        if recent_msgs:
            context_text = "\n\n【目标窗口最近对话】\n" + "\n".join(m.format_for_llm() for m in recent_msgs)

        try:
            chain = MessageChain().message(content)
            await self.context.send_message(umo, chain)
            # 写入缓存，后续能看到自己发过什么
            self.cache.append(platform or "default", True, str(target_id), CachedMessage(
                sender_id="bot",
                sender_name="bot",
                content=content,
                timestamp=time.time(),
                is_bot=True,
            ))
            return f"已发送到 {target_id}。{context_text}"
        except Exception as e:
            return f"发送失败: {e}"

    @llm_tool(name="kuuki_read_cache")
    async def tool_read_cache(self, event: AstrMessageEvent, target_id: str, count: str = "20", keyword: str = ""):
        '''读取指定群聊或私聊的最近缓存。当对话中提到其他群或其他人时使用。

        Args:
            target_id(string): 群号或用户 ID
            count(string): 读取条数，默认 20
            keyword(string): 可选，按关键词搜索
        '''
        try:
            n = min(int(count), 100)
        except ValueError:
            n = 20

        platform = event.get_platform_name()
        if keyword:
            msgs = self.cache.search(platform, False, target_id, keyword, n)
            if not msgs:
                msgs = self.cache.search(platform, True, target_id, keyword, n)
        else:
            msgs = self.cache.get_recent(platform, False, target_id, n)
            if not msgs:
                msgs = self.cache.get_recent(platform, True, target_id, n)

        if not msgs:
            return f"没有 {target_id} 的缓存。"
        return f"{target_id} 最近 {len(msgs)} 条:\n" + "\n".join(m.format_for_llm() for m in msgs)

    @llm_tool(name="kuuki_forward_history")
    async def tool_forward_history(self, event: AstrMessageEvent, source_id: str, target_id: str, count: str = "10"):
        '''把某群的最近记录转发到另一个群或私聊。

        Args:
            source_id(string): 来源群号
            target_id(string): 目标群号或用户 ID
            count(string): 转发条数，默认 10
        '''
        try:
            n = min(int(count), 50)
        except ValueError:
            n = 10

        tools_cfg = self.cfg.get("tools_settings") or {}
        allowed = tools_cfg.get("allowed_send_targets") or []
        if allowed and str(target_id) not in [str(a) for a in allowed]:
            return f"安全限制：目标 {target_id} 不在允许列表中。"

        msgs = self.cache.get_recent(event.get_platform_name(), False, source_id, n)
        if not msgs:
            return f"没有群 {source_id} 的缓存。"

        summary = f"来自 {self.social.get_group_name(source_id)} 的最近 {len(msgs)} 条:\n"
        summary += "\n".join(m.format_for_llm() for m in msgs)

        umo = self._lookup_umo(str(target_id))
        if not umo:
            return f"找不到 {target_id} 的会话记录。"
        try:
            await self.context.send_message(umo, MessageChain().message(summary))
            # 写入目标缓存
            self.cache.append(event.get_platform_name() or "default", True, str(target_id), CachedMessage(
                sender_id="bot",
                sender_name="bot",
                content=summary,
                timestamp=time.time(),
                is_bot=True,
            ))
            return f"已转发 {len(msgs)} 条到 {target_id}。"
        except Exception as e:
            return f"转发失败: {e}"

    # ══════════════════════════════════════════════════════════
    #  管理命令
    # ══════════════════════════════════════════════════════════

    @filter.command_group("kuukiyomi", alias={"ky", "读空气"})
    def cmd_group(self):
        pass

    @cmd_group.command("help", alias=["帮助"])
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "读空气 帮助\n"
            "前缀: /ky 或 /读空气\n\n"
            "/ky status — 缓存状态\n"
            "/ky history [N] — 查看缓存\n"
            "/ky social — 社交系统状态\n"
            "/ky emotion — 当前情绪\n"
            "/ky reset — 清空当前缓存"
        )

    @cmd_group.command("status", alias=["状态"])
    async def cmd_status(self, event: AstrMessageEvent):
        keys = self.cache.get_all_keys()
        g = len([k for k in keys if ":group:" in k])
        p = len([k for k in keys if ":private:" in k])

        platform = event.get_platform_name()
        is_priv = event.is_private_chat()
        cid = event.get_sender_id() if is_priv else event.get_group_id()
        cur = len(self.cache.get_recent(platform, is_priv, cid, 1000)) if cid else 0

        yield event.plain_result(
            f"📊 缓存: 群{g}个 私聊{p}个\n"
            f"当前会话: {cur} 条\n"
            f"通讯录: {len(self._contacts)} 人\n"
            f"UMO: {len(self._umo_registry)} 条"
        )

    @cmd_group.command("social", alias=["社交"])
    async def cmd_social(self, event: AstrMessageEvent):
        emotion = self.social.emotion.format_for_llm()
        profiles = self.social.format_profiles_for_llm()
        groups = self.social.format_groups_for_llm()
        yield event.plain_result(f"🎭 {emotion}\n\n👥 社交:\n{profiles}\n\n🏠 群聊:\n{groups}")

    @cmd_group.command("emotion", alias=["情绪"])
    async def cmd_emotion(self, event: AstrMessageEvent):
        yield event.plain_result(self.social.emotion.format_for_llm())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @cmd_group.command("history", alias=["记录"])
    async def cmd_history(self, event: AstrMessageEvent, count: int = 10):
        count = min(count, 30)
        platform = event.get_platform_name()
        is_priv = event.is_private_chat()
        cid = event.get_sender_id() if is_priv else event.get_group_id()
        if not cid:
            yield event.plain_result("无法获取会话 ID")
            return
        text = self.cache.format_recent_for_llm(platform, is_priv, cid, count)
        yield event.plain_result(f"最近 {count} 条:\n{text}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @cmd_group.command("reset", alias=["清空"])
    async def cmd_reset(self, event: AstrMessageEvent, target: str = ""):
        platform = event.get_platform_name()
        if target:
            self.cache.clear_bucket(platform, False, target)
            self._pending_private_ctx.pop(str(target), None)
            self._save_pending_ctx()
            yield event.plain_result(f"已清空群 {target} 的缓存")
        else:
            is_priv = event.is_private_chat()
            cid = event.get_sender_id() if is_priv else event.get_group_id()
            if cid:
                self.cache.clear_bucket(platform, is_priv, cid)
                self._pending_private_ctx.pop(str(cid), None)
                self._save_pending_ctx()
                yield event.plain_result("已清空当前会话缓存")
