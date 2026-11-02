"""One native agent per process; credentials enter through stdin, never arguments."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import common
from events import EventWriter
import runtimes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', choices=tuple(common.PROVIDERS), required=True)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--mode', choices=('review', 'edit'), default='review')
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    key = ''
    try:
        payload = json.load(sys.stdin)
        key = payload['key']
        project = args.project.resolve()
        if not args.project.is_absolute() or not project.is_dir():
            raise common.RelayError('项目目录必须为存在的绝对路径。')
        args.run_dir.mkdir(parents=True, exist_ok=True)
        events = EventWriter(args.run_dir)
        events.emit('started', '正在启动运行环境')
        result = asyncio.run(runtimes.run(args.backend, payload['task'], project, args.mode,
                                         payload['profile'], key, args.run_dir, events))
        if not isinstance(result, str) or not result.strip():
            raise common.RelayError('代理未返回可见结果，任务未确认完成。')
        print(json.dumps({'ok': True, 'text': common.redact(result, key)}, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': common.redact(str(exc), key)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stdin.reconfigure(encoding='utf-8')
    raise SystemExit(main())
