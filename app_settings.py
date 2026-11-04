"""Versioned application preferences shared by local panels and service processes."""
from __future__ import annotations

import hashlib
import json
import os

import common

SCHEMA = json.loads((common.APP_DIR / "settings-schema.json").read_text(encoding="utf-8"))
DEFAULTS = {name: field["default"] for name, field in SCHEMA.items()}
TASK_KEYS = ("max_model_calls", "max_tool_calls", "claude_max_turns", "deepseek_max_tokens")


def validate(values: dict) -> dict:
    if not isinstance(values, dict) or values.keys() - SCHEMA.keys():
        raise common.RelayError("应用设置包含未知字段或格式无效。")
    for name, value in values.items():
        field = SCHEMA[name]
        if "options" in field:
            valid = isinstance(value, str) and value in field["options"]
        else:
            valid = type(value) is int and field["min"] <= value <= field["max"]
        if not valid:
            allowed = "、".join(field["options"]) if "options" in field else f'{field["min"]}–{field["max"]} 的整数'
            raise common.RelayError(f'{field["label"]}须为 {allowed}。')
    return values


def read() -> dict:
    with common.configuration_lock():
        try:
            raw = (common.STATE_DIR / "app-settings.json").read_bytes()
            saved = DEFAULTS | validate(json.loads(raw))
        except FileNotFoundError:
            raw, saved = b"", DEFAULTS.copy()
        except (OSError, ValueError, TypeError, common.RelayError):
            raise common.RelayError("应用设置无法读取，请检查本地 app-settings.json。") from None
        effective, overrides = saved.copy(), {}
        name, environment = "max_concurrent_tasks", "AGENT_HUB_MAX_CONCURRENT_TASKS"
        if environment in os.environ:
            try:
                value = int(os.environ[environment])
                validate({name: value})
            except (ValueError, common.RelayError):
                raise common.RelayError(f"{environment} 必须为 1 到 8 的整数。") from None
            effective[name] = value
            overrides[name] = {"environment": environment, "value": value}
        return {"values": effective, "saved_values": saved, "overrides": overrides,
                "revision": hashlib.sha256(raw).hexdigest(), "schema": SCHEMA}


def values() -> dict:
    return read()["values"]


def save(changes: dict, revision: str) -> dict:
    validate(changes)
    with common.configuration_lock():
        current = read()
        if revision != current["revision"]:
            raise common.ConfigurationConflict("应用设置已被其他窗口更新。草稿已保留，请重新加载后合并修改。")
        for name in changes.keys() & current["overrides"].keys():
            if changes[name] != current["saved_values"][name]:
                raise common.RelayError("并发数由此实例的环境变量覆盖；请移除覆盖并重启该实例后再修改。")
        common._atomic_write(common.STATE_DIR / "app-settings.json",
                             json.dumps(current["saved_values"] | changes, ensure_ascii=False, indent=2).encode("utf-8"))
        return read()


def task_values(profile: dict | None = None) -> dict:
    """An accepted task keeps its budget even when another panel changes defaults."""
    source = (profile or {}).get("runtime_settings")
    if source is None:
        source = values()
    return {name: source.get(name, DEFAULTS[name]) for name in TASK_KEYS}


def operation_timeout() -> int:
    """Bound long panel operations by the same preferences used by their workers."""
    return values()["operation_timeout_seconds"]
