"""Independent provider profiles, Windows credentials and native protocol discovery."""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError, URLError

APP_DIR = Path(__file__).resolve().parent


def default_state_dir() -> Path:
    """Keep credentials and session data outside a checkout by default."""
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".local" / "share"
    return base / "CodexNativeAgentHub"


STATE_DIR = Path(os.environ.get("AGENT_HUB_STATE_DIR", str(default_state_dir()))).expanduser().resolve()
DEFAULT_BASE = ""
PROVIDERS = {
    "gemini": {"label": "Gemini", "runtime": "Antigravity SDK", "protocol": "Gemini native"},
    "claude": {"label": "Claude", "runtime": "Claude Code · Agent SDK", "protocol": "Anthropic Messages"},
    "deepseek": {"label": "DeepSeek", "runtime": "DeepSeek Harness · dsh", "protocol": "DeepSeek Messages"},
}
_settings_lock = threading.RLock()
_settings_local = threading.local()


class RelayError(RuntimeError):
    pass


class ConfigurationConflict(RelayError):
    pass


@contextmanager
def configuration_lock():
    """Serialize short profile transactions across threads AND service processes."""
    with _settings_lock:
        if getattr(_settings_local, "depth", 0):
            yield
            return
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / ".config.lock").open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if not stream.tell():
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
            _settings_local.depth = 1
            try:
                yield
            finally:
                _settings_local.depth = 0
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)


def profile_revision(backend: str) -> str:
    """Opaque version from profile data and credential-file metadata, never key material."""
    check_backend(backend)
    with configuration_lock():
        path = STATE_DIR / "settings.json"
        profile = json.loads(path.read_text(encoding="utf-8")).get("profiles", {}).get(backend, {}) if path.exists() else {}
        try:
            info = (STATE_DIR / (backend + ".dpapi")).stat()
            credential_version = (info.st_ino, info.st_mtime_ns, info.st_size)
        except FileNotFoundError:
            credential_version = None
        version = [profile, credential_version,
                   bool(os.environ.get("AGENT_HUB_" + backend.upper() + "_API_KEY", "").strip())]
        return hashlib.sha256(json.dumps(version, sort_keys=True).encode()).hexdigest()


def require_revision(backend: str, revision: str | None) -> None:
    # Call inside configuration_lock when followed by a write or credential snapshot.
    if not revision or revision != profile_revision(backend):
        raise ConfigurationConflict("配置已更新或缺少版本信息。请重新加载已保存配置，合并本地修改后再保存。")


def check_backend(backend: str) -> str:
    if backend not in PROVIDERS:
        raise RelayError("未知代理。请选择 gemini、claude 或 deepseek。")
    return backend


