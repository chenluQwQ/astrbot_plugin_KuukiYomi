"""跨群通知处理器 — 扫描各群动态，把感兴趣的话题通知给对应的人"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from astrbot.api import logger

from .air_reader import extract_json

if TYPE_CHECKING:
    from ..main import KuukiYomi


class CrossGroupHandler:
    """定期扫描各群缓存，有匹配话题就私聊通知对应的人

    需要宿主插件提供：
        host.context, host.social, host.cache, host.cfg,
        host._cached_persona, host._contacts, host.topic_log,
        host._pending_private_ctx, host._save_pending_ctx(),
        host._cfg_group(), host._build_fake_tool_call(),
        host._do_private_send()
    """

    def __init__(self, host: KuukiYomi):
        self.host = host
        self.last_scan_ts: float = 0  # 上次扫描时间

    async def try_fire(self, air_cfg: dict) -> bool:
        """尝试触发一次跨群扫描，返回是否有通知发出"""
        topic_interests = air_cfg.get("topic_interests") or []
        if not topic_interests:
            return False

        if (time.time() - self.last_scan_ts) < 1800:
            return False

        fired = await self._do_scan(topic_interests)
        self.last_scan_ts = time.time()
        return fired

    async def _do_scan(self, topic_interests: list) -> bool:
        """执行跨群扫描，返回是否发出了通知"""
        host = self.host

        judge_provider_name = host._cfg_group("air_reading", "judge_provider_name", "")
        if not judge_provider_name:
            return False

        try:
            provider = host.context.get_provider_by_id(judge_provider_name)
            if not provider:
                return False
        except Exception:
            return False

        # 解析话题配置
        topics = []
        for entry in topic_interests:
            parts = str(entry).split(":", 1)
            if len(parts) == 2:
                topics.append({"topic": parts[0].strip(), "target": parts[1].strip()})
        if not topics:
            return False

        fired = False

        # 遍历所有群缓存
        group_keys = host.cache.get_group_keys()
        for key in group_keys:
            _, _, group_id = host.cache.parse_key(key)
            # 只看最近 30 分钟的消息
            recent = host.cache.get_recent("default", False, group_id, 20)
            recent_msgs = [m for m in recent if (time.time() - m.timestamp) < 1800 and not m.is_bot]
            if not recent_msgs:
                continue

            history_text = "\n".join(m.format_for_llm() for m in recent_msgs)
            group_name = host.social.get_group_name(group_id)
            topics_desc = "\n".join(f"  「{t['topic']}」→ 通知 {t['target']}" for t in topics)

            prompt = f"""以下是群「{group_name}」最近的聊天记录：
{history_text}

以下是话题通知规则：
{topics_desc}

请判断最近的聊天是否涉及以上话题。如果涉及，用 JSON 回复：
```json
{{"notify": true, "target": "目标QQ号", "topic": "匹配的话题", "summary": "一句话概括群里在聊什么"}}
```
如果不涉及任何话题：
```json
{{"notify": false}}
```
**只回复 JSON！**"""

            try:
                resp = await provider.text_chat(prompt=prompt, contexts=[])
                data = extract_json(resp.completion_text.strip())

                if data.get("notify") and data.get("target") and data.get("summary"):
                    result = await self._notify(
                        target=str(data["target"]),
                        topic=data.get("topic", "你感兴趣的话题"),
                        raw_summary=data["summary"],
                        group_name=group_name,
                        history_text=history_text,
                    )
                    if result:
                        fired = True
            except Exception as e:
                logger.debug(f"[CrossGroup] 扫描失败 ({group_id}): {e}")

        return fired

    async def _notify(self, *, target: str, topic: str, raw_summary: str,
                      group_name: str, history_text: str) -> bool:
        """生成通知消息并发送"""
        host = self.host
        target_name = host._contacts.get(target, target)

        # 用主模型 + 人设生成自然的通知消息
        main_provider = host.context.get_using_provider()
        if main_provider:
            persona = host._cached_persona or ""
            gen_prompt = f"""群「{group_name}」最近在聊{topic}相关的内容。
概况：{raw_summary}

你想把这个消息告诉 {target_name}，给对方发一条简短的私聊消息（1~2句话），用中文，语气符合你的性格，不要使用emoji或颜文字。
只输出消息内容。"""

            full_prompt = f"【你的人设】\n{persona}\n\n【任务】\n{gen_prompt}" if persona else gen_prompt
            gen_resp = await main_provider.text_chat(prompt=full_prompt, contexts=[])
            content = gen_resp.completion_text.strip()
        else:
            content = f"群「{group_name}」在聊{topic}：{raw_summary}"

        if not content or len(content) >= 300:
            return False

        # 构建 fake_tool_call 上下文
        fake_msgs = host._build_fake_tool_call(
            group_name=group_name,
            group_id=group_name,
            history_text=history_text,
            bot_message=content,
            target_name=target_name,
        )
        host._pending_private_ctx[target] = {
            "messages": fake_msgs, "ts": time.time(), "bot_message": content
        }
        host._save_pending_ctx()

        await host._do_private_send(target, content)
        host.topic_log.log_topic(target_name, content, source="cross_group")
        logger.info(f"[CrossGroup] 📢 跨群通知 → {target}: {content[:50]}")
        return True
