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
import time
import uuid
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
    "技能是**她自己的技能**：只有她本人完成的练习才算。"
    "她回复里描述自己做了（「我今天穿高跟鞋走了半小时」）→ 记一次；"
    "主人让她练、她确认做了（「你去练一下」→「我走完了」）→ 记一次；"
    "主人说他自己做了（「我今天穿高跟鞋走了半小时」）→ 那是主人的经历，**不算她的练习，不要记**。"
    "只提到、计划、假设、回忆都不算；她说「我练了」但没细说也算一次。同一轮最多记一次。"
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
        self._practice_turns: dict[str, str] = {}
        ledger.configure_timezone(str(self._cfg("timezone", "Asia/Shanghai")))
        self._config_path = os.path.join(str(get_astrbot_data_path()), "config", f"{PLUGIN_NAME}_config.json")
        self._config_mtime = 0.0
        self._applied = self._config_values()
        self._apply_config_values(self._applied, record=False)
        self._push_config()

    # ---------------- 技能提案 / 衰减 ----------------

    @property
    def _proposals_path(self) -> str:
        return os.path.join(os.path.dirname(self.ledger_path), "proposals.json")

    def _load_proposals(self) -> dict[str, Any]:
        try:
            with open(self._proposals_path, "r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        items = raw.get("items")
        return {"version": 1, "items": items if isinstance(items, list) else []}

    def _save_proposals(self, data: dict[str, Any]) -> None:
        tmp = f"{self._proposals_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self._proposals_path)

    def _pending_proposals(self) -> list[dict[str, Any]]:
        data = self._load_proposals()
        now = ledger.now_local()
        alive: list[dict[str, Any]] = []
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            if item.get("status") != "pending":
                continue
            try:
                expired = ledger.datetime.fromisoformat(str(item.get("expires_at") or "")) < now
            except Exception:
                expired = False
            if expired:
                item["status"] = "expired"
                continue
            alive.append(item)
        return alive

    async def _apply_decay(self) -> list[dict[str, Any]]:
        now = ledger.now_local()
        changed: list[dict[str, Any]] = []
        async with self._lock:
            for key, metric in (self._data.get("metrics") or {}).items():
                entry = ledger.decay_if_needed(metric, now, max_history=self._history_max)
                if entry:
                    changed.append({"key": key, **entry})
            if changed:
                self._save()
        for item in changed:
            logger.info(f"{LOG} {item['key']} 衰减 {item['from']} -> {item['to']}（{item.get('reason')}）")
        return changed

    # ---------------- 配置页双向同步 ----------------

    def _warn_once(self, message: str) -> None:
        if message in self._warned:
            return
        self._warned.add(message)
        logger.warning(f"{LOG} {message}")

    def _memory_bridge(self) -> Any:
        try:
            stars = list(self.context.get_all_stars() or [])
        except Exception:
            stars = []
        for meta in stars:
            names = {str(getattr(meta, attr, "") or "") for attr in ("name", "display_name", "root_dir_name")}
            if not any("memory_companion" in name for name in names):
                continue
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
            if bridge is not None:
                return bridge
        return None

    async def _write_skill_memory(self, key: str, metric: dict[str, Any], phase: str) -> None:
        """技能里程碑写入长期记忆：approved / proficient / mastered。"""
        if not bool(self._cfg("skill_memory_enabled", True)):
            return
        uid = str(self._cfg("memory_sync_target_id", "") or "").strip()
        if not uid:
            return
        bridge = self._memory_bridge()
        if bridge is None:
            self._warn_once("找不到 memory_companion 桥接，技能里程碑未写入记忆")
            return
        label = metric.get("label") or key
        level, _lo, _hi = ledger.skill_level(metric)
        if phase == "approved":
            content = f"我决定练「{label}」，目标是完全掌握。当前熟练度 {ledger.fmt(metric)}/100（{level}）。"
            importance = 0.7
        elif phase == "proficient":
            content = f"「{label}」的熟练度到 {ledger.fmt(metric)} 了，已经算熟练，穿高跟鞋走路不慌了。"
            importance = 0.8
        elif phase == "mastered":
            content = f"我已经完全掌握「{label}」了，熟练度 100，这件事变成了我自己的技能。"
            importance = 0.9
        else:
            return
        try:
            await bridge.record_external_memory(
                user_id=uid,
                content=content,
                source_plugin=PLUGIN_NAME,
                idempotency_key=f"fact_ledger:skill:{key}:{phase}",
                importance=importance,
                tags=["skill", "growth", key],
            )
            logger.info(f"{LOG} 技能记忆已写入：{key}/{phase}")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{LOG} 技能记忆写入失败: {exc}")

    def _turn_id(self, event: AstrMessageEvent) -> str:
        obj = getattr(event, "message_obj", None)
        return str(getattr(obj, "message_id", "") or "")

    async def _record_practice(
        self, event: AstrMessageEvent, key: str, sessions: int, note: str
    ) -> tuple[str, dict[str, Any] | None]:
        """统一入口：每轮去重 + 每日上限 + 里程碑记忆。"""
        metric = (self._data.get("metrics") or {}).get(key)
        if not metric:
            return f"没有名为 {key} 的技能。", None
        if str(metric.get("rule")) != "skill":
            return f"{metric.get('label')} 不是技能指标。", None
        turn = self._turn_id(event)
        if turn and self._practice_turns.get(key) == turn:
            return f"这一轮已经给「{metric.get('label')}」记过一次了。", None
        old_value = float(metric.get("value") or 0.0)
        async with self._lock:
            entry = ledger.practice(
                metric, sessions=sessions,
                reason=note or f"练习 {sessions} 次（没细说）",
                evidence=f"{ledger.now_local():%Y-%m-%d %H:%M} 对话记录",
                max_history=self._history_max,
            )
            if entry:
                self._save()
        if not entry:
            level, _lo, _hi = ledger.skill_level(metric)
            return (
                f"{metric.get('label')} 本轮没有增加（今天练够了，或已经掌握）。当前 {ledger.fmt(metric)}，{level}。",
                None,
            )
        if turn:
            self._practice_turns[key] = turn
        await self._sync_changed()
        new_value = float(metric.get("value") or 0.0)
        if old_value < 46.0 <= new_value:
            await self._write_skill_memory(key, metric, "proficient")
        if old_value < 100.0 <= new_value:
            await self._write_skill_memory(key, metric, "mastered")
        level, _lo, _hi = ledger.skill_level(metric)
        return (
            f"{metric.get('label')} 熟练度 {entry['from']} → {entry['to']}"
            f"（+{entry['delta']}），当前等级 {level}，已练 {metric.get('practice_count')} 次。",
            entry,
        )

    def _save(self) -> None:
        """落盘台账并把最新值回写到插件配置页。"""
        ledger.save_ledger(self.ledger_path, self._data)
        self._push_config()

    def _config_values(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for key, _metric, _field, _kind in CONFIG_FIELDS:
            try:
                values[key] = self.config.get(key)
            except Exception:
                values[key] = getattr(self.config, key, None)
        return values

    def _read_config_file(self) -> dict[str, Any]:
        try:
            with open(self._config_path, "r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {key: raw.get(key) for key, _m, _f, _k in CONFIG_FIELDS if key in raw}

    def _config_file_mtime(self) -> float:
        try:
            return os.path.getmtime(self._config_path)
        except OSError:
            return 0.0

    def _apply_config_values(self, values: dict[str, Any], *, record: bool) -> None:
        metrics = self._data.get("metrics") or {}
        dirty = False
        for cfg_key, metric_key, field, kind in CONFIG_FIELDS:
            if cfg_key not in values:
                continue
            metric = metrics.get(metric_key)
            if not metric:
                continue
            raw = values.get(cfg_key)
            if raw is None or raw == "":
                continue
            try:
                new_value: Any = float(raw) if kind == "float" else str(raw).strip()
            except (TypeError, ValueError):
                continue
            old_value = metric.get(field)
            if field == "target":
                if old_value is not None and float(old_value) == float(new_value):
                    continue
                if ledger.target_error(metric, float(new_value)):
                    continue
                ledger.set_target(metric, float(new_value), reason="配置页修改")
                dirty = True
                continue
            if field == "value" and record:
                entry = (
                    ledger.set_value(metric, float(new_value), reason="配置页修改", evidence="插件配置页")
                    if kind == "float"
                    else ledger.set_text(metric, str(new_value), reason="配置页修改", evidence="插件配置页")
                )
                if entry:
                    dirty = True
                continue
            if old_value != new_value:
                if field == "value":
                    metric["prev"] = old_value
                    metric["updated_at"] = ledger.now_local().isoformat(timespec="seconds")
                metric[field] = new_value
                dirty = True
        if dirty:
            ledger.save_ledger(self.ledger_path, self._data)

    def _push_config(self) -> None:
        """把台账最新值写回配置页（直接改配置文件，避免内存副本过期覆盖用户编辑）。"""
        metrics = self._data.get("metrics") or {}
        current: dict[str, Any] = {}
        try:
            if os.path.exists(self._config_path):
                with open(self._config_path, "r", encoding="utf-8-sig") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    current = loaded
        except Exception:
            current = {}
        changed = False
        for cfg_key, metric_key, field, _kind in CONFIG_FIELDS:
            metric = metrics.get(metric_key)
            if not metric:
                continue
            value = metric.get(field)
            if value is None:
                continue
            if current.get(cfg_key) != value:
                current[cfg_key] = value
                changed = True
        if not changed:
            self._applied = {key: current.get(key) for key, *_ in CONFIG_FIELDS}
            return
        tmp = f"{self._config_path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(current, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self._config_path)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{LOG} 配置回写失败: {exc}")
            return
        try:
            for key in current:
                self.config[key] = current[key]
        except Exception:
            pass
        self._config_mtime = self._config_file_mtime()
        self._applied = {key: current.get(key) for key, *_ in CONFIG_FIELDS}

    def _maybe_reload_config(self) -> None:
        mtime = self._config_file_mtime()
        if not mtime or mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        values = self._read_config_file()
        if not values:
            return
        diff = {key: value for key, value in values.items() if self._applied.get(key) != value}
        if not diff:
            self._applied.update(values)
            return
        self._apply_config_values(diff, record=True)
        self._applied = self._config_values()

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
        today = now.date().isoformat()
        for key, metric in (self._data.get("metrics") or {}).items():
            if str(metric.get("rule") or "") != "skill":
                continue
            slot = ledger.parse_cadence(metric.get("cadence", ""))
            if slot is None:
                continue
            if str(metric.get("last_practice_on") or "") == today:
                continue
            if (now.hour, now.minute) < slot:
                continue
            found = True
            level, _lo, _hi = ledger.skill_level(metric)
            lines.append(
                f"- 技能 {metric.get('label')}（{key}）：今天还没练，当前 {ledger.fmt(metric)}/100（{level}）。"
                f"可以提醒哥哥练一次；他说练了就调用 fact_ledger_practice('{key}')。"
            )
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
            await self._apply_decay()
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

    @filter.llm_tool(name="fact_ledger_practice")
    async def tool_practice(self, event: AstrMessageEvent, key: str, sessions: int = 1, note: str = "") -> str:
        """记录一次技能练习，增加熟练度（按次数，受每日上限约束）。

        只要这一轮对话里她真的练了就要调用：她自己描述做完了，或主人确认她做了。
        只提到、计划、假设、回忆都不算；她说「我练了」但没细说也算一次。

        Args:
            key (string): 技能指标键名，例如 skill_highheels。
            sessions (number): 练习次数，默认 1。
            note (string): 练习说明，例如「穿高跟鞋走了半小时」。
        """
        if not self._can_read_private(event):
            return "这个只能由主人私聊我时做。"
        text, _entry = await self._record_practice(event, str(key or "").strip(), sessions, note)
        return text

    @filter.llm_tool(name="fact_task_propose")
    async def tool_propose(self, event: AstrMessageEvent, key: str, label: str, cadence: str = "", reason: str = "") -> str:
        """向主人提一个新的技能任务（需要主人同意后才生效）。

        Args:
            key (string): 英文键名，例如 skill_highheels。
            label (string): 技能名称，例如 穿高跟鞋走路。
            cadence (string): 提醒节奏，例如 weekly@3,20:00；留空表示不提醒。
            reason (string): 为什么想加这个任务。
        """
        if not self._can_read_private(event):
            return "这个只能由主人私聊我时做。"
        import re as _re
        key = str(key or "").strip()
        label = str(label or "").strip()
        if not _re.fullmatch(r"[a-z0-9_]{2,40}", key):
            return "key 只能用 2~40 位小写字母、数字、下划线。"
        if not label:
            return "技能名称不能为空。"
        if key in (self._data.get("metrics") or {}):
            return f"台账里已经有 {key} 了。"
        pending = self._pending_proposals()
        if len(pending) >= 3:
            return "待批准的提案已经有 3 个了，先处理一下再加。"
        today = ledger.now_local().date().isoformat()
        created_today = sum(1 for item in self._load_proposals().get("items") or []
                            if str(item.get("created_at") or "")[:10] == today)
        if created_today >= 2:
            return "今天已经提了 2 个新任务了，明天再说吧。"
        proposal = {
            "id": "p_" + uuid.uuid4().hex[:8],
            "type": "skill",
            "status": "pending",
            "created_at": ledger.now_local().isoformat(timespec="seconds"),
            "expires_at": (ledger.now_local() + ledger.timedelta(days=7)).isoformat(timespec="seconds"),
            "payload": {"key": key, "label": label, "cadence": str(cadence or "").strip(), "reason": str(reason or "")},
        }
        data = self._load_proposals()
        data["items"].append(proposal)
        self._save_proposals(data)
        return (
            f"已生成提案 {proposal['id']}：把「{label}」加进台账（目标 100，按次数累计）。"
            f"哥哥同意的话回我「同意」，或者说 /台账 批准 {proposal['id']}。"
        )

    @filter.llm_tool(name="fact_task_approve")
    async def tool_approve(self, event: AstrMessageEvent, proposal_id: str = "") -> str:
        """主人同意后，把待批准的提案转成正式技能指标。

        Args:
            proposal_id (string): 提案 ID；留空表示批准最近的那一条。
        """
        if not self._can_read_private(event):
            return "这个只能由主人私聊我时做。"
        return await self._approve_proposal(proposal_id)

    @filter.llm_tool(name="fact_task_list")
    async def tool_task_list(self, event: AstrMessageEvent) -> str:
        """列出待批准的技能提案。"""
        if not self._can_read_private(event):
            return "这个只能由主人私聊我时做。"
        pending = self._pending_proposals()
        if not pending:
            return "现在没有待批准的提案。"
        return "\n".join(
            f"- {item['id']}：{item['payload'].get('label')}（{item['payload'].get('key')}）"
            + (f"，提醒 {item['payload'].get('cadence')}" if item['payload'].get("cadence") else "")
            for item in pending
        )

    async def _approve_proposal(self, proposal_id: str = "") -> str:
        pending = self._pending_proposals()
        if not pending:
            return "没有待批准的提案。"
        target = None
        if proposal_id:
            target = next((item for item in pending if item.get("id") == proposal_id), None)
            if target is None:
                return f"找不到待批准的提案 {proposal_id}。"
        else:
            target = sorted(pending, key=lambda item: str(item.get("created_at") or ""))[-1]
        payload = target.get("payload") or {}
        key = str(payload.get("key") or "").strip()
        label = str(payload.get("label") or "").strip()
        if not key or not label:
            return "这条提案内容不完整，已忽略。"
        if key in (self._data.get("metrics") or {}):
            return f"台账里已经有 {key} 了。"
        metric = ledger.new_metric(
            key, label, unit="", value=0, target=100, rule="skill",
            cadence=str(payload.get("cadence") or ""), decimals=0, priority=70,
        )
        metric["last_practice_on"] = ""
        async with self._lock:
            self._data.setdefault("metrics", {})[key] = metric
            self._save()
        data = self._load_proposals()
        for item in data.get("items") or []:
            if item.get("id") == target.get("id"):
                item["status"] = "approved"
        self._save_proposals(data)
        await self._write_skill_memory(key, metric, "approved")
        return f"已批准：把「{label}」加进台账，当前熟练度 0/100（生疏）。以后你练了我就自动记上。"

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

        if sub == "练习" and args:
            sessions = 1
            if len(args) > 1:
                try:
                    sessions = max(1, int(args[1]))
                except ValueError:
                    sessions = 1
            yield event.plain_result(await self._apply_practice(args[0], sessions, " ".join(args[2:])))
            return
        if sub == "提案":
            yield event.plain_result(self._format_proposals())
            return
        if sub == "批准":
            yield event.plain_result(await self._approve_proposal(args[0] if args else ""))
            return
        if sub == "驳回" and args:
            yield event.plain_result(self._reject_proposal(args[0]))
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
                "/台账 练习 <key> [次数] [说明]\n"
                "/台账 提案 / 批准 [id] / 驳回 <id>\n"
                "/台账 改 <key> <数值> [原因]\n"
                "/台账 推进 <key> [原因]\n"
                "/台账 加 <key> <名称> <单位> <初值> <目标> <规则> <步长> [daily@15:00]\n"
                "/台账 删 <key>"
            )

    async def _apply_practice(self, key: str, sessions: int, note: str) -> str:
        metric = (self._data.get("metrics") or {}).get(key)
        if not metric:
            return f"没有名为 {key} 的技能。"
        if str(metric.get("rule")) != "skill":
            return f"{metric.get('label')} 不是技能指标。"
        old_value = float(metric.get("value") or 0.0)
        async with self._lock:
            entry = ledger.practice(
                metric, sessions=sessions,
                reason=note or f"练习 {sessions} 次",
                evidence=f"{ledger.now_local():%Y-%m-%d %H:%M} 指令 /台账 练习",
                max_history=self._history_max,
            )
            if entry:
                self._save()
        if not entry:
            return f"{metric.get('label')} 本轮没有增加（今天练够了，或已掌握）。当前 {ledger.fmt(metric)}。"
        if old_value < 46.0 <= float(metric.get("value") or 0.0):
            await self._write_skill_memory(key, metric, "proficient")
        if old_value < 100.0 <= float(metric.get("value") or 0.0):
            await self._write_skill_memory(key, metric, "mastered")
        level, _lo, _hi = ledger.skill_level(metric)
        return f"{metric.get('label')}：{entry['from']} → {entry['to']}（+{entry['delta']}），等级 {level}。"

    def _format_proposals(self) -> str:
        pending = self._pending_proposals()
        if not pending:
            return "没有待批准的提案。"
        lines = ["📋 待批准的提案"]
        for item in pending:
            payload = item.get("payload") or {}
            lines.append(f"· {item['id']}：{payload.get('label')}（{payload.get('key')}）")
            if payload.get("cadence"):
                lines.append(f"   提醒：{payload['cadence']}")
            if payload.get("reason"):
                lines.append(f"   理由：{payload['reason']}")
        lines.append("同意用 /台账 批准 <id>，不要用 /台账 驳回 <id>。")
        return "\n".join(lines)

    def _reject_proposal(self, proposal_id: str) -> str:
        data = self._load_proposals()
        found = None
        for item in data.get("items") or []:
            if item.get("id") == proposal_id and item.get("status") == "pending":
                item["status"] = "rejected"
                found = item
                break
        if not found:
            return f"找不到待批准的提案 {proposal_id}。"
        self._save_proposals(data)
        return f"已驳回提案 {proposal_id}。"

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
