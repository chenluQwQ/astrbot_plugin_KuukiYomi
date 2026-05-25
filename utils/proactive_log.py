"""主动聊天话题日志 — 记录今日主动聊过的话题，防止重复"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

from astrbot.api import logger


class ProactiveTopicLog:
    """管理主动聊天的话题记录（按天清理，防止同一天重复聊同一话题）"""

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        self._log: list[dict] = []
        self._load()

    # ── 公开接口 ──

    def log_topic(self, target_name: str, content: str, source: str = "idle"):
        """记录一条主动聊天话题

        Args:
            target_name: 聊天对象名称
            content: 消息内容
            source: 来源 (idle / emotion / cross_group)
        """
        self._clean()
        self._log.append({
            "ts": time.time(),
            "target": target_name,
            "content": content[:100],
            "date": datetime.now().strftime("%Y-%m-%d"),
            "source": source,
        })
        self._save()

    def get_today_summary(self, target_name: str = "") -> str:
        """获取今天主动聊天的摘要文本，供生成 prompt 参考

        Args:
            target_name: 限定对象（空字符串 = 所有人）
        """
        self._clean()
        if not self._log:
            return ""

        entries = self._log
        if target_name:
            entries = [e for e in entries if e.get("target") == target_name]
        if not entries:
            return ""

        lines = ["【今天已主动聊过的话题（不要重复）】"]
        for e in entries:
            src_label = {
                "idle": "闲聊",
                "emotion": "情绪倾诉",
                "cross_group": "话题通知",
            }.get(e.get("source", ""), "主动")
            lines.append(f"  - [{src_label}] → {e.get('target', '?')}: {e.get('content', '')}")
        return "\n".join(lines)

    def count_today(self, source: str = "") -> int:
        """统计今日某类来源的主动消息数量"""
        self._clean()
        if source:
            return sum(1 for e in self._log if e.get("source") == source)
        return len(self._log)

    def clean(self):
        """手动触发清理（每日自动调用也可）"""
        self._clean()

    def save(self):
        """手动持久化（关闭时调用）"""
        self._save()

    # ── 内部方法 ──

    def _clean(self):
        today = datetime.now().strftime("%Y-%m-%d")
        before = len(self._log)
        self._log = [e for e in self._log if e.get("date") == today]
        if len(self._log) != before:
            self._save()

    def _save(self):
        try:
            path = os.path.join(self._data_dir, "proactive_log.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._log, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"[ProactiveTopicLog] 保存失败: {e}")

    def _load(self):
        try:
            path = os.path.join(self._data_dir, "proactive_log.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    self._log = json.load(f)
            self._clean()
        except Exception:
            self._log = []
