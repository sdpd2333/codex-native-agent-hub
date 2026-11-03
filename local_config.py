"""Opt-in, one-way client connection import. Never execute client hooks or helpers."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import time

import yaml
import common

FIELDS = ("base_url", "api_key", "model", "effort")
_previews: dict[str, dict] = {}


class _ConfigLoader(yaml.BaseLoader):
    def construct_object(self, node, deep=False):
        if node.tag not in {"tag:yaml.org,2002:" + name for name in
                            ("map", "seq", "str", "null", "bool", "int", "float", "timestamp", "merge")}:
            return None  # Dynamic/custom tags are not static connection values.
        return super().construct_object(node, deep)


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise common.RelayError("客户端配置不是普通文件或超过 1 MB。")
    return path.read_text(encoding="utf-8-sig")


def _json(path: Path) -> dict:
    value = json.loads(_read(path) or "{}")
    if not isinstance(value, dict):
        raise ValueError()
    return value


def _yaml(path: Path):
    # BaseLoader reads only strings/lists/maps. Dynamic tags such as !!js are ignored.
    # No objects, expressions, shell commands, or environment expansion are evaluated.
    return yaml.load(_read(path), Loader=_ConfigLoader) or {}


def _env(path: Path) -> dict:
    result = {}
    for line in _read(path).splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*?)\s*", line)
        if match:
            name, value = match.groups()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0]
            result[name] = value
    return result


def _string(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def sources(backend: str) -> list[dict]:
    """Called only by an explicit detect action; never scan arbitrary project trees."""
    common.check_backend(backend)
    home = Path.home()
    if backend == "claude":
        directory = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")).expanduser().resolve()
        return [{"backend": backend, "directory": str(directory), "profile": "", "label": "Claude Code 用户设置"}]
    if backend == "gemini":
        directory = Path(os.environ.get("GEMINI_CLI_HOME", home)).expanduser().resolve() / ".gemini"
        return [{"backend": backend, "directory": str(directory), "profile": "", "label": "Gemini CLI 用户设置"}]
    directory = Path(os.environ.get("DSH_HOME", home / ".dsh")).expanduser().resolve()
    result = [{"backend": backend, "directory": str(directory), "profile": "", "label": "DeepSeek Harness 用户设置"}]
    profiles = directory / "profiles"
    if profiles.is_dir():
        for path in sorted(profiles.iterdir())[:30]:
            if path.is_dir() and (path / "cordis.patch.yml").is_file():
                result.append({"backend": backend, "directory": str(directory), "profile": path.name,
                               "label": "DeepSeek Harness · " + path.name})
    return result


def read_source(source: dict) -> dict:
    directory, backend = Path(source["directory"]), source["backend"]
    values, notes, files = {}, [], []
    auth = "api_key"
    if backend == "claude":
        files = [directory / "settings.json"]
        data = _json(files[0])
        env = data.get("env", {})
        values = {"base_url": env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                  "api_key": env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN", ""),
                  "model": env.get("ANTHROPIC_MODEL") or data.get("model", ""),
                  "effort": env.get("CLAUDE_CODE_EFFORT_LEVEL") or data.get("effortLevel", "")}
        if not values["api_key"] or _string(values["api_key"]).startswith("sk-ant-oat"):
            values["api_key"] = ""
            auth = "unsupported"
            notes.append("未发现静态 API 凭据；OAuth/订阅登录及 apiKeyHelper 不会导入或执行。")
    elif backend == "gemini":
        files = [directory / "settings.json", directory / ".env"]
        data, env = _json(files[0]), _env(files[1])
        selected = data.get("security", {}).get("auth", {}).get("selectedType", "")
        values = {"base_url": env.get("GOOGLE_GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"),
                  "api_key": env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY", ""),
                  "model": env.get("GEMINI_MODEL") or data.get("model", {}).get("name", ""), "effort": ""}
        if (selected not in ("", "gemini-api-key") or not values["api_key"]
                or env.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("true", "1")):
            auth = "unsupported"
            values["api_key"] = ""
            notes.append("仅支持 Gemini API Key 配置；Google OAuth/Vertex 登录不导入。")
    else:
        files = [directory / "config.yaml", directory / "cordis.patch.yml", directory / "settings.yaml",
                 directory / ".env", directory / ".credentials.yaml"]
        if source.get("profile"):
            files.append(directory / "profiles" / source["profile"] / "cordis.patch.yml")
        entries = {}
        for path in [files[0], files[1], *files[5:]]:
            data = _yaml(path)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and isinstance(item.get("config"), dict):
                        entries[item.get("id")] = item["config"]
        overrides = _yaml(files[2])
        if isinstance(overrides, dict):
            for identifier, fields in overrides.items():
                if isinstance(fields, dict):
                    entries[identifier] = {**entries.get(identifier, {}), **fields}
        default = entries.get("agent-default-model", {})
        if not default:
            agents = entries.get("agent-loop", {}).get("agents", [])
            default = next((a for a in agents if isinstance(a, dict) and a.get("id") == "main"), {})
        provider = default.get("provider", "deepseek-official")
        config = entries.get("llm-deepseek", {})
        env, creds = _env(files[3]), _yaml(files[4])
        key_ref = "DEEPSEEK_API_KEY"
        if provider != "deepseek-official":
            config = entries.get("llm-pi-ai", {}).get("providers", {}).get(provider, {})
            key_ref = config.get("apiKeyEnv", "")
            if config.get("api") != "anthropic-messages":
                auth = "unsupported"
                notes.append("此 dsh 自定义提供商不是 Anthropic Messages 协议，无法用于当前原生运行时。")
        values = {"base_url": config.get("baseURL", env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")),
                  "api_key": creds.get("refs", {}).get(key_ref) or config.get("apiKey") or env.get(key_ref, ""),
                  "model": default.get("model", ""),
                  "effort": default.get("reasoningEffort", config.get("reasoningEffort", ""))}
        if not values["api_key"]:
            auth = "unsupported"
            notes.append("未发现可导入的静态 dsh API 凭据。")
        notes.append("读取用户层与所选 profile 的连接字段；不推断运行中会话或命令行覆盖值。")
    values = {field: _string(values.get(field)) for field in FIELDS}
    if any(re.search(r"\$(?:[A-Za-z_]|\{|\()|`", value) for value in values.values()):
        raise common.RelayError("配置含动态表达式；请在插件中手动配置静态连接。")
    present = [str(path) for path in files if path.is_file()]
    if not present:
        raise common.RelayError("未发现该客户端的本地配置文件。")
    if values["base_url"]:
        values["base_url"] = common.normalize_base(values["base_url"])
    # This digest remains in memory/state, never a credential or a public credential hash.
    fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    return {"values": values, "fingerprint": fingerprint, "auth": auth, "notes": notes,
            "files": present, "source": source}


def _state() -> dict:
    path = common.STATE_DIR / "local-sync.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"revision": "initial", "profiles": {}}


def _write(state: dict):
    state["revision"] = secrets.token_hex(16)
    common._atomic_write(common.STATE_DIR / "local-sync.json", json.dumps(state, ensure_ascii=False, indent=2).encode())


def status(backend: str) -> dict:
    with common.configuration_lock():
        state = _state()
        item = state["profiles"].get(backend, {})
        return {"revision": state["revision"], "mode": item.get("mode", "off"),
                "source": item.get("source"), "fields": item.get("fields", []),
                "status": item.get("status", "off"), "message": item.get("message", "本地配置检测与同步已关闭。")}


def detect(backend: str) -> dict:
    results = []
    with common.configuration_lock():
        for token, preview in list(_previews.items()):
            if preview["expires"] < time.monotonic():
                del _previews[token]
        for source in sources(backend):
            try:
                data = read_source(source)
                token = secrets.token_urlsafe(24)
                if len(_previews) >= 128:
                    _previews.pop(next(iter(_previews)))
                _previews[token] = {**data, "expires": time.monotonic() + 600}
                results.append({"token": token, "source": source, "files": data["files"],
                                "values": {k: v for k, v in data["values"].items() if k != "api_key"},
                                "key_available": bool(data["values"]["api_key"]),
                                "auth": data["auth"], "notes": data["notes"]})
            except Exception:
                # Parser exceptions may contain credential lines. Never surface their text.
                results.append({"source": source, "error": "未发现配置或格式不受支持；请检查客户端配置。"})
    return {"sources": results}


def _apply(backend: str, data: dict, fields: list[str]):
    values = data["values"]
    settings = common.load_settings(backend)
    effective = {**settings, **{k: values[k] for k in fields if k != "api_key"}}
    common.validate_profile(effective["base_url"], effective["model"], backend, effective["effort"])
    if "api_key" in fields and (data["auth"] != "api_key" or not values["api_key"]):
        raise common.RelayError("所选来源没有受支持的 API Key；请取消凭据字段或手动配置。")
    changed_base = effective["base_url"] != settings["base_url"]
    if changed_base and "api_key" not in fields:
        raise common.RelayError("更换地址须同时导入该来源的 API Key，避免将旧凭据发送到新地址。")
    env_key = os.environ.get("AGENT_HUB_" + backend.upper() + "_API_KEY", "").strip()
    if env_key and (changed_base or "api_key" in fields) and env_key != values["api_key"]:
        raise common.RelayError("插件环境变量中的 Key 优先级更高；请先移除该覆盖再同步地址或凭据。")
    paths = [common.STATE_DIR / "settings.json", common.STATE_DIR / (backend + ".dpapi")]
    previous = [p.read_bytes() if p.exists() else None for p in paths]
    try:
        if "api_key" in fields:
            common.save_key(values["api_key"], backend)
        common.save_settings(effective["base_url"], effective["model"], backend, effective["effort"])
    except Exception:
        for path, old in zip(paths, previous):
            if old is None:
                path.unlink(missing_ok=True)
            else:
                common._atomic_write(path, old)
        raise


def _managed_version(backend: str, fields: list[str]) -> str:
    settings = common.load_settings(backend)
    managed = {name: settings[name] for name in fields if name != "api_key"}
    if "api_key" in fields:
        managed["api_key"] = common.load_key(backend)
        # A followed credential must never migrate to a manually changed destination.
        managed["base_url"] = settings["base_url"]
    return hashlib.sha256(json.dumps(managed, sort_keys=True).encode()).hexdigest()


@contextmanager
def _transaction(backend: str):
    """Roll back a synchronous write failure across profile, encrypted key and sync metadata."""
    paths = [common.STATE_DIR / name for name in ("settings.json", backend + ".dpapi", "local-sync.json")]
    previous = [p.read_bytes() if p.exists() else None for p in paths]
    try:
        yield
    except Exception:
        for path, old in zip(paths, previous):
            if old is None:
                path.unlink(missing_ok=True)
            elif not path.exists() or path.read_bytes() != old:
                common._atomic_write(path, old)
        raise


def configure(backend: str, mode: str, fields: list[str], token: str, revision: str, sync_revision: str) -> dict:
    common.check_backend(backend)
    if mode not in ("off", "import", "follow"):
        raise common.RelayError("同步模式无效。")
    if not isinstance(fields, list) or any(not isinstance(f, str) or f not in FIELDS for f in fields) or (mode != "off" and not fields):
        raise common.RelayError("请选择要导入的连接字段。")
    with common.configuration_lock(), _transaction(backend):
        common.require_revision(backend, revision)
        state = _state()
        if sync_revision != state["revision"]:
            raise common.ConfigurationConflict("同步设置已更新，请重新加载后操作。")
        item = {"mode": mode, "fields": fields, "status": "off", "message": "同步已关闭；保留当前插件配置。"}
        if mode != "off":
            preview = _previews.get(token)
            if not preview or preview["expires"] < time.monotonic() or preview["source"]["backend"] != backend:
                raise common.ConfigurationConflict("检测结果已过期，请重新检测。")
            try:
                current = read_source(preview["source"])
            except Exception:
                raise common.RelayError("客户端配置无法读取，请重新检测。") from None
            if current["fingerprint"] != preview["fingerprint"] or current["auth"] != preview["auth"]:
                raise common.ConfigurationConflict("来源在检测后已变化，请重新检测。")
            _apply(backend, current, fields)
            item.update(source=preview["source"], fingerprint=current["fingerprint"],
                        managed_version=_managed_version(backend, fields), status="synced",
                        message="已导入；后续修改将同步到新任务。" if mode == "follow" else "已导入一次；后续独立管理。")
            del _previews[token]
        state["profiles"][backend] = item
        _write(state)
    return status(backend)


def refresh(backend: str):
    with common.configuration_lock(), _transaction(backend):
        state = _state()
        item = state["profiles"].get(backend, {})
        if item.get("mode") != "follow":
            return
        previous = (item.get("status"), item.get("message"), item.get("fingerprint"))
        try:
            changed = item.get("managed_version") != _managed_version(backend, item["fields"])
        except common.RelayError:
            changed = True
        if changed:
            item.update(status="conflict", message="插件配置已手动修改，跟随已暂停。请重新检测并导入，或关闭同步。")
        else:
            try:
                current = read_source(item["source"])
                if current["auth"] != "api_key" and "api_key" in item["fields"]:
                    raise common.RelayError("来源不再含有效 API 凭据。")
                if current["fingerprint"] != item["fingerprint"]:
                    _apply(backend, current, item["fields"])
                    item.update(fingerprint=current["fingerprint"], managed_version=_managed_version(backend, item["fields"]))
                item.update(status="synced", message="正在跟随所选本地配置；仅影响新任务。")
            except Exception:
                item.update(status="blocked", message="来源缺失、格式变化或连接字段不完整，跟随已暂停。请重新检测或关闭同步。")
        if previous != (item.get("status"), item.get("message"), item.get("fingerprint")):
            _write(state)


def require_ready(backend: str):
    refresh(backend)
    current = status(backend)
    if current["mode"] == "follow" and current["status"] != "synced":
        raise common.RelayError(current["message"])
