"""fact_ledger 的核心逻辑（纯标准库，不依赖 AstrBot，便于单测）。

一个指标 = 一个状态机：当前值 / 上一次值 / 目标 / 规则 / 步长 / 证据链。
规则只负责「数值怎么变」，不判断「今天到底做没做」——那由触发源（定时任务、
工具调用、其他插件事件）决定。
"""

from __future__ import annotations

import json
import os
import random
import tempfile
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore
from typing import Any

LOCAL_TZ = timezone(timedelta(hours=8))


def configure_timezone(name: str) -> None:
    """按配置切换时区；解析失败退回 +08:00。"""
    global LOCAL_TZ
    text = str(name or "").strip()
    if text and ZoneInfo is not None:
        try:
            LOCAL_TZ = ZoneInfo(text)
            return
        except Exception:
            pass
    LOCAL_TZ = timezone(timedelta(hours=8))

RULES = ("decrease", "increase", "streak", "stage", "counter", "gauge", "fixed", "skill")

RULE_LABELS = {
    "decrease": "每日递减到目标",
    "increase": "每日递增到目标",
    "streak": "连续天数（断则归零）",
    "stage": "阶段（只进不退）",
    "counter": "累计计数",
    "gauge": "手动/事件设值",
    "fixed": "固定属性（只读，人工维护）",
    "skill": "技能熟练度（练习累计，到 100 掌握）",
}

DEFAULT_STATE = {"version": 1, "metrics": {}}


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_metric(raw: dict[str, Any]) -> dict[str, Any]:
    """补齐缺省字段，拒绝不认识的规则。"""
    metric = dict(raw or {})
    rule = str(metric.get("rule") or "gauge").strip().lower()
    metric["rule"] = rule if rule in RULES else "gauge"
    metric["label"] = str(metric.get("label") or metric.get("key") or "未命名").strip()
    metric["unit"] = str(metric.get("unit") or "").strip()
    raw_value = metric.get("value")
    parsed = _num(raw_value, None)
    metric["value"] = parsed if parsed is not None else (str(raw_value).strip() if raw_value not in (None, "") else 0.0)
    prev_parsed = _num(metric.get("prev"), None)
    metric["prev"] = prev_parsed if prev_parsed is not None else metric["value"]
    metric["target"] = _num(metric.get("target"), None)
    metric["step"] = abs(_num(metric.get("step"), 0.0) or 0.0)
    metric["floor"] = _num(metric.get("floor"), None)
    metric["ceiling"] = _num(metric.get("ceiling"), None)
    metric["decimals"] = max(0, min(3, _int(metric.get("decimals"), 1)))
    metric["cadence"] = str(metric.get("cadence") or "").strip()
    metric["priority"] = _int(metric.get("priority"), 50)
    metric["inject"] = bool(metric.get("inject", True))
    metric["enabled"] = bool(metric.get("enabled", True))
    metric["last_advanced_on"] = str(metric.get("last_advanced_on") or "")
    metric["updated_at"] = str(metric.get("updated_at") or "")
    metric["history"] = [item for item in (metric.get("history") or []) if isinstance(item, dict)]
    metric["deadline"] = str(metric.get("deadline") or "").strip()
    metric["note"] = str(metric.get("note") or "").strip()
    metric["sensitivity"] = str(metric.get("sensitivity") or "public").strip().lower()
    metric["category"] = str(metric.get("category") or "state").strip().lower()
    metric["authority"] = str(metric.get("authority") or "ledger").strip().lower()
    flat_p = max(0.0, min(1.0, _num(metric.get("flat_probability"), 0.0) or 0.0))
    metric["flat_probability"] = flat_p
    metric["gain_probability"] = round(max(0.0, min(1.0 - flat_p, _num(metric.get("gain_probability"), 0.0) or 0.0)), 4)
    metric["gain_step"] = abs(_num(metric.get("gain_step"), 0.0) or 0.0)
    metric["target_history"] = [item for item in (metric.get("target_history") or []) if isinstance(item, dict)]
    metric["daily_cap"] = abs(_num(metric.get("daily_cap"), 8.0) or 8.0)
    # gain_base=0 表示按等级自动取（生疏 5 / 入门 4 / 熟练 3 / 精通 2）
    metric["gain_base"] = abs(_num(metric.get("gain_base"), 0.0) or 0.0)
    metric["decay_after_days"] = max(1, _int(metric.get("decay_after_days"), 3))
    metric["decay_per_day"] = abs(_num(metric.get("decay_per_day"), 1.0) or 1.0)
    metric["decay_protect_level"] = abs(_num(metric.get("decay_protect_level"), 46.0) or 46.0)
    metric["practice_count"] = max(0, _int(metric.get("practice_count"), 0))
    metric["last_practice_on"] = str(metric.get("last_practice_on") or "")
    metric["last_decay_on"] = str(metric.get("last_decay_on") or "")
    metric["today_gain_date"] = str(metric.get("today_gain_date") or "")
    metric["today_gain"] = abs(_num(metric.get("today_gain"), 0.0) or 0.0)
    metric["mastered_at"] = str(metric.get("mastered_at") or "")
    return metric


