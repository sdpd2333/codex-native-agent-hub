"""Native runtime adapters; imports are lazy so diagnostics stay available."""
import importlib
from importlib.metadata import version, PackageNotFoundError

PACKAGES = {"gemini": "google-antigravity", "claude": "claude-agent-sdk", "deepseek": "deepseek-harness-sdk"}


def doctor():
    result = {}
    for backend, package in PACKAGES.items():
        try:
            result[backend] = {"installed": True, "package": package, "version": version(package)}
        except PackageNotFoundError:
            result[backend] = {"installed": False, "package": package}
    return result


async def run(backend, task, project, mode, profile, key, run_dir, events):
    adapter = importlib.import_module("runtimes." + backend)
    return await adapter.run(task, project, mode, profile, key, run_dir, events)
