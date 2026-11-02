"""Content-free progress events shared by native runtimes and the local panel."""
from datetime import datetime, timezone
import json
from pathlib import Path
import threading

LABELS = {
    "read": "读取文件", "view_file": "读取文件", "glob": "查找文件", "find_file": "查找文件",
    "grep": "搜索代码", "grep_search": "搜索代码", "search_dir": "搜索目录", "list_dir": "浏览目录",
    "write": "写入文件", "create_file": "创建文件", "edit": "修改文件", "edit_file": "修改文件",
    "apply_patch": "应用补丁", "bash": "执行命令", "pwsh": "执行命令", "run_command": "执行命令",
    "working_directory": "查看工作目录", "finish": "整理结果",
}


class EventWriter:
    def __init__(self, run_dir: Path):
        self.path = run_dir / "activity.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.models = set()
        self.tools = set()
        self.lock = threading.RLock()

    def emit(self, kind, label):
        with self.lock:
            row = {"timestamp": datetime.now(timezone.utc).isoformat(), "kind": kind, "label": label}
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def model(self, identity):
        with self.lock:
            if identity not in self.models:
                self.models.add(identity)
                self.emit("model", "模型处理任务")
                if len(self.models) > 40:
                    raise RuntimeError("模型调用超过 40 次，已停止此任务。")

    def tool(self, identity, name, *, done=False, failed=False):
        with self.lock:
            if not done:
                if identity in self.tools:
                    return
                self.tools.add(identity)
                if len(self.tools) > 100:
                    raise RuntimeError("工具调用超过 100 次，已停止此任务。")
            label = LABELS.get(str(name).lower(), "使用工具")
            if done:
                label += " · 未成功" if failed else " · 已结束"
            self.emit("tool_end" if done else "tool", label)


def read_activity(path: Path) -> dict:
    """Only normalized fields created by EventWriter are returned to the panel."""
    models = tools = 0
    events = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for number, line in enumerate(stream):
                if number >= 2000 or len(line) > 4096:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                kind = row.get("kind")
                if kind not in ("model", "tool", "tool_end", "started", "finished"):
                    continue
                label = row.get("label")
                valid = {"模型处理任务", "正在启动运行环境", "整理结果"} | set(LABELS.values())
                if not isinstance(label, str) or label.split(" · ")[0] not in valid | {"使用工具"}:
                    continue
                stamp = row.get("timestamp")
                if not isinstance(stamp, str) or len(stamp) > 40:
                    stamp = None
                models += kind == "model"
                tools += kind == "tool"
                events.append({"timestamp": stamp, "label": label})
    except OSError:
        pass
    return {"phase": events[-1]["label"] if events else "正在启动运行环境", "model_calls": models,
            "tool_calls": tools, "budget_model_calls": 40, "events": events[-30:],
            "last_activity_at": events[-1]["timestamp"] if events else None}
