"""
対話キャッシュ (对话缓存系统)

按 platform/chat_type/chat_id 分桶存储消息，
使用 deque 实现 FIFO 滚动覆盖，支持定期持久化到磁盘。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from astrbot.api import logger

try:
    from astrbot.api.star import StarTools
    HAS_STARTOOLS = True
except ImportError:
    HAS_STARTOOLS = False


@dataclass
class CachedMessage:
    """单条缓存消息"""
    sender_id: str
    sender_name: str
    content: str
    timestamp: float
    message_id: str = ""
    # 群聊中有用：区分是谁发的
    is_bot: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CachedMessage":
        return cls(
            sender_id=data.get("sender_id", ""),
            sender_name=data.get("sender_name", "未知"),
            content=data.get("content", ""),
            timestamp=data.get("timestamp", 0.0),
            message_id=data.get("message_id", ""),
            is_bot=data.get("is_bot", False),
        )

    def format_for_llm(self) -> str:
        """格式化为 LLM 可读的文本行"""
        from datetime import datetime
        try:
            t = datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S")
        except Exception:
            t = "??:??:??"
        tag = " [bot]" if self.is_bot else ""
        return f"[{t}] {self.sender_name}({self.sender_id}){tag}: {self.content}"


class CacheBucket:
    """
    单个会话的缓存桶（一个群 or 一个私聊）。
    内部用 deque(maxlen=N) 实现 FIFO。
    """

    def __init__(self, maxlen: int = 200):
        self._maxlen = min(max(maxlen, 1), 1000)
        self._messages: deque[CachedMessage] = deque(maxlen=self._maxlen)

    @property
    def maxlen(self) -> int:
        return self._maxlen

    def resize(self, new_maxlen: int):
        """动态调整缓存大小"""
        new_maxlen = min(max(new_maxlen, 1), 1000)
        if new_maxlen == self._maxlen:
            return
        old = list(self._messages)
        self._maxlen = new_maxlen
        self._messages = deque(old[-new_maxlen:], maxlen=new_maxlen)

    def append(self, msg: CachedMessage):
        """添加一条消息（超出 maxlen 自动淘汰最旧的）"""
        self._messages.append(msg)

    def get_recent(self, count: int = 20) -> List[CachedMessage]:
        """获取最近 N 条消息"""
        msgs = list(self._messages)
        return msgs[-count:] if count < len(msgs) else msgs

    def get_all(self) -> List[CachedMessage]:
        return list(self._messages)

    def search(self, keyword: str, limit: int = 20) -> List[CachedMessage]:
        """按关键词搜索缓存"""
        results = []
        for msg in reversed(self._messages):
            if keyword in msg.content or keyword in msg.sender_name:
                results.append(msg)
                if len(results) >= limit:
                    break
        results.reverse()
        return results

    def clear(self):
        self._messages.clear()

    def __len__(self) -> int:
        return len(self._messages)

    def to_list(self) -> List[dict]:
        return [m.to_dict() for m in self._messages]

    @classmethod
    def from_list(cls, data: List[dict], maxlen: int = 200) -> "CacheBucket":
        bucket = cls(maxlen=maxlen)
        for item in data[-maxlen:]:
            try:
                bucket.append(CachedMessage.from_dict(item))
            except Exception:
                continue
        return bucket


class ConversationCache:
    """
    全局对话缓存管理器。

    结构：
        _buckets[cache_key] = CacheBucket
        cache_key 格式: "{platform}:{chat_type}:{chat_id}"
            chat_type: "group" / "private"
    """

    def __init__(
        self,
        group_maxlen: int = 200,
        private_maxlen: int = 100,
        persist_dir: Optional[str] = None,
        persist_interval: int = 60,
    ):
        self._group_maxlen = min(max(group_maxlen, 1), 1000)
        self._private_maxlen = min(max(private_maxlen, 1), 1000)
        self._buckets: Dict[str, CacheBucket] = {}
        self._persist_dir = persist_dir
        self._persist_interval = persist_interval
        self._persist_task: Optional[asyncio.Task] = None
        self._dirty = False

        # 加载已有的持久化数据
        if self._persist_dir:
            os.makedirs(self._persist_dir, exist_ok=True)
            self._load_from_disk()

    # ── key 生成 ──

    @staticmethod
    def make_key(platform: str, is_private: bool, chat_id: str) -> str:
        chat_type = "private" if is_private else "group"
        return f"{platform}:{chat_type}:{chat_id}"

    @staticmethod
    def parse_key(key: str):
        """解析 cache_key → (platform, chat_type, chat_id)"""
        parts = key.split(":", 2)
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        return "unknown", "unknown", key

    # ── 读写操作 ──

    def _get_or_create(self, key: str) -> CacheBucket:
        if key not in self._buckets:
            _, chat_type, _ = self.parse_key(key)
            maxlen = self._private_maxlen if chat_type == "private" else self._group_maxlen
            self._buckets[key] = CacheBucket(maxlen=maxlen)
        return self._buckets[key]

    def append(self, platform: str, is_private: bool, chat_id: str, msg: CachedMessage):
        """往指定会话追加一条消息"""
        key = self.make_key(platform, is_private, chat_id)
        self._get_or_create(key).append(msg)
        self._dirty = True

    def get_recent(self, platform: str, is_private: bool, chat_id: str, count: int = 20) -> List[CachedMessage]:
        key = self.make_key(platform, is_private, chat_id)
        bucket = self._buckets.get(key)
        return bucket.get_recent(count) if bucket else []

    def search(self, platform: str, is_private: bool, chat_id: str, keyword: str, limit: int = 20) -> List[CachedMessage]:
        key = self.make_key(platform, is_private, chat_id)
        bucket = self._buckets.get(key)
        return bucket.search(keyword, limit) if bucket else []

    def get_all_keys(self) -> List[str]:
        """获取所有缓存桶的 key"""
        return list(self._buckets.keys())

    def get_group_keys(self) -> List[str]:
        """只获取群聊桶的 key"""
        return [k for k in self._buckets if ":group:" in k]

    def clear_bucket(self, platform: str, is_private: bool, chat_id: str):
        key = self.make_key(platform, is_private, chat_id)
        if key in self._buckets:
            self._buckets[key].clear()
            self._dirty = True

    def format_recent_for_llm(self, platform: str, is_private: bool, chat_id: str, count: int = 20) -> str:
        """获取最近消息并格式化为 LLM 可读文本"""
        messages = self.get_recent(platform, is_private, chat_id, count)
        if not messages:
            return "(暂无聊天记录)"
        return "\n".join(m.format_for_llm() for m in messages)

    # ── 持久化 ──

    def start_persist_loop(self):
        """启动定期持久化的后台任务"""
        if self._persist_interval <= 0 or not self._persist_dir:
            return
        self._persist_task = asyncio.create_task(self._persist_loop())
        logger.info(f"[KuukiYomi] 缓存持久化循环已启动，间隔 {self._persist_interval}s")

    async def _persist_loop(self):
        try:
            while True:
                await asyncio.sleep(self._persist_interval)
                if self._dirty:
                    self._save_to_disk()
                    self._dirty = False
        except asyncio.CancelledError:
            # 退出前最后保存一次
            if self._dirty:
                self._save_to_disk()

    def _save_to_disk(self):
        """保存所有缓存到磁盘"""
        if not self._persist_dir:
            return
        try:
            data = {}
            for key, bucket in self._buckets.items():
                data[key] = {
                    "maxlen": bucket.maxlen,
                    "messages": bucket.to_list(),
                }
            path = os.path.join(self._persist_dir, "cache.json")
            json_str = json.dumps(data, ensure_ascii=False, default=str)
            # 清理 surrogate 字符（QQ 表情可能产生孤立 surrogate）
            json_str = json_str.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')
            with open(path, "w", encoding="utf-8") as f:
                f.write(json_str)
            logger.debug(f"[KuukiYomi] 缓存已保存: {len(data)} 个会话")
        except Exception as e:
            logger.error(f"[KuukiYomi] 缓存保存失败: {e}")

    def _load_from_disk(self):
        """从磁盘加载缓存"""
        path = os.path.join(self._persist_dir, "cache.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            for key, bucket_data in data.items():
                _, chat_type, _ = self.parse_key(key)
                default_maxlen = self._private_maxlen if chat_type == "private" else self._group_maxlen
                maxlen = bucket_data.get("maxlen", default_maxlen)
                messages = bucket_data.get("messages", [])
                self._buckets[key] = CacheBucket.from_list(messages, maxlen=maxlen)
            logger.info(f"[KuukiYomi] 从磁盘加载了 {len(self._buckets)} 个会话缓存")
        except Exception as e:
            logger.error(f"[KuukiYomi] 缓存加载失败: {e}")

    def force_save(self):
        """强制保存（用于插件退出时）"""
        self._save_to_disk()
        self._dirty = False

    async def stop(self):
        """停止持久化循环"""
        if self._persist_task and not self._persist_task.done():
            self._persist_task.cancel()
            try:
                await self._persist_task
            except asyncio.CancelledError:
                pass