def load_ledger(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        # 文件坏了不要静默清空：先留一份现场，再回退空台账。
        try:
            stamp = datetime.now(LOCAL_TZ).strftime("%Y%m%d%H%M%S")
            os.replace(path, f"{path}.corrupt-{stamp}")
        except OSError:
            pass
        return json.loads(json.dumps(DEFAULT_STATE))
    if not isinstance(raw, dict):
        return json.loads(json.dumps(DEFAULT_STATE))
    metrics = raw.get("metrics")
    out = {"version": _int(raw.get("version"), 1), "metrics": {}}
    if isinstance(metrics, dict):
        for key, item in metrics.items():
            if isinstance(item, dict):
                out["metrics"][str(key)] = normalize_metric(item)
    return out


def save_ledger(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ledger-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def parse_cadence(text: str) -> tuple[int, int] | None:
    """'daily@15:00' / '15:00' -> (15, 0)；其余返回 None。"""
    value = str(text or "").strip().lower()
    if not value:
        return None
    if "@" in value:
        value = value.split("@", 1)[1].strip()
    if ":" not in value:
        value = value.replace("：", ":", 1)
    if ":" not in value:
        return None
    head, _, tail = value.partition(":")
    try:
        hour, minute = int(head), int(tail)
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def is_due(metric: dict[str, Any], now: datetime | None = None) -> bool:
    """今天到点且今天还没推进过。"""
    now = now or now_local()
    if not metric.get("enabled", True):
        return False
    slot = parse_cadence(metric.get("cadence", ""))
    if slot is None:
        return False
    if metric.get("rule") not in ("decrease", "increase", "counter"):
        return False
    deadline = str(metric.get("deadline") or "").strip()
    if deadline and now.date().isoformat() > deadline:
        return False
    if metric.get("last_advanced_on") == now.date().isoformat():
        return False
    return (now.hour, now.minute) >= slot


def _clamp(metric: dict[str, Any], value: float) -> float:
    floor = metric.get("floor")
    ceiling = metric.get("ceiling")
    if floor is not None and value < floor:
        value = floor
    if ceiling is not None and value > ceiling:
        value = ceiling
    return value


def _record(
    metric: dict[str, Any],
    old: float,
    new: float,
    now: datetime,
    *,
    reason: str,
    evidence: str,
    max_history: int = 30,
) -> dict[str, Any]:
    decimals = metric.get("decimals", 1)
    entry = {
        "at": now.isoformat(timespec="seconds"),
        "from": round(old, decimals),
        "to": round(new, decimals),
        "delta": round(new - old, decimals),
        "reason": str(reason or "").strip() or "定时推进",
        "evidence": str(evidence or "").strip(),
    }
    history = list(metric.get("history") or [])
    history.append(entry)
    metric["history"] = history[-max(1, max_history):]
    metric["prev"] = round(old, decimals)
    metric["value"] = round(new, decimals)
    metric["updated_at"] = now.isoformat(timespec="seconds")
    metric["last_advanced_on"] = now.date().isoformat()
    return entry


def advance(
    metric: dict[str, Any],
    now: datetime | None = None,
    *,
    reason: str = "",
    evidence: str = "",
    max_history: int = 30,
    rng: Any = None,
) -> dict[str, Any] | None:
    """按规则推进一步；无可推进（已达标/规则不支持）返回 None。"""
    now = now or now_local()
    deadline = str(metric.get("deadline") or "").strip()
    if deadline and now.date().isoformat() > deadline:
        return None
    rule = metric.get("rule", "gauge")
    old = float(metric.get("value") or 0.0)
    step = abs(float(metric.get("step") or 0.0))
    decimals = metric.get("decimals", 1)

    if rule == "decrease":
        target = metric.get("target")
        roll_source = rng or random
        roll = float(roll_source.random())
        flat_p = float(metric.get("flat_probability") or 0.0)
        gain_p = float(metric.get("gain_probability") or 0.0)
        if roll < flat_p:
            # 平台期：今天没掉秤。仍记一条 0 变化，避免当天的待确认一直挂着。
            return _record(
                metric, old, old, now,
                reason=f"{reason or '每日称重'}（今天没掉秤）",
                evidence=evidence,
                max_history=max_history,
            )
        if roll < flat_p + gain_p:
            new = old + float(metric.get("gain_step") or 0.0)
        else:
            if step <= 0:
                return None
            new = old - step
        if target is not None:
            new = max(float(target), new)
        if new >= old and roll >= flat_p + gain_p:
            return None
    elif rule == "increase":
        target = metric.get("target")
        if step <= 0:
            return None
        new = old + step
        if target is not None:
            new = min(float(target), new)
        if new <= old:
            return None
    elif rule == "counter":
        new = old + max(1.0, step or 1.0)
    elif rule == "streak":
        last_day = str(metric.get("last_advanced_on") or "")
        today = now.date().isoformat()
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        if last_day == today:
            return None
        new = old + 1.0 if last_day in (yesterday, "") else 1.0
    elif rule == "stage":
        new = old + max(1.0, step or 1.0)
    else:
        return None

    new = _clamp(metric, new)
    if round(new, decimals) == round(old, decimals):
        return None
    return _record(metric, old, new, now, reason=reason, evidence=evidence, max_history=max_history)


def set_value(
    metric: dict[str, Any],
    value: float,
    now: datetime | None = None,
    *,
    reason: str = "",
    evidence: str = "",
    max_history: int = 30,
) -> dict[str, Any] | None:
    now = now or now_local()
    old = float(metric.get("value") or 0.0)
    new = _clamp(metric, float(value))
    if round(new, metric.get("decimals", 1)) == round(old, metric.get("decimals", 1)):
        return None
    return _record(metric, old, new, now, reason=reason or "手动设值", evidence=evidence, max_history=max_history)


def set_text(
    metric: dict[str, Any],
    text: str,
    now: datetime | None = None,
    *,
    reason: str = "",
    evidence: str = "",
    max_history: int = 30,
) -> dict[str, Any] | None:
    """给文本类固定属性设值（例如罩杯 C）。"""
    now = now or now_local()
    old = metric.get("value")
    new = str(text or "").strip()
    if not new or str(old) == new:
        return None
    entry = {
        "at": now.isoformat(timespec="seconds"),
        "from": str(old),
        "to": new,
        "delta": "",
        "reason": str(reason or "").strip() or "手动设值",
        "evidence": str(evidence or "").strip(),
    }
    history = list(metric.get("history") or [])
    history.append(entry)
    metric["history"] = history[-max(1, max_history):]
    metric["prev"] = old
    metric["value"] = new
    metric["updated_at"] = now.isoformat(timespec="seconds")
    metric["last_advanced_on"] = now.date().isoformat()
    return entry


def target_error(metric: dict[str, Any], target: float | None) -> str:
    """目标是否合理；返回错误文案，空串表示通过。"""
    if target is None:
        return ""
    value = metric.get("value")
    if isinstance(value, str):
        return ""
    try:
        current = float(value or 0)
    except (TypeError, ValueError):
        return ""
    rule = str(metric.get("rule") or "")
    if rule == "decrease" and float(target) >= current:
        return f"目标必须小于当前值 {fmt(metric)}"
    if rule == "increase" and float(target) <= current:
        return f"目标必须大于当前值 {fmt(metric)}"
    floor = metric.get("floor")
    if rule == "decrease" and floor is not None and float(target) < float(floor):
        return f"目标低于下限 {float(floor):g}，请先把下限调低"
    ceiling = metric.get("ceiling")
    if rule == "increase" and ceiling is not None and float(target) > float(ceiling):
        return f"目标高于上限 {float(ceiling):g}，请先把上限调高"
    return ""


def set_target(
    metric: dict[str, Any],
    target: float | None,
    now: datetime | None = None,
    *,
    deadline: str = "",
    reason: str = "",
    max_history: int = 20,
) -> dict[str, Any]:
    """调整目标（可带截止日期）。只改目标，不动当前值。"""
    now = now or now_local()
    old = metric.get("target")
    metric["target"] = None if target is None else float(target)
    if deadline:
        metric["deadline"] = str(deadline).strip()
    record = {
        "at": now.isoformat(timespec="seconds"),
        "from": old,
        "to": metric.get("target"),
        "deadline": metric.get("deadline", ""),
        "reason": str(reason or "").strip() or "目标调整",
    }
    history = list(metric.get("target_history") or [])
    history.append(record)
    metric["target_history"] = history[-max(1, max_history):]
    return record


SKILL_LEVELS = (
    (20.0, "生疏"),
    (45.0, "入门"),
    (70.0, "熟练"),
    (90.0, "精通"),
    (100.0, "掌握"),
)
#: 单次练习的基础增益（按次数计，越往后越慢）
SKILL_GAIN_BY_LEVEL = {"生疏": 5.0, "入门": 4.0, "熟练": 3.0, "精通": 2.0, "掌握": 0.0}


def skill_level(metric: dict[str, Any], value: float | None = None) -> tuple[str, float, float]:
    """返回 (等级名, 该级下限, 该级上限)。"""
    try:
        current = float(metric.get("value") if value is None else value)
    except (TypeError, ValueError):
        current = 0.0
    lower = 0.0
    for upper, name in SKILL_LEVELS:
        if current <= upper:
            return name, lower, upper
        lower = upper
    return "掌握", 90.0, 100.0


def practice(
    metric: dict[str, Any],
    now: datetime | None = None,
    *,
    sessions: int = 1,
    reason: str = "",
    evidence: str = "",
    max_history: int = 30,
) -> dict[str, Any] | None:
    """按「次数」增加熟练度；受每日上限约束。"""
    now = now or now_local()
    try:
        value = float(metric.get("value") or 0.0)
        target = float(metric.get("target") or 100.0)
    except (TypeError, ValueError):
        return None
    if value >= target:
        return None
    try:
        sessions = max(1, int(sessions))
    except (TypeError, ValueError):
        sessions = 1

    today = now.date().isoformat()
    if metric.get("today_gain_date") != today:
        metric["today_gain_date"] = today
        metric["today_gain"] = 0.0
    used = float(metric.get("today_gain") or 0.0)
    remaining = max(0.0, float(metric.get("daily_cap") or 8.0) - used)
    if remaining <= 0:
        return None

    level, _lo, _hi = skill_level(metric, value)
    per = float(metric.get("gain_base") or 0.0) or SKILL_GAIN_BY_LEVEL.get(level, 4.0)
    gain = min(per * sessions, remaining)
    if gain <= 0:
        return None
    new_value = min(target, value + gain)
    entry = _record(
        metric, value, new_value, now,
        reason=reason or f"练习 {sessions} 次",
        evidence=evidence,
        max_history=max_history,
    )
    metric["today_gain"] = used + (new_value - value)
    metric["practice_count"] = int(metric.get("practice_count") or 0) + sessions
    metric["last_practice_on"] = today
    if new_value >= target:
        metric["mastered_at"] = now.isoformat(timespec="seconds")
    return entry


def decay_if_needed(
    metric: dict[str, Any],
    now: datetime | None = None,
    *,
    max_history: int = 30,
) -> dict[str, Any] | None:
    """太久没练会掉熟练度；到了保护线（默认「熟练」46）就不再衰减。"""
    if str(metric.get("rule")) != "skill":
        return None
    now = now or now_local()
    try:
        value = float(metric.get("value") or 0.0)
    except (TypeError, ValueError):
        return None
    protect = float(metric.get("decay_protect_level") or 46.0)
    if value >= protect:
        return None
    last = str(metric.get("last_practice_on") or "")
    if not last:
        return None
    try:
        last_date = datetime.fromisoformat(last).date()
    except ValueError:
        return None
    idle = (now.date() - last_date).days
    if idle <= int(metric.get("decay_after_days") or 3):
        return None
    if str(metric.get("last_decay_on") or "") == now.date().isoformat():
        return None
    level, low, _high = skill_level(metric, value)
    new_value = max(low, value - float(metric.get("decay_per_day") or 1.0))
    new_value = min(new_value, protect)
    if new_value >= value:
        return None
    entry = _record(
        metric, value, new_value, now,
        reason=f"太久没练（{idle} 天）",
        evidence="自动衰减",
        max_history=max_history,
    )
    metric["last_decay_on"] = now.date().isoformat()
    return entry


def fmt(metric: dict[str, Any], value: float | None = None) -> str:
    number = metric.get("value") if value is None else value
    if isinstance(number, str):
        unit = metric.get("unit") or ""
        return f"{number}{unit}" if unit else number
    decimals = int(metric.get("decimals", 1) or 0)
    text = f"{float(number or 0):.{decimals}f}"
    # 去掉多余的 .0（体重 112 而不是 112.0）
    if text.endswith(".0") and decimals == 1:
        text = text[:-2]
    unit = metric.get("unit") or ""
    return f"{text}{unit}" if unit else text


def describe(metric: dict[str, Any]) -> str:
    parts = [f"{metric.get('label')}：{fmt(metric)}"]
    target = metric.get("target")
    if target is not None:
        target_text = f"目标 {fmt(metric, float(target))}"
        deadline = str(metric.get("deadline") or "").strip()
        if deadline:
            expired = now_local().date().isoformat() > deadline
            target_text += f"（截止 {deadline}{'，已过期' if expired else ''}）"
        else:
            target_text += "（未设截止日期）"
        value = metric.get("value")
        if not isinstance(value, str):
            try:
                rule = str(metric.get("rule") or "")
                if rule == "decrease" and float(value) <= float(target):
                    target_text += "，已达成（保持期）"
                elif rule == "increase" and float(value) >= float(target):
                    target_text += "，已达成"
            except (TypeError, ValueError):
                pass
        parts.append(target_text)
    prev = metric.get("prev")
    if prev is not None and not isinstance(metric.get("value"), str) and not isinstance(prev, str):
        delta = float(metric.get("value") or 0) - float(prev or 0)
        if abs(delta) >= 10 ** (-int(metric.get("decimals", 1) or 0)) / 2:
            sign = "+" if delta > 0 else ""
            parts.append(f"较上次 {sign}{delta:.{int(metric.get('decimals', 1) or 0)}f}")
    if str(metric.get("rule") or "") == "skill":
        level, _lo, _hi = skill_level(metric)
        parts.append(f"等级 {level}")
        if metric.get("practice_count"):
            parts.append(f"已练 {metric['practice_count']} 次")
    if metric.get("updated_at"):
        parts.append(f"更新于 {metric['updated_at'][:16].replace('T', ' ')}")
    if metric.get("note"):
        parts.append(str(metric["note"]))
    text = "｜".join(parts)
    if str(metric.get("sensitivity") or "public").lower() == "private":
        text += "（私密）"
    return text


def build_identity_card(metrics: dict[str, Any]) -> str:
    """固定外貌（肤色/五官/发色等）：以人设为准，只做镜像，不参与数值判断。"""
    items = [
        item for item in (metrics or {}).values()
        if item.get("enabled", True)
        and str(item.get("category") or "").lower() == "appearance"
        and str(item.get("sensitivity") or "public").lower() != "private"
    ]
    if not items:
        return ""
    items.sort(key=lambda item: (-int(item.get("priority", 50)), str(item.get("label", ""))))
    lines = [
        "<identity_card>",
        "她固定的外貌设定（与 persona 人设一致，任何情况下都不改写；与本轮人设文本冲突时以人设文本为准）：",
    ]
    for item in items:
        value = item.get("value")
        if not str(value or "").strip():
            continue
        line = f"- {item.get('label')}：{value}"
        if item.get("note"):
            line += f"（{item['note']}）"
        lines.append(line)
    lines.append("</identity_card>")
    return "\n".join(lines)


def build_injection(metrics: dict[str, Any], *, max_metrics: int = 3, max_chars: int = 600) -> str:
    items = [
        item
        for item in (metrics or {}).values()
        if item.get("enabled", True)
        and item.get("inject", True)
        and str(item.get("sensitivity") or "public").lower() != "private"
        and str(item.get("category") or "").lower() != "appearance"
    ]
    if not items:
        return ""
    items.sort(key=lambda item: (-int(item.get("priority", 50)), str(item.get("label", ""))))
    lines = ["<fact_ledger>", "以下是系统维护的当前状态，回答相关问题时以这里为准，不要自己编新数字："]
    for item in items[: max(1, int(max_metrics))]:
        line = "- " + describe(item)
        last = (item.get("history") or [{}])[-1] if item.get("history") else {}
        if last.get("reason"):
            line += f"｜原因：{last['reason']}"
        lines.append(line)
    for item in items[: max(1, int(max_metrics))]:
        rule = str(item.get("rule") or "")
        target = item.get("target")
        if rule in ("decrease", "increase") and target is not None:
            direction = "减" if rule == "decrease" else "增"
            lines.append(
                f"- 她正在{direction}：当前 {fmt(item)} → 目标 {fmt(item, float(target))}。"
                f"涉及吃、运动、身材、穿衣、累不累这类自我判断时，要参考这条，不要夸大变化。"
            )
            break
    for item in items[: max(1, int(max_metrics))]:
        if str(item.get("rule") or "") == "skill":
            level, _lo, _hi = skill_level(item)
            lines.append(
                f"- 她正在练「{item.get('label')}」：熟练度 {fmt(item)}/100（{level}），已练 {item.get('practice_count') or 0} 次。"
                f"涉及这件事时按当前水平说，不要说已经会了。"
            )
            break
    has_private = any(
        str(item.get("sensitivity") or "public").lower() == "private" and item.get("enabled", True)
        for item in (metrics or {}).values()
    )
    if has_private:
        lines.append(
            "- 有私密属性（三围/罩杯等）尚未提供：只有主人本人问起时，先用 fact_ledger_get 查询再回答；"
            "不要主动提，也不要对其他人说。"
        )
    lines.append("</fact_ledger>")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 20].rstrip() + "\n</fact_ledger>"
    return text


def new_metric(
    key: str,
    label: str,
    *,
    unit: str = "",
    value: float = 0.0,
    target: float | None = None,
    rule: str = "gauge",
    step: float = 0.0,
    cadence: str = "",
    decimals: int = 1,
    priority: int = 50,
) -> dict[str, Any]:
    return normalize_metric({
        "key": key,
        "label": label,
        "unit": unit,
        "value": value,
        "target": target,
        "rule": rule,
        "step": step,
        "cadence": cadence,
        "decimals": decimals,
        "priority": priority,
        "inject": True,
        "enabled": True,
        "last_advanced_on": now_local().date().isoformat(),
        "updated_at": now_local().isoformat(timespec="seconds"),
        "history": [],
    })
