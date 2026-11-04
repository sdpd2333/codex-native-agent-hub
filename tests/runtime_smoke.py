"""Real native runtimes + local fake endpoints: read and write tools, no paid API calls."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from events import EventWriter, read_activity
from progress import TranscriptTracker
import app_settings
import runtimes
from server import worker_environment, stop_process

SENTINEL = 'native-tool-sentinel-314159'
WRITTEN = 'native-write-confirmed-271828'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        case = self.server.case
        case['requests'].append(request)
        gemini = case['backend'] == 'gemini'
        tools = ([t for group in request.get('tools', []) for t in group.get('functionDeclarations', [])]
                 if gemini else request.get('tools', []))
        case['tools'] = {t.get('name') for t in tools}
        stage = len(case['requests'])
        first = stage == 1
        command_step = case['mode'] == 'edit' and stage == 2
        if first or command_step:
            wanted = ('view_file' if gemini else 'Read' if case['backend'] == 'claude' else 'read') if case['mode'] == 'review' else (
                'write_to_file' if gemini else 'Write' if case['backend'] == 'claude' else 'write')
            if command_step:
                wanted = 'run_command' if gemini else 'Bash' if case['backend'] == 'claude' else 'pwsh'
            declaration = next((t for t in tools if t.get('name') == wanted), None)
            if declaration is None:
                case['failure'] = 'Expected native tool missing: ' + wanted
                self.send_error(400)
                return
            schema = declaration.get('parametersJsonSchema', declaration.get('parameters', declaration.get('input_schema', {})))
            case['schema'] = schema
            arguments = {}
            command = ('cmd.exe /d /c "echo native-shell-ok>shell.txt"' if gemini else
                       "printf 'native-shell-ok\\n' > shell.txt" if case['backend'] == 'claude' else
                       "[System.IO.File]::WriteAllText((Join-Path (Get-Location) 'shell.txt'), 'native-shell-ok')")
            for name, spec in schema.get('properties', {}).items():
                folded = name.lower().replace('_', '')
                if command_step and folded in ('command', 'commandline', 'code', 'script'):
                    arguments[name] = command
                elif command_step and folded in ('cwd', 'workingdirectory', 'workdir'):
                    arguments[name] = str(case['project'])
                elif any(part in folded for part in ('path', 'filename', 'targetfile')):
                    arguments[name] = str(case['project'] / ('sentinel.txt' if case['mode'] == 'review' else 'written.txt'))
                elif folded in ('content', 'contents', 'code', 'filecontent', 'text', 'rawcontent', 'codecontent'):
                    arguments[name] = WRITTEN
                elif name in schema.get('required', []):
                    arguments[name] = True if spec.get('type', '').lower() == 'boolean' else 1000 if spec.get('type', '').lower() in ('integer', 'number') else 'smoke test'
            case['arguments'] = arguments
            part = {'functionCall': {'name': wanted, 'args': arguments}} if gemini else {
                'type': 'tool_use', 'id': 'tool_smoke_' + str(stage), 'name': wanted, 'input': arguments}
        else:
            # The sentinel is only in the file, so its presence proves native reading occurred.
            case['read_confirmed'] = SENTINEL in json.dumps(request)
            part = {'text': 'NATIVE_OK'} if gemini else {'type': 'text', 'text': 'NATIVE_OK'}
        if gemini:
            response = {'candidates': [{'content': {'role': 'model', 'parts': [part]}, 'finishReason': 'STOP', 'index': 0}],
                        'usageMetadata': {'promptTokenCount': 20, 'candidatesTokenCount': 10, 'totalTokenCount': 30}}
            data = ('data: ' + json.dumps(response) + '\n\n').encode()
        else:
            identity = 'msg_smoke_' + str(len(case['requests']))
            chunks = [('message_start', {'type': 'message_start', 'message': {'id': identity, 'type': 'message',
                'role': 'assistant', 'content': [], 'model': request.get('model'), 'stop_reason': None,
                'stop_sequence': None, 'usage': {'input_tokens': 20, 'output_tokens': 0}}})]
            if first or command_step:
                chunks += [('content_block_start', {'type': 'content_block_start', 'index': 0,
                    'content_block': {**part, 'input': {}}}),
                    ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                    'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(arguments)}})]
            else:
                chunks += [('content_block_start', {'type': 'content_block_start', 'index': 0,
                    'content_block': {'type': 'text', 'text': ''}}),
                    ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                    'delta': {'type': 'text_delta', 'text': 'NATIVE_OK'}})]
            chunks += [('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if first or command_step else 'end_turn',
                    'stop_sequence': None}, 'usage': {'output_tokens': 10}}), ('message_stop', {'type': 'message_stop'})]
            data = ''.join('event: ' + name + '\ndata: ' + json.dumps(row) + '\n\n' for name, row in chunks).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


async def main():
    # Do not accidentally import real credentials or custom endpoints into test runtimes.
    safe = worker_environment()
    os.environ.clear()
    os.environ.update(safe)
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        for backend in (sys.argv[1:] or ('gemini', 'claude', 'deepseek')):
            scenarios = [('review', None), ('edit', None)]
            if backend in ('claude', 'deepseek'):
                scenarios.append(('edit', 1))
            for mode, tool_budget in scenarios:
                with tempfile.TemporaryDirectory(prefix='native-hub-smoke-') as tmp:
                    root = Path(tmp).resolve()
                    if os.name == 'nt':
                        # Python 3.13+ uses an OWNER RIGHTS-only ACL for mkdtemp.
                        # Restore inherited permissions only on this fake-data fixture,
                        # so the native restricted token can use the workspace normally.
                        subprocess.run(['icacls.exe', str(root), '/reset'],
                                       check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                    project, run_dir = root / 'project', root / 'job'
                    project.mkdir()
                    run_dir.mkdir()
                    (project / 'sentinel.txt').write_text(SENTINEL, encoding='utf-8')
                    case = {'backend': backend, 'mode': mode, 'project': project, 'requests': []}
                    http.case = case
                    profile = {'base_url': f'http://127.0.0.1:{http.server_port}', 'model': backend + '-test', 'effort': ''}
                    profile['runtime_settings'] = {name: app_settings.DEFAULTS[name] for name in app_settings.TASK_KEYS}
                    if tool_budget is not None:
                        profile['runtime_settings']['max_tool_calls'] = tool_budget
                    writer = EventWriter(run_dir)
                    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
                    worker = await asyncio.create_subprocess_exec(sys.executable, str(APP / 'agent_runner.py'),
                        '--backend', backend, '--project', str(project), '--mode', mode, '--run-dir', str(run_dir),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        env=safe | {'AGENT_HUB_STATE_DIR': str(root / 'state')}, **options)
                    try:
                        async with asyncio.timeout(90):
                            stdout, stderr = await worker.communicate(json.dumps({'task': 'Read sentinel.txt and report findings.' if mode == 'review'
                                else 'Create written.txt with the supplied test content.', 'profile': profile, 'key': 'fake-local-key'}).encode())
                        response = json.loads(stdout.decode('utf-8'))
                        if tool_budget is not None:
                            assert not response.get('ok') and '工具调用超过 1 次' in response.get('error', ''), (backend, response)
                            assert (project / 'written.txt').read_text(encoding='utf-8').strip() == WRITTEN
                            assert not (project / 'shell.txt').exists(), (backend, 'Over-budget command executed')
                            assert len(case['requests']) == 2, (backend, 'Continued after exhausted budget', len(case['requests']))
                            assert read_activity(writer.path)['tool_calls'] == 1
                            print(json.dumps({'backend': backend, 'mode': mode, 'tool_budget': tool_budget,
                                              'ok': True, 'excess_tool_blocked': True}), flush=True)
                            continue
                        assert worker.returncode == 0 and response.get('ok'), (backend, mode, response, stderr.decode('utf-8', 'replace')[-2000:])
                        result = response.get('text')
                    finally:
                        await stop_process(worker)
                    assert result and 'NATIVE_OK' in result, (backend, mode, result, case.get('failure'))
                    assert len(case['requests']) >= 2, (backend, mode, 'No tool round trip')
                    if mode == 'review':
                        assert case.get('read_confirmed'), (backend, 'Read tool did not return sentinel', case['arguments'], case['requests'][-1].get('messages'))
                        mutators = {'write', 'edit', 'apply_patch', 'bash', 'pwsh', 'run_command', 'create_file', 'edit_file'}
                        assert not mutators.intersection(t.lower() for t in case['tools']), (backend, case['tools'])
                        assert not (project / 'written.txt').exists()
                    else:
                        assert (project / 'written.txt').read_text(encoding='utf-8').strip() == WRITTEN, (backend, case['arguments'])
                        assert (project / 'shell.txt').exists(), (backend, case['arguments'], case['schema'], case['requests'][-1].get('messages', [])[-1:])
                        assert (project / 'shell.txt').read_text(encoding='utf-8-sig').strip() == 'native-shell-ok', (backend, case['arguments'])
                    activity = TranscriptTracker().update(run_dir) if backend == 'gemini' else read_activity(writer.path)
                    assert activity['model_calls'] >= 2, (backend, activity)
                    assert activity['tool_calls'] >= 1, (backend, activity)
                    assert 'fake-local-key' not in json.dumps(activity)
                    assert SENTINEL not in json.dumps(activity)
                    print(json.dumps({'backend': backend, 'mode': mode, 'ok': True, 'model_calls': activity['model_calls'],
                                      'tool_calls': activity['tool_calls'], 'native_tools': sorted(case['tools'])}, ensure_ascii=False), flush=True)
    finally:
        http.shutdown()
        http.server_close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