def normalize_base(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RelayError("Base URL 必须是有效的 HTTPS 地址。")
    if parsed.query or parsed.fragment:
        raise RelayError("Base URL 不应包含查询参数或片段。")
    for suffix in ("/v1beta", "/v1"):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
            break
    return value


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                # Windows readers (including a panel poll) can temporarily deny
                # replacement. Keep the original intact and bound retries to 310 ms.
                if getattr(exc, "winerror", None) not in (5, 32) or attempt == 5:
                    raise
                time.sleep(0.01 * 2 ** attempt)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_settings(backend: str = "gemini") -> dict:
    check_backend(backend)
    path = STATE_DIR / "settings.json"
    if not path.exists():
        return {"base_url": DEFAULT_BASE, "model": "", "effort": "", "auto_select_model": False}
    try:
        with _settings_lock:
            data = json.loads(path.read_text(encoding="utf-8")).get("profiles", {}).get(backend, {})
        base = str(data.get("base_url", ""))
        return {"base_url": normalize_base(base) if base else "", "model": str(data.get("model", "")),
                "effort": str(data.get("effort", "")), "auto_select_model": data.get("auto_select_model") is True}
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
        raise RelayError("本地配置无法读取，请检查状态目录中的 settings.json。") from exc


def validate_profile(base_url: str, model: str, backend: str = "gemini", effort: str = "") -> dict:
    check_backend(backend)
    if not model.strip() or len(model) > 512:
        raise RelayError("请先选择一个模型。")
    allowed = {"gemini": {""}, "claude": {"", "low", "medium", "high", "xhigh", "max"},
               "deepseek": {"", "high", "max"}}
    if effort not in allowed[backend]:
        raise RelayError("该代理不支持此推理强度。")
    return {"base_url": normalize_base(base_url), "model": model.strip(), "effort": effort}


def save_settings(base_url: str, model: str, backend: str = "gemini", effort: str = "",
                  auto_select_model: bool | None = None) -> None:
    payload = validate_profile(base_url, model, backend, effort)
    if auto_select_model is not None and not isinstance(auto_select_model, bool):
        raise RelayError("自动选择适配模型必须为布尔值。")
    with configuration_lock():
        path = STATE_DIR / "settings.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"profiles": {}}
        previous = data.get("profiles", {}).get(backend, {})
        payload["auto_select_model"] = (previous.get("auto_select_model") is True
                                        if auto_select_model is None else auto_select_model)
        payload["revision"] = uuid.uuid4().hex
        data.setdefault("profiles", {})[backend] = payload
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RelayError("此凭据存储使用 Windows DPAPI；其他系统请使用 AGENT_HUB_<代理>_API_KEY 环境变量。")

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    source_buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RelayError("Windows 凭据加密/解密失败。")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel.LocalFree(result.pbData)


def save_key(key: str, backend: str = "gemini") -> None:
    check_backend(backend)
    key = key.strip()
    if not key:
        raise RelayError("API Key 不能为空。")
    with configuration_lock():
        _atomic_write(STATE_DIR / (backend + ".dpapi"), _dpapi(key.encode("utf-8")))


def load_key(backend: str = "gemini") -> str:
    check_backend(backend)
    value = os.environ.get("AGENT_HUB_" + backend.upper() + "_API_KEY", "").strip()
    if value:
        return value
    path = STATE_DIR / (backend + ".dpapi")
    if not path.exists():
        raise RelayError("尚未填写该代理的 API Key。请先在面板连接设置中保存。")
    return _dpapi(path.read_bytes(), decrypt=True).decode("utf-8")


def has_key(backend: str = "gemini") -> bool:
    check_backend(backend)
    return bool(os.environ.get("AGENT_HUB_" + backend.upper() + "_API_KEY", "").strip()) or (STATE_DIR / (backend + ".dpapi")).exists()


def redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return text


class _NoCrossOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urlsplit(req.full_url)
        new = urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise RelayError("中转站发生跨域重定向，已停止发送凭据。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request_json(url: str, key: str, *, native: bool = False, payload: dict | None = None, backend: str = "gemini") -> dict:
    headers = {"Accept": "application/json", "User-Agent": "Codex-Native-Agent-Hub/0.1"}
    headers["x-goog-api-key" if native else "Authorization"] = key if native else "Bearer " + key
    if backend in ("claude", "deepseek"):
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers=headers)
    try:
        with build_opener(_NoCrossOriginRedirect()).open(req, timeout=45) as response:
            # Model catalogs can be large, but never read an unbounded response.
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise RelayError("接口响应超过 8 MB。")
            result = json.loads(raw.decode("utf-8"))
            if not isinstance(result, dict):
                raise RelayError("接口返回的 JSON 格式不正确。")
            return result
    except HTTPError as exc:
        detail = ""
        try:
            parsed = json.loads(exc.read(8192).decode("utf-8", "replace"))
            error = parsed.get("error", {})
            detail = error.get("message", "") if isinstance(error, dict) else str(error)
        except (ValueError, AttributeError):
            pass
        friendly = {401: "API Key 无效或未授权", 403: "此 Key 没有接口或模型权限",
                    404: "接口或模型不存在", 429: "额度不足或请求受到限流"}.get(exc.code, "中转站请求失败")
        detail = redact(str(detail), key)[:400]
        exc.close()
        raise RelayError(f"HTTP {exc.code}：{friendly}。{detail}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise RelayError(redact(f"网络连接失败：{exc}", key)) from None
    except (UnicodeError, ValueError) as exc:
        raise RelayError("接口未返回有效 JSON。请检查 Base URL。") from exc


def fetch_models(base_url: str, key: str, backend: str = "gemini") -> list[str]:
    check_backend(backend)
    base_url = normalize_base(base_url)
    failures = []
    try:
        models = set()
        page = ""
        for _ in range(25):
            suffix = "?" + urlencode({"after_id": page}) if page else ""
            data = request_json(base_url + "/v1/models" + suffix, key, backend=backend)
            models.update(str(item["id"]) for item in data.get("data", []) if isinstance(item, dict) and item.get("id"))
            if backend != "claude" or not data.get("has_more"):
                break
            next_page = data.get("last_id", "")
            if not next_page or next_page == page:
                break
            page = next_page
        if models:
            return sorted(models, key=str.casefold)
        failures.append("/v1/models 没有返回模型列表")
    except RelayError as exc:
        failures.append(str(exc))
    if backend != "gemini":
        raise RelayError("拉取模型失败：\n" + "\n".join(failures))
    try:
        models = set()
        page = ""
        for _ in range(25):
            url = base_url + "/v1beta/models" + ("?" + urlencode({"pageToken": page}) if page else "")
            data = request_json(url, key, native=True)
            for item in data.get("models", []):
                if isinstance(item, dict) and item.get("name"):
                    models.add(str(item["name"]).removeprefix("models/"))
            page = data.get("nextPageToken", "")
            if not page:
                break
        if models:
            return sorted(models, key=str.casefold)
        failures.append("/v1beta/models 没有返回模型列表")
    except RelayError as exc:
        failures.append(str(exc))
    raise RelayError("拉取模型失败：\n" + "\n".join(failures))


def test_native_model(base_url: str, key: str, model: str, backend: str = "gemini") -> str:
    check_backend(backend)
    base = normalize_base(base_url)
    if backend in ("claude", "deepseek"):
        result = request_json(base + "/v1/messages", key, backend=backend,
                              payload={"model": model, "max_tokens": 128, "messages": [{"role": "user", "content": "Reply with OK."}]})
        text = "".join(part.get("text", "") for part in result.get("content", []) if part.get("type") == "text")
    else:
        return _test_gemini(base, key, model)
    if not text.strip():
        raise RelayError("接口返回成功，但没有可见文本；请检查模型、协议或输出额度。")
    return redact(text, key)[:800]


def _test_gemini(base_url: str, key: str, model: str) -> str:
    model = model.removeprefix("models/")
    result = request_json(normalize_base(base_url) + "/v1beta/models/" + quote(model, safe="") + ":generateContent", key,
                          native=True, payload={"contents": [{"role": "user", "parts": [{"text": "Reply with OK."}]}],
                                                "generationConfig": {"maxOutputTokens": 128}})
    parts = [part.get("text", "") for candidate in result.get("candidates", [])
             for part in candidate.get("content", {}).get("parts", []) if not part.get("thought")]
    text = "".join(parts).strip()
    if not text:
        raise RelayError("原生接口返回成功，但没有可见文本；请检查模型权限或换一个 Gemini 模型。")
    return redact(text, key)[:800]
