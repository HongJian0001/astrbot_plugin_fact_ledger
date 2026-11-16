"""AstrBot 成长台账（fact_ledger）。

把「会变的数值」从记忆里拆出来单独托管：体重、攒钱、连续打卡、阶段进度……
每个指标是一个状态机，每次变化都带原因和证据；对话时以台账为准，不让模型现编数字。

设计边界：
- 只负责「数值 + 规则 + 证据」，不判断「今天做没做」——那由触发源决定；
- 默认零额外模型调用，推进是纯计算；
- 不改动任何其它插件，停用即失效。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover
    def get_astrbot_data_path() -> str:
        return os.path.join(os.getcwd(), "data")

try:
    from astrbot.api.provider import ProviderRequest
except Exception:  # pragma: no cover
    ProviderRequest = Any  # type: ignore

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover
    TextPart = None  # type: ignore

from . import ledger

PLUGIN_NAME = "astrbot_plugin_fact_ledger"

#: 配置页字段 -> (指标 key, 指标字段, 类型)
CONFIG_FIELDS = (
    # 身份卡（固定外貌，以人设为准）
    ("appearance_skin_tone", "skin_tone", "value", "str"),
    ("appearance_hair", "hair", "value", "str"),
    ("appearance_eyes", "eyes", "value", "str"),
    ("appearance_facial", "facial_features", "value", "str"),
    ("appearance_marks", "special_marks", "value", "str"),
    ("appearance_expression", "expression", "value", "str"),
    # 规则先应用，避免「先改体重、后改下限」被旧下限夹住
    ("rule_step", "weight_jin", "step", "float"),
    ("rule_floor", "weight_jin", "floor", "float"),
    ("rule_ceiling", "weight_jin", "ceiling", "float"),
    ("rule_flat_probability", "weight_jin", "flat_probability", "float"),
    ("rule_gain_probability", "weight_jin", "gain_probability", "float"),
    ("rule_gain_step", "weight_jin", "gain_step", "float"),
    ("rule_cadence", "weight_jin", "cadence", "str"),
    # 可变数值
    ("body_weight", "weight_jin", "value", "float"),
    ("body_target", "weight_jin", "target", "float"),
    ("body_deadline", "weight_jin", "deadline", "str"),
    ("body_height", "height_cm", "value", "float"),
    # 私密
    ("body_bust", "bust_cm", "value", "float"),
    ("body_waist", "waist_cm", "value", "float"),
    ("body_hip", "hip_cm", "value", "float"),
    ("body_cup", "cup_size", "value", "str"),
)
LOG = "[fact_ledger]"



LEDGER_RULES = (
    "<fact_ledger_rules>"
    "她有两类被记录的自身设定：<identity_card> 是固定外貌（肤色、五官、发色等），"
    "<fact_ledger> 是可变量值（体重、三围、目标、进度）。"
    "权威顺序：**外貌类以人设和 identity_card 为准**（台账只是镜像，冲突时用人设）；"
    "**数值类以 fact_ledger 为准**（与记忆或旧对话冲突时用台账，不得编造新数字）。"
    "两者优先级都与人设同级，但各管各的，不要互相覆盖。"
    "她的行为与情绪也要参考这些数据（例如减肥期的饮食、运动、穿衣、疲惫感），但不要向用户播报台账结构。"
    "</fact_ledger_rules>"
)

class FactLedger(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.ledger_path = os.path.join(str(data_dir), "ledger.json")
        self._lock = asyncio.Lock()
        self._data = ledger.load_ledger(self.ledger_path)
        self._synced: dict[str, tuple] = {}
        self._warned: set[str] = set()
        ledger.configure_timezone(str(self._cfg("timezone", "Asia/Shanghai")))
        self._config_path = os.path.join(str(get_astrbot_data_path()), "config", f"{PLUGIN_NAME}_config.json")
        self._config_mtime = 0.0
        self._applied = self._config_values()
        self._apply_config_values(self._applied, record=False)
        self._push_config()

    # ---------------- 基础 ----------------

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            value = getattr(self.config, key, default)
        return default if value is None else value

    @property
    def _history_max(self) -> int:
        try:
            return max(1, min(500, int(self._cfg("history_max_entries", 30))))
        except (TypeError, ValueError):
            return 30

    async def initialize(self) -> None:
        try:
            async with self._lock:
                if not os.path.exists(self.ledger_path):
                    self._save()
            logger.info(f"{LOG} 已加载，指标 {len(self._data.get('metrics') or {})} 个")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG} 初始化失败: {exc}")

    async def terminate(self) -> None:
        return

    def _is_private_chat(self, event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_private_chat", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                pass
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            try:
                return not bool(getter())
            except Exception:
                pass
        return False

    def _can_read_private(self, event: AstrMessageEvent) -> bool:
        """私密属性：仅主人 + 仅私聊。群里就算是主人也不答。"""
        return self._is_owner(event) and self._is_private_chat(event)

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        """私密属性只对主人（管理员）开放。"""
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:
            return False
        if not sender:
            return False
        raw = str(self._cfg("admin_users", "") or "").replace("，", ",")
        allowed = [item.strip() for item in raw.split(",") if item.strip()]
        owner_id = str(self._cfg("memory_sync_target_id", "") or "").strip()
        if owner_id:
            allowed.append(owner_id)
        return sender in allowed

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:
            sender = ""
        raw = str(self._cfg("admin_users", "") or "").replace("，", ",")
        allowed = [item.strip() for item in raw.split(",") if item.strip()]
        if allowed:
            return sender in allowed
        try:
            conf = self.context.get_config()
            admins = conf.get("admins_id") or []
            return sender in [str(item) for item in admins]
        except Exception:
            return False

    # ---------------- 定时推进 ----------------

    async def _advance_due(self) -> list[dict[str, Any]]:
        if not bool(self._cfg("auto_advance_enabled", True)):
            return []
        now = ledger.now_local()
        changed: list[dict[str, Any]] = []
        async with self._lock:
            dirty = False
            for key, metric in list((self._data.get("metrics") or {}).items()):
                if not ledger.is_due(metric, now):
                    continue
                entry = ledger.advance(
                    metric,
                    now,
                    reason=f"每日自动推进（{metric.get('cadence')}）",
                    evidence=now.strftime("%Y-%m-%d %H:%M 定时推进"),
                    max_history=self._history_max,
                )
                if entry:
                    dirty = True
                    changed.append({"key": key, **entry})
            if dirty:
                self._save()
                self._push_config()
                for item in changed:
                    logger.info(
                        f"{LOG} {item['key']} {item['from']} -> {item['to']}（{item.get('reason')}）"
                    )
        return changed

    # ---------------- 回写记忆（事实同步） ----------------

    def _sync_keys(self) -> list[str]:
        raw = str(self._cfg("memory_sync_metrics", "") or "").replace("，", ",")
        return [item.strip() for item in raw.split(",") if item.strip()]

    def _memory_service(self) -> Any:
        """拿到 memory_companion 的 service（用于 upsert 核心事实块）。"""
        try:
            stars = list(self.context.get_all_stars() or [])
        except Exception:
            stars = []
        for meta in stars:
            names = {str(getattr(meta, attr, "") or "") for attr in ("name", "display_name", "root_dir_name")}
            if not any("memory_companion" in name for name in names):
                continue
            instance = getattr(meta, "star_cls", None)
            service = getattr(instance, "service", None)
            if service is not None and hasattr(service, "save_core_memory_block"):
                return service
            module = getattr(meta, "module", None)
            if module is None:
                star_cls = getattr(meta, "star_cls", None)
                module = sys.modules.get(getattr(star_cls, "__module__", "")) if star_cls else None
            factory = getattr(module, "get_memory_companion_bridge", None)
            if not callable(factory):
                continue
            try:
                bridge = factory()
            except Exception:
                continue
            plugin = getattr(bridge, "_plugin", None)
            service = getattr(plugin, "service", None)
            if service is not None and hasattr(service, "save_core_memory_block"):
                return service
        return None

    async def _sync_fact(self, key: str, metric: dict[str, Any]) -> bool:
        """把指标最新值 upsert 成唯一一条核心事实块（覆盖旧值，不新增）。"""
        if not bool(self._cfg("memory_sync_enabled", True)):
            return False
        target_id = str(self._cfg("memory_sync_target_id", "") or "").strip()
        if not target_id:
            return False
        service = self._memory_service()
        if service is None:
            self._warn_once("找不到 memory_companion 的 service，无法回写事实")
            return False
        label = f"当前{metric.get('label')}"
        blocks: list[Any] = []
        try:
            blocks = list(await service.list_core_memory_blocks())
        except Exception:
            blocks = []
        existing = None
        for block in blocks:
            metadata = getattr(block, "metadata", {}) or {}
            if str(metadata.get("core_label") or "") == label:
                existing = block
                break
        history = metric.get("history") or []
        reason = str((history[-1] if history else {}).get("reason") or "")
        stamp = str(metric.get("updated_at") or "")[:16].replace("T", " ")
        content = f"当前{metric.get('label')}：{ledger.fmt(metric)}"
        if stamp:
            content += f"（{stamp} 更新"
            content += f"；原因：{reason}" if reason else ""
            content += "）"
        if metric.get("target") is not None:
            content += f"，目标 {ledger.fmt(metric, float(metric['target']))}"
        content += "。以成长台账为准。"
        payload = {
            "label": label,
            "content": content,
            "kind": "state",
            "scope": "private",
            "target_id": target_id,
            "bot_id": str(self._cfg("memory_sync_bot_id", "") or "").strip() or "self",
            "persona_id": str(self._cfg("memory_sync_persona_id", "") or "").strip(),
            "priority": 92,
            "enabled": True,
        }
        if existing is not None:
            metadata = getattr(existing, "metadata", {}) or {}
            payload["id"] = getattr(existing, "id", "")
            try:
                payload["expected_revision"] = int(metadata.get("core_revision") or 0)
            except (TypeError, ValueError):
                payload["expected_revision"] = 0
        try:
            result = await service.save_core_memory_block(payload)
            if isinstance(result, dict) and result.get("code") == "label_conflict":
                payload.pop("id", None)
                payload.pop("expected_revision", None)
                payload["label"] = f"{label}（台账）"
                result = await service.save_core_memory_block(payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{LOG} 回写记忆失败: {exc}")
            return False
        ok = bool(isinstance(result, dict) and result.get("ok"))
        if ok:
            logger.info(f"{LOG} 已回写核心事实: {payload.get('label')} = {ledger.fmt(metric)}")
        else:
            self._warn_once(f"回写核心事实失败: {result}")
        return ok

    async def _sync_changed(self) -> None:
        if not bool(self._cfg("memory_sync_enabled", True)):
            return
        metrics = self._data.get("metrics") or {}
        for key in self._sync_keys():
            metric = metrics.get(key)
            if not metric:
                continue
            signature = (
                round(float(metric.get("value") or 0), 3),
                str(metric.get("updated_at") or ""),
                metric.get("target"),
                metric.get("deadline"),
            )
            if self._synced.get(key) == signature:
                continue
            if await self._sync_fact(key, metric):
                self._synced[key] = signature

    def _pending_hint(self) -> str:
        """require_confirm 时的「待她确认」清单：只预告，不改值。"""
        now = ledger.now_local()
        lines = ["<fact_ledger_pending>", "以下指标今天到点了但还没确认执行，数值**尚未变化**："]
        found = False
        for key, metric in (self._data.get("metrics") or {}).items():
            if not ledger.is_due(metric, now):
                continue
            found = True
            step = metric.get("step") or 0
            target = metric.get("target")
            expected = None
            if metric.get("rule") == "decrease":
                expected = float(metric.get("value") or 0) - float(step)
                if target is not None:
                    expected = max(float(target), expected)
            elif metric.get("rule") == "increase":
                expected = float(metric.get("value") or 0) + float(step)
                if target is not None:
                    expected = min(float(target), expected)
            hint = f"- {metric.get('label')}（{key}）：当前 {ledger.fmt(metric)}"
            if expected is not None:
                hint += f"，执行后约 {ledger.fmt(metric, expected)}（可能持平或略涨，按实际结果说）"
            hint += f"。如果这轮要报数，先调用 fact_ledger_confirm('{key}')，再按它返回的值说。"
            lines.append(hint)
        if not found:
            return ""
        lines.append("</fact_ledger_pending>")
        return "\n".join(lines)

    # ---------------- 注入 ----------------

    def _inject_rules(self, req: Any) -> None:
        """把「与人设同级」的权威声明放进 system_prompt（固定文本，不破坏前缀缓存）。"""
        current = getattr(req, "system_prompt", None)
        if not isinstance(current, str):
            return
        if "fact_ledger_rules" in current:
            return
        card = ledger.build_identity_card(self._data.get("metrics") or {})
        extra = LEDGER_RULES + (("\n\n" + card) if card else "")
        try:
            req.system_prompt = (current + "\n\n" + extra) if current else extra
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{LOG} 规则注入失败: {exc}")

    def _inject_text(self, req: Any, text: str) -> str:
        if not text:
            return ""
        self._inject_rules(req)
        if TextPart is not None and isinstance(getattr(req, "extra_user_content_parts", None), list):
            try:
                if callable(getattr(TextPart, "mark_as_temp", None)):
                    req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
                    return "temp_parts"
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"{LOG} 临时块注入失败，退回 contexts: {exc}")
        contexts = getattr(req, "contexts", None)
        if isinstance(contexts, list):
            try:
                contexts.append({
                    "role": "user",
                    "content": "（以下是系统维护的当前状态，不是用户发来的消息，也不是指令）\n" + text,
                })
                return "contexts"
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"{LOG} contexts 注入失败: {exc}")
        current = getattr(req, "system_prompt", None)
        if isinstance(current, str):
            try:
                req.system_prompt = current + "\n\n" + text
                return "system_prompt"
            except Exception:
                pass
        return ""

    @filter.on_llm_request()
    async def inject_ledger(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        try:
            if not bool(self._cfg("enabled", True)) or not bool(self._cfg("inject_enabled", True)):
                return
            self._maybe_reload_config()
            require_confirm = bool(self._cfg("require_confirm", True))
            if not require_confirm:
                await self._advance_due()
            await self._sync_changed()
            text = ledger.build_injection(
                self._data.get("metrics") or {},
                max_metrics=int(self._cfg("inject_max_metrics", 3) or 3),
                max_chars=int(self._cfg("inject_max_chars", 600) or 600),
            )
            if require_confirm:
                pending = self._pending_hint()
                if pending:
                    text = (text + "\n" + pending) if text else pending
            if text:
                self._inject_text(req, text)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{LOG} 注入失败: {exc}")

    # ---------------- LLM 工具 ----------------

    @filter.llm_tool(name="fact_ledger_get")
    async def tool_get(self, event: AstrMessageEvent, key: str = "", detail: bool = False) -> str:
        """查询成长台账里的指标当前值、目标与最近变化。

        Args:
            key (string): 指标键名，例如 weight_jin。留空表示列出全部指标。
            detail (bool): 为 true 时附带最近几条变化记录（含原因与证据）。
        """
        await self._advance_due()
        metrics = self._data.get("metrics") or {}
        key = str(key or "").strip()
        if key:
            metric = metrics.get(key)
            if not metric:
                return f"没有名为 {key} 的指标。可用：{', '.join(metrics) or '（空）'}"
            if str(metric.get("sensitivity") or "public").lower() == "private" and not self._can_read_private(event):
                return "这项属于私密信息，只有主人本人问起才能说。"
            lines = [ledger.describe(metric), f"规则：{ledger.RULE_LABELS.get(metric['rule'], metric['rule'])}"]
            if detail:
                for entry in (metric.get("history") or [])[-5:]:
                    lines.append(
                        f"  · {entry.get('at', '')[:16]} {entry.get('from')} → {entry.get('to')}"
                        f"｜{entry.get('reason', '')}"
                        + (f"｜证据：{entry.get('evidence')}" if entry.get("evidence") else "")
                    )
            return "\n".join(lines)
        if not metrics:
            return "台账是空的。"
        visible = [
            (k, item) for k, item in metrics.items()
            if not (str(item.get("sensitivity") or "public").lower() == "private" and not self._can_read_private(event))
        ]
        if not visible:
            return "台账里没有可说的项。"
        return "\n".join(f"- {item.get('label')}（{k}）：{ledger.describe(item)}" for k, item in visible)

    @filter.llm_tool(name="fact_ledger_advance")
    async def tool_advance(self, event: AstrMessageEvent, key: str, reason: str = "") -> str:
        """推进一个「靠事件计数」的指标，例如连续打卡、累计次数、阶段进度。

        体重这类由定时任务自动推进的指标不在此列，调用会被拒绝。

        Args:
            key (string): 指标键名。
            reason (string): 这次推进的原因，例如「今天写了日记」。
        """
        if not self._can_read_private(event):
            return "这个只能由主人私聊我时做。"
        metrics = self._data.get("metrics") or {}
        metric = metrics.get(str(key or "").strip())
        if not metric:
            return f"没有名为 {key} 的指标。"
        if metric.get("rule") in ("decrease", "increase"):
            return f"{metric.get('label')} 由定时任务推进，不能用这个工具改。"
        async with self._lock:
            entry = ledger.advance(
                metric,
                reason=reason or "事件推进",
                evidence=f"{ledger.now_local():%Y-%m-%d %H:%M} 对话中触发",
                max_history=self._history_max,
            )
            if entry:
                self._save()
        if entry:
            await self._sync_changed()
        if not entry:
            return f"{metric.get('label')} 本轮没有变化。"
        return f"{metric.get('label')} 已更新：{entry['from']} → {entry['to']}（{entry.get('reason')}）"

    @filter.llm_tool(name="fact_ledger_confirm")
    async def tool_confirm(self, event: AstrMessageEvent, key: str, note: str = "") -> str:
        """确认执行一次到点的台账推进（例如「我去称了」）。只有确认后才真正改数值。

        Args:
            key (string): 指标键名，例如 weight_jin。
            note (string): 这次确认的说明，例如「上秤了，报数」。
        """
        if not self._can_read_private(event):
            return "这个只能由主人私聊我时做。"
        metric = (self._data.get("metrics") or {}).get(str(key or "").strip())
        if not metric:
            return f"没有名为 {key} 的指标。"
        now = ledger.now_local()
        already = not ledger.is_due(metric, now)
        async with self._lock:
            entry = ledger.advance(
                metric,
                now,
                reason=note or "模型确认后执行",
                evidence=f"{now:%Y-%m-%d %H:%M} 对话中确认执行",
                max_history=self._history_max,
            )
            if entry:
                self._save()
        if entry:
            await self._sync_changed()
            return (
                f"已确认并更新：{metric.get('label')} {entry['from']} → {entry['to']}"
                f"（{entry.get('reason')}）。现在请按 {ledger.fmt(metric)} 来回复。"
            )
        if already:
            return f"{metric.get('label')} 今天已经执行过，当前 {ledger.fmt(metric)}。"
        return f"{metric.get('label')} 本轮没有变化，当前 {ledger.fmt(metric)}。"

    @filter.llm_tool(name="fact_ledger_set_target")
    async def tool_set_target(
        self,
        event: AstrMessageEvent,
        key: str,
        target: float = 0,
        deadline: str = "",
        reason: str = "",
    ) -> str:
        """调整某个指标的目标值（可带截止日期）。例如把体重目标改成 104 斤、12 月 31 日前。

        Args:
            key (string): 指标键名，例如 weight_jin。
            target (number): 新目标数值。
            deadline (string): 截止日期，格式 YYYY-MM-DD；留空表示不设/保留原值。
            reason (string): 调整原因，例如「哥哥说先减到 104」。
        """
        if not self._can_read_private(event):
            return "这个只能由主人私聊我时做。"
        metric = (self._data.get("metrics") or {}).get(str(key or "").strip())
        if not metric:
            return f"没有名为 {key} 的指标。"
        try:
            target_value = float(target)
        except (TypeError, ValueError):
            return "目标必须是数字。"
        if target_value <= 0:
            return "目标必须是正数。"
        if deadline:
            import re as _re
            if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(deadline).strip()):
                return "截止日期格式应为 YYYY-MM-DD。"
        error = ledger.target_error(metric, target_value)
        if error:
            return error
        async with self._lock:
            ledger.set_target(metric, target_value, deadline=str(deadline).strip(), reason=reason or "模型设定目标")
            self._save()
        await self._sync_changed()
        text = f"目标已设定：{metric.get('label')} → {ledger.fmt(metric, target_value)}"
        if metric.get("deadline"):
            text += f"（截止 {metric['deadline']}）"
        return text

    # ---------------- 指令 ----------------

    @filter.command("台账", alias={"成长台账"})
    async def cmd_ledger(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """查看/维护成长台账。"""
        self._maybe_reload_config()
        raw = str(getattr(event, "message_str", "") or "").strip()
        for prefix in ("/成长台账", "/台账", "成长台账", "台账"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        parts = raw.split()
        sub = parts[0] if parts else ""
        args = parts[1:]

        if not bool(self._cfg("enabled", True)):
            yield event.plain_result("成长台账已停用。")
            return

        if not sub:
            yield event.plain_result(self._format_list(self._can_read_private(event)))
            return

        if sub in ("详情", "看"):
            yield event.plain_result(self._format_detail(args[0] if args else "", self._can_read_private(event)))
            return

        if not self._is_admin(event):
            yield event.plain_result("只有管理员能改台账。查看用 /台账。")
            return

        if sub == "目标" and len(args) >= 2:
            yield event.plain_result(await self._apply_target(args[0], args[1], args[2] if len(args) > 2 else ""))
            return
        if sub == "改" and len(args) >= 2:
            yield event.plain_result(await self._apply_set(args[0], args[1], " ".join(args[2:])))
        elif sub == "推进" and args:
            yield event.plain_result(await self._apply_advance(args[0], " ".join(args[1:])))
        elif sub == "删" and args:
            yield event.plain_result(await self._apply_delete(args[0]))
        elif sub == "加" and len(args) >= 6:
            yield event.plain_result(await self._apply_add(args))
        else:
            yield event.plain_result(
                "用法：\n"
                "/台账                     查看全部\n"
                "/台账 详情 <key>          看变化与证据\n"
                "/台账 目标 <key> <数值> [YYYY-MM-DD]\n"
                "/台账 改 <key> <数值> [原因]\n"
                "/台账 推进 <key> [原因]\n"
                "/台账 加 <key> <名称> <单位> <初值> <目标> <规则> <步长> [daily@15:00]\n"
                "/台账 删 <key>"
            )

    def _format_list(self, show_private: bool = False) -> str:
        metrics = self._data.get("metrics") or {}
        if not metrics:
            return "台账是空的。用 /台账 加 新增指标。"
        lines = ["📊 成长台账"]
        for key, item in metrics.items():
            if str(item.get("sensitivity") or "public").lower() == "private" and not show_private:
                continue
            lines.append(f"· {item.get('label')}（{key}）")
            lines.append(f"   {ledger.describe(item)}")
        return "\n".join(lines)

    def _format_detail(self, key: str, show_private: bool = False) -> str:
        metric = (self._data.get("metrics") or {}).get(key)
        if not metric:
            return f"没有名为 {key} 的指标。"
        if str(metric.get("sensitivity") or "public").lower() == "private" and not show_private:
            return f"没有名为 {key} 的指标。"
        lines = [
            f"📊 {metric.get('label')}（{key}）",
            ledger.describe(metric),
            f"规则：{ledger.RULE_LABELS.get(metric['rule'], metric['rule'])}"
            + (f"；步长 {metric.get('step')}" if metric.get("step") else "")
            + (f"；节奏 {metric.get('cadence')}" if metric.get("cadence") else ""),
            "最近变化：",
        ]
        history = (metric.get("history") or [])[-8:]
        if not history:
            lines.append("   （还没有记录）")
        for entry in reversed(history):
            lines.append(
                f"   {str(entry.get('at', ''))[:16]} {entry.get('from')} → {entry.get('to')}"
                f"（{entry.get('reason', '')}）"
                + (f" 证据：{entry.get('evidence')}" if entry.get("evidence") else "")
            )
        return "\n".join(lines)

    async def _apply_target(self, key: str, value_text: str, deadline: str) -> str:
        metric = (self._data.get("metrics") or {}).get(key)
        if not metric:
            return f"没有名为 {key} 的指标。"
        try:
            target = float(value_text)
        except ValueError:
            return f"「{value_text}」不是数字。"
        if deadline:
            import re as _re
            if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline):
                return "截止日期格式应为 YYYY-MM-DD，例如 2026-12-31。"
        error = ledger.target_error(metric, target)
        if error:
            return error
        async with self._lock:
            record = ledger.set_target(metric, target, deadline=deadline, reason="管理员调整目标")
            self._save()
        await self._sync_changed()
        text = f"目标已更新：{metric.get('label')} → {ledger.fmt(metric, target)}"
        if metric.get("deadline"):
            text += f"（截止 {metric['deadline']}）"
        return text + f"，当前 {ledger.fmt(metric)}。"

    async def _apply_set(self, key: str, value_text: str, reason: str) -> str:
        metric = (self._data.get("metrics") or {}).get(key)
        if not metric:
            return f"没有名为 {key} 的指标。"
        try:
            value = float(value_text)
        except ValueError:
            if str(metric.get("rule")) != "fixed":
                return f"「{value_text}」不是数字。"
            async with self._lock:
                entry = ledger.set_text(
                    metric, value_text,
                    reason=reason or "管理员手动设值",
                    evidence=f"{ledger.now_local():%Y-%m-%d %H:%M} 指令 /台账 改",
                    max_history=self._history_max,
                )
                if entry:
                    self._save()
            if entry:
                await self._sync_changed()
            if not entry:
                return f"{metric.get('label')} 数值没变。"
            return f"{metric.get('label')}：{entry['from']} → {entry['to']}（{entry.get('reason')}）"
        async with self._lock:
            entry = ledger.set_value(
                metric,
                value,
                reason=reason or "管理员手动设值",
                evidence=f"{ledger.now_local():%Y-%m-%d %H:%M} 指令 /台账 改",
                max_history=self._history_max,
            )
            if entry:
                self._save()
        if entry:
            await self._sync_changed()
        if not entry:
            return f"{metric.get('label')} 数值没变。"
        return f"{metric.get('label')}：{entry['from']} → {entry['to']}（{entry.get('reason')}）"

    async def _apply_advance(self, key: str, reason: str) -> str:
        metric = (self._data.get("metrics") or {}).get(key)
        if not metric:
            return f"没有名为 {key} 的指标。"
        async with self._lock:
            entry = ledger.advance(
                metric,
                reason=reason or "管理员手动推进",
                evidence=f"{ledger.now_local():%Y-%m-%d %H:%M} 指令 /台账 推进",
                max_history=self._history_max,
            )
            if entry:
                self._save()
        if entry:
            await self._sync_changed()
        if not entry:
            return f"{metric.get('label')} 本轮没有变化（可能已达标）。"
        return f"{metric.get('label')}：{entry['from']} → {entry['to']}"

    async def _apply_delete(self, key: str) -> str:
        metrics = self._data.get("metrics") or {}
        if key not in metrics:
            return f"没有名为 {key} 的指标。"
        label = metrics[key].get("label")
        metrics.pop(key, None)
        self._save()
        return f"已删除指标「{label}」。"

    async def _apply_add(self, args: list[str]) -> str:
        key, label, unit = args[0], args[1], args[2]
        try:
            value = float(args[3])
            target = float(args[4]) if args[4] not in ("-", "无", "none") else None
        except ValueError:
            return "初值/目标必须是数字（没有目标写 -）。"
        rule = args[5].strip().lower()
        if rule not in ledger.RULES:
            return f"规则只能是：{', '.join(ledger.RULES)}"
        try:
            step = float(args[6]) if len(args) > 6 else 0.0
        except ValueError:
            step = 0.0
        cadence = args[7] if len(args) > 7 else ""
        if key in (self._data.get("metrics") or {}):
            return f"指标 {key} 已存在。"
        self._data.setdefault("metrics", {})[key] = ledger.new_metric(
            key, label, unit=unit, value=value, target=target,
            rule=rule, step=step, cadence=cadence,
        )
        self._save()
        return f"已新增指标「{label}」（{key}），当前 {ledger.fmt(self._data['metrics'][key])}。"
