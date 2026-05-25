"""
记忆桥接 — KuukiYomi ↔ 晨露记忆插件联动

当记忆插件安装时，从它的数据库读取：
- 用户画像（印象、昵称、喜好等）
- 四维关系（好感度等）
- 群聊印象

未安装时所有方法返回空/默认值，不影响 KuukiYomi 正常运行。
"""

import json
import os
import sqlite3
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class MemoryBridge:
    """读取晨露记忆插件的数据，不依赖记忆插件本身的代码"""

    def __init__(self, astrbot_data_dir: str = ""):
        self._db_path: str | None = None
        self._available = False
        self._try_locate_db(astrbot_data_dir)

    def _try_locate_db(self, data_dir: str):
        """尝试定位记忆插件的数据库"""
        candidates = []
        if data_dir:
            candidates.append(os.path.join(data_dir, "plugin_data", "astrbot_plugin_shuangxu_memory", "memory.db"))
        # 常见路径
        candidates.append(os.path.join(os.getcwd(), "data", "plugin_data", "astrbot_plugin_shuangxu_memory", "memory.db"))

        for path in candidates:
            if os.path.exists(path):
                self._db_path = path
                self._available = True
                logger.info(f"[MemoryBridge] 已连接记忆数据库: {path}")
                return

        logger.info("[MemoryBridge] 未找到记忆插件数据库，将使用内置社交系统")

    @property
    def available(self) -> bool:
        return self._available

    def _get_conn(self) -> sqlite3.Connection | None:
        if not self._db_path or not os.path.exists(self._db_path):
            return None
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            return conn
        except Exception as e:
            logger.debug(f"[MemoryBridge] 数据库连接失败: {e}")
            return None

    # ========== 用户画像 ==========

    def get_user_profile(self, user_id: str) -> dict | None:
        """获取用户画像，不存在返回 None"""
        conn = self._get_conn()
        if not conn:
            return None
        try:
            row = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (str(user_id),)).fetchone()
            return dict(row) if row else None
        except Exception:
            return None
        finally:
            conn.close()

    def get_user_impression(self, user_id: str) -> str:
        """获取用户印象文本，不存在返回空字符串"""
        profile = self.get_user_profile(user_id)
        if not profile:
            return ""
        return profile.get("impression", "")

    def format_user_for_judge(self, user_id: str, user_name: str = "") -> str:
        """
        格式化用户信息给小模型判断用。
        有画像 → 详细信息
        无画像 → 标注为新用户
        """
        profile = self.get_user_profile(user_id)
        rel = self.get_relationship(user_id)

        if not profile and not rel:
            return f"{user_name}({user_id}): 新用户，暂无印象（请友善对待）"

        parts = [f"{(profile or {}).get('user_name', user_name) or user_name}({user_id}):"]

        # 印象
        impression = (profile or {}).get("impression", "")
        if impression:
            parts.append(f"印象={impression}")

        # 昵称
        nickname = (profile or {}).get("nickname", "")
        if nickname:
            parts.append(f"昵称={nickname}")

        # 喜好
        likes = (profile or {}).get("likes", "")
        if likes:
            parts.append(f"喜欢={likes}")

        # 好感度
        if rel:
            fav = rel.get("favorability", 5.0)
            level = self._favorability_level(fav)
            parts.append(f"好感={fav:.1f}/10({level})")

        return " ".join(parts)

    def format_social_context_for_judge(self, participant_ids: list[str] = None) -> str:
        """
        格式化社交上下文给小模型。
        如果传入参与者 ID 列表，只格式化这些人。
        """
        if not self._available:
            return ""

        conn = self._get_conn()
        if not conn:
            return ""

        try:
            if participant_ids:
                placeholders = ",".join("?" * len(participant_ids))
                rows = conn.execute(
                    f"SELECT * FROM user_profiles WHERE user_id IN ({placeholders})",
                    [str(uid) for uid in participant_ids],
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM user_profiles ORDER BY updated_at DESC LIMIT 20"
                ).fetchall()

            if not rows:
                return "(暂无用户画像数据)"

            lines = []
            for row in rows:
                r = dict(row)
                uid = r["user_id"]
                name = r.get("user_name", uid)
                impression = r.get("impression", "")
                rel = self.get_relationship(uid)
                fav = rel.get("favorability", 5.0) if rel else 5.0
                level = self._favorability_level(fav)

                line = f"{name}({uid}): 好感={fav:.0f}/10({level})"
                if impression:
                    line += f", {impression}"
                lines.append(line)

            return "\n".join(lines)
        except Exception as e:
            logger.debug(f"[MemoryBridge] 查询失败: {e}")
            return ""
        finally:
            conn.close()

    # ========== 四维关系 ==========

    def get_relationship(self, user_id: str) -> dict | None:
        """获取关系数据"""
        conn = self._get_conn()
        if not conn:
            return None
        try:
            row = conn.execute("SELECT * FROM relationships WHERE user_id = ?", (str(user_id),)).fetchone()
            return dict(row) if row else None
        except Exception:
            return None
        finally:
            conn.close()

    def get_favorability(self, user_id: str) -> float:
        """获取好感度，默认 5.0"""
        rel = self.get_relationship(user_id)
        if rel:
            return rel.get("favorability", 5.0)
        return 5.0

    def get_top_favorability(self, n: int = 5) -> list[dict]:
        """获取好感度最高的 N 个用户"""
        conn = self._get_conn()
        if not conn:
            return []
        try:
            rows = conn.execute(
                "SELECT r.*, p.user_name FROM relationships r "
                "LEFT JOIN user_profiles p ON r.user_id = p.user_id "
                "ORDER BY r.favorability DESC LIMIT ?",
                (n,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    # ========== 群聊印象 ==========

    def get_group_impression(self, group_id: str) -> str:
        """获取群聊印象"""
        conn = self._get_conn()
        if not conn:
            return ""
        try:
            row = conn.execute("SELECT impression FROM group_profiles WHERE group_id = ?", (str(group_id),)).fetchone()
            return row["impression"] if row else ""
        except Exception:
            return ""
        finally:
            conn.close()

    # ========== 辅助 ==========

    @staticmethod
    def _favorability_level(fav: float) -> str:
        if fav >= 8:   return "很亲近"
        if fav >= 6:   return "有好感"
        if fav >= 4:   return "一般"
        if fav >= 2:   return "不太熟"
        return "陌生"

    # ========== 记忆检索（闲聊用） ==========

    def get_today_memories(self, user_id: str | None = None) -> list[dict]:
        """
        获取今天的记忆。
        user_id 不为空时，只返回与该用户相关的记忆（私聊 + 群聊中涉及该用户的）。
        """
        conn = self._get_conn()
        if not conn:
            return []
        try:
            from datetime import datetime
            today = datetime.now().strftime("%Y-%m-%d")

            if user_id:
                # 私聊记忆
                rows = conn.execute(
                    "SELECT * FROM memories WHERE level = 'today' AND date(time) = ? "
                    "AND ((source_type = 'private' AND source_id = ?) "
                    "OR (source_type = 'group' AND related_users LIKE ?))"
                    " ORDER BY time DESC",
                    (today, str(user_id), f"%{user_id}%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE level = 'today' AND date(time) = ? ORDER BY time DESC",
                    (today,),
                ).fetchall()

            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug(f"[MemoryBridge] 查询今日记忆失败: {e}")
            return []
        finally:
            conn.close()

    def get_user_memories(self, user_id: str, limit: int = 10) -> list[dict]:
        """
        获取某用户的所有层级记忆（今日 + 近期 + 长期），按时间倒序。
        包含私聊记忆和群聊中涉及该用户的记忆。
        """
        conn = self._get_conn()
        if not conn:
            return []
        try:
            rows = conn.execute(
                "SELECT * FROM memories WHERE "
                "(source_type = 'private' AND source_id = ?) "
                "OR (source_type = 'group' AND related_users LIKE ?) "
                "ORDER BY time DESC LIMIT ?",
                (str(user_id), f"%{user_id}%", limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug(f"[MemoryBridge] 查询用户记忆失败: {e}")
            return []
        finally:
            conn.close()

    def pick_idle_chat_target(self, umo_lookup: callable = None, min_fav: float = 5.0) -> dict | None:
        """
        为无聊闲聊挑选一个目标。
        
        策略：今天有过互动 + 好感度 ≥ 阈值 + 有可用 UMO。
        返回 {"user_id": ..., "user_name": ..., "favorability": ..., "today_memories": [...], "past_memories": [...]}
        """
        conn = self._get_conn()
        if not conn:
            return None

        try:
            from datetime import datetime
            today = datetime.now().strftime("%Y-%m-%d")

            # 找今天互动过的用户（私聊来源）
            rows = conn.execute(
                "SELECT DISTINCT source_id FROM memories "
                "WHERE level = 'today' AND date(time) = ? AND source_type = 'private'",
                (today,),
            ).fetchall()
            candidates = [r["source_id"] for r in rows]

            # 也收集群聊中出现的用户
            group_rows = conn.execute(
                "SELECT related_users FROM memories "
                "WHERE level = 'today' AND date(time) = ? AND source_type = 'group'",
                (today,),
            ).fetchall()
            import json as _json
            for r in group_rows:
                try:
                    users = _json.loads(r["related_users"]) if isinstance(r["related_users"], str) else r["related_users"]
                    for u in users:
                        # related_users 可能是 "名字(QQ号)" 格式，提取 QQ 号
                        if "(" in u and u.endswith(")"):
                            uid = u.rsplit("(", 1)[1].rstrip(")")
                        else:
                            uid = u
                        if uid not in candidates:
                            candidates.append(uid)
                except Exception:
                    pass

            if not candidates:
                return None

            # 按好感度排序，挑最合适的
            best = None
            for uid in candidates:
                # 检查 UMO 可用
                if umo_lookup and not umo_lookup(uid):
                    continue

                rel = self.get_relationship(uid)
                fav = rel.get("favorability", 5.0) if rel else 5.0
                if fav < min_fav:
                    continue

                if best is None or fav > best["favorability"]:
                    profile = self.get_user_profile(uid)
                    best = {
                        "user_id": uid,
                        "user_name": (profile or {}).get("user_name", uid),
                        "favorability": fav,
                    }

            if not best:
                return None

            # 拿到目标后，收集记忆
            uid = best["user_id"]
            best["today_memories"] = self.get_today_memories(uid)
            best["past_memories"] = self.get_user_memories(uid, limit=5)

            # 去重：past 里排除 today 已有的
            today_ids = {m["id"] for m in best["today_memories"]}
            best["past_memories"] = [m for m in best["past_memories"] if m["id"] not in today_ids]

            return best

        except Exception as e:
            logger.debug(f"[MemoryBridge] 挑选闲聊目标失败: {e}")
            return None
        finally:
            conn.close()

    def format_memories_for_chat(self, memories: list[dict], label: str = "记忆") -> str:
        """将记忆列表格式化为给 LLM 的文本"""
        if not memories:
            return ""
        lines = [f"【{label}】"]
        for m in memories[:8]:  # 最多 8 条，省 token
            source = "私聊" if m.get("source_type") == "private" else "群聊"
            lines.append(f"- [{m.get('time', '')}] ({source}) {m.get('content', '')}")
        return "\n".join(lines)
