import re
import os
import json
import random
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api import logger, AstrBotConfig

try:
    import astrbot.api.message_components as Comp
    from astrbot.core.utils.session_waiter import (
        session_waiter,
        SessionFilter,
        SessionController,
    )
    HAS_SESSION_WAITER = True
except ImportError:
    HAS_SESSION_WAITER = False

try:
    from astrbot.api import llm_tool
    HAS_LLM_TOOL = True
except ImportError:
    def llm_tool(*args, **kwargs):
        def decorator(func):
            return func
        return decorator
    HAS_LLM_TOOL = False

from .managers.backpack import _name_match
from .managers import (
    PocketMoneyManager,
    BackpackManager,
    UserIsolationManager,
    ThankLetterManager,
    ShopManager,
    GamesManager,
    TurtleSoupManager,
    LevelManager,
    BankManager,
    AchievementManager,
    GiftManager,
    TarotManager,
    verify_gift,
)


@register("astrbot_plugin_pocketmoney", "晨露", "小金库系统 - 零花钱、超市、小游戏、背包", "2.0.0")
class PocketMoneyPlugin(Star):
    """
    v2.0.0 - 模块化重构 + 去硬编码 + 超市系统 + 小游戏（刮刮乐/炒股）
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.data_dir = os.path.join("data", "plugin_data", "astrbot_plugin_pocketmoney")
        self._migrate_data_if_needed()

        # 辅助读取嵌套配置
        def cfg(key, default=None):
            """支持 flat key 和 nested object key"""
            if key in self.config:
                return self.config[key] if self.config[key] is not None else default
            for section in self.config.values():
                if isinstance(section, dict) and key in section:
                    v = section[key]
                    return v if v is not None else default
            return default

        self._cfg = cfg

        # ---------- 初始化管理器 ----------
        self.manager = PocketMoneyManager(
            self.data_dir,
            cfg("initial_balance", 0),
            cfg("max_records", 100),
        )
        self.thank_manager = ThankLetterManager(self.data_dir)
        self.backpack_manager = BackpackManager(
            self.data_dir,
            cfg("max_shared_slots", 10),
            cfg("max_user_slots", 3),
        )
        self.isolation_manager = UserIsolationManager(self.data_dir)
        self.shop_manager = ShopManager(
            self.data_dir,
            cfg("shop_daily_count", 10),
        )
        self.games_manager = GamesManager(
            self.data_dir,
            {
                "scratch_ticket_price": cfg("scratch_ticket_price", 3),
                "slots_bet": cfg("slots_bet", 5),
            },
        )
        self.level_manager = LevelManager(self.data_dir)
        self.bank_manager = BankManager(self.data_dir)
        self.achievement_manager = AchievementManager(self.data_dir)
        self.gift_manager = GiftManager(self.data_dir)
        self.turtle_soup_manager = TurtleSoupManager(self.data_dir)
        self.tarot_manager = TarotManager(self.data_dir)
        # 设置塔罗牌图片目录（插件根目录下的 images/tarot/）
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        tarot_images_dir = os.path.join(plugin_dir, "tarot")
        self.tarot_manager.set_images_dir(tarot_images_dir)

        # 赠送系统配置
        self._gift_bot_name = cfg("gift_bot_name", "")

        # 从配置加载黑名单
        for uid in cfg("blacklist_users", []):
            self.isolation_manager.add_to_blacklist(str(uid), self.manager, self.backpack_manager)

        # ---------- LLM 工具注册 ----------
        if HAS_LLM_TOOL:
            for tool_name in [
                "pm_sign_in", "pm_view_shop", "pm_browse_shop", "pm_buy_item", "pm_view_stocks",
                "pm_buy_stock", "pm_sell_stock", "pm_my_stocks", "pm_check_balance",
                "pm_play_scratch", "pm_give_to_user", "pm_use_user_item", "pm_gift_item",
                "pm_play_slots", "pm_toggle_games", "pm_play_blackjack",
                "pm_tarot", "pm_spin_wheel",
            ]:
                try:
                    self.context.activate_llm_tool(tool_name)
                except Exception:
                    pass

        # ---------- AI 商品生成配置 ----------
        self._shop_provider_name = cfg("shop_provider_name", "")
        self._shop_generate_hour = cfg("shop_generate_hour", 6)
        self._shop_gen_task = None
        self._soup_judge_provider = cfg("soup_judge_provider", "")

        # ---------- 游戏 LLM 节能开关 ----------
        self._games_llm_enabled = True
        self._GAME_LLM_TOOLS = [
            "pm_sign_in", "pm_view_shop", "pm_browse_shop", "pm_buy_item", "pm_view_stocks",
            "pm_buy_stock", "pm_sell_stock", "pm_my_stocks", "pm_check_balance",
            "pm_play_scratch", "pm_give_to_user", "pm_use_user_item",
            "pm_gift_item", "pm_play_slots", "pm_play_blackjack",
            "pm_tarot", "pm_spin_wheel",
        ]

        # ---------- 正则表达式 ----------
        self._compile_patterns()

        # 防重复处理
        self.processed_message_ids = set()

    # ===============================================================
    #  工具方法
    # ===============================================================

    def _admin_name(self) -> str:
        return self._cfg("admin_name", "管理员")

    def _admin_denied_msg(self) -> str:
        return self._cfg(
            "admin_permission_denied_msg",
            f"只有{self._admin_name()}能操作哦",
        )

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return event.role == "admin"

    def _parse_amount(self, amount_str: str, allow_zero: bool = False) -> tuple:
        try:
            val = float(amount_str)
            if val < 0 or (val == 0 and not allow_zero):
                return (False, "金额必须是正数")
            return (True, val)
        except ValueError:
            return (False, "金额格式不正确")

    def _get_managers_for_user(self, user_id: str) -> tuple:
        if self.isolation_manager.is_blacklisted(user_id):
            managers = self.isolation_manager.get_isolated_managers(
                user_id, self.manager, self.backpack_manager
            )
            return self.manager, managers["backpack"], True
        return self.manager, self.backpack_manager, False

    def _format_records(self, records: List[Dict[str, Any]], show_type: bool = True) -> str:
        if not records:
            return "暂无"
        lines = []
        for r in records:
            if show_type:
                t = "+" if r["type"] == "income" else "-"
                lines.append(f"{r['time']}: {t}{r['amount']}元 ({r['reason']})")
            else:
                lines.append(f"{r['time']}: {r['amount']}元 ({r['reason']})")
        return "; ".join(lines)

    def _get_weekday_info(self) -> tuple:
        allowance_day = self._cfg("allowance_day", 1)
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        today = datetime.now()
        current_weekday = today.weekday()
        allowance_weekday_idx = (allowance_day - 1) % 7
        days_until = (allowance_weekday_idx - current_weekday) % 7
        return (weekday_names[allowance_weekday_idx], weekday_names[current_weekday], days_until)

    def _migrate_data_if_needed(self):
        import shutil
        old_dirs = [
            os.path.join("data", "PocketMoney"),
            os.path.join("data", "plugin_data", "PocketMoney"),
        ]
        new_files = os.listdir(self.data_dir) if os.path.exists(self.data_dir) else []
        if new_files:
            return
        for old_data_dir in old_dirs:
            if os.path.exists(old_data_dir) and os.path.isdir(old_data_dir):
                old_files = os.listdir(old_data_dir)
                if old_files:
                    os.makedirs(self.data_dir, exist_ok=True)
                    logger.info(f"[PocketMoney] 数据迁移: {old_data_dir} -> {self.data_dir}")
                    for filename in old_files:
                        old_path = os.path.join(old_data_dir, filename)
                        new_path = os.path.join(self.data_dir, filename)
                        if os.path.isfile(old_path):
                            shutil.copy2(old_path, new_path)
                    return

    def _compile_patterns(self):
        """编译所有正则表达式"""
        # 出账
        self.spend_pattern = re.compile(r"\s*\[(?=[^\]]*(?:Spend|花费|支出))[^\]]*\]\s*", re.IGNORECASE | re.DOTALL)
        self.amount_pattern = re.compile(r"(?:Spend|花费|支出)\s*[:：]\s*(\d+(?:\.\d+)?)")
        self.reason_pattern = re.compile(r"(?:Reason|原因|用途)\s*[:：]\s*(.+?)(?=\s*[,，\]]|\])")
        self.reason_fallback_pattern = re.compile(r"(?:Spend|花费|支出)\s*[:：]\s*\d+(?:\.\d+)?\s*[,，]\s*(.+?)(?=\s*\])")
        # 入库
        self.store_pattern = re.compile(r"\s*\[(?=[^\]]*(?:Store|入库|收纳))[^\]]*\]\s*", re.IGNORECASE | re.DOTALL)
        self.store_name_pattern = re.compile(r"(?:Store|入库|收纳)\s*[:：]\s*(.+?)(?=\s*[,，])")
        self.store_desc_pattern = re.compile(r"(?:Desc|描述|说明)\s*[:：]\s*(.+?)(?=\s*\])")
        # 使用
        self.use_pattern = re.compile(r"\s*\[(?=[^\]]*(?:(?<!e)Use(?!Gift)|使用(?!礼物)|用掉))[^\]]*\]\s*", re.IGNORECASE | re.DOTALL)
        self.use_name_pattern = re.compile(r"(?<!e)(?:Use)(?!Gift)\s*[:：]\s*(.+?)(?=\s*\])|(?:使用)(?!礼物)\s*[:：]\s*(.+?)(?=\s*\])|(?:用掉)\s*[:：]\s*(.+?)(?=\s*\])", re.IGNORECASE)
        # 礼物
        self.gift_pattern = re.compile(r"\s*\[(?=[^\]]*(?:Gift|礼物|收礼))[^\]]*\]\s*", re.IGNORECASE | re.DOTALL)
        self.gift_name_pattern = re.compile(r"(?:Gift|礼物|收礼)\s*[:：]\s*(.+?)(?=\s*[,\uff0c])")
        self.gift_from_pattern = re.compile(r"(?:From|来自|送礼人)\s*[:：]\s*(.+?)(?=\s*[,\uff0c])")
        self.gift_desc_pattern = re.compile(r"(?:Desc|描述|说明)\s*[:：]\s*(.+?)(?=\s*\])")
        # 使用礼物
        self.use_gift_pattern = re.compile(r"\s*\[(?=[^\]]*(?:UseGift|使用礼物|用礼物))[^\]]*\]\s*", re.IGNORECASE | re.DOTALL)
        self.use_gift_name_pattern = re.compile(r"(?:UseGift|使用礼物|用礼物)\s*[:：]\s*(.+?)(?=\s*\])")
        # 退款
        self.refund_pattern = re.compile(r"\s*\[(?=[^\]]*(?:Refund|退款|退钱))[^\]]*\]\s*", re.IGNORECASE | re.DOTALL)
        self.refund_amount_pattern = re.compile(r"(?:Refund|退款|退钱)\s*[:：]\s*(\d+(?:\.\d+)?)")
        self.refund_reason_pattern = re.compile(r"(?:Reason|原因|理由)\s*[:：]\s*(.+?)(?=\s*[,，\]]|\])")
        # 笔记
        self.note_pattern = re.compile(r"\s*\[(?=[^\]]*(?:Note|笔记|备忘|记录))[^\]]*\]\s*", re.IGNORECASE | re.DOTALL)
        self.note_content_pattern = re.compile(r"(?:Note|笔记|备忘|记录)\s*[:：]\s*(.+?)(?=\s*\])")
        # 申请取款
        self.apply_withdraw_pattern = re.compile(r"\s*\[(?=[^\]]*(?:ApplyWithdraw|申请取款|取存折))[^\]]*\]\s*", re.IGNORECASE | re.DOTALL)
        self.apply_withdraw_amount_pattern = re.compile(r"(?:ApplyWithdraw|申请取款|取存折)\s*[:：]\s*(\d+(?:\.\d+)?)")
        self.apply_withdraw_reason_pattern = re.compile(r"(?:Reason|原因|理由)\s*[:：]\s*(.+?)(?=\s*[,，\]]|\])")
        self.apply_withdraw_reason_fallback_pattern = re.compile(r"(?:ApplyWithdraw|申请取款|取存折)\s*[:：]\s*\d+(?:\.\d+)?\s*[,，]\s*(.+?)(?=\s*\])")

    # ===============================================================
    #  LLM 钩子
    # ===============================================================

    @filter.on_llm_request()
    async def add_context_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """向LLM注入小金库状态"""
        current_user_id = event.get_sender_id()
        current_user_name = event.get_sender_name() or current_user_id
        money_mgr, backpack_mgr, is_isolated = self._get_managers_for_user(current_user_id)

        if is_isolated:
            self.isolation_manager.process_pending_refunds(money_mgr)

        income_count = self._cfg("income_record_count", 2)
        expense_count = self._cfg("expense_record_count", 5)
        income_records = money_mgr.get_recent_income_records(income_count)

        if is_isolated:
            expense_records = money_mgr.get_recent_expense_records(expense_count)
            expense_records = [
                {**r, "reason": "出账"} if r.get("operator_id") != current_user_id else r
                for r in expense_records
            ]
            balance = money_mgr.get_balance()
            today_expense = money_mgr.get_today_expense()
        else:
            all_expense = [r for r in money_mgr.data.get("records", [])
                           if r["type"] == "expense" and not r.get("isolation")]
            expense_records = all_expense[-expense_count:] if all_expense else []
            pending_total = sum(r.get("amount", 0) for r in self.isolation_manager.pending_refunds)
            balance = round(money_mgr.get_balance() + pending_total, 2)
            today = datetime.now().strftime("%Y-%m-%d")
            today_expense = sum(
                r["amount"] for r in money_mgr.data.get("records", [])
                if r["type"] == "expense" and not r.get("isolation") and r["time"].startswith(today)
            )

        shared_items = backpack_mgr.format_shared_items_for_prompt()
        shared_slots = f"{backpack_mgr.get_shared_item_count()}/{backpack_mgr.max_shared_slots}"
        user_items = backpack_mgr.format_user_items_for_prompt(current_user_id)
        user_slots = f"{backpack_mgr.get_user_item_count(current_user_id)}/{backpack_mgr.max_user_slots}"

        if is_isolated:
            savings_balance = 0
            pending_count = 0
        else:
            savings_balance = self.manager.get_savings_balance()
            pending_count = len(self.manager.get_pending_withdrawals())

        income_str = self._format_records(income_records, show_type=False)
        expense_str = self._format_records(expense_records, show_type=False)
        allowance_weekday, today_weekday, days_until = self._get_weekday_info()
        today_thank_bonus = self.thank_manager.get_today_bonus()
        note = money_mgr.get_note()
        pending_info = f"（有{pending_count}个待审批申请）" if pending_count > 0 else ""
        admin_name = self._admin_name()

        pocketmoney_template = self._cfg("pocketmoney_prompt", "")
        pocketmoney_prompt = pocketmoney_template.format(
            balance=balance, savings_balance=savings_balance,
            pending_info=pending_info, unit="元",
            allowance_weekday=allowance_weekday, today_weekday=today_weekday,
            days_until=days_until, income_records=income_str,
            expense_records=expense_str, today_thank_bonus=today_thank_bonus,
            today_expense=today_expense, admin_name=admin_name,
        )
        note_str = f"\n【我的笔记】{note}" if note else ""
        if note_str and "</小金库系统>" in pocketmoney_prompt:
            pocketmoney_prompt = pocketmoney_prompt.replace("</小金库系统>", f"{note_str}\n</小金库系统>")

        backpack_template = self._cfg("backpack_prompt", "")
        backpack_prompt = backpack_template.format(
            shared_slots=shared_slots, shared_items=shared_items,
            user_name=current_user_name, user_slots=user_slots,
            user_items=user_items,
        )

        req.system_prompt += f"\n{pocketmoney_prompt}"
        req.system_prompt += f"\n{backpack_prompt}"

        # 海龟汤裁判上下文注入（仅在没有单独裁判 API 时注入主 LLM）
        soup_session_key = TurtleSoupManager.get_session_key(event)
        active_soup = self.turtle_soup_manager.get_active_soup(soup_session_key)
        if active_soup and not self._soup_judge_configured():
            req.system_prompt = TurtleSoupManager.build_judge_system_prompt(active_soup)

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """处理LLM响应中的标记"""

        # ====== 海龟汤裁判 API 拦截 ======
        # 如果配了单独裁判 API + 当前有活跃汤 → 丢弃主 LLM 回复，用裁判 API 的回复替换
        if self._soup_judge_configured():
            soup_session_key = TurtleSoupManager.get_session_key(event)
            active_soup = self.turtle_soup_manager.get_active_soup(soup_session_key)
            if active_soup:
                user_msg = getattr(event, "message_str", "") or ""
                if user_msg.strip():
                    judge_reply = await self._call_soup_judge(active_soup, user_msg)
                    if judge_reply:
                        resp.completion_text = judge_reply
                        return  # 跳过后续标记处理（裁判回复不含花钱/入库标记）

        original_text = resp.completion_text
        cleaned_text = original_text

        message_id = getattr(event, 'message_id', None) or id(event)
        response_hash = hash(original_text[:100]) if original_text else 0
        unique_key = f"{message_id}_{response_hash}"
        if unique_key in self.processed_message_ids:
            return
        self.processed_message_ids.add(unique_key)
        if len(self.processed_message_ids) > 1000:
            self.processed_message_ids = set(list(self.processed_message_ids)[-500:])

        current_user_id = event.get_sender_id()
        current_user_name = event.get_sender_name() or current_user_id
        money_mgr, backpack_mgr, is_isolated = self._get_managers_for_user(current_user_id)
        log_prefix = "[隔离池] " if is_isolated else ""

        # --- 出账 ---
        spend_matches = list(self.spend_pattern.finditer(cleaned_text))
        if spend_matches:
            cleaned_text = self.spend_pattern.sub('', cleaned_text).strip()
            spend_block = spend_matches[-1].group(0)
            amount_match = self.amount_pattern.search(spend_block)
            if amount_match:
                try:
                    amount = float(amount_match.group(1))
                    reason_match = self.reason_pattern.search(spend_block)
                    if reason_match:
                        reason = reason_match.group(1).strip()
                    else:
                        fb = self.reason_fallback_pattern.search(spend_block)
                        reason = fb.group(1).strip() if fb else "未说明原因"
                    current_balance = money_mgr.get_balance()
                    if amount <= current_balance:
                        if money_mgr.add_expense(amount, reason, current_user_id, isolation=is_isolated):
                            logger.info(f"[PocketMoney] {log_prefix}出账: {amount} - {reason}")
                            if is_isolated:
                                self.isolation_manager.add_pending_refund(amount, reason, current_user_id)
                    else:
                        if current_balance > 0:
                            fb_reason = f"{reason}（原请求{amount}元，余额不足，扣除全部）"
                            money_mgr.add_expense(current_balance, fb_reason, current_user_id, isolation=is_isolated)
                            if is_isolated:
                                self.isolation_manager.add_pending_refund(current_balance, fb_reason, current_user_id)
                except ValueError:
                    pass

        # --- 入库 ---
        store_matches = list(self.store_pattern.finditer(cleaned_text))
        if store_matches:
            cleaned_text = self.store_pattern.sub('', cleaned_text).strip()
            store_block = store_matches[-1].group(0)
            name_match = self.store_name_pattern.search(store_block)
            desc_match = self.store_desc_pattern.search(store_block)
            if name_match:
                item_name = name_match.group(1).strip()
                item_desc = desc_match.group(1).strip() if desc_match else "无描述"
                if backpack_mgr.add_shared_item(item_name, item_desc):
                    logger.info(f"[PocketMoney] {log_prefix}入库: {item_name}")
                    if not is_isolated:
                        self.isolation_manager.sync_store_to_shared(item_name, item_desc, self.backpack_manager)

        # --- 使用礼物（先于Use处理） ---
        use_gift_matches = list(self.use_gift_pattern.finditer(cleaned_text))
        if use_gift_matches:
            cleaned_text = self.use_gift_pattern.sub('', cleaned_text).strip()
            for m in use_gift_matches:
                nm = self.use_gift_name_pattern.search(m.group(0))
                if nm:
                    backpack_mgr.use_user_item(current_user_id, nm.group(1).strip())

        # --- 使用共享背包物品 ---
        use_matches = list(self.use_pattern.finditer(cleaned_text))
        if use_matches:
            cleaned_text = self.use_pattern.sub('', cleaned_text).strip()
            for m in use_matches:
                nm = self.use_name_pattern.search(m.group(0))
                if nm:
                    item_name = next((g.strip() for g in nm.groups() if g), None)
                    if item_name:
                        if backpack_mgr.use_shared_item(item_name, current_user_id):
                            if not is_isolated:
                                self.isolation_manager.sync_use_to_shared(item_name, self.backpack_manager)

        # --- 礼物入库 ---
        gift_matches = list(self.gift_pattern.finditer(cleaned_text))
        if gift_matches:
            cleaned_text = self.gift_pattern.sub('', cleaned_text).strip()
            for gm in gift_matches:
                block = gm.group(0)
                gn = self.gift_name_pattern.search(block)
                if gn:
                    gift_name = gn.group(1).strip()
                    gf = self.gift_from_pattern.search(block)
                    gd = self.gift_desc_pattern.search(block)
                    gift_from = gf.group(1).strip() if gf else current_user_name
                    gift_desc = gd.group(1).strip() if gd else "无描述"
                    # 检查用户是否有格子权限，没有则放入共享背包
                    is_admin_user = self._is_admin(event)
                    if backpack_mgr.is_user_slots_allowed(current_user_id, is_admin=is_admin_user):
                        backpack_mgr.add_user_gift(current_user_id, gift_name, gift_desc, gift_from)
                    else:
                        backpack_mgr.add_shared_item(gift_name, f"来自{gift_from}: {gift_desc}")

        # --- 退款 ---
        refund_matches = list(self.refund_pattern.finditer(cleaned_text))
        if refund_matches:
            cleaned_text = self.refund_pattern.sub('', cleaned_text).strip()
            block = refund_matches[-1].group(0)
            am = self.refund_amount_pattern.search(block)
            if am:
                try:
                    refund_amount = float(am.group(1))
                    rm = self.refund_reason_pattern.search(block)
                    refund_reason = rm.group(1).strip() if rm else "退款"
                    if refund_amount > 0:
                        money_mgr.add_income(refund_amount, f"退款：{refund_reason}", current_user_id)
                except ValueError:
                    pass

        # --- 笔记（清除标记，不自动追加） ---
        if self.note_pattern.search(cleaned_text):
            cleaned_text = self.note_pattern.sub('', cleaned_text).strip()

        # --- 申请取款 ---
        aw_matches = list(self.apply_withdraw_pattern.finditer(cleaned_text))
        if aw_matches:
            cleaned_text = self.apply_withdraw_pattern.sub('', cleaned_text).strip()
            block = aw_matches[-1].group(0)
            am = self.apply_withdraw_amount_pattern.search(block)
            if am:
                try:
                    amount = float(am.group(1))
                    rm = self.apply_withdraw_reason_pattern.search(block)
                    if rm:
                        reason = rm.group(1).strip()
                    else:
                        fb = self.apply_withdraw_reason_fallback_pattern.search(block)
                        reason = fb.group(1).strip() if fb else "未说明原因"
                    if is_isolated:
                        pass  # 静默忽略
                    else:
                        savings_balance = self.manager.get_savings_balance()
                        if amount <= savings_balance:
                            group_id = event.get_group_id()
                            source_info = {"group_id": group_id, "is_group": bool(group_id), "user_id": current_user_id}
                            application_id = self.manager.apply_withdrawal(amount, reason, source_info)
                            if application_id:
                                admin_qq = self._cfg("admin_qq", "")
                                try:
                                    notify = (
                                        f"📋 存折取款申请\n申请ID：{application_id}\n"
                                        f"金额：{amount}元\n原因：{reason}\n"
                                        f"存折余额：{savings_balance}元\n"
                                        f"回复「批准取款 {application_id}」批准"
                                    )
                                    await event.bot.send_private_msg(user_id=int(admin_qq), message=notify)
                                except Exception as e:
                                    logger.warning(f"[PocketMoney] 通知管理员失败: {e}")
                except ValueError:
                    pass

        resp.completion_text = cleaned_text

    # ===============================================================
    #  管理员：零花钱命令
    # ===============================================================

    @filter.command("发零花钱")
    async def admin_add_income(self, event: AstrMessageEvent, amount: str, *, reason: str = "零花钱"):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        ok, val = self._parse_amount(amount)
        if not ok:
            yield event.plain_result(f"错误：{val}"); return
        if self.manager.add_income(val, reason, event.get_sender_id()):
            yield event.plain_result(f"入账成功！+{val}元\n原因：{reason}\n当前余额：{self.manager.get_balance()}元")

    @filter.command("扣零花钱")
    async def admin_add_expense(self, event: AstrMessageEvent, amount: str, *, reason: str = "扣款"):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        ok, val = self._parse_amount(amount)
        if not ok:
            yield event.plain_result(f"错误：{val}"); return
        if val > self.manager.get_balance():
            yield event.plain_result(f"错误：余额不足。当前余额：{self.manager.get_balance()}元"); return
        if self.manager.add_expense(val, reason, event.get_sender_id()):
            yield event.plain_result(f"扣款成功！-{val}元\n原因：{reason}\n当前余额：{self.manager.get_balance()}元")

    @filter.command("设置余额")
    async def admin_set_balance(self, event: AstrMessageEvent, amount: str, *, reason: str = "余额调整"):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        ok, val = self._parse_amount(amount, allow_zero=True)
        if not ok:
            yield event.plain_result(f"错误：{val}"); return
        old = self.manager.get_balance()
        if self.manager.set_balance(val, reason, event.get_sender_id()):
            yield event.plain_result(f"余额已调整！\n{old}元 → {val}元\n原因：{reason}")

    @filter.command("查账")
    async def admin_check_balance(self, event: AstrMessageEvent, num: str = "5"):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        count = max(1, int(num)) if num.isdigit() else 5
        all_records = [r for r in self.manager.get_all_records() if not r.get("isolation")]
        records = all_records[-count:] if all_records else []
        pending_total = sum(r.get("amount", 0) for r in self.isolation_manager.pending_refunds)
        display_balance = round(self.manager.get_balance() + pending_total, 2)
        response = f"💰 小金库余额：{display_balance}元\n\n📋 最近{count}条记录：\n"
        if not records:
            response += "暂无记录"
        else:
            for i, r in enumerate(reversed(records), 1):
                t = "📈 入账" if r["type"] == "income" else "📉 出账"
                response += f"{i}. {t} {r['amount']}元 | {r['time']} | {r['reason']}\n"
        yield event.plain_result(response)

    @filter.command("查流水")
    async def admin_check_all_records(self, event: AstrMessageEvent, num: str = "20"):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        limit = max(1, int(num)) if num.isdigit() else 20
        all_records = [r for r in self.manager.get_all_records() if not r.get("isolation")]
        if not all_records:
            yield event.plain_result("暂无交易记录。"); return
        records = all_records[-limit:]
        response = f"📜 交易流水（最近{len(records)}条）：\n\n"
        total_income, total_expense = 0, 0
        for r in reversed(records):
            t = "+" if r["type"] == "income" else "-"
            op = f" | @{r.get('operator_id', '')}" if r.get('operator_id') else ""
            response += f"{r['time']} | {t}{r['amount']}元 | {r['reason']}{op}\n"
            if r["type"] == "income":
                total_income += r["amount"]
            else:
                total_expense += r["amount"]
        response += f"\n📊 统计：入账 +{total_income}元，出账 -{total_expense}元"
        yield event.plain_result(response)

    @filter.command("清空流水")
    async def admin_clear_records(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        count = len(self.manager.data.get("records", []))
        self.manager.data["records"] = []
        self.manager._save_data()
        yield event.plain_result(f"已清空 {count} 条交易记录，余额保持不变。")

    @filter.command("零花钱日期")
    async def check_allowance_date(self, event: AstrMessageEvent):
        wd, today, days = self._get_weekday_info()
        if days == 0:
            yield event.plain_result(f"📅 今天是{today}，就是发零花钱的日子！")
        else:
            yield event.plain_result(f"📅 发零花钱日：{wd}\n今天：{today}\n还有 {days} 天")

    # ===============================================================
    #  表扬信 / 投诉信
    # ===============================================================

    async def _process_thank_letter(self, event: AstrMessageEvent):
        uid, name = event.get_sender_id(), event.get_sender_name() or event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(uid)
        if not self.thank_manager.can_send_today(uid):
            return None, "你今天已经发过表扬信啦，明天再来吧"
        amount = random.randint(
            self._cfg("thank_letter_min_amount", 1),
            self._cfg("thank_letter_max_amount", 10),
        )
        if not self.thank_manager.record_thank_letter(uid, name, amount):
            return None, "发送失败了，请稍后再试..."
        money_mgr.data["balance"] = round(money_mgr.get_balance() + amount, 2)
        money_mgr._save_data()
        msg = (
            f"收到 {name} 的表扬信！\n"
            f"🎉 获得表扬奖金：+{amount}元\n"
            f"📊 本日表扬奖金：{self.thank_manager.get_today_bonus()}元\n"
            f"💰 当前余额：{money_mgr.get_balance()}元"
        )
        return amount, msg

    @filter.command("发表扬信")
    async def send_thank_letter(self, event: AstrMessageEvent):
        _, msg = await self._process_thank_letter(event)
        yield event.plain_result(msg)

    @filter.regex(r"统一发表扬信")
    async def send_thank_letter_broadcast(self, event: AstrMessageEvent):
        amount, msg = await self._process_thank_letter(event)
        if amount is None:
            return
        yield event.plain_result(msg)

    @filter.command("发投诉信")
    async def send_complaint_letter(self, event: AstrMessageEvent, *, reason: str = ""):
        uid, name = event.get_sender_id(), event.get_sender_name() or event.get_sender_id()
        reason = reason.strip() or "未说明原因"
        src = f"群{event.get_group_id()}" if event.get_group_id() else "私聊"
        msg = f"📮 投诉信！\n来源：{src}\n投诉人：{name}({uid})\n理由：{reason}"
        try:
            await event.bot.send_private_msg(user_id=int(self._cfg("admin_qq", "")), message=msg)
            yield event.plain_result(f"投诉信已转交给{self._admin_name()}")
        except Exception:
            yield event.plain_result(f"投诉已记录：{reason}")

    @filter.command("表扬信排行")
    async def thank_letter_ranking(self, event: AstrMessageEvent, num: str = "10"):
        top_n = max(1, int(num)) if num.isdigit() else 10
        ranking = self.thank_manager.get_ranking(top_n)
        if not ranking:
            yield event.plain_result("还没有人发过表扬信呢"); return
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"💌 表扬信排行榜（TOP {len(ranking)}）：\n"]
        for i, (key, count) in enumerate(ranking, 1):
            name = key.split("|", 1)[-1]
            m = medals[i - 1] if i <= 3 else f"{i}."
            lines.append(f"{m} {name}：{count} 封")
        lines.append(f"\n📊 累计奖金：{self.thank_manager.get_total_bonus()}元")
        yield event.plain_result("\n".join(lines))

    @filter.command("今日表扬")
    async def today_thank_bonus(self, event: AstrMessageEvent):
        yield event.plain_result(
            f"💌 本日：{self.thank_manager.get_today_bonus()}元\n"
            f"📊 累计：{self.thank_manager.get_total_bonus()}元"
        )

    # ===============================================================
    #  背包命令
    # ===============================================================

    @filter.command("我的格子")
    async def my_slots(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        user_name = event.get_sender_name() or user_id
        _, backpack_mgr, _ = self._get_managers_for_user(user_id)
        items = backpack_mgr.get_user_items(user_id)
        slots = f"{backpack_mgr.get_user_item_count(user_id)}/{backpack_mgr.max_user_slots}"
        if not items:
            yield event.plain_result(f"🎁 {user_name}，你的专属格子（{slots}）：空空如也"); return
        response = f"🎁 {user_name}，你的专属格子（{slots}）：\n\n"
        for i, item in enumerate(items, 1):
            expiry = f"\n   ⏰ 保质期至 {item['expires_at'][:10]}" if item.get("expires_at") else ""
            response += f"{i}. {item['name']}\n   🎁 来自：{item.get('from', '未知')}\n   📝 {item['description']}{expiry}\n\n"
        yield event.plain_result(response)

    @filter.command("查看背包")
    async def view_backpack(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        items = self.backpack_manager.get_shared_items()
        slots = f"{self.backpack_manager.get_shared_item_count()}/{self.backpack_manager.max_shared_slots}"
        if not items:
            yield event.plain_result(f"🎒 共享背包（{slots}）：空空如也~"); return
        response = f"🎒 共享背包（{slots}）：\n\n"
        for i, item in enumerate(items, 1):
            expiry = f"\n   ⏰ 保质期至 {item['expires_at'][:10]}" if item.get("expires_at") else ""
            response += f"{i}. {item['name']}\n   📝 {item['description']}\n   🕐 {item['time']}{expiry}\n\n"
        yield event.plain_result(response)

    @filter.command("查看专属格子")
    async def view_user_slots(self, event: AstrMessageEvent, user_id: str = ""):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        if user_id.strip():
            user_id = user_id.strip()
            items = self.backpack_manager.get_user_items(user_id)
            slots = f"{self.backpack_manager.get_user_item_count(user_id)}/{self.backpack_manager.max_user_slots}"
            if not items:
                yield event.plain_result(f"🎁 用户 {user_id} 的专属格子（{slots}）：空空如也~"); return
            response = f"🎁 用户 {user_id} 的专属格子（{slots}）：\n\n"
            for i, item in enumerate(items, 1):
                response += f"{i}. {item['name']}\n   🎁 来自：{item.get('from', '未知')}\n   📝 {item['description']}\n\n"
            yield event.plain_result(response)
        else:
            all_slots = self.backpack_manager.get_all_user_slots()
            if not all_slots:
                yield event.plain_result("🎁 还没有任何用户有专属格子物品"); return
            response = "🎁 所有用户的专属格子：\n\n"
            for uid, items in all_slots.items():
                if items:
                    response += f"用户 {uid}（{len(items)}/{self.backpack_manager.max_user_slots}）：\n"
                    for item in items:
                        response += f"  - {item['name']} (来自{item.get('from', '未知')})\n"
                    response += "\n"
            yield event.plain_result(response)

    @filter.command("清空背包")
    async def clear_backpack(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        count = self.backpack_manager.get_shared_item_count()
        self.backpack_manager.clear_shared_items()
        yield event.plain_result(f"已清空共享背包，移除了 {count} 件物品")

    @filter.command("清空专属格子")
    async def clear_user_slots(self, event: AstrMessageEvent, user_id: str):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        user_id = user_id.strip()
        count = self.backpack_manager.get_user_item_count(user_id)
        self.backpack_manager.clear_user_items(user_id)
        yield event.plain_result(f"已清空用户 {user_id} 的专属格子，移除了 {count} 件物品")

    @filter.command("背包移除")
    async def remove_from_backpack(self, event: AstrMessageEvent, *, item_name: str = ""):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        if not item_name.strip():
            yield event.plain_result("请指定要移除的物品名称"); return
        if self.backpack_manager.use_shared_item(item_name.strip()):
            yield event.plain_result(f"已从共享背包移除：{item_name}")
        else:
            yield event.plain_result(f"共享背包中没有找到：{item_name}")

    @filter.command("专属格子移除")
    async def remove_from_user_slots(self, event: AstrMessageEvent, user_id: str, *, item_name: str = ""):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        user_id = user_id.strip()
        if not item_name.strip():
            yield event.plain_result("请指定要移除的物品名称"); return
        if self.backpack_manager.use_user_item(user_id, item_name.strip()):
            yield event.plain_result(f"已从用户 {user_id} 的专属格子移除：{item_name}")
        else:
            yield event.plain_result(f"用户 {user_id} 的专属格子中没有找到：{item_name}")

    @filter.command("使用记录")
    async def usage_log(self, event: AstrMessageEvent):
        """查看物品使用记录"""
        yield event.plain_result(self.backpack_manager.format_usage_log())

    # ===============================================================
    #  笔记命令（管理员）
    # ===============================================================

    @filter.command("追加笔记")
    async def append_note(self, event: AstrMessageEvent, *, content: str = ""):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        if not content.strip():
            yield event.plain_result("请输入要追加的内容，例如：追加笔记 记得还小明5块钱"); return
        max_entries = self._cfg("max_note_entries", 5)
        self.manager.add_note(content.strip(), max_entries)
        yield event.plain_result(f"📝 笔记已追加，当前完整笔记：\n{self.manager.get_note()}")

    @filter.command("查看笔记")
    async def view_note(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        note = self.manager.get_note()
        yield event.plain_result(f"📝 小金库笔记：\n{note}" if note else "📝 小金库笔记为空")

    @filter.command("删除笔记")
    async def delete_note(self, event: AstrMessageEvent, index: str = ""):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        if not index.strip():
            yield event.plain_result("请指定要删除的笔记序号"); return
        try:
            note_index = int(index.strip())
            if note_index <= 0:
                yield event.plain_result("错误：序号必须是正整数"); return
        except ValueError:
            yield event.plain_result("错误：请输入有效的序号数字"); return
        notes = self.manager.get_notes()
        if not notes:
            yield event.plain_result("📝 当前没有笔记可删除"); return
        if note_index > len(notes):
            yield event.plain_result(f"错误：序号超出范围，当前共有 {len(notes)} 条笔记"); return
        deleted = notes[note_index - 1]
        if self.manager.delete_note(note_index):
            current = self.manager.get_note()
            yield event.plain_result(f"📝 已删除第 {note_index} 条：{deleted}\n\n{'当前笔记：' + chr(10) + current if current else '笔记已清空'}")

    @filter.command("清空笔记")
    async def clear_note(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        self.manager.clear_note()
        yield event.plain_result("📝 小金库笔记已全部清空")

    # ===============================================================
    #  黑名单 / 隔离池
    # ===============================================================

    @filter.command("零花钱拉黑")
    async def add_to_blacklist(self, event: AstrMessageEvent, user_id: str):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        user_id = user_id.strip()
        if not user_id:
            yield event.plain_result("请指定用户QQ号"); return
        user_items_count = self.backpack_manager.get_user_item_count(user_id)
        if self.isolation_manager.add_to_blacklist(user_id, self.manager, self.backpack_manager):
            self.isolation_manager.get_isolated_managers(user_id, self.manager, self.backpack_manager)
            migrate_info = f"\n已迁移 {user_items_count} 件专属格子物品到隔离池" if user_items_count > 0 else ""
            yield event.plain_result(f"🚫 用户 {user_id} 已加入隔离池{migrate_info}")
        else:
            yield event.plain_result(f"用户 {user_id} 已在黑名单中")

    @filter.command("零花钱解除拉黑")
    async def remove_from_blacklist(self, event: AstrMessageEvent, user_id: str):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        user_id = user_id.strip()
        if not user_id:
            yield event.plain_result("请指定用户QQ号"); return
        managers = self.isolation_manager.get_isolated_managers(user_id, self.manager, self.backpack_manager)
        isolated_items_count = managers["backpack"].get_user_item_count(user_id)
        if self.isolation_manager.remove_from_blacklist(user_id, self.backpack_manager):
            migrate_info = f"\n已迁移 {isolated_items_count} 件专属格子物品回真实背包" if isolated_items_count > 0 else ""
            yield event.plain_result(f"✅ 用户 {user_id} 已从黑名单移除{migrate_info}")
        else:
            yield event.plain_result(f"用户 {user_id} 不在黑名单中")

    @filter.command("零花钱黑名单")
    async def view_blacklist(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        blacklist = self.isolation_manager.get_blacklist()
        if not blacklist:
            yield event.plain_result("🚫 黑名单为空"); return
        self.isolation_manager.process_pending_refunds(self.manager)
        response = f"🚫 黑名单用户（{len(blacklist)}人）：\n"
        for uid in blacklist:
            response += f"- {uid}\n"
        yield event.plain_result(response)

    @filter.command("零花钱隔离池")
    async def view_isolation_data(self, event: AstrMessageEvent, user_id: str = ""):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        blacklist = self.isolation_manager.get_blacklist()
        if not blacklist:
            yield event.plain_result("🚫 黑名单为空，隔离池未启用"); return
        self.isolation_manager.process_pending_refunds(self.manager)
        managers = self.isolation_manager.get_isolated_managers("", self.manager, self.backpack_manager)
        isolation_records = [r for r in self.manager.get_all_records() if r.get("isolation")]
        response = f"🔒 隔离池详情（{len(blacklist)}人）\n💰 余额：{self.manager.get_balance()}元\n"
        response += f"📋 隔离出账记录：{len(isolation_records)}条\n"
        pending = self.isolation_manager.pending_refunds
        if pending:
            pt = sum(r.get("amount", 0) for r in pending)
            response += f"⏰ 待退款：{len(pending)}条 ({pt}元)\n"
        yield event.plain_result(response)

    # ===============================================================
    #  存折命令
    # ===============================================================

    @filter.command("存入存折")
    async def deposit_to_savings(self, event: AstrMessageEvent, amount: str, *, reason: str = "存入存折"):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        ok, val = self._parse_amount(amount)
        if not ok:
            yield event.plain_result(f"错误：{val}"); return
        if val > self.manager.get_balance():
            yield event.plain_result(f"错误：小金库余额不足。当前：{self.manager.get_balance()}元"); return
        if self.manager.deposit_to_savings(val, reason, event.get_sender_id()):
            yield event.plain_result(
                f"📒 存折存入成功！\n💰 存入：{val}元\n"
                f"💳 小金库余额：{self.manager.get_balance()}元\n"
                f"📒 存折余额：{self.manager.get_savings_balance()}元"
            )

    @filter.command("查看存折")
    async def view_savings(self, event: AstrMessageEvent, num: str = "5"):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        balance = self.manager.get_savings_balance()
        pending_count = len(self.manager.get_pending_withdrawals())
        response = f"📒 存折余额：{balance}元\n"
        if pending_count > 0:
            response += f"⏳ 待审批申请：{pending_count}个\n"
        response += "\n💡 使用「待审批取款」查看详细申请"
        yield event.plain_result(response)

    @filter.command("批准取款")
    async def approve_withdrawal(self, event: AstrMessageEvent, application_id: str, *, reason: str = ""):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        if not application_id.strip():
            yield event.plain_result("请指定申请ID"); return
        success, amount, apply_reason, source_info = self.manager.approve_withdrawal(
            application_id.strip(), event.get_sender_id(), reason.strip()
        )
        if not success:
            yield event.plain_result(f"批准失败：{apply_reason}"); return
        approve_note = f"（{reason.strip()}）" if reason.strip() else ""
        if source_info:
            notify_msg = f"✅ 存折取款申请已批准{approve_note}！\n💰 取款：{amount}元\n💳 小金库余额：{self.manager.get_balance()}元"
            try:
                if source_info.get("is_group") and source_info.get("group_id"):
                    await event.bot.send_group_msg(group_id=int(source_info["group_id"]), message=notify_msg)
                elif source_info.get("user_id"):
                    await event.bot.send_private_msg(user_id=int(source_info["user_id"]), message=notify_msg)
            except Exception:
                pass
        yield event.plain_result(
            f"✅ 取款申请已批准{approve_note}！\n📋 ID：{application_id}\n"
            f"💰 金额：{amount}元\n💳 小金库余额：{self.manager.get_balance()}元\n"
            f"📒 存折余额：{self.manager.get_savings_balance()}元"
        )

    @filter.command("拒绝取款")
    async def reject_withdrawal(self, event: AstrMessageEvent, application_id: str, *, reject_reason: str = ""):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        if not application_id.strip():
            yield event.plain_result("请指定申请ID"); return
        success, amount, reason, source_info = self.manager.reject_withdrawal(
            application_id.strip(), reject_reason.strip(), event.get_sender_id()
        )
        if not success:
            yield event.plain_result(f"拒绝失败：{reason}"); return
        if source_info:
            try:
                notify = f"❌ 存折取款申请被拒绝\n💰 金额：{amount}元\n📝 原因：{reason}"
                if source_info.get("is_group") and source_info.get("group_id"):
                    await event.bot.send_group_msg(group_id=int(source_info["group_id"]), message=notify)
                elif source_info.get("user_id"):
                    await event.bot.send_private_msg(user_id=int(source_info["user_id"]), message=notify)
            except Exception:
                pass
        yield event.plain_result(f"❌ 取款申请已拒绝\n📋 ID：{application_id}\n💰 金额：{amount}元")

    @filter.command("忽略取款")
    async def ignore_withdrawal(self, event: AstrMessageEvent, application_id: str):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        if not application_id.strip():
            yield event.plain_result("请指定申请ID"); return
        success, amount, reason = self.manager.ignore_withdrawal(application_id.strip())
        if not success:
            yield event.plain_result(f"忽略失败：{reason}"); return
        yield event.plain_result(f"🔇 取款申请已静默移除\n📋 ID：{application_id}\n💰 金额：{amount}元")

    @filter.command("直接取款")
    async def direct_withdrawal(self, event: AstrMessageEvent, amount: str, *, reason: str = "管理员直接取款"):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        ok, val = self._parse_amount(amount)
        if not ok:
            yield event.plain_result(f"错误：{val}"); return
        if val > self.manager.get_savings_balance():
            yield event.plain_result(f"错误：存折余额不足。当前：{self.manager.get_savings_balance()}元"); return
        if self.manager.withdraw_from_savings(val, reason, event.get_sender_id()):
            yield event.plain_result(
                f"📒 存折取款成功！\n💰 取款：{val}元\n"
                f"💳 小金库余额：{self.manager.get_balance()}元\n"
                f"📒 存折余额：{self.manager.get_savings_balance()}元"
            )

    @filter.command("待审批取款")
    async def pending_withdrawals(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        pending = self.manager.get_pending_withdrawals()
        if not pending:
            yield event.plain_result("📋 当前没有待审批的取款申请"); return
        response = f"📋 待审批取款申请（{len(pending)}个）：\n\n"
        for w in pending:
            response += f"📌 ID：{w['id']}\n   金额：{w['amount']}元\n   原因：{w['reason']}\n   时间：{w['time']}\n\n"
        response += f"📒 存折余额：{self.manager.get_savings_balance()}元\n\n"
        response += "回复「批准取款 <ID>」或「拒绝取款 <ID> <原因>」"
        yield event.plain_result(response)

    # ===============================================================
    #  超市系统
    # ===============================================================

    @filter.command("逛超市")
    async def view_shop(self, event: AstrMessageEvent):
        """查看便利超市"""
        yield event.plain_result(self.shop_manager.browse_shop("便利超市", event.get_sender_id()))

    @filter.command("购物")
    async def view_shops_alt(self, event: AstrMessageEvent):
        """查看购物街"""
        yield event.plain_result(self.shop_manager.format_shop_list())

    @filter.command("逛")
    async def browse_specific_shop(self, event: AstrMessageEvent, shop_name: str = ""):
        """浏览指定店铺"""
        shop_name = shop_name.strip()
        if not shop_name:
            yield event.plain_result(self.shop_manager.format_shop_list()); return

        user_id = event.get_sender_id()
        result = self.shop_manager.browse_shop(shop_name, user_id)
        if result:
            yield event.plain_result(result); return

        # 没有这家店 → 尝试AI生成（仅用小模型）
        if self._shop_provider_name:
            yield event.plain_result(f"🔍 正在寻找其他店...")
            try:
                prompt = ShopManager.build_ai_shop_prompt(shop_name)
                llm_resp = await self.context.llm_generate(chat_provider_id=self._shop_provider_name, prompt=prompt)
                resp_text = (llm_resp.completion_text or "").strip().strip("`").strip()
                if resp_text.startswith("json"):
                    resp_text = resp_text[4:].strip()
                import json as _json
                shop_data = _json.loads(resp_text)
                if isinstance(shop_data, dict) and "items" in shop_data:
                    self.shop_manager.save_ai_shop(shop_name, shop_data)
                    browse_result = self.shop_manager.browse_shop(shop_name, user_id)
                    if browse_result:
                        yield event.plain_result(f"✨ 找到了！{shop_name}开张啦！\n\n" + browse_result); return
            except Exception as e:
                logger.debug(f"[Shop] AI生成店铺失败: {e}")
        yield event.plain_result(f"没有找到「{shop_name}」，发「购物」看看今天开了哪些店~")

    @filter.command("购买")
    async def buy_from_shop_cmd(self, event: AstrMessageEvent, item_input: str = ""):
        """购买商品（编号或商品名，跨所有店铺搜索）"""
        if not item_input.strip():
            yield event.plain_result("请指定商品，如「购买 1」（便利超市编号）或「购买 草莓蛋糕」"); return

        user_id = event.get_sender_id()
        money_mgr, backpack_mgr, is_isolated = self._get_managers_for_user(user_id)

        item_input = item_input.strip()

        # 按编号买（便利超市）
        if item_input.isdigit():
            item_id = int(item_input)
            shop_item = None
            for si in self.shop_manager.get_today_items():
                if si["id"] == item_id:
                    shop_item = si
                    break
            if not shop_item:
                yield event.plain_result(f"便利超市没有编号 {item_id} 的商品"); return
            buy_name = shop_item["name"]
            price = shop_item["price"]
        else:
            # 按名称跨店搜索
            result = self.shop_manager.find_item_across_shops(item_input)
            if not result:
                yield event.plain_result(f"所有店铺都没有「{item_input}」~"); return
            found_item, shop_name = result
            buy_name = found_item["name"]
            price = found_item["price"]

        if price > money_mgr.get_balance():
            yield event.plain_result(f"余额不足！需要 {price}元，当前余额 {money_mgr.get_balance()}元"); return
        if backpack_mgr.is_shared_full():
            yield event.plain_result("共享背包满了，先用掉一些东西吧~"); return

        purchased = self.shop_manager.buy_from_shop(buy_name, user_id)
        if not purchased:
            yield event.plain_result("购买失败，可能已售罄"); return
        if isinstance(purchased, dict) and purchased.get("error") == "already_owned":
            yield event.plain_result(f"你已经拥有「{purchased['name']}」了，收藏品只能买一个哦~"); return

        money_mgr.add_expense(price, f"超市购买：{purchased['name']}", user_id, isolation=is_isolated)
        if is_isolated:
            self.isolation_manager.add_pending_refund(price, f"超市购买：{purchased['name']}", user_id)

        backpack_mgr.add_shared_item(
            purchased["name"], purchased["desc"],
            expires_at=purchased.get("expires_at"),
        )
        self.achievement_manager.increment_counter(user_id, "purchase_count")

        expiry_info = ""
        if purchased.get("expires_at"):
            expiry_info = f"\n⏰ 保质期至 {purchased['expires_at'][:10]}"

        yield event.plain_result(
            f"🛒 购买成功！\n"
            f"📦 {purchased['name']} - {purchased['desc']}\n"
            f"💰 花费 {price}元 | 余额 {money_mgr.get_balance()}元{expiry_info}"
        )

    # ===============================================================
    #  小游戏：刮刮乐
    # ===============================================================

    @filter.command("刮刮乐")
    async def play_scratch(self, event: AstrMessageEvent):
        """玩刮刮乐"""
        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)
        ticket_price = 3

        if ticket_price > money_mgr.get_balance():
            yield event.plain_result(f"余额不足！刮刮乐一张 {ticket_price}元，当前余额 {money_mgr.get_balance()}元"); return

        # 扣票价
        money_mgr.add_expense(ticket_price, "刮刮乐", user_id, isolation=is_isolated)
        if is_isolated:
            self.isolation_manager.add_pending_refund(ticket_price, "刮刮乐", user_id)

        # 开刮
        prize_name, winnings, _ = self.games_manager.play_scratch_card(user_id, ticket_price)

        # 发奖金
        if winnings > 0:
            money_mgr.data["balance"] = round(money_mgr.get_balance() + winnings, 2)
            money_mgr._save_data()
            net = round(winnings - ticket_price, 2)
            net_str = f"+{net}" if net >= 0 else str(net)
            yield event.plain_result(
                f"🎰 刮刮乐结果：{prize_name}！\n"
                f"💰 奖金：{winnings}元（净收益 {net_str}元）\n"
                f"💳 当前余额：{money_mgr.get_balance()}元"
            )
        else:
            yield event.plain_result(
                f"🎰 刮刮乐结果：{prize_name}\n"
                f"💸 花费 {ticket_price}元，下次好运~\n"
                f"💳 当前余额：{money_mgr.get_balance()}元"
            )

    @filter.command("刮刮乐统计")
    async def scratch_stats(self, event: AstrMessageEvent):
        """查看个人刮刮乐统计"""
        user_id = event.get_sender_id()
        stats = self.games_manager.get_scratch_stats(user_id)
        if stats["played"] == 0:
            yield event.plain_result("你还没玩过刮刮乐呢，试试「刮刮乐」指令吧~"); return
        net = round(stats["won"] - stats["spent"], 2)
        net_str = f"+{net}" if net >= 0 else str(net)
        yield event.plain_result(
            f"🎰 你的刮刮乐统计：\n"
            f"🎫 共玩 {stats['played']} 次\n"
            f"💸 花费 {stats['spent']}元\n"
            f"💰 赢得 {stats['won']}元\n"
            f"📊 净收益：{net_str}元"
        )

    # ===============================================================
    #  小游戏：炒股
    # ===============================================================

    @filter.command("股市")
    async def view_stock_market(self, event: AstrMessageEvent):
        """查看今日股市行情"""
        user_id = event.get_sender_id()
        user_level = self.level_manager.get_level(user_id)
        yield event.plain_result(self.games_manager.format_stock_market(user_level, user_id=user_id))

    @filter.command("买股票")
    async def buy_stock(self, event: AstrMessageEvent, code: str = "", shares: str = "1"):
        """买入股票"""
        if not code.strip():
            yield event.plain_result("请指定股票代码和数量，例如：买股票 NEKO 5"); return
        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)

        try:
            shares_int = int(shares)
        except ValueError:
            yield event.plain_result("数量必须是整数"); return

        # 先检查股票和费用
        stocks = self.games_manager.get_stock_market_with_holdings(user_id)
        code_upper = code.strip().upper()
        if code_upper not in stocks:
            yield event.plain_result(f"没有这支股票: {code_upper}，输入「股市」看看有哪些~"); return

        # 检查等级
        user_level = self.level_manager.get_level(user_id)
        min_level = stocks[code_upper].get("min_level", 0)
        if user_level < min_level:
            yield event.plain_result(f"🔒 {stocks[code_upper]['name']} 需要 Lv.{min_level} 才能交易，你当前 Lv.{user_level}"); return

        total_cost = round(stocks[code_upper]["price"] * shares_int, 2)
        if total_cost > money_mgr.get_balance():
            yield event.plain_result(f"余额不足！需要 {total_cost}元，当前余额 {money_mgr.get_balance()}元"); return

        # 扣钱
        money_mgr.add_expense(total_cost, f"买入股票 {code_upper} x{shares_int}", user_id, isolation=is_isolated)
        if is_isolated:
            self.isolation_manager.add_pending_refund(total_cost, f"买入股票 {code_upper}", user_id)

        # 记录持仓
        success, msg, _ = self.games_manager.buy_stock(user_id, code_upper, shares_int)
        yield event.plain_result(f"📈 {msg}\n💳 当前余额：{money_mgr.get_balance()}元")

    @filter.command("卖股票")
    async def sell_stock(self, event: AstrMessageEvent, code: str = "", shares: str = "1"):
        """卖出股票"""
        if not code.strip():
            yield event.plain_result("请指定股票代码和数量，例如：卖股票 NEKO 5"); return
        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)

        try:
            shares_int = int(shares)
        except ValueError:
            yield event.plain_result("数量必须是整数"); return

        success, msg, income = self.games_manager.sell_stock(user_id, code.strip().upper(), shares_int)
        if not success:
            yield event.plain_result(msg); return

        # 收入入账
        if income > 0:
            money_mgr.data["balance"] = round(money_mgr.get_balance() + income, 2)
            money_mgr._save_data()

        yield event.plain_result(f"📉 {msg}\n💳 当前余额：{money_mgr.get_balance()}元")

    @filter.command("我的股票")
    async def my_stocks(self, event: AstrMessageEvent):
        """查看个人股票持仓"""
        yield event.plain_result(self.games_manager.format_user_portfolio(event.get_sender_id()))

    # ===============================================================
    #  小游戏：猜大小
    # ===============================================================

    @filter.command("猜大")
    async def guess_big(self, event: AstrMessageEvent, amount: str = ""):
        """猜大：掷两个骰子，总和>=7为大"""
        async for r in self._play_guess(event, "大", amount):
            yield r

    @filter.command("猜小")
    async def guess_small(self, event: AstrMessageEvent, amount: str = ""):
        """猜小：掷两个骰子，总和<=6为小"""
        async for r in self._play_guess(event, "小", amount):
            yield r

    async def _play_guess(self, event: AstrMessageEvent, guess: str, amount_str: str):
        if not amount_str.strip():
            yield event.plain_result("请下注金额，例如：猜大 10"); return
        ok, bet = self._parse_amount(amount_str)
        if not ok:
            yield event.plain_result(f"错误：{bet}"); return

        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)

        if bet > money_mgr.get_balance():
            yield event.plain_result(f"余额不足！当前 {money_mgr.get_balance()}元"); return

        # 掷骰子
        d1 = random.randint(1, 6)
        d2 = random.randint(1, 6)
        total = d1 + d2
        result = "大" if total >= 7 else "小"
        win = (guess == result)

        if win:
            winnings = round(bet, 2)
            money_mgr.data["balance"] = round(money_mgr.get_balance() + winnings, 2)
            money_mgr._save_data()
            xp_gain = max(5, int(winnings / 10))
            self.level_manager.add_xp(user_id, xp_gain, "猜大小赢了")
            self.achievement_manager.increment_counter(user_id, "dice_count")
            self.achievement_manager.increment_counter(user_id, "dice_count_flag", 0)  # 标记玩过
            if not self.achievement_manager.get_counter(user_id, "dice_count_flag"):
                self.achievement_manager.increment_counter(user_id, "dice_count_flag", 1)
            # 连胜追踪
            cur_streak = self.achievement_manager.get_counter(user_id, "dice_win_streak") + 1
            self.achievement_manager.increment_counter(user_id, "dice_win_streak", 1)
            if cur_streak > self.achievement_manager.get_counter(user_id, "dice_streak"):
                self.achievement_manager.data.setdefault("users", {}).setdefault(user_id, {}).setdefault("counters", {})["dice_streak"] = cur_streak
            # 单次最大赢钱追踪
            if winnings > self.achievement_manager.get_counter(user_id, "biggest_win"):
                self.achievement_manager.data["users"][user_id]["counters"]["biggest_win"] = winnings
            yield event.plain_result(
                f"🎲 {d1} + {d2} = {total}（{result}）\n"
                f"✅ 你猜{guess}，猜对了！+{winnings}元（+{xp_gain}XP）\n"
                f"💳 余额：{money_mgr.get_balance()}元"
            )
        else:
            money_mgr.add_expense(bet, f"猜大小：猜{guess}输了", user_id, isolation=is_isolated)
            if is_isolated:
                self.isolation_manager.add_pending_refund(bet, "猜大小", user_id)
            self.achievement_manager.increment_counter(user_id, "dice_count")
            # 连胜归零
            self.achievement_manager.data.setdefault("users", {}).setdefault(user_id, {}).setdefault("counters", {})["dice_win_streak"] = 0
            yield event.plain_result(
                f"🎲 {d1} + {d2} = {total}（{result}）\n"
                f"❌ 你猜{guess}，没猜对！-{bet}元\n"
                f"💳 余额：{money_mgr.get_balance()}元"
            )

        self._check_achievements(user_id)

    # ===============================================================
    #  小游戏：老虎机
    # ===============================================================

    @filter.command("老虎机")
    async def play_slots(self, event: AstrMessageEvent, bet_str: str = ""):
        """玩老虎机"""
        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)

        default_bet = self._cfg("slots_bet", 5)
        if bet_str.strip():
            ok, bet = self._parse_amount(bet_str)
            if not ok:
                yield event.plain_result(f"错误：{bet}"); return
        else:
            bet = default_bet

        if bet > money_mgr.get_balance():
            yield event.plain_result(
                f"余额不足！投币 {bet}元，当前余额 {money_mgr.get_balance()}元"
            ); return

        # 扣钱
        money_mgr.add_expense(bet, "老虎机", user_id, isolation=is_isolated)
        if is_isolated:
            self.isolation_manager.add_pending_refund(bet, "老虎机", user_id)

        # 转！
        desc, winnings, _, reels = self.games_manager.play_slots(user_id, bet)
        net = round(winnings - bet, 2)

        # 赢了就加回
        if winnings > 0:
            money_mgr.data["balance"] = round(money_mgr.get_balance() + winnings, 2)
            money_mgr._save_data()
            self.level_manager.add_xp(user_id, 5, "老虎机赢了")
            yield event.plain_result(
                f"🎰 ┃ {reels[0]} ┃ {reels[1]} ┃ {reels[2]} ┃\n"
                f"{desc}\n"
                f"💰 奖金 {winnings}元（净赚 +{net}元）\n"
                f"💳 余额：{money_mgr.get_balance()}元"
            )
        else:
            yield event.plain_result(
                f"🎰 ┃ {reels[0]} ┃ {reels[1]} ┃ {reels[2]} ┃\n"
                f"{desc}\n"
                f"💸 投入 {bet}元\n"
                f"💳 余额：{money_mgr.get_balance()}元"
            )

        self._check_achievements(user_id)

    @filter.command("老虎机统计")
    async def slots_stats(self, event: AstrMessageEvent):
        """查看老虎机统计"""
        user_id = event.get_sender_id()
        stats = self.games_manager.get_slots_stats(user_id)
        if stats["played"] == 0:
            yield event.plain_result("你还没玩过老虎机呢，试试「老虎机」或「老虎机 <金额>」吧~"); return
        net = round(stats["won"] - stats["spent"], 2)
        net_str = f"+{net}" if net >= 0 else str(net)
        best = f"最高{stats['best_multi']}倍" if stats.get("best_multi", 0) > 0 else "暂无"
        yield event.plain_result(
            f"🎰 你的老虎机统计：\n"
            f"  🎮 玩了 {stats['played']} 次\n"
            f"  💸 投入 {stats['spent']}元\n"
            f"  💰 赢得 {stats['won']}元\n"
            f"  📊 净盈亏 {net_str}元\n"
            f"  🏆 三连次数 {stats.get('jackpots', 0)}次（{best}）"
        )

    # ===============================================================
    #  海龟汤
    # ===============================================================

    @filter.command("海龟汤")
    async def start_turtle_soup(self, event: AstrMessageEvent, arg: str = ""):
        """开始海龟汤游戏"""
        session_key = TurtleSoupManager.get_session_key(event)

        # 已有活跃汤
        if self.turtle_soup_manager.has_active_soup(session_key):
            yield event.plain_result(
                "🐢 已有海龟汤进行中~\n"
                "发送「揭晓汤底」结束当前题目，或「换汤」换一道"
            ); return

        arg = arg.strip()
        puzzle = None

        # 指定编号
        if arg.isdigit():
            pid = int(arg)
            puzzle = self.turtle_soup_manager.get_puzzle(pid)
            if not puzzle:
                yield event.plain_result(f"#{pid} 还没有内容哦，用「海龟汤列表」查看可用题目~"); return
        else:
            puzzle = self.turtle_soup_manager.get_random_puzzle()

        # 库里没有 → 尝试在线搜索（配了 key 即自动启用）
        if not puzzle:
            online_ok = self._soup_search_configured()
            if online_ok:
                puzzle = await self._generate_soup_online(event)
            if not puzzle:
                msg = "题库是空的"
                if not online_ok:
                    msg += "，配置搜索 API Key 即可自动联网找题"
                else:
                    msg += "，在线搜索也失败了，请稍后再试"
                yield event.plain_result(f"🐢 {msg}"); return

        # 缓存活跃汤 → add_context_prompt 会自动注入裁判上下文
        self.turtle_soup_manager.set_active_soup(
            session_key, puzzle, event.get_sender_id()
        )
        self.turtle_soup_manager.record_play()
        title = puzzle.get("title") or "海龟汤"
        pid_display = f"#{puzzle['id']}" if isinstance(puzzle.get("id"), int) else puzzle.get("id", "")

        yield event.plain_result(
            f"🐢 海龟汤开始！—— {title} {pid_display}\n\n"
            f"📜 汤面：\n{puzzle['surface']}\n\n"
            f"直接发消息提问，我只回答「是/不是/不相关」\n"
            f"💡 猜汤底 <猜测> | 揭晓汤底 | 换汤 | 退出海龟汤"
        )

    @filter.command("揭晓汤底")
    async def reveal_soup(self, event: AstrMessageEvent):
        """揭晓当前海龟汤答案"""
        session_key = TurtleSoupManager.get_session_key(event)
        puzzle = self.turtle_soup_manager.clear_active_soup(session_key)
        if not puzzle:
            yield event.plain_result("🐢 当前没有进行中的海龟汤~"); return
        self.turtle_soup_manager.record_reveal()
        yield event.plain_result(f"🐢 汤底揭晓：\n\n{puzzle['answer']}")

    @filter.command("猜汤底")
    async def guess_soup(self, event: AstrMessageEvent, guess: str = ""):
        """猜测汤底，LLM 判断是否大体正确"""
        session_key = TurtleSoupManager.get_session_key(event)
        puzzle = self.turtle_soup_manager.get_active_soup(session_key)
        if not puzzle:
            yield event.plain_result("🐢 当前没有进行中的海龟汤~"); return

        guess = guess.strip()
        if not guess:
            yield event.plain_result("🐢 用法：猜汤底 <你的猜测>\n例如：猜汤底 那个人其实已经死了"); return

        # 调用 LLM 判断猜测与汤底的相似度
        answer = puzzle.get("answer", "")
        judge_prompt = (
            "你是海龟汤游戏裁判。请判断玩家的猜测是否与汤底的核心真相大体一致。\n\n"
            f"【汤底（标准答案）】：{answer}\n"
            f"【玩家猜测】：{guess}\n\n"
            "判断标准：玩家不需要说出每个细节，只要抓住了核心反转/关键真相即算正确。\n"
            "请严格按以下 JSON 格式返回，不要有任何其他内容：\n"
            '{"correct": true或false, "comment": "一句话点评"}'
        )

        resp_text = ""
        try:
            # 确定 provider：优先裁判 Provider，否则走主 LLM
            if self._soup_judge_configured():
                prov_id = self._soup_judge_provider
            else:
                umo = event.unified_msg_origin
                prov_id = await self.context.get_current_chat_provider_id(umo=umo)
                if not prov_id:
                    yield event.plain_result("🐢 无法调用判定，请直接「揭晓汤底」查看答案"); return

            llm_resp = await self.context.llm_generate(
                chat_provider_id=prov_id, prompt=judge_prompt,
            )
            try:
                resp_text = llm_resp.completion_text or ""
            except Exception:
                pass
            if not resp_text.strip():
                for attr in ("raw_completion", "text", "content"):
                    try:
                        val = getattr(llm_resp, attr, None)
                        if val and isinstance(val, str) and val.strip():
                            resp_text = val; break
                    except Exception:
                        pass

            result = self._parse_puzzle_json_generic(resp_text)

            if result and result.get("correct"):
                # 猜对了 → 公布汤底并结束
                self.turtle_soup_manager.clear_active_soup(session_key)
                self.turtle_soup_manager.record_reveal()
                comment = result.get("comment", "")
                yield event.plain_result(
                    f"🎉 恭喜猜对了！{comment}\n\n"
                    f"🐢 汤底：\n{answer}"
                )
            elif result:
                comment = result.get("comment", "还差一点，继续猜~")
                yield event.plain_result(f"🐢 {comment}\n💡 继续提问或再猜一次~")
            else:
                yield event.plain_result("🐢 判定失败了，再试一次或直接「揭晓汤底」")

        except Exception as e:
            logger.warning(f"[TurtleSoup] 猜汤底判定异常: {e}")
            yield event.plain_result("🐢 判定出错了，请直接「揭晓汤底」查看答案")

    @staticmethod
    def _parse_puzzle_json_generic(text: str) -> Optional[dict]:
        """通用 JSON 提取（用于猜汤底判定等）"""
        import re
        if not text:
            return None
        text = text.strip()
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass
        code_block = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if code_block:
            try:
                return json.loads(code_block.group(1))
            except (json.JSONDecodeError, TypeError):
                pass
        brace_match = re.search(r'\{.*\}', text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except (json.JSONDecodeError, TypeError):
                pass
        return None

    @filter.command("换汤")
    async def swap_soup(self, event: AstrMessageEvent):
        """换一道海龟汤"""
        session_key = TurtleSoupManager.get_session_key(event)
        current = self.turtle_soup_manager.get_active_soup(session_key)
        if not current:
            yield event.plain_result("🐢 当前没有进行中的海龟汤，发「海龟汤」开始一局~"); return

        new_puzzle = self.turtle_soup_manager.get_random_puzzle(
            exclude_id=current.get("id")
        )
        if not new_puzzle and self._soup_search_configured():
            new_puzzle = await self._generate_soup_online(event)
        if not new_puzzle:
            yield event.plain_result("🐢 没有更多汤了~"); return

        # 更新缓存
        self.turtle_soup_manager.set_active_soup(
            session_key, new_puzzle, event.get_sender_id()
        )
        self.turtle_soup_manager.record_play()
        title = new_puzzle.get("title") or "海龟汤"
        pid_display = f"#{new_puzzle['id']}" if isinstance(new_puzzle.get("id"), int) else new_puzzle.get("id", "")

        yield event.plain_result(
            f"🐢 换汤！—— {title} {pid_display}\n\n"
            f"📜 新汤面：\n{new_puzzle['surface']}\n\n"
            f"继续提问吧~"
        )

    @filter.command("退出海龟汤")
    async def quit_soup(self, event: AstrMessageEvent):
        """退出海龟汤（不揭晓答案）"""
        session_key = TurtleSoupManager.get_session_key(event)
        puzzle = self.turtle_soup_manager.clear_active_soup(session_key)
        if not puzzle:
            yield event.plain_result("🐢 当前没有进行中的海龟汤~"); return
        yield event.plain_result("🐢 海龟汤结束，下次再玩~")

    # 内置搜索 API 地址
    _SOUP_SEARCH_URLS = {
        "tavily": "https://api.tavily.com/search",
        "bocha": "https://api.bochaai.com/v1/web-search",
        "openai": "https://api.openai.com/v1/chat/completions",
        "grok": "https://api.x.ai/v1/chat/completions",
    }

    def _soup_search_configured(self) -> bool:
        """检查联网搜索是否已配置（填了 type + key 即视为启用，兼容老配置）"""
        search_type = self._cfg("soup_search_type", "")
        search_key = self._cfg("soup_search_key", "")
        if search_type and search_key:
            return True
        # 向后兼容：老配置只有 url + key，没有 type
        search_url = self._cfg("soup_search_url", "")
        if search_url and search_key and not search_type:
            return True
        return False

    async def _generate_soup_online(self, event: AstrMessageEvent) -> Optional[dict]:
        """通过搜索 API 联网查找海龟汤（随机关键词 + 排除已出题 + 最多重试3次）"""
        search_type = self._cfg("soup_search_type", "").strip().lower()
        search_key = self._cfg("soup_search_key", "")
        custom_url = self._cfg("soup_search_url", "").strip()

        if not search_key:
            logger.warning("[TurtleSoup] 未配置 soup_search_key")
            return None

        # 向后兼容：老配置只有 url 没有 type → 从 URL 自动推断
        if not search_type and custom_url:
            url_lower = custom_url.lower()
            if "tavily" in url_lower:
                search_type = "tavily"
            elif "bocha" in url_lower:
                search_type = "bocha"
            elif any(k in url_lower for k in ("openai", "x.ai", "grok")):
                search_type = "grok" if "x.ai" in url_lower or "grok" in url_lower else "openai"
            else:
                search_type = "tavily"
            logger.info(f"[TurtleSoup] 从 URL 自动推断搜索类型: {search_type}")

        if not search_type:
            logger.warning("[TurtleSoup] 未配置 soup_search_type")
            return None

        search_url = custom_url or self._SOUP_SEARCH_URLS.get(search_type, "")
        if not search_url:
            logger.warning(f"[TurtleSoup] 不支持的搜索类型: {search_type}")
            return None

        try:
            import aiohttp
        except ImportError:
            logger.error("[TurtleSoup] 需要 aiohttp 库"); return None

        # 获取已出过的题目标题，用于排除
        cached_titles = self.turtle_soup_manager.get_cached_titles()

        # 最多重试 3 次（每次用不同关键词）
        max_retries = 3
        used_queries = set()

        for attempt in range(max_retries):
            query = self._random_soup_query()
            # 尽量不重复同一组关键词
            retry_count = 0
            while query in used_queries and retry_count < 5:
                query = self._random_soup_query()
                retry_count += 1
            used_queries.add(query)

            puzzle = None
            try:
                if search_type == "tavily":
                    raw_text = await self._search_tavily(search_url, search_key, query)
                    if raw_text:
                        puzzle = await self._parse_search_results(event, raw_text, cached_titles)

                elif search_type == "bocha":
                    raw_text = await self._search_bocha(search_url, search_key, query)
                    if raw_text:
                        puzzle = await self._parse_search_results(event, raw_text, cached_titles)

                elif search_type in ("openai", "grok"):
                    puzzle = await self._search_chat_api(search_url, search_key, search_type, cached_titles)

                else:
                    raw_text = await self._search_tavily(search_url, search_key, query)
                    if not raw_text:
                        raw_text = await self._search_bocha(search_url, search_key, query)
                    if raw_text:
                        puzzle = await self._parse_search_results(event, raw_text, cached_titles)

            except Exception as e:
                logger.warning(f"[TurtleSoup] 在线搜索第{attempt+1}次失败: {e}")
                continue

            if puzzle:
                if self.turtle_soup_manager.is_duplicate_online(puzzle):
                    logger.info(f"[TurtleSoup] 第{attempt+1}次搜索结果重复，换关键词重试: {puzzle.get('title')}")
                    continue
                # 成功 → 缓存 + 分配编号
                self.turtle_soup_manager.cache_online_puzzle(puzzle)
                puzzle["id"] = self.turtle_soup_manager.next_online_id()
                logger.info(f"[TurtleSoup] 在线搜索成功(第{attempt+1}次): {puzzle['id']} {puzzle.get('title')}")
                return puzzle

        logger.warning(f"[TurtleSoup] {max_retries}次搜索均未找到新题")
        return None

    # 多组搜索关键词，随机轮换避免总搜到同一道
    _SOUP_SEARCH_QUERIES = [
        "海龟汤 情境猜谜 横向思维 汤面 汤底 题目",
        "海龟汤 经典题目 情境推理 谜题",
        "横向思维谜题 海龟汤 脑洞题 有趣",
        "海龟汤 推理 悬疑故事 猜谜 题库",
        "情境猜谜 是不是 汤面汤底 合集",
        "海龟汤题目大全 横向思维 推理小故事",
        "海龟汤 新题 冷门 有意思的",
        "lateral thinking puzzle 海龟汤 中文",
    ]

    def _random_soup_query(self) -> str:
        """随机选一组搜索关键词"""
        return random.choice(self._SOUP_SEARCH_QUERIES)

    async def _search_tavily(self, url: str, key: str, query: str = "") -> Optional[str]:
        """Tavily Search API"""
        import aiohttp
        body = {
            "api_key": key,
            "query": query or self._random_soup_query(),
            "max_results": 5,
            "search_depth": "advanced",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers={"Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json()
        results = data.get("results", [])
        if not results:
            return None
        return "\n\n".join(
            f"标题: {r.get('title', '')}\n内容: {r.get('content', '')}"
            for r in results[:5]
        )

    async def _search_bocha(self, url: str, key: str, query: str = "") -> Optional[str]:
        """Bocha (博查) Search API"""
        import aiohttp
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {"query": query or self._random_soup_query(), "count": 5, "summary": True}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json()
        pages = data.get("data", {}).get("webPages", {}).get("value", [])
        if not pages:
            return None
        return "\n\n".join(
            f"标题: {p.get('name', '')}\n内容: {p.get('snippet', '')}"
            for p in pages[:5]
        )

    def _soup_judge_configured(self) -> bool:
        """检查是否配置了单独的裁判 Provider"""
        return bool(self._soup_judge_provider)

    async def _call_soup_judge(self, puzzle: Dict, user_message: str) -> Optional[str]:
        """调用单独的裁判 Provider"""
        if not self._soup_judge_provider:
            return None

        answer = puzzle.get("answer", "")
        prompt = TurtleSoupManager.build_judge_direct_prompt(answer, user_message)

        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=self._soup_judge_provider,
                prompt=prompt,
            )
            text = (llm_resp.completion_text or "").strip()
            if text:
                logger.info(f"[TurtleSoup] 裁判 Provider 返回: {text[:100]}")
                return text
        except Exception as e:
            logger.warning(f"[TurtleSoup] 裁判 Provider 调用失败: {e}")
        return None

    async def _search_chat_api(self, url: str, key: str, search_type: str, exclude_titles: list = None) -> Optional[dict]:
        """OpenAI / Grok(xAI) 兼容的 Chat API（自带联网）"""
        import aiohttp
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        prompt = TurtleSoupManager.build_search_prompt(exclude_titles=exclude_titles)

        # 自动选 model
        if search_type == "grok":
            model = "grok-3"
        else:
            model = "gpt-4o-search-preview"

        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1000,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                data = await resp.json()

        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return self._parse_puzzle_json(text)

    async def _parse_search_results(self, event: AstrMessageEvent, raw_text: str, exclude_titles: list = None) -> Optional[dict]:
        """将搜索原始结果交给当前 LLM 提取成固定格式 puzzle"""
        try:
            umo = event.unified_msg_origin
            prov_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not prov_id:
                logger.warning("[TurtleSoup] 无法获取 chat_provider_id")
                return None

            exclude_hint = ""
            if exclude_titles:
                titles_str = "、".join(exclude_titles[:10])
                exclude_hint = f"【重要】不要选以下已经出过的题目：{titles_str}\n\n"

            prompt = (
                "以下是网上搜索到的海龟汤相关内容，请从中提取一道完整的海龟汤题目。\n"
                "如果搜索结果中有多道题，随机选一道。\n"
                "如果没有完整题目，请基于搜索内容整理一道。\n\n"
                f"{exclude_hint}"
                f"--- 搜索结果 ---\n{raw_text[:3000]}\n--- 结束 ---\n\n"
                "请严格按以下 JSON 格式返回，不要有任何其他内容（不要 markdown 代码块）：\n"
                '{"title": "简短标题", "surface": "汤面内容", "answer": "汤底内容"}'
            )
            llm_resp = await self.context.llm_generate(
                chat_provider_id=prov_id, prompt=prompt,
            )

            # 尝试多种方式提取 LLM 返回文本
            resp_text = ""

            # 方式1: completion_text（标准）
            try:
                resp_text = llm_resp.completion_text or ""
            except Exception:
                pass

            # 方式2: 如果为空，尝试 raw_completion（某些版本）
            if not resp_text.strip():
                for attr in ("raw_completion", "text", "content", "message"):
                    try:
                        val = getattr(llm_resp, attr, None)
                        if val and isinstance(val, str) and val.strip():
                            resp_text = val
                            logger.info(f"[TurtleSoup] 从 llm_resp.{attr} 获取到文本")
                            break
                    except Exception:
                        pass

            # 方式3: 如果还是空，尝试遍历对象属性找 JSON
            if not resp_text.strip():
                logger.warning(f"[TurtleSoup] completion_text 为空，尝试遍历响应对象")
                for attr_name in dir(llm_resp):
                    if attr_name.startswith("_"):
                        continue
                    try:
                        val = getattr(llm_resp, attr_name)
                        if isinstance(val, str) and "{" in val and "surface" in val:
                            resp_text = val
                            logger.info(f"[TurtleSoup] 从 llm_resp.{attr_name} 找到含 puzzle 的文本")
                            break
                    except Exception:
                        pass

            logger.info(f"[TurtleSoup] LLM 返回文本 (len={len(resp_text)}): {resp_text[:300] if resp_text else '(空)'}")

            if not resp_text.strip():
                # 最后手段：打印对象所有属性帮助诊断
                attrs = {}
                for attr_name in dir(llm_resp):
                    if attr_name.startswith("_"):
                        continue
                    try:
                        val = getattr(llm_resp, attr_name)
                        if callable(val):
                            continue
                        attrs[attr_name] = str(val)[:100]
                    except Exception:
                        pass
                logger.warning(f"[TurtleSoup] 响应对象属性: {json.dumps(attrs, ensure_ascii=False)[:500]}")
                return None

            return self._parse_puzzle_json(resp_text)
        except Exception as e:
            import traceback
            logger.warning(f"[TurtleSoup] LLM解析搜索结果失败: {e}\n{traceback.format_exc()}")
        return None

    @staticmethod
    def _parse_puzzle_json(text: str) -> Optional[dict]:
        """从 LLM 返回文本中提取 puzzle JSON（兼容 thinking 模型、markdown 代码块等）"""
        import re
        if not text:
            logger.warning("[TurtleSoup] _parse_puzzle_json: 空文本")
            return None
        text = text.strip()

        # 策略1: 直接解析（纯 JSON 返回）
        try:
            result = json.loads(text)
            if result.get("surface") and result.get("answer"):
                result.setdefault("title", "在线搜索")
                return result
        except (json.JSONDecodeError, TypeError):
            pass

        # 策略2: 提取 ```json ... ``` 代码块
        code_block = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if code_block:
            try:
                result = json.loads(code_block.group(1))
                if result.get("surface") and result.get("answer"):
                    result.setdefault("title", "在线搜索")
                    return result
            except (json.JSONDecodeError, TypeError):
                pass

        # 策略3: 找文本中第一个 { ... } 块（贪婪匹配最后一个 }）
        brace_match = re.search(r'\{.*\}', text, re.DOTALL)
        if brace_match:
            try:
                result = json.loads(brace_match.group(0))
                if result.get("surface") and result.get("answer"):
                    result.setdefault("title", "在线搜索")
                    return result
            except (json.JSONDecodeError, TypeError):
                pass

        logger.warning(f"[TurtleSoup] _parse_puzzle_json 解析失败，原文前200字: {text[:200]}")
        return None

    @filter.command("海龟汤搜索")
    async def toggle_soup_online(self, event: AstrMessageEvent, toggle: str = ""):
        """查看/开关在线搜索状态"""
        toggle = toggle.strip()
        if toggle in ("开", "开启", "on"):
            self.turtle_soup_manager.set_online(True)
            yield event.plain_result("🌐 海龟汤在线搜索已强制开启~")
        elif toggle in ("关", "关闭", "off"):
            self.turtle_soup_manager.set_online(False)
            yield event.plain_result("🔒 海龟汤在线搜索已强制关闭")
        else:
            configured = self._soup_search_configured()
            override = self.turtle_soup_manager.is_online_enabled()
            search_type = self._cfg("soup_search_type", "") or "未设置"
            if configured:
                yield event.plain_result(
                    f"🐢 在线搜索：✅ 已配置\n"
                    f"搜索类型：{search_type}\n"
                    f"配了 Key 自动启用，无需手动开关"
                )
            else:
                yield event.plain_result(
                    f"🐢 在线搜索：❌ 未配置\n"
                    f"在插件设置中填写 soup_search_type 和 soup_search_key 即可启用\n"
                    f"支持：tavily / bocha / openai / grok"
                )

    @filter.command("海龟汤列表")
    async def list_soup_puzzles(self, event: AstrMessageEvent):
        """查看题库"""
        listing = self.turtle_soup_manager.list_puzzles()
        # 替换在线搜索状态为实际配置状态
        if self._soup_search_configured():
            search_type = self._cfg("soup_search_type", "")
            listing = listing.replace("🔒 在线搜索：关闭", f"🌐 在线搜索：已配置（{search_type}）")
            listing = listing.replace("🌐 在线搜索：开启", f"🌐 在线搜索：已配置（{search_type}）")
        else:
            listing = listing.replace("🌐 在线搜索：开启", "🔒 在线搜索：未配置")
        yield event.plain_result(listing)

    # 诊断方法（保留，不注册指令，需要时可手动调用或临时加回装饰器）
    async def diagnose_soup(self, event: AstrMessageEvent):
        """逐步诊断海龟汤在线搜索"""
        lines = ["🔧 海龟汤在线搜索诊断\n"]

        # Step 1: 配置检查
        search_type = self._cfg("soup_search_type", "").strip().lower()
        search_key = self._cfg("soup_search_key", "")
        custom_url = self._cfg("soup_search_url", "").strip()
        lines.append(f"1️⃣ 配置")
        lines.append(f"  search_type = '{search_type}' {'✅' if search_type else '⚠️ 未设置'}")
        lines.append(f"  search_key = {'✅ 已填' if search_key else '❌ 未填'} ({len(search_key)}字符)")
        lines.append(f"  custom_url = '{custom_url or '(空，用内置)'}'\n")

        if not search_key:
            lines.append("⛔ search_key 未填，无法继续")
            yield event.plain_result("\n".join(lines)); return

        # 向后兼容：从 URL 推断 type
        if not search_type and custom_url:
            url_lower = custom_url.lower()
            if "tavily" in url_lower:
                search_type = "tavily"
            elif "bocha" in url_lower:
                search_type = "bocha"
            elif "x.ai" in url_lower or "grok" in url_lower:
                search_type = "grok"
            elif "openai" in url_lower:
                search_type = "openai"
            else:
                search_type = "tavily"
            lines.append(f"  ℹ️ 从 URL 自动推断 type = '{search_type}'")

        if not search_type:
            lines.append("⛔ search_type 未设置且无 URL 可推断")
            yield event.plain_result("\n".join(lines)); return

        search_url = custom_url or self._SOUP_SEARCH_URLS.get(search_type, "")
        lines.append(f"  实际URL = {search_url}")
        if not search_url:
            lines.append("⛔ 无法确定 URL")
            yield event.plain_result("\n".join(lines)); return

        # Step 2: 搜索 API 调用
        lines.append(f"\n2️⃣ 搜索 API ({search_type})")
        try:
            import aiohttp
        except ImportError:
            lines.append("  ❌ aiohttp 未安装")
            yield event.plain_result("\n".join(lines)); return

        raw_text = None
        puzzle = None
        try:
            if search_type == "tavily":
                raw_text = await self._search_tavily(search_url, search_key)
            elif search_type == "bocha":
                raw_text = await self._search_bocha(search_url, search_key)
            elif search_type in ("openai", "grok"):
                puzzle = await self._search_chat_api(search_url, search_key, search_type)
            else:
                raw_text = await self._search_tavily(search_url, search_key)

            if search_type in ("openai", "grok"):
                lines.append(f"  Chat API 返回: {'✅ 拿到puzzle' if puzzle else '❌ 解析失败'}")
                if puzzle:
                    lines.append(f"  title = {puzzle.get('title', '?')}")
                    lines.append(f"  surface 前50字 = {puzzle.get('surface', '')[:50]}")
            else:
                lines.append(f"  搜索结果: {'✅ ' + str(len(raw_text)) + '字符' if raw_text else '❌ 空'}")
                if raw_text:
                    lines.append(f"  前100字: {raw_text[:100]}")
        except Exception as e:
            lines.append(f"  ❌ 异常: {e}")
            yield event.plain_result("\n".join(lines)); return

        # Step 3: LLM 解析（仅 tavily/bocha）
        if raw_text and not puzzle:
            lines.append(f"\n3️⃣ LLM 解析搜索结果")
            try:
                umo = event.unified_msg_origin
                prov_id = await self.context.get_current_chat_provider_id(umo=umo)
                lines.append(f"  provider_id = {prov_id}")
                if not prov_id:
                    lines.append("  ❌ 无法获取 provider"); yield event.plain_result("\n".join(lines)); return

                prompt = (
                    "以下是网上搜索到的海龟汤相关内容，请从中提取一道完整的海龟汤题目。\n"
                    "如果搜索结果中有多道题，随机选一道。\n"
                    "如果没有完整题目，请基于搜索内容整理一道。\n\n"
                    f"--- 搜索结果 ---\n{raw_text[:3000]}\n--- 结束 ---\n\n"
                    "请严格按以下 JSON 格式返回，不要有任何其他内容（不要 markdown 代码块）：\n"
                    '{"title": "简短标题", "surface": "汤面内容", "answer": "汤底内容"}'
                )
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=prov_id, prompt=prompt,
                )

                # 详细检查 llm_resp 的每个属性
                lines.append(f"  llm_resp 类型 = {type(llm_resp).__name__}")

                resp_text = ""
                for attr_name in ["completion_text", "raw_completion", "text", "content", "message"]:
                    try:
                        val = getattr(llm_resp, attr_name, "❌不存在")
                        if val == "❌不存在":
                            lines.append(f"  .{attr_name} → 不存在")
                        elif callable(val):
                            lines.append(f"  .{attr_name} → (方法，跳过)")
                        elif isinstance(val, str):
                            lines.append(f"  .{attr_name} → [{len(val)}字] {val[:80] if val else '(空)'}")
                            if val.strip() and not resp_text:
                                resp_text = val
                        else:
                            lines.append(f"  .{attr_name} → [{type(val).__name__}] {str(val)[:80]}")
                    except Exception as ex:
                        lines.append(f"  .{attr_name} → 访问异常: {ex}")

                # 尝试解析
                if resp_text:
                    puzzle = self._parse_puzzle_json(resp_text)
                    lines.append(f"\n  解析结果: {'✅ 成功' if puzzle else '❌ 失败'}")
                    if puzzle:
                        lines.append(f"  title = {puzzle.get('title')}")
                else:
                    lines.append(f"\n  ❌ 所有属性均无有效文本")

            except Exception as e:
                lines.append(f"  ❌ 异常: {e}")

        # 最终结果
        lines.append(f"\n{'✅ 诊断完成，在线搜索正常' if puzzle else '❌ 在线搜索有问题，请查看上方日志'}")
        yield event.plain_result("\n".join(lines))

    # ===============================================================
    #  帮助菜单
    # ===============================================================

    @filter.command("零花钱帮助")
    async def show_help(self, event: AstrMessageEvent):
        """显示全部可用指令"""
        help_text = (
            "📖 零花钱系统 - 指令大全\n\n"
            "💰 经济\n"
            "  签到 | 等级 | 成就 | 银行 | 存银行 <金额> | 取银行 <编号>\n\n"
            "🏪 购物\n"
            "  购物 | 逛超市 | 逛 <店名> | 购买 <编号/商品名>\n\n"
            "🎮 小游戏\n"
            "  刮刮乐 | 刮刮乐统计\n"
            "  老虎机 [金额] | 老虎机统计\n"
            "  猜大 <金额> | 猜小 <金额>\n"
            "  21点 <金额> | 要牌 | 停牌 | 加倍 | 21点统计\n"
            "  猜数字 <金额> | 猜 <数字>\n"
            "  转盘 <金额>\n"
            "  股市 | 买股票 <代码> <数量> | 卖股票 <代码> <数量> | 我的股票\n\n"
            "🔮 塔罗占卜\n"
            "  塔罗 单牌/三牌/六芒星 [问题]\n"
            "  今日运势 | 塔罗统计\n\n"
            "🐢 海龟汤\n"
            "  海龟汤 [编号] | 海龟汤列表 | 海龟汤搜索\n"
            "  游戏中：猜汤底 <猜测> | 揭晓汤底 | 换汤 | 退出海龟汤\n\n"
            "🎒 背包\n"
            "  我的格子 | 使用记录\n\n"
            "💌 社交\n"
            "  发表扬信 | 发投诉信 <理由> | 表扬信排行\n\n"
            "📅 其他\n"
            "  零花钱日期 | 今日表扬\n\n"
            "🔧 管理员\n"
            "  发零花钱 | 扣零花钱 | 设置余额 | 查账 | 查流水 | 清空流水\n"
            "  存入存折 | 查看存折 | 批准取款 | 拒绝取款 | 直接取款 | 待审批取款\n"
            "  查看背包 | 清空背包 | 背包移除 | 查看专属格子 | 清空专属格子\n"
            "  开放格子 <QQ号> | 关闭格子 <QQ号> | 格子白名单\n"
            "  追加笔记 | 查看笔记 | 删除笔记 | 清空笔记\n"
            "  零花钱拉黑 | 零花钱解除拉黑 | 零花钱黑名单 | 零花钱隔离池\n"
            "  刷新超市\n\n"
            "💡 大部分功能也可以直接和bot说话触发~"
        )
        yield event.plain_result(help_text)

    # ===============================================================
    #  签到 / 等级 / 成就
    # ===============================================================

    def _check_achievements(self, user_id: str, extra_stats: Dict = None) -> List[str]:
        """检查并解锁成就，返回新解锁的提示消息列表"""
        # 基础状态
        stats = {
            "balance": self.manager.get_balance(),
            "level": self.level_manager.get_level(user_id),
            "sign_streak": self.level_manager.get_sign_in_info(user_id).get("streak", 0),
            "sign_total": self.level_manager.get_sign_in_info(user_id).get("total", 0),
            "bank_interest": self.bank_manager._get_account(user_id).get("total_interest", 0),
            "bank_deposit_count": len(self.bank_manager._get_account(user_id).get("deposits", [])),
        }

        # 游戏统计
        scratch_stats = self.games_manager.get_scratch_stats(user_id)
        slots_stats = self.games_manager.get_slots_stats(user_id)
        bj_stats = self.games_manager.get_blackjack_stats(user_id)
        tarot_stats = self.tarot_manager.get_stats(user_id)

        stats["scratch_count"] = scratch_stats.get("played", 0)
        stats["slots_count"] = slots_stats.get("played", 0)
        stats["slots_jackpots"] = slots_stats.get("jackpots", 0)
        stats["bj_count"] = bj_stats.get("played", 0)
        stats["bj_wins"] = bj_stats.get("won", 0)
        stats["bj_blackjacks"] = bj_stats.get("blackjacks", 0)
        stats["tarot_count"] = tarot_stats.get("total", 0)
        stats["tarot_daily_count"] = tarot_stats.get("spreads", {}).get("daily", 0)

        # 身无分文检测
        if stats["balance"] <= 0:
            self.achievement_manager.increment_counter(user_id, "been_broke", 1)

        # 统计玩过的游戏种类
        game_types = 0
        if scratch_stats.get("played", 0) > 0: game_types += 1
        if slots_stats.get("played", 0) > 0: game_types += 1
        if bj_stats.get("played", 0) > 0: game_types += 1
        if tarot_stats.get("total", 0) > 0: game_types += 1
        stats["games_played_types"] = game_types + self.achievement_manager.get_counter(user_id, "dice_count_flag")

        if extra_stats:
            stats.update(extra_stats)

        newly = self.achievement_manager.check_and_unlock(user_id, **stats)
        msgs = []
        for name, desc, xp_reward in newly:
            self.level_manager.add_xp(user_id, xp_reward, f"成就: {name}")
            msgs.append(f"🏆 成就解锁：{name} - {desc}（+{xp_reward}XP）")
        return msgs

    @filter.command("签到")
    async def sign_in(self, event: AstrMessageEvent):
        """每日签到"""
        user_id = event.get_sender_id()
        user_name = event.get_sender_name() or user_id
        success, xp, streak, new_level = self.level_manager.sign_in(user_id)
        if not success:
            yield event.plain_result("你今天已经签到过啦，明天再来~"); return

        # 签到奖励零花钱
        sign_bonus = self._cfg("sign_in_bonus", 2)
        self.manager.data["balance"] = round(self.manager.get_balance() + sign_bonus, 2)
        self.manager._save_data()

        response = (
            f"✅ {user_name} 签到成功！\n"
            f"✨ 经验 +{xp} | 零花钱 +{sign_bonus}元\n"
            f"📅 连续签到：{streak}天\n"
            f"⭐ Lv.{new_level} | 余额：{self.manager.get_balance()}元"
        )

        # 检查成就
        ach_msgs = self._check_achievements(user_id)
        if ach_msgs:
            response += "\n\n" + "\n".join(ach_msgs)

        yield event.plain_result(response)

    @filter.command("等级")
    async def view_level(self, event: AstrMessageEvent):
        """查看等级信息"""
        user_id = event.get_sender_id()
        user_name = event.get_sender_name() or user_id
        yield event.plain_result(self.level_manager.format_level_card(user_id, user_name))

    @filter.command("成就")
    async def view_achievements(self, event: AstrMessageEvent):
        """查看成就"""
        # 先触发一次检查
        user_id = event.get_sender_id()
        self._check_achievements(user_id)
        yield event.plain_result(self.achievement_manager.format_achievements(user_id))

    # ===============================================================
    #  银行系统
    # ===============================================================

    @filter.command("银行")
    async def view_bank(self, event: AstrMessageEvent):
        """查看银行账户"""
        user_id = event.get_sender_id()
        user_level = self.level_manager.get_level(user_id)
        # 结算利息
        interest = self.bank_manager.settle_interest(user_id, user_level)
        response = self.bank_manager.format_bank_info(user_id, user_level)
        if interest > 0:
            response = f"💹 今日利息已到账：+{interest}元\n\n" + response
        yield event.plain_result(response)

    @filter.command("存银行")
    async def bank_deposit(self, event: AstrMessageEvent, amount: str = ""):
        """存钱到银行"""
        if not amount.strip():
            yield event.plain_result(f"请指定金额，例如：存银行 10000（最低{self.bank_manager.MIN_DEPOSIT}元）"); return
        user_id = event.get_sender_id()
        ok, val = self._parse_amount(amount)
        if not ok:
            yield event.plain_result(f"错误：{val}"); return
        if val > self.manager.get_balance():
            yield event.plain_result(f"余额不足！当前余额 {self.manager.get_balance()}元"); return
        success, msg = self.bank_manager.deposit(user_id, val)
        if not success:
            yield event.plain_result(msg); return
        self.manager.add_expense(val, f"银行存款", user_id)
        self.achievement_manager.increment_counter(user_id, "bank_deposit_count")
        ach_msgs = self._check_achievements(user_id)
        response = f"🏦 {msg}\n💳 小金库余额：{self.manager.get_balance()}元"
        if ach_msgs:
            response += "\n\n" + "\n".join(ach_msgs)
        yield event.plain_result(response)

    @filter.command("取银行")
    async def bank_withdraw(self, event: AstrMessageEvent, index: str = "1"):
        """从银行取款"""
        user_id = event.get_sender_id()
        try:
            idx = int(index) - 1
        except ValueError:
            yield event.plain_result("请输入存款编号"); return
        # 先结算利息
        user_level = self.level_manager.get_level(user_id)
        self.bank_manager.settle_interest(user_id, user_level)
        success, msg, amount = self.bank_manager.withdraw(user_id, idx)
        if not success:
            yield event.plain_result(msg); return
        self.manager.data["balance"] = round(self.manager.get_balance() + amount, 2)
        self.manager._save_data()
        yield event.plain_result(f"🏦 {msg}\n💳 小金库余额：{self.manager.get_balance()}元")

    # ===============================================================
    #  AI 商品生成
    # ===============================================================

    async def _generate_ai_shop(self):
        """调用AI生成今日商品"""
        if not self._shop_provider_name:
            self.shop_manager.refresh_with_defaults()
            return

        try:
            prompt = ShopManager.build_ai_prompt()
            llm_resp = await self.context.llm_generate(
                chat_provider_id=self._shop_provider_name,
                prompt=prompt,
            )
            text = llm_resp.completion_text.strip()
            # 提取JSON
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            items = json.loads(text)
            if isinstance(items, list) and len(items) > 0:
                self.shop_manager.refresh_with_ai_items(items)
                logger.info(f"[Shop] AI生成了 {len(items)} 个商品")
                return
        except Exception as e:
            logger.warning(f"[Shop] AI生成商品失败，使用默认: {e}")

        self.shop_manager.refresh_with_defaults()

    @filter.command("刷新超市")
    async def refresh_shop(self, event: AstrMessageEvent):
        """(管理员) 手动刷新超市"""
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        await self._generate_ai_shop()
        yield event.plain_result("🏪 超市已刷新！\n" + self.shop_manager.format_shop_display(event.get_sender_id()))

    # ===============================================================
    #  游戏 LLM 节能开关
    # ===============================================================

    def _set_games_llm(self, enabled: bool):
        """开关所有游戏 LLM 工具"""
        self._games_llm_enabled = enabled
        if not HAS_LLM_TOOL:
            return
        for tool_name in self._GAME_LLM_TOOLS:
            try:
                if enabled:
                    self.context.activate_llm_tool(tool_name)
                else:
                    self.context.deactivate_llm_tool(tool_name)
            except Exception:
                pass

    @filter.command("游戏节能")
    async def games_llm_off(self, event: AstrMessageEvent):
        """(管理员) 关闭所有游戏LLM工具，节省token"""
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        self._set_games_llm(False)
        yield event.plain_result("🔋 游戏节能模式已开启！所有游戏LLM工具已关闭，文字指令不受影响。\n发送「游戏恢复」重新开启。")

    @filter.command("游戏恢复")
    async def games_llm_on(self, event: AstrMessageEvent):
        """(管理员) 恢复所有游戏LLM工具"""
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        self._set_games_llm(True)
        yield event.plain_result("⚡ 游戏LLM工具已全部恢复！")

    @llm_tool(name="pm_toggle_games")
    async def tool_toggle_games(self, event: AstrMessageEvent, action: str):
        '''开启或关闭零花钱游戏系统的LLM工具。关闭后可节省token，文字指令仍可用。

        Args:
            action(string): "开启"或"关闭"
        '''
        if "关" in action or "off" in action.lower():
            self._set_games_llm(False)
            return "🔋 游戏节能模式已开启，所有游戏LLM工具已关闭。文字指令（如「签到」「逛超市」）仍可正常使用。"
        else:
            self._set_games_llm(True)
            return "⚡ 游戏LLM工具已全部恢复！"

    # ===============================================================
    #  LLM 工具（bot自己能用的）
    # ===============================================================

    @llm_tool(name="pm_sign_in")
    async def tool_sign_in(self, event: AstrMessageEvent):
        '''每日签到，获得经验和零花钱。

        Args:
        '''
        user_id = event.get_sender_id()
        user_name = event.get_sender_name() or user_id
        success, xp, streak, new_level = self.level_manager.sign_in(user_id)
        if not success:
            return "今天已经签到过啦，明天再来~"
        sign_bonus = self._cfg("sign_in_bonus", 2)
        self.manager.data["balance"] = round(self.manager.get_balance() + sign_bonus, 2)
        self.manager._save_data()
        ach_msgs = self._check_achievements(user_id)
        result = f"签到成功！经验+{xp}，零花钱+{sign_bonus}元，连续{streak}天，Lv.{new_level}，余额{self.manager.get_balance()}元"
        if ach_msgs:
            result += "。" + "；".join(ach_msgs)
        return result

    @llm_tool(name="pm_view_shop")
    async def tool_view_shop(self, event: AstrMessageEvent):
        '''查看购物街所有店铺列表。

        Args:
        '''
        return self.shop_manager.format_shop_list()

    @llm_tool(name="pm_browse_shop")
    async def tool_browse_shop(self, event: AstrMessageEvent, shop_name: str):
        '''浏览指定店铺的商品。购物街有很多固定店铺（蛋糕店、烤肉店、快餐店、寿司店、奶茶店、花店等），优先逛这些店。如果用户想逛的店不存在才会AI生成。

        Args:
            shop_name(string): 店铺名称，如"蛋糕店"、"烤肉店"、"寿司店"等
        '''
        user_id = event.get_sender_id()
        result = self.shop_manager.browse_shop(shop_name, user_id)
        if result:
            return result

        # AI生成店铺（仅在配置了小模型时）
        if self._shop_provider_name:
            try:
                prompt = ShopManager.build_ai_shop_prompt(shop_name)
                llm_resp = await self.context.llm_generate(chat_provider_id=self._shop_provider_name, prompt=prompt)
                resp_text = (llm_resp.completion_text or "").strip().strip("`").strip()
                if resp_text.startswith("json"):
                    resp_text = resp_text[4:].strip()
                import json as _json
                shop_data = _json.loads(resp_text)
                if isinstance(shop_data, dict) and "items" in shop_data:
                    self.shop_manager.save_ai_shop(shop_name, shop_data)
                    browse_result = self.shop_manager.browse_shop(shop_name, user_id)
                    if browse_result:
                        return f"✨ 找到了！{shop_name}开张啦！\n\n" + browse_result
            except Exception as e:
                logger.debug(f"[Shop] AI生成店铺失败: {e}")
        return f"没有找到「{shop_name}」，可以发「购物」看看今天开了哪些店"

    @llm_tool(name="pm_buy_item")
    async def tool_buy_item(self, event: AstrMessageEvent, item_name: str):
        '''购买商品，自动搜索所有店铺（便利超市+全部主题店），直接用商品名就行。

        Args:
            item_name(string): 商品名称
        '''
        user_id = event.get_sender_id()
        money_mgr, backpack_mgr, is_isolated = self._get_managers_for_user(user_id)

        shop_item = self.shop_manager.find_item_by_name(item_name)
        if not shop_item:
            return f"所有店铺都没有「{item_name}」"

        price = shop_item["price"]
        if price > money_mgr.get_balance():
            return f"余额不足！需要{price}元，余额{money_mgr.get_balance()}元"

        if backpack_mgr.is_shared_full():
            return "共享背包满了，需要先用掉或送出一些东西腾出空位"

        purchased = self.shop_manager.buy_from_shop(shop_item["name"], user_id)
        if not purchased:
            return "已售罄"
        if isinstance(purchased, dict) and purchased.get("error") == "already_owned":
            return f"你已经拥有「{purchased['name']}」了，收藏品只能买一个~"

        money_mgr.add_expense(price, f"购买：{purchased['name']}", user_id, isolation=is_isolated)
        backpack_mgr.add_shared_item(purchased["name"], purchased["desc"], expires_at=purchased.get("expires_at"))
        self.achievement_manager.increment_counter(user_id, "purchase_count")

        return f"购买成功：{purchased['name']}，花费{price}元，余额{money_mgr.get_balance()}元"

    @llm_tool(name="pm_view_stocks")
    async def tool_view_stocks(self, event: AstrMessageEvent):
        '''查看今日股市行情。

        Args:
        '''
        user_level = self.level_manager.get_level(event.get_sender_id())
        return self.games_manager.format_stock_market(user_level, user_id=event.get_sender_id())

    @llm_tool(name="pm_buy_stock")
    async def tool_buy_stock(self, event: AstrMessageEvent, code: str, shares: str):
        '''买入股票。

        Args:
            code(string): 股票代码
            shares(string): 数量
        '''
        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)
        try:
            shares_int = int(shares)
        except ValueError:
            return "数量必须是整数"

        stocks = self.games_manager.get_stock_market_with_holdings(user_id)
        code_upper = code.upper()
        if code_upper not in stocks:
            return f"没有这支股票: {code_upper}"

        user_level = self.level_manager.get_level(user_id)
        if stocks[code_upper].get("min_level", 0) > user_level:
            return f"等级不够，需要Lv.{stocks[code_upper]['min_level']}"

        total_cost = round(stocks[code_upper]["price"] * shares_int, 2)
        if total_cost > money_mgr.get_balance():
            return f"余额不足，需要{total_cost}元"

        money_mgr.add_expense(total_cost, f"买入 {code_upper} x{shares_int}", user_id, isolation=is_isolated)
        success, msg, _ = self.games_manager.buy_stock(user_id, code_upper, shares_int)
        return f"{msg}，余额{money_mgr.get_balance()}元"

    @llm_tool(name="pm_sell_stock")
    async def tool_sell_stock(self, event: AstrMessageEvent, code: str, shares: str):
        '''卖出股票。

        Args:
            code(string): 股票代码
            shares(string): 数量
        '''
        user_id = event.get_sender_id()
        money_mgr, _, _ = self._get_managers_for_user(user_id)
        try:
            shares_int = int(shares)
        except ValueError:
            return "数量必须是整数"

        success, msg, income = self.games_manager.sell_stock(user_id, code.upper(), shares_int)
        if not success:
            return msg
        if income > 0:
            money_mgr.data["balance"] = round(money_mgr.get_balance() + income, 2)
            money_mgr._save_data()
        return f"{msg}，余额{money_mgr.get_balance()}元"

    @llm_tool(name="pm_my_stocks")
    async def tool_my_stocks(self, event: AstrMessageEvent):
        '''查看用户当前持有的股票持仓情况。

        Args:
        '''
        return self.games_manager.format_user_portfolio(event.get_sender_id())

    @llm_tool(name="pm_check_balance")
    async def tool_check_balance(self, event: AstrMessageEvent):
        '''查看余额和背包。

        Args:
        '''
        user_id = event.get_sender_id()
        money_mgr, backpack_mgr, _ = self._get_managers_for_user(user_id)
        items = backpack_mgr.get_user_items(user_id)
        items_str = "、".join(i["name"] for i in items) if items else "空"
        return f"余额{money_mgr.get_balance()}元，背包({len(items)}/{backpack_mgr.max_user_slots})：{items_str}"

    @llm_tool(name="pm_play_scratch")
    async def tool_play_scratch(self, event: AstrMessageEvent):
        '''刮刮乐，3元一张。返回结果面板请原样输出，不要改写。

        Args:
        '''
        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)
        ticket_price = 3
        if ticket_price > money_mgr.get_balance():
            return f"余额不足，需要{ticket_price}元"

        money_mgr.add_expense(ticket_price, "刮刮乐", user_id, isolation=is_isolated)
        prize_name, winnings, _ = self.games_manager.play_scratch_card(user_id, ticket_price)
        if winnings > 0:
            money_mgr.data["balance"] = round(money_mgr.get_balance() + winnings, 2)
            money_mgr._save_data()
        panel = GamesManager.format_scratch_immersive(prize_name, winnings, ticket_price, money_mgr.get_balance())
        return f"请将以下刮刮乐面板原样发送给用户，不要缩写或口述，可以在面板后加一句简短评论：\n\n{panel}"

    @llm_tool(name="pm_play_slots")
    async def tool_play_slots(self, event: AstrMessageEvent):
        '''老虎机，默认5元一次，三个相同符号大奖。返回结果面板请原样输出，不要改写。

        Args:
        '''
        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)
        bet = self._cfg("slots_bet", 5)
        if bet > money_mgr.get_balance():
            return f"余额不足，需要{bet}元"

        money_mgr.add_expense(bet, "老虎机", user_id, isolation=is_isolated)
        desc, winnings, _, reels = self.games_manager.play_slots(user_id, bet)
        if winnings > 0:
            money_mgr.data["balance"] = round(money_mgr.get_balance() + winnings, 2)
            money_mgr._save_data()
        panel = GamesManager.format_slots_immersive(desc, winnings, bet, reels, money_mgr.get_balance())
        return f"请将以下老虎机面板原样发送给用户，不要缩写或口述，可以在面板后加一句简短评论：\n\n{panel}"

    @llm_tool(name="pm_play_blackjack")
    async def tool_play_blackjack(self, event: AstrMessageEvent, bet: str):
        '''21点游戏，下注后发牌。返回牌桌面板请原样输出，不要改写。开局后玩家需要通过「要牌」「停牌」「加倍」指令继续。

        Args:
            bet(string): 下注金额
        '''
        self.__init_blackjack_sessions()
        session_key = self._bj_session_key(event)

        if session_key in self._blackjack_sessions:
            game = self._blackjack_sessions[session_key]
            if game["status"] == "playing":
                panel = self.games_manager.format_blackjack_table(game) + "\n\n🃏 游戏进行中！回复「要牌」「停牌」「加倍」"
                return f"请将以下牌桌面板原样发送给用户，不要缩写或口述：\n\n{panel}"

        ok, bet_val = self._parse_amount(bet)
        if not ok:
            return f"错误：{bet_val}"

        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)

        if bet_val > money_mgr.get_balance():
            return f"余额不足！当前 {money_mgr.get_balance()}元"

        money_mgr.add_expense(bet_val, "21点", user_id, isolation=is_isolated)
        if is_isolated:
            self.isolation_manager.add_pending_refund(bet_val, "21点", user_id)

        game = self.games_manager.start_blackjack(user_id, bet_val)
        self._blackjack_sessions[session_key] = game

        if game["status"] != "playing":
            net, result_desc = self.games_manager.blackjack_settle(game)
            if net > 0:
                money_mgr.data["balance"] = round(money_mgr.get_balance() + bet_val + net, 2)
                money_mgr._save_data()
            elif net == 0:
                money_mgr.data["balance"] = round(money_mgr.get_balance() + bet_val, 2)
                money_mgr._save_data()
            self.games_manager.record_blackjack_stats(user_id, bet_val, net, game["status"])
            del self._blackjack_sessions[session_key]
            net_str = f"+{net}" if net >= 0 else str(net)
            panel = (
                self.games_manager.format_blackjack_table(game, reveal_dealer=True) +
                f"\n\n{result_desc}\n💰 净收益：{net_str}元\n💳 余额：{money_mgr.get_balance()}元"
            )
            return f"请将以下牌桌面板原样发送给用户，不要缩写或口述，可以在面板后加一句简短评论：\n\n{panel}"
        else:
            panel = (
                self.games_manager.format_blackjack_table(game) +
                "\n\n🃏 回复「要牌」继续拿牌\n🃏 回复「停牌」让庄家翻牌\n🃏 回复「加倍」加倍赌注并只拿一张牌"
            )
            return f"请将以下牌桌面板原样发送给用户，不要缩写或口述：\n\n{panel}"

    # ===============================================================
    #  跨bot赠送系统
    # ===============================================================

    @llm_tool(name="pm_give_to_user")
    async def tool_give_to_user(self, event: AstrMessageEvent, item_name: str):
        '''把共享背包的物品送给当前用户。如果用户有专属格子权限则放入格子，否则直接发话送出。

        Args:
            item_name(string): 物品名称
        '''
        user_id = event.get_sender_id()
        user_name = event.get_sender_name() or user_id

        # 从共享背包查找
        shared = self.backpack_manager.get_shared_items()
        found = None
        for item in shared:
            if _name_match(item["name"], item_name):
                found = item
                break
        if not found:
            return f"背包里没有「{item_name}」"

        is_admin_user = self._is_admin(event)
        desc = found.get("description", "")
        expires = found.get("expires_at")

        if self.backpack_manager.is_user_slots_allowed(user_id, is_admin=is_admin_user):
            # 有格子权限 → 放入专属格子
            if self.backpack_manager.is_user_slots_full(user_id):
                return f"{user_name}的格子满了，放不下"
            self.backpack_manager.use_shared_item(found["name"])
            self.backpack_manager.add_user_gift(
                user_id, found["name"], desc,
                "背包赠送", expires_at=expires,
            )
            return f"已把「{found['name']}」送给{user_name}，放进了ta的专属格子"
        else:
            # 没有格子权限 → 从共享背包取出，直接"送出"（不入格子）
            self.backpack_manager.use_shared_item(found["name"])
            return f"已把「{found['name']}」送给{user_name}啦~（{desc}）"

    @llm_tool(name="pm_use_user_item")
    async def tool_use_user_item(self, event: AstrMessageEvent, item_name: str):
        '''消耗/丢弃用户格子或背包里的物品。

        Args:
            item_name(string): 物品名称
        '''
        user_id = event.get_sender_id()
        _, backpack_mgr, _ = self._get_managers_for_user(user_id)
        if backpack_mgr.use_user_item(user_id, item_name):
            return f"已使用「{item_name}」，从专属格子中移除了"
        # 也试试共享背包
        if backpack_mgr.use_shared_item(item_name, user_id):
            return f"已使用「{item_name}」，从共享背包中移除了"
        return f"没有找到「{item_name}」"

    @llm_tool(name="pm_gift_item")
    async def tool_gift_item(self, event: AstrMessageEvent, item_names: str, to_user: str):
        '''跨bot赠送物品，支持一次赠送多个。多个物品用逗号分隔。

        Args:
            item_names(string): 物品名称，多个用逗号/顿号分隔，如 "苹果,蛋糕" 或 "苹果、蛋糕"
            to_user(string): 对象名字
        '''
        if not self._gift_bot_name:
            return "赠送功能未配置bot名称（gift_bot_name）"

        user_id = event.get_sender_id()
        _, backpack_mgr, _ = self._get_managers_for_user(user_id)

        # 解析多个物品名
        import re as _re
        names = [n.strip() for n in _re.split(r'[,，、]', item_names) if n.strip()]
        if not names:
            return "请指定要赠送的物品名称"

        # 先收集所有要赠送的物品（验证阶段，不修改数据）
        found_items = []
        not_found = []
        for name in names:
            found = None
            source = None
            # 先找用户格子
            items = backpack_mgr.get_user_items(user_id)
            for item in items:
                if _name_match(item["name"], name):
                    found = item
                    source = "user"
                    break
            # 再找共享背包
            if not found:
                shared = backpack_mgr.get_shared_items()
                for item in shared:
                    if _name_match(item["name"], name):
                        found = item
                        source = "shared"
                        break
            if found:
                found_items.append((found, source))
            else:
                not_found.append(name)

        if not found_items:
            return f"背包里没有找到这些物品：{'、'.join(not_found)}"

        # 执行赠送（修改数据阶段）
        sent_names = []
        for found, source in found_items:
            # 从背包取出
            if source == "shared":
                backpack_mgr.use_shared_item(found["name"])
            else:
                backpack_mgr.use_user_item(user_id, found["name"])

            # 创建赠送记录
            record = self.gift_manager.create_outgoing(
                item_name=found["name"],
                item_desc=found.get("description", found.get("desc", "")),
                from_bot=self._gift_bot_name,
                to_user=to_user,
                sender_user_id=user_id,
                expires_at=found.get("expires_at"),
            )

            # 发送赠送消息
            gift_msg = GiftManager.format_gift_offer(
                self._gift_bot_name, found["name"], to_user,
                record["timestamp"], record["signature"],
            )
            await event.send(event.plain_result(gift_msg))
            sent_names.append(found["name"])

        result_parts = []
        if sent_names:
            result_parts.append(f"已发出赠送{'、'.join(f'「{n}」' for n in sent_names)}给{to_user}的请求，等待对方bot接收（5分钟超时自动退回）")
        if not_found:
            result_parts.append(f"以下物品未找到：{'、'.join(not_found)}")
        return "。".join(result_parts)

    @filter.regex(r"「.+?」发起赠送「.+?」给 @.+?，是否接收？\[GK:[a-f0-9]{6}:\d+\]")
    async def on_gift_offer(self, event: AstrMessageEvent):
        """收到赠送消息时：验签 → 调API决定接不接 → 回复"""
        if not self._gift_bot_name:
            return

        text = event.message_str
        parsed = GiftManager.parse_gift_offer(text)
        if not parsed:
            return

        # 不接收自己发的
        if parsed["bot_name"] == self._gift_bot_name:
            return

        # 只接收发给自己的赠送
        if parsed.get("to_user", "").strip() != self._gift_bot_name:
            return

        # 验签
        if not verify_gift(parsed["item_name"], parsed["bot_name"], parsed["timestamp"], parsed["signature"]):
            logger.debug(f"[Gift] 签名验证失败，忽略")
            return

        # 检查背包空间
        # 赠送是给bot的，所以用共享背包
        if self.backpack_manager.is_shared_full():
            ts = parsed["timestamp"]
            sig = parsed["signature"]
            msg = GiftManager.format_reject(
                self._gift_bot_name, parsed["item_name"],
                "专属格子满了放不下啦...", ts, sig,
            )
            yield event.plain_result(msg)
            return

        # 调用LLM决定是否接收（走人设）
        flavor = "那我就不客气啦~"
        accept = True
        try:
            umo = event.unified_msg_origin
            prov_id = await self.context.get_current_chat_provider_id(umo=umo)
            if prov_id:
                prompt = (
                    f"有人（{parsed['bot_name']}）要送你一个「{parsed['item_name']}」。"
                    f"你要接受吗？请只回复一个JSON："
                    f'{{"accept": true/false, "reply": "你的一句话回复"}}'
                )
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=prov_id, prompt=prompt,
                )
                resp_text = llm_resp.completion_text.strip()
                if "```" in resp_text:
                    resp_text = resp_text.split("```")[1]
                    if resp_text.startswith("json"):
                        resp_text = resp_text[4:]
                result = json.loads(resp_text)
                accept = result.get("accept", True)
                flavor = result.get("reply", flavor)
        except Exception as e:
            logger.debug(f"[Gift] LLM决策失败，默认接收: {e}")

        ts = parsed["timestamp"]
        sig = parsed["signature"]

        if accept:
            # 入库
            self.backpack_manager.add_shared_item(parsed["item_name"], f"来自{parsed['bot_name']}的赠送")
            self.gift_manager.log_received(
                parsed["item_name"], "", parsed["bot_name"],
                self._gift_bot_name, "",
            )
            msg = GiftManager.format_accept(
                self._gift_bot_name, parsed["item_name"], flavor, ts, sig,
            )
        else:
            msg = GiftManager.format_reject(
                self._gift_bot_name, parsed["item_name"], flavor, ts, sig,
            )

        yield event.plain_result(msg)

    @filter.regex(r"「.+?」(?:接收了|拒绝了)「.+?」[！。].*?\[G[AR]:[a-f0-9]{6}:\d+\]")
    async def on_gift_response(self, event: AstrMessageEvent):
        """收到接收/拒绝回复时：验签 → 结算 → 调API说一句话"""
        if not self._gift_bot_name:
            return

        text = event.message_str
        parsed = GiftManager.parse_gift_response(text)
        if not parsed:
            return

        # 不处理自己发的回复
        if parsed["bot_name"] == self._gift_bot_name:
            return

        # 验签
        if not verify_gift(parsed["item_name"], self._gift_bot_name, parsed["timestamp"], parsed["signature"]):
            return

        # 查找对应的待处理赠送
        record = self.gift_manager.find_pending(parsed["item_name"], self._gift_bot_name)
        if not record:
            return

        if parsed["accepted"]:
            self.gift_manager.mark_accepted(parsed["item_name"], self._gift_bot_name)
            # 调API说一句话
            flavor = "送出去啦~"
            try:
                umo = event.unified_msg_origin
                prov_id = await self.context.get_current_chat_provider_id(umo=umo)
                if prov_id:
                    prompt = (
                        f"{parsed['bot_name']}接收了你送出的「{parsed['item_name']}」。"
                        f"请用一句话表达你的心情，直接回复这句话即可。"
                    )
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=prov_id, prompt=prompt,
                    )
                    flavor = llm_resp.completion_text.strip()
            except Exception:
                pass
            yield event.plain_result(flavor)
        else:
            # 拒绝 → 物品退回
            self.gift_manager.mark_rejected(parsed["item_name"], self._gift_bot_name)
            sender_id = record.get("sender_user_id", "")
            _, backpack_mgr, _ = self._get_managers_for_user(sender_id)
            backpack_mgr.add_shared_item(
                record["item_name"], record.get("item_desc", ""),
                expires_at=record.get("item_expires_at"),
            )
            # 调API说一句话
            flavor = "被退回来了..."
            try:
                umo = event.unified_msg_origin
                prov_id = await self.context.get_current_chat_provider_id(umo=umo)
                if prov_id:
                    prompt = (
                        f"{parsed['bot_name']}拒绝了你送出的「{parsed['item_name']}」。"
                        f"请用一句话表达你的心情，直接回复这句话即可。"
                    )
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=prov_id, prompt=prompt,
                    )
                    flavor = llm_resp.completion_text.strip()
            except Exception:
                pass
            yield event.plain_result(flavor)

    # ===============================================================
    #  塔罗占卜系统
    # ===============================================================

    @filter.command("塔罗")
    async def tarot_reading(self, event: AstrMessageEvent, spread_arg: str = "", *, question: str = ""):
        """塔罗占卜：单牌/三牌/六芒星"""
        if not spread_arg.strip():
            yield event.plain_result(self.tarot_manager.format_menu()); return

        # 解析牌阵类型
        spread_map = {
            "单牌": "single", "单": "single", "1": "single", "one": "single",
            "三牌": "three", "三": "three", "3": "three", "three": "three",
            "时间之流": "three", "过去现在未来": "three",
            "六芒星": "hexagram", "六": "hexagram", "6": "hexagram", "hex": "hexagram",
        }
        spread_type = spread_map.get(spread_arg.strip(), "")
        if not spread_type:
            yield event.plain_result(
                f"未知牌阵「{spread_arg}」\n"
                "可选：单牌 / 三牌 / 六芒星\n"
                "例如：塔罗 三牌 我的感情运势如何"
            ); return

        spread = self.tarot_manager.SPREADS[spread_type]
        price = spread["price"]

        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)

        if price > money_mgr.get_balance():
            yield event.plain_result(
                f"余额不足！{spread['name']}需要 {price}元，当前余额 {money_mgr.get_balance()}元"
            ); return

        # 扣费
        money_mgr.add_expense(price, f"塔罗占卜：{spread['name']}", user_id, isolation=is_isolated)
        if is_isolated:
            self.isolation_manager.add_pending_refund(price, f"塔罗：{spread['name']}", user_id)

        # 抽牌
        cards = self.tarot_manager.draw_cards(spread["count"])
        self.tarot_manager.record_reading(user_id, spread_type)

        # LLM 解读
        interpretation = ""
        try:
            umo = event.unified_msg_origin
            prov_id = await self.context.get_current_chat_provider_id(umo=umo)
            if prov_id:
                prompt = TarotManager.build_interpretation_prompt(
                    spread["name"], cards, spread["labels"], question.strip()
                )
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=prov_id, prompt=prompt,
                )
                resp_text = (llm_resp.completion_text or "").strip()
                if resp_text:
                    interpretation = resp_text
        except Exception as e:
            logger.debug(f"[Tarot] LLM 解读失败: {e}")

        # 构造图文混排消息链
        chain = self._build_tarot_chain(
            spread_type, cards, spread["labels"], spread["name"],
            price, money_mgr.get_balance(), interpretation
        )
        if chain:
            yield event.chain_result(chain)
        else:
            # 无图片兜底：纯文字
            display = self.tarot_manager.format_spread_display(spread_type, cards)
            display += f"\n\n💰 花费 {price}元 | 💳 余额 {money_mgr.get_balance()}元"
            if interpretation:
                display += f"\n\n🔮 解读：\n{interpretation}"
            yield event.plain_result(display)

        self._check_achievements(user_id)

    @filter.command("今日运势")
    async def daily_fortune(self, event: AstrMessageEvent):
        """每日运势（免费，每日一次）"""
        user_id = event.get_sender_id()

        if self.tarot_manager.has_daily_fortune_today(user_id):
            yield event.plain_result("🌅 你今天已经看过运势啦，明天再来~"); return

        # 抽三张
        cards = self.tarot_manager.draw_cards(3)
        self.tarot_manager.record_daily_fortune(user_id)
        self.tarot_manager.record_reading(user_id, "daily")

        # LLM 解读
        interpretation = ""
        try:
            umo = event.unified_msg_origin
            prov_id = await self.context.get_current_chat_provider_id(umo=umo)
            if prov_id:
                prompt = TarotManager.build_daily_prompt(cards)
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=prov_id, prompt=prompt,
                )
                resp_text = (llm_resp.completion_text or "").strip()
                if resp_text:
                    interpretation = resp_text
        except Exception as e:
            logger.debug(f"[Tarot] 日运 LLM 解读失败: {e}")

        # 构造图文混排消息链
        daily_labels = ["总运", "事业/学业", "感情"]
        chain = self._build_tarot_chain(
            "daily", cards, daily_labels, "今日运势",
            0, 0, interpretation
        )
        if chain:
            yield event.chain_result(chain)
        else:
            display = self.tarot_manager.format_daily_display(cards)
            if interpretation:
                display += f"\n\n🔮 {interpretation}"
            yield event.plain_result(display)

    def _build_tarot_chain(self, spread_type: str, cards: List[Dict],
                            labels: List[str], title: str,
                            price: float, balance: float,
                            interpretation: str = "") -> Optional[List]:
        """
        构造塔罗图文混排消息链 [Plain, Image, Plain, Image, ...]
        如果没有图片则返回 None（走纯文字兜底）
        """
        if not self.tarot_manager.has_images():
            return None

        try:
            chain = []

            # 标题
            if spread_type == "daily":
                chain.append(Comp.Plain("🌅 ── 今日运势 ──\n\n"))
            else:
                chain.append(Comp.Plain(f"🔮 ── {title} ──\n\n"))

            # 逐张牌：文字标签 + 图片
            for i, card in enumerate(cards):
                label = labels[i] if i < len(labels) else f"第{i+1}张"
                pos_icon = "△" if card["position"] == "正位" else "▽"
                card_text = f"【{label}】{card['name']}（{pos_icon}{card['position']}）\n"
                chain.append(Comp.Plain(card_text))

                img_path = self.tarot_manager.get_card_image_path(card)
                if img_path:
                    chain.append(Comp.Image.fromFileSystem(img_path))

                # 关键词
                chain.append(Comp.Plain(f"🔑 {card['keywords']}\n\n"))

            # 费用信息（占卜才有，日运免费）
            if price > 0:
                chain.append(Comp.Plain(f"💰 花费 {price}元 | 💳 余额 {balance}元\n"))

            # LLM 解读
            if interpretation:
                chain.append(Comp.Plain(f"\n🔮 解读：\n{interpretation}"))

            return chain
        except Exception as e:
            logger.debug(f"[Tarot] 构造图文链失败: {e}")
            return None

    @filter.command("塔罗统计")
    async def tarot_stats(self, event: AstrMessageEvent):
        """查看塔罗统计"""
        user_id = event.get_sender_id()
        stats = self.tarot_manager.get_stats(user_id)
        if stats["total"] == 0:
            yield event.plain_result("🔮 你还没做过塔罗占卜呢，试试「塔罗 单牌」吧~"); return
        lines = [f"🔮 你的塔罗统计：\n  📊 总占卜次数：{stats['total']}"]
        spread_names = {
            "single": "单牌", "three": "时间之流", "hexagram": "六芒星", "daily": "日运"
        }
        for key, count in stats.get("spreads", {}).items():
            name = spread_names.get(key, key)
            lines.append(f"  🃏 {name}：{count}次")
        yield event.plain_result("\n".join(lines))

    @llm_tool(name="pm_tarot")
    async def tool_tarot(self, event: AstrMessageEvent, spread_type: str = "三牌", question: str = ""):
        '''塔罗占卜。默认抽三张牌（时间之流牌阵）。返回牌面面板请原样输出，不要改写。

        Args:
            spread_type(string): 牌阵类型：默认"三牌"。可选"单牌"(1张2元)、"三牌"(3张5元)、"六芒星"(6张10元)、"日运"(免费每天一次)
            question(string): 提问者的问题（可选）
        '''
        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)

        # 日运特殊处理
        if "日运" in spread_type or "运势" in spread_type:
            if self.tarot_manager.has_daily_fortune_today(user_id):
                return "今天已经看过运势啦，明天再来~"
            cards = self.tarot_manager.draw_cards(3)
            self.tarot_manager.record_daily_fortune(user_id)
            self.tarot_manager.record_reading(user_id, "daily")
            return f"请将以下牌面原样发送给用户，不要改写：\n\n{self.tarot_manager.format_daily_display(cards)}"

        # 解析牌阵
        spread_map = {
            "单牌": "single", "单": "single", "1": "single", "one": "single",
            "三牌": "three", "三": "three", "3": "three", "three": "three",
            "时间之流": "three", "过去现在未来": "three",
            "六芒星": "hexagram", "六": "hexagram", "6": "hexagram", "hex": "hexagram",
            "塔罗": "three", "占卜": "three",  # 通用词默认三牌
        }
        st = spread_map.get(spread_type.strip(), "three")
        spread = self.tarot_manager.SPREADS[st]
        price = spread["price"]

        if price > money_mgr.get_balance():
            return f"余额不足！{spread['name']}需要{price}元，当前余额{money_mgr.get_balance()}元"

        money_mgr.add_expense(price, f"塔罗：{spread['name']}", user_id, isolation=is_isolated)
        if is_isolated:
            self.isolation_manager.add_pending_refund(price, f"塔罗：{spread['name']}", user_id)

        cards = self.tarot_manager.draw_cards(spread["count"])
        self.tarot_manager.record_reading(user_id, st)

        display = self.tarot_manager.format_spread_display(st, cards)
        display += f"\n\n💰 花费{price}元 | 💳 余额{money_mgr.get_balance()}元"
        return f"请将以下牌面原样发送给用户，不要改写，可以在后面加一句简短的占卜感言：\n\n{display}"

    # ===============================================================
    #  用户格子白名单管理
    # ===============================================================

    @filter.command("开放格子")
    async def add_user_slots_whitelist(self, event: AstrMessageEvent, user_id: str = ""):
        """(管理员) 给指定用户开放专属格子权限"""
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        user_id = user_id.strip()
        if not user_id:
            yield event.plain_result("请指定用户QQ号，例如：开放格子 123456"); return
        if self.backpack_manager.add_to_slots_whitelist(user_id):
            yield event.plain_result(f"✅ 用户 {user_id} 已获得专属格子权限")
        else:
            yield event.plain_result(f"用户 {user_id} 已有格子权限")

    @filter.command("关闭格子")
    async def remove_user_slots_whitelist(self, event: AstrMessageEvent, user_id: str = ""):
        """(管理员) 取消指定用户的专属格子权限"""
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        user_id = user_id.strip()
        if not user_id:
            yield event.plain_result("请指定用户QQ号，例如：关闭格子 123456"); return
        if self.backpack_manager.remove_from_slots_whitelist(user_id):
            yield event.plain_result(f"❌ 用户 {user_id} 的专属格子权限已取消")
        else:
            yield event.plain_result(f"用户 {user_id} 不在格子白名单中")

    @filter.command("格子白名单")
    async def view_slots_whitelist(self, event: AstrMessageEvent):
        """(管理员) 查看有专属格子权限的用户列表"""
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        whitelist = self.backpack_manager.get_slots_whitelist()
        if not whitelist:
            yield event.plain_result("🎁 格子白名单为空（只有管理员有格子权限）")
        else:
            lines = [f"🎁 专属格子白名单（{len(whitelist)}人）："]
            for uid in whitelist:
                items_count = self.backpack_manager.get_user_item_count(uid)
                lines.append(f"  - {uid}（{items_count}件物品）")
            lines.append("\n💡 管理员始终有格子权限")
            yield event.plain_result("\n".join(lines))

    # ===============================================================
    #  小游戏：猜数字
    # ===============================================================

    def __init_guess_sessions(self):
        if not hasattr(self, '_guess_sessions'):
            self._guess_sessions = {}

    @filter.command("猜数字")
    async def start_guess_number(self, event: AstrMessageEvent, bet_str: str = ""):
        """开始猜数字游戏 / 游戏中直接猜"""
        self.__init_guess_sessions()
        session_key = self._bj_session_key(event).replace("bj_", "gn_")

        # 如果有进行中的游戏，把参数当作猜测
        if session_key in self._guess_sessions:
            game, bet = self._guess_sessions[session_key]
            if game["status"] == "playing":
                if bet_str.strip():
                    try:
                        guess_val = int(bet_str.strip())
                        if 1 <= guess_val <= 100:
                            # 转发到猜测逻辑
                            async for r in self.guess_number_attempt(event, bet_str):
                                yield r
                            return
                    except ValueError:
                        pass
                # 参数不是有效数字，显示当前状态
                yield event.plain_result(
                    self.games_manager.format_guess_number_table(game, bet) +
                    "\n\n🔢 游戏进行中！直接发数字来猜，如「猜 50」或「猜数字 50」"
                ); return

        if not bet_str.strip():
            yield event.plain_result("请下注金额，例如：猜数字 10"); return
        ok, bet = self._parse_amount(bet_str)
        if not ok:
            yield event.plain_result(f"错误：{bet}"); return

        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)

        if bet > money_mgr.get_balance():
            yield event.plain_result(f"余额不足！当前 {money_mgr.get_balance()}元"); return

        money_mgr.add_expense(bet, "猜数字", user_id, isolation=is_isolated)

        game = self.games_manager.start_guess_number(user_id)
        self._guess_sessions[session_key] = (game, bet)

        yield event.plain_result(
            self.games_manager.format_guess_number_table(game, bet) +
            f"\n\n🔢 猜一个1~100的数字吧！{game['max_attempts']}次机会\n"
            "💡 发「猜 50」或「猜数字 50」来猜"
        )

    @filter.command("猜")
    async def guess_number_attempt(self, event: AstrMessageEvent, num_str: str = ""):
        """猜数字 - 提交猜测"""
        self.__init_guess_sessions()
        session_key = self._bj_session_key(event).replace("bj_", "gn_")

        if session_key not in self._guess_sessions:
            yield event.plain_result("🔢 没有进行中的猜数字游戏，发「猜数字 <金额>」开始~"); return

        game, bet = self._guess_sessions[session_key]
        if game["status"] != "playing":
            del self._guess_sessions[session_key]
            yield event.plain_result("🔢 游戏已结束，发「猜数字 <金额>」开新局"); return

        try:
            guess = int(num_str.strip())
        except ValueError:
            yield event.plain_result("请输入一个数字，如「猜 50」"); return

        if guess < 1 or guess > 100:
            yield event.plain_result("范围是 1~100 哦~"); return

        user_id = event.get_sender_id()
        money_mgr, _, _ = self._get_managers_for_user(user_id)

        game = self.games_manager.guess_number(game, guess)

        if game["status"] == "won":
            reward = self.games_manager.guess_number_reward(game["attempts"], bet)
            money_mgr.data["balance"] = round(money_mgr.get_balance() + reward, 2)
            money_mgr._save_data()
            xp = max(10, int(reward / 5))
            self.level_manager.add_xp(user_id, xp, "猜数字赢了")
            del self._guess_sessions[session_key]
            yield event.plain_result(
                self.games_manager.format_guess_number_table(game, bet) +
                f"\n\n💳 余额：{money_mgr.get_balance()}元（+{xp}XP）"
            )
            self._check_achievements(user_id, {"biggest_win": reward})
        elif game["status"] == "lost":
            del self._guess_sessions[session_key]
            yield event.plain_result(
                self.games_manager.format_guess_number_table(game, bet) +
                f"\n\n💸 -${bet}元\n💳 余额：{money_mgr.get_balance()}元"
            )
            self._check_achievements(user_id)
        else:
            # 继续猜
            remaining = game["max_attempts"] - game["attempts"]
            yield event.plain_result(
                self.games_manager.format_guess_number_table(game, bet) +
                f"\n\n还有 {remaining} 次机会！继续猜~"
            )

    # ===============================================================
    #  小游戏：幸运转盘
    # ===============================================================

    @filter.command("转盘")
    async def spin_wheel(self, event: AstrMessageEvent, bet_str: str = ""):
        """幸运转盘"""
        if not bet_str.strip():
            yield event.plain_result(
                "🎡 幸运转盘\n"
                "  💎 钻石 x5 | ⭐ 星星 x3 | 🎁 礼物 x2\n"
                "  🍀 幸运 x1.5 | 🔔 回本 x1 | 💨 半价 x0.5\n"
                "  ❌ 落空 x0 | 🌀 免费再转！\n\n"
                "💡 用法：转盘 <金额>"
            ); return

        ok, bet = self._parse_amount(bet_str)
        if not ok:
            yield event.plain_result(f"错误：{bet}"); return

        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)

        if bet > money_mgr.get_balance():
            yield event.plain_result(f"余额不足！当前 {money_mgr.get_balance()}元"); return

        money_mgr.add_expense(bet, "幸运转盘", user_id, isolation=is_isolated)

        emoji, name, winnings, free_spin = self.games_manager.spin_wheel(user_id, bet)

        if free_spin:
            # 退回赌注
            money_mgr.data["balance"] = round(money_mgr.get_balance() + bet, 2)
            money_mgr._save_data()
            result = self.games_manager.format_wheel_result(
                emoji, name, winnings, bet, money_mgr.get_balance(), free_spin=True
            )
            yield event.plain_result(result + f"\n\n🌀 再发一次「转盘 {bet}」免费转！")
            # 标记免费转（简化：直接退回钱了）
        else:
            if winnings > 0:
                money_mgr.data["balance"] = round(money_mgr.get_balance() + winnings, 2)
                money_mgr._save_data()
                if winnings > bet:
                    xp = max(5, int(winnings / 10))
                    self.level_manager.add_xp(user_id, xp, "转盘赢了")

            result = self.games_manager.format_wheel_result(
                emoji, name, winnings, bet, money_mgr.get_balance()
            )
            yield event.plain_result(result)

        self._check_achievements(user_id, {"biggest_win": winnings})

    @llm_tool(name="pm_spin_wheel")
    async def tool_spin_wheel(self, event: AstrMessageEvent, bet: str):
        '''幸运转盘，返回转盘面板请原样输出。

        Args:
            bet(string): 下注金额
        '''
        ok, bet_val = self._parse_amount(bet)
        if not ok:
            return f"错误：{bet_val}"

        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)

        if bet_val > money_mgr.get_balance():
            return f"余额不足！当前 {money_mgr.get_balance()}元"

        money_mgr.add_expense(bet_val, "幸运转盘", user_id, isolation=is_isolated)
        emoji, name, winnings, free_spin = self.games_manager.spin_wheel(user_id, bet_val)

        if free_spin:
            money_mgr.data["balance"] = round(money_mgr.get_balance() + bet_val, 2)
            money_mgr._save_data()
            panel = self.games_manager.format_wheel_result(
                emoji, name, winnings, bet_val, money_mgr.get_balance(), free_spin=True
            )
        else:
            if winnings > 0:
                money_mgr.data["balance"] = round(money_mgr.get_balance() + winnings, 2)
                money_mgr._save_data()
            panel = self.games_manager.format_wheel_result(
                emoji, name, winnings, bet_val, money_mgr.get_balance()
            )
        return f"请将以下转盘面板原样发送给用户，不要改写，可以在后面加一句评论：\n\n{panel}"

    # ===============================================================
    #  小游戏：21点 (Blackjack)
    # ===============================================================

    def __init_blackjack_sessions(self):
        """懒初始化21点会话存储"""
        if not hasattr(self, '_blackjack_sessions'):
            self._blackjack_sessions = {}

    def _bj_session_key(self, event: AstrMessageEvent) -> str:
        """21点会话key（与海龟汤类似）"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        if group_id:
            return f"bj_{group_id}_{user_id}"
        return f"bj_private_{user_id}"

    @filter.command("21点")
    async def start_blackjack(self, event: AstrMessageEvent, bet_str: str = ""):
        """开始21点游戏"""
        self.__init_blackjack_sessions()
        session_key = self._bj_session_key(event)

        if session_key in self._blackjack_sessions:
            game = self._blackjack_sessions[session_key]
            if game["status"] == "playing":
                yield event.plain_result(
                    self.games_manager.format_blackjack_table(game) +
                    "\n\n🃏 游戏进行中！回复「要牌」「停牌」「加倍」"
                ); return

        if not bet_str.strip():
            yield event.plain_result("请下注金额，例如：21点 10"); return
        ok, bet = self._parse_amount(bet_str)
        if not ok:
            yield event.plain_result(f"错误：{bet}"); return

        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)

        if bet > money_mgr.get_balance():
            yield event.plain_result(f"余额不足！当前 {money_mgr.get_balance()}元"); return

        # 扣钱
        money_mgr.add_expense(bet, "21点", user_id, isolation=is_isolated)
        if is_isolated:
            self.isolation_manager.add_pending_refund(bet, "21点", user_id)

        # 开局
        game = self.games_manager.start_blackjack(user_id, bet)
        self._blackjack_sessions[session_key] = game

        if game["status"] != "playing":
            # 天胡或庄家天胡
            net, result_desc = self.games_manager.blackjack_settle(game)
            if net > 0:
                money_mgr.data["balance"] = round(money_mgr.get_balance() + bet + net, 2)
                money_mgr._save_data()
            elif net == 0:
                # 平局退回赌注
                money_mgr.data["balance"] = round(money_mgr.get_balance() + bet, 2)
                money_mgr._save_data()
            self.games_manager.record_blackjack_stats(user_id, bet, net, game["status"])
            del self._blackjack_sessions[session_key]

            net_str = f"+{net}" if net >= 0 else str(net)
            yield event.plain_result(
                self.games_manager.format_blackjack_table(game, reveal_dealer=True) +
                f"\n\n{result_desc}\n💰 净收益：{net_str}元\n💳 余额：{money_mgr.get_balance()}元"
            )
            self._check_achievements(user_id)
        else:
            yield event.plain_result(
                self.games_manager.format_blackjack_table(game) +
                "\n\n🃏 回复「要牌」继续拿牌\n🃏 回复「停牌」让庄家翻牌\n🃏 回复「加倍」加倍赌注并只拿一张牌"
            )

    @filter.command("要牌")
    async def blackjack_hit(self, event: AstrMessageEvent):
        """21点要牌"""
        self.__init_blackjack_sessions()
        session_key = self._bj_session_key(event)
        game = self._blackjack_sessions.get(session_key)
        if not game or game["status"] != "playing":
            yield event.plain_result("🃏 没有进行中的21点，发「21点 <金额>」开始~"); return

        user_id = event.get_sender_id()
        money_mgr, _, _ = self._get_managers_for_user(user_id)

        game = self.games_manager.blackjack_hit(game)

        if game["status"] != "playing":
            # 爆了
            net, result_desc = self.games_manager.blackjack_settle(game)
            if net > 0:
                money_mgr.data["balance"] = round(money_mgr.get_balance() + game["bet"] + net, 2)
                money_mgr._save_data()
            elif net == 0:
                money_mgr.data["balance"] = round(money_mgr.get_balance() + game["bet"], 2)
                money_mgr._save_data()
            self.games_manager.record_blackjack_stats(user_id, game["bet"], net, game["status"])
            del self._blackjack_sessions[session_key]

            net_str = f"+{net}" if net >= 0 else str(net)
            yield event.plain_result(
                self.games_manager.format_blackjack_table(game, reveal_dealer=True) +
                f"\n\n{result_desc}\n💰 净收益：{net_str}元\n💳 余额：{money_mgr.get_balance()}元"
            )
            self._check_achievements(user_id)
        else:
            player_val = GamesManager._hand_value(game["player_hand"])
            yield event.plain_result(
                self.games_manager.format_blackjack_table(game) +
                f"\n\n当前 {player_val} 点，继续「要牌」还是「停牌」？"
            )

    @filter.command("停牌")
    async def blackjack_stand(self, event: AstrMessageEvent):
        """21点停牌"""
        self.__init_blackjack_sessions()
        session_key = self._bj_session_key(event)
        game = self._blackjack_sessions.get(session_key)
        if not game or game["status"] != "playing":
            yield event.plain_result("🃏 没有进行中的21点，发「21点 <金额>」开始~"); return

        user_id = event.get_sender_id()
        money_mgr, _, _ = self._get_managers_for_user(user_id)

        game = self.games_manager.blackjack_stand(game)
        net, result_desc = self.games_manager.blackjack_settle(game)

        if net > 0:
            money_mgr.data["balance"] = round(money_mgr.get_balance() + game["bet"] + net, 2)
            money_mgr._save_data()
        elif net == 0:
            money_mgr.data["balance"] = round(money_mgr.get_balance() + game["bet"], 2)
            money_mgr._save_data()

        self.games_manager.record_blackjack_stats(user_id, game["bet"], net, game["status"])
        del self._blackjack_sessions[session_key]

        net_str = f"+{net}" if net >= 0 else str(net)
        yield event.plain_result(
            self.games_manager.format_blackjack_table(game, reveal_dealer=True) +
            f"\n\n{result_desc}\n💰 净收益：{net_str}元\n💳 余额：{money_mgr.get_balance()}元"
        )
        self._check_achievements(user_id)

    @filter.command("加倍")
    async def blackjack_double(self, event: AstrMessageEvent):
        """21点加倍"""
        self.__init_blackjack_sessions()
        session_key = self._bj_session_key(event)
        game = self._blackjack_sessions.get(session_key)
        if not game or game["status"] != "playing":
            yield event.plain_result("🃏 没有进行中的21点，发「21点 <金额>」开始~"); return

        if len(game["player_hand"]) != 2:
            yield event.plain_result("🃏 加倍只能在拿到初始两张牌时使用"); return

        user_id = event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(user_id)
        extra_bet = game["bet"]  # 加倍的额外赌注

        if extra_bet > money_mgr.get_balance():
            yield event.plain_result(f"余额不足！加倍需要额外 {extra_bet}元，当前余额 {money_mgr.get_balance()}元"); return

        # 扣额外赌注
        money_mgr.add_expense(extra_bet, "21点加倍", user_id, isolation=is_isolated)
        if is_isolated:
            self.isolation_manager.add_pending_refund(extra_bet, "21点加倍", user_id)

        game = self.games_manager.blackjack_double(game)
        net, result_desc = self.games_manager.blackjack_settle(game)

        if net > 0:
            money_mgr.data["balance"] = round(money_mgr.get_balance() + game["bet"] + net, 2)
            money_mgr._save_data()
        elif net == 0:
            money_mgr.data["balance"] = round(money_mgr.get_balance() + game["bet"], 2)
            money_mgr._save_data()

        self.games_manager.record_blackjack_stats(user_id, game["bet"], net, game["status"])
        del self._blackjack_sessions[session_key]

        net_str = f"+{net}" if net >= 0 else str(net)
        yield event.plain_result(
            self.games_manager.format_blackjack_table(game, reveal_dealer=True) +
            f"\n\n🔥 加倍！赌注：{game['bet']}元\n{result_desc}\n💰 净收益：{net_str}元\n💳 余额：{money_mgr.get_balance()}元"
        )
        self._check_achievements(user_id)

    @filter.command("21点统计")
    async def blackjack_stats(self, event: AstrMessageEvent):
        """查看21点统计"""
        user_id = event.get_sender_id()
        stats = self.games_manager.get_blackjack_stats(user_id)
        if stats["played"] == 0:
            yield event.plain_result("你还没玩过21点呢，试试「21点 <金额>」吧~"); return
        net = stats["total_net"]
        net_str = f"+{net}" if net >= 0 else str(net)
        win_rate = round(stats["won"] / stats["played"] * 100, 1) if stats["played"] > 0 else 0
        yield event.plain_result(
            f"🃏 你的21点统计：\n"
            f"  🎮 玩了 {stats['played']} 局\n"
            f"  ✅ 赢 {stats['won']} | ❌ 输 {stats['lost']} | 🤝 平 {stats['push']}\n"
            f"  📊 胜率 {win_rate}%\n"
            f"  💰 总下注 {stats['total_bet']}元\n"
            f"  📊 净盈亏 {net_str}元\n"
            f"  🃏 Blackjack {stats.get('blackjacks', 0)}次"
        )

    # ===============================================================
    #  终止
    # ===============================================================

    async def terminate(self):
        # 清理超时赠送，退回物品
        expired = self.gift_manager.cleanup_expired()
        for record in expired:
            sender_id = record.get("sender_user_id", "")
            if sender_id:
                _, backpack_mgr, _ = self._get_managers_for_user(sender_id)
                backpack_mgr.add_shared_item(
                    record["item_name"], record.get("item_desc", ""),
                    expires_at=record.get("item_expires_at"),
                )
            logger.info(f"[Gift] 赠送超时退回: {record['item_name']}")

        self.manager._save_data()
        self.thank_manager._save_data()
        self.backpack_manager._save_data()
        self.isolation_manager._save_data()
        self.level_manager._save_data()
        self.bank_manager._save_data()
        self.achievement_manager._save_data()
        self.gift_manager._save_data()
        self.tarot_manager._save_data()
