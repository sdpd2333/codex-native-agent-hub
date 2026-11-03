"""Ordered native-agent rules. Codex interprets intent; the server enforces pools and snapshots."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import time

import common
import local_config
import model_selection
import runtimes

_contexts: dict[str, dict] = {}
_salt = secrets.token_bytes(32)


def policy() -> dict:
    with common.configuration_lock():
        path = common.STATE_DIR / "routing.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "revision": "initial", "enabled": False, "rules": [], "fallback": []}


def _text(value, limit: int, label: str, empty=True) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise common.RelayError(label + "无效。")
    return value.strip()


def _pool(value) -> list[str]:
    if not isinstance(value, list) or len(value) > 100 or any(
            not isinstance(v, str) or not re.fullmatch(r"(?:claude|gemini|deepseek):[a-f0-9]{24}", v) for v in value):
        raise common.RelayError("候选须从实时模型列表选择，最多 100 项。")
    return list(dict.fromkeys(value))


def save(data: dict, revision: str) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
        raise common.RelayError("规则配置无效。")
    rules = data.get("rules", [])
    if not isinstance(rules, list) or len(rules) > 50:
        raise common.RelayError("最多保存 50 条规则。")
    result, ids = [], set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise common.RelayError("规则格式无效。")
        identifier = _text(rule.get("id"), 80, "规则 ID", False)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier) or identifier in ids:
            raise common.RelayError("规则 ID 重复或无效。")
        ids.add(identifier)
        conditions = rule.get("conditions", {})
        if not isinstance(conditions, dict) or set(conditions) - {"modes", "complexities", "task_kinds"}:
            raise common.RelayError("规则条件无效。")
        checked = {}
        for field, allowed in {"modes": {"review", "edit"}, "complexities": set(model_selection.COMPLEXITIES),
                               "task_kinds": {"general", "connectivity"}}.items():
            values = conditions.get(field, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or v not in allowed for v in values):
                raise common.RelayError("规则条件值无效。")
            checked[field] = list(dict.fromkeys(values))
        project = _text(rule.get("project_dir", ""), 4096, "项目范围")
        if project and not Path(project).is_absolute():
            raise common.RelayError("项目范围须为绝对目录；该目录及子目录适用。")
        for field in ("enabled", "allow_fallback"):
            if not isinstance(rule.get(field, True), bool):
                raise common.RelayError("规则开关无效。")
        effort = _text(rule.get("effort", ""), 20, "推理强度")
        if effort not in ("", "low", "medium", "high", "xhigh", "max"):
            raise common.RelayError("推理强度无效。")
        result.append({"id": identifier, "name": _text(rule.get("name"), 100, "规则名称", False),
                       "enabled": rule.get("enabled", True), "project_dir": str(Path(project).resolve()) if project else "",
                       "conditions": checked, "intent": _text(rule.get("intent", ""), 2000, "任务意图"),
                       "preference": _text(rule.get("preference", ""), 2000, "选择偏好"),
                       "candidates": _pool(rule.get("candidates", [])), "effort": effort,
                       "allow_fallback": rule.get("allow_fallback", True)})
    with common.configuration_lock():
        if revision != policy()["revision"]:
            raise common.ConfigurationConflict("规则已在其他面板更新，请重新加载并合并修改。")
        value = {"revision": secrets.token_hex(16), "enabled": data["enabled"], "rules": result,
                 "fallback": _pool(data.get("fallback", []))}
        common._atomic_write(common.STATE_DIR / "routing.json", json.dumps(value, ensure_ascii=False, indent=2).encode())
        return value


def candidate_id(backend: str, model: str) -> str:
    # A connection currently is the independent profile belonging to one native backend.
    return backend + ":" + hashlib.sha256(model.encode()).hexdigest()[:24]


def compatible(model: str, backend: str) -> bool:
    if not isinstance(model, str) or not model.strip() or len(model) > 512 or any(ord(c) < 32 for c in model):
        return False
    _, words = model_selection._tokens(model)
    _, name_words = model_selection._tokens(model.rsplit("/", 1)[-1])
    if words & (model_selection.OTHER_FAMILIES | ({"gemini", "claude", "deepseek"} - {backend})):
        return False
    if name_words & model_selection.SPECIALIZED or "banana" in name_words:
        return False
    return backend in words or model_selection.describe_model(model, backend)["eligible"]


def _credential_version(key: str) -> str:
    return hmac.new(_salt, key.encode(), hashlib.sha256).hexdigest()


async def catalog() -> dict:
    snapshots, excluded = {}, []
    doctor = runtimes.doctor()
    for backend in common.PROVIDERS:
        try:
            await asyncio.to_thread(local_config.require_ready, backend)
            with common.configuration_lock():
                profile, key = common.load_settings(backend), common.load_key(backend)
                common.validate_profile(profile["base_url"], profile["model"], backend, profile["effort"])
                if not doctor.get(backend, {}).get("installed"):
                    raise common.RelayError("SDK 未安装。")
                snapshots[backend] = {"profile": profile, "key": key, "revision": common.profile_revision(backend),
                                      "credential_version": _credential_version(key)}
        except Exception:
            excluded.append({"backend": backend, "reason": "连接未配置、同步暂停或原生 SDK 未安装。"})

    async def discover(backend, snapshot):
        try:
            models = await asyncio.to_thread(common.fetch_models, snapshot["profile"]["base_url"], snapshot["key"], backend)
            return backend, models
        except Exception:
            return backend, None

    candidates = []
    for backend, models in await asyncio.gather(*(discover(b, s) for b, s in snapshots.items())):
        snapshot = snapshots[backend]
        if models is None:
            excluded.append({"backend": backend, "reason": "实时模型列表读取失败。"})
            continue
        for model in dict.fromkeys(models):
            if not compatible(model, backend):
                excluded.append({"backend": backend, "model": model, "reason": "名称无法确认属于此原生代理的通用模型。"})
                continue
            candidates.append({"id": candidate_id(backend, model), "backend": backend, "connection": backend,
                               "model": model, "runtime": common.PROVIDERS[backend]["runtime"],
                               "revision": snapshot["revision"], "default": model == snapshot["profile"]["model"]})
    return {"candidates": candidates, "excluded": excluded, "snapshots": snapshots}


def _validate_task(task, project_dir, mode, complexity, task_kind, requested_backend):
    _text(task, 100000, "任务", False)
    if not isinstance(project_dir, str) or not Path(project_dir).is_absolute() or not Path(project_dir).is_dir():
        raise common.RelayError("project_dir 必须是存在的绝对路径。")
    if mode not in ("review", "edit") or complexity not in model_selection.COMPLEXITIES or task_kind not in ("general", "connectivity"):
        raise common.RelayError("规则任务条件无效。")
    if requested_backend and requested_backend not in common.PROVIDERS:
        raise common.RelayError("指定代理无效。")
    if task_kind == "connectivity" and mode != "review":
        raise common.RelayError("连通性检测须为只读任务。")
    return {"task": task, "project_dir": str(Path(project_dir).resolve()), "mode": mode,
            "task_complexity": "simple" if task_kind == "connectivity" else complexity,
            "task_kind": task_kind, "requested_backend": requested_backend}


def _matches(rule, task):
    if not rule["enabled"]:
        return False
    if rule["project_dir"] and not Path(task["project_dir"]).is_relative_to(Path(rule["project_dir"])):
        return False
    return all(not rule["conditions"].get(k) or task[v] in rule["conditions"][k] for k, v in (
        ("modes", "mode"), ("complexities", "task_complexity"), ("task_kinds", "task_kind")))


async def context(task: str, project_dir: str, mode="review", task_complexity="standard", task_kind="general", requested_backend=""):
    task_data = _validate_task(task, project_dir, mode, task_complexity, task_kind, requested_backend)
    current_policy = policy()
    listing = await catalog()
    with common.configuration_lock():
        if policy()["revision"] != current_policy["revision"]:
            raise common.ConfigurationConflict("拉取模型期间规则变化，请重试。")
        _check_snapshots(listing["snapshots"])
        for token, old in list(_contexts.items()):
            if old["expires"] < time.monotonic():
                del _contexts[token]
        if len(_contexts) >= 128:
            _contexts.pop(next(iter(_contexts)))
        token = secrets.token_urlsafe(24)
        _contexts[token] = {**listing, "policy": current_policy, "task": task_data, "expires": time.monotonic() + 600,
                            "connection_revisions": {b: common.profile_revision(b) for b in common.PROVIDERS}}
    return {"token": token, "expires_in": 600, "policy_revision": current_policy["revision"],
            "enabled": current_policy["enabled"], "rules": [r for r in current_policy["rules"] if _matches(r, task_data)],
            "fallback": current_policy["fallback"], "candidates": listing["candidates"], "excluded": listing["excluded"],
            "instructions": "按顺序判断每条 intent，提供所有匹配的 matched_intents 规则 ID；空 intent 自动匹配。"
                            "先用 preview_routing 确认命中池，再按 preference 在池内选择 candidate_id，并解释 reason。"
                            "这是本次任务决策，不是用户显式型号覆盖。规则/目录/连接改变后须重新获取上下文。"}


def _check_snapshots(snapshots):
    for backend, snapshot in snapshots.items():
        if (common.profile_revision(backend) != snapshot["revision"]
                or _credential_version(common.load_key(backend)) != snapshot["credential_version"]):
            raise common.ConfigurationConflict("连接配置已变化，请重新获取规则上下文。")


def _get(token):
    if not isinstance(token, str):
        raise common.RelayError("规则上下文 token 无效。")
    stored = _contexts.get(token)
    if not stored or stored["expires"] < time.monotonic():
        raise common.ConfigurationConflict("规则上下文已过期或服务已重启，请重新获取。")
    if policy()["revision"] != stored["policy"]["revision"]:
        raise common.ConfigurationConflict("规则已更新，请重新获取规则上下文。")
    if not stored["policy"]["enabled"]:
        raise common.RelayError("跨代理规则模式尚未开启。")
    _check_snapshots(stored["snapshots"])
    if any(common.profile_revision(b) != revision for b, revision in stored["connection_revisions"].items()):
        raise common.ConfigurationConflict("连接配置已变化，请重新获取规则上下文。")
    return stored


def decide(stored, matched_intents, candidate="", reason="", start_rule="") -> dict:
    _text(candidate, 100, "候选 ID")
    if not isinstance(matched_intents, list) or any(not isinstance(v, str) for v in matched_intents):
        raise common.RelayError("matched_intents 须为匹配意图的规则 ID 列表。")
    eligible = [r for r in stored["policy"]["rules"] if _matches(r, stored["task"])]
    intent_ids = {r["id"] for r in eligible if r["intent"]}
    if set(matched_intents) - intent_ids:
        raise common.RelayError("意图匹配包含未知或不满足硬条件的规则。")
    rules = [r for r in eligible if not r["intent"] or r["id"] in matched_intents]
    rules.append({"id": "__fallback__", "name": "默认候选池", "candidates": stored["policy"]["fallback"],
                  "effort": "", "allow_fallback": False, "preference": "按保存顺序选择可用候选。"})
    if start_rule:
        rules = rules[next(i for i, rule in enumerate(rules) if rule["id"] == start_rule):]
    available = {c["id"]: c for c in stored["candidates"]}
    trace = []
    for rule in rules:
        pool, unavailable = [], []
        for identifier in rule["candidates"]:
            item = available.get(identifier)
            if not item:
                unavailable.append({"id": identifier, "reason": "连接或模型当前不可用。"})
                continue
            backend = item["backend"]
            if stored["task"]["requested_backend"] and backend != stored["task"]["requested_backend"]:
                unavailable.append({"id": identifier, "reason": "用户指定了其他代理。"})
                continue
            try:
                profile = stored["snapshots"][backend]["profile"]
                common.validate_profile(profile["base_url"], item["model"], backend, rule["effort"] or profile["effort"])
            except common.RelayError:
                unavailable.append({"id": identifier, "reason": "该代理不支持规则要求的推理强度。"})
                continue
            pool.append(item)
        trace.append({"rule_id": rule["id"], "name": rule["name"], "unavailable": unavailable})
        if pool:
            if candidate and candidate not in {c["id"] for c in pool}:
                raise common.RelayError("候选不属于第一条命中且可用的规则池。请重新预览选择。")
            selected = next((c for c in pool if c["id"] == candidate), pool[0])
            return {"mode": "rules", "policy_revision": stored["policy"]["revision"],
                    "rule_id": rule["id"], "rule_name": rule["name"], "preference": rule["preference"],
                    "candidate_id": selected["id"], "backend": selected["backend"], "model": selected["model"],
                    "effort": rule["effort"] or stored["snapshots"][selected["backend"]]["profile"]["effort"],
                    "reason": _text(reason, 1000, "选择理由") or "按规则候选顺序选择首个可用组合。",
                    "pool": pool, "trace": trace, "fallback": len(trace) > 1 or rule["id"] == "__fallback__"}
        if not rule["allow_fallback"]:
            raise common.RelayError("规则「" + rule["name"] + "」无可用候选，且不允许继续回退。")
    raise common.RelayError("没有可用的规则候选。请检查连接、规则池与实时模型列表。")


def preview(token: str, matched_intents=None, candidate_id="", reason="") -> dict:
    with common.configuration_lock():
        return decide(_get(token), matched_intents or [], candidate_id, reason)


async def resolve(decision: dict, task: dict) -> tuple[str, dict, str, dict]:
    if not isinstance(decision, dict):
        raise common.RelayError("routing_decision 无效。")
    token = decision.get("token")
    with common.configuration_lock():
        stored = _get(token)
        expected = {k: v for k, v in stored["task"].items() if k != "requested_backend"}
        if expected != task:
            raise common.ConfigurationConflict("任务内容或条件已变化，请为新任务重新获取规则上下文。")
        original = decide(stored, decision.get("matched_intents", []), decision.get("candidate_id", ""), decision.get("reason", ""))
    # Refresh availability BEFORE workspace creation or any native tool side effects.
    fresh = await catalog()
    with common.configuration_lock():
        _get(token)
        current = {**stored, "candidates": [c for c in fresh["candidates"] if c["backend"] in stored["snapshots"]]}
        result = decide(current, decision.get("matched_intents", []), reason=original["reason"], start_rule=original["rule_id"])
        if original["candidate_id"] in {c["id"] for c in result["pool"]}:
            result = decide(current, decision.get("matched_intents", []), original["candidate_id"], original["reason"], original["rule_id"])
        else:
            result["reason"] = "执行前候选不可用，按规则顺序回退。原选择理由：" + original["reason"]
            result["fallback"] = True
            result["previous_candidate_id"] = original["candidate_id"]
        backend = result["backend"]
        snapshot = stored["snapshots"][backend]
        profile = {**snapshot["profile"], "model": result["model"], "effort": result["effort"]}
        key = snapshot["key"]
        result.pop("pool")
        result["reason"] = common.redact(result["reason"], *(s["key"] for s in stored["snapshots"].values()))
        result["configuration_revision"] = snapshot["revision"]
        result["matched_intents"] = list(decision.get("matched_intents", []))
        result["complexity"] = stored["task"]["task_complexity"]
        result["requested_backend"] = stored["task"]["requested_backend"]
        result["trace"] = original["trace"][:-1] + result["trace"]
        result["fallback"] = result["fallback"] or original["fallback"]
        result["excluded"] = fresh["excluded"]
        del _contexts[token]
        return backend, profile, key, result
