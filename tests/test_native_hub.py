"""Profile isolation, native permissions, immutable jobs and reviewable edits."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import common
import app_settings
from events import EventWriter, read_activity
import progress
import server
import workspaces
from runtimes.claude import make_options


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hub-test-')
        self.state = patch.object(common, 'STATE_DIR', Path(self.temp.name))
        self.environment = patch.dict(os.environ, {'AGENT_HUB_' + b.upper() + '_API_KEY': '' for b in common.PROVIDERS})
        self.state.start()
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.state.stop()
        self.temp.cleanup()

    def test_profiles_and_credentials_are_independent(self):
        for backend in common.PROVIDERS:
            common.save_settings('https://' + backend + '.example.com', backend + '-exact-ID', backend)
            common.save_key('fixture-' + backend, backend)
        for backend in common.PROVIDERS:
            self.assertEqual(common.load_key(backend), 'fixture-' + backend)
            self.assertEqual(common.load_settings(backend)['model'], backend + '-exact-ID')
        public = json.dumps(server.get_configuration())
        self.assertNotIn('fixture-', public)

    def test_concurrent_capacity_configuration_is_bounded_and_defaults_to_two(self):
        with patch.dict(os.environ):
            os.environ.pop('AGENT_HUB_MAX_CONCURRENT_TASKS', None)
            self.assertEqual(server.max_concurrent_tasks(), 2)
            for value in ('1', '3', '8'):
                os.environ['AGENT_HUB_MAX_CONCURRENT_TASKS'] = value
                self.assertEqual(server.get_configuration()['max_concurrent_tasks'], int(value))
            for value in ('', '0', '9', '-1', '3.5', 'invalid'):
                os.environ['AGENT_HUB_MAX_CONCURRENT_TASKS'] = value
                with self.assertRaises(common.RelayError):
                    server.max_concurrent_tasks()

    def test_no_reuse_between_providers_or_changed_origins(self):
        common.save_settings('https://one.example.com', 'gemini-exact')
        common.save_key('fixture-gemini')
        with self.assertRaises(common.RelayError):
            server.panel_credentials({'base_url': 'https://two.example.com'}, 'gemini')
        with self.assertRaises(common.RelayError):
            server.panel_credentials({'base_url': 'https://one.example.com'}, 'claude')

    def test_deepseek_and_claude_test_the_messages_protocol(self):
        for backend in ('claude', 'deepseek'):
            with patch.object(common, 'request_json', return_value={'content': [{'type': 'text', 'text': 'OK'}]}) as req:
                self.assertEqual(common.test_native_model('https://relay.example.com/v1', 'fixture', 'model', backend), 'OK')
                self.assertEqual(req.call_args.args[0], 'https://relay.example.com/v1/messages')
                self.assertEqual(req.call_args.kwargs['backend'], backend)

    def test_model_pagination_preserves_exact_claude_ids(self):
        with patch.object(common, 'request_json', side_effect=[
            {'data': [{'id': 'vendor-A'}], 'has_more': True, 'last_id': 'vendor-A'},
            {'data': [{'id': 'vendor-B'}], 'has_more': False}]) as req:
            self.assertEqual(common.fetch_models('https://relay.example.com', 'fixture', 'claude'), ['vendor-A', 'vendor-B'])
            self.assertIn('after_id=vendor-A', req.call_args.args[0])

    def test_no_provider_keys_or_endpoints_in_worker_environment(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fixture-a', 'AGENT_HUB_CLAUDE_API_KEY': 'fixture-b',
                                    'DEEPSEEK_API_KEY': 'fixture-c', 'DSH_HOME': 'private', 'ANTHROPIC_BASE_URL': 'private'}):
            env = server.worker_environment()
        for name in ('ANTHROPIC_API_KEY', 'AGENT_HUB_CLAUDE_API_KEY', 'DEEPSEEK_API_KEY', 'DSH_HOME', 'ANTHROPIC_BASE_URL'):
            self.assertNotIn(name, env)

    def test_activity_contains_only_static_metadata(self):
        writer = EventWriter(Path(self.temp.name))
        writer.model('private-model-call-id')
        writer.tool('private-tool-id', 'Read')
        writer.tool('private-tool-id', 'Read')
        writer.tool('private-tool-id', 'Read', done=True)
        content = writer.path.read_text(encoding='utf-8')
        activity = read_activity(writer.path)
        self.assertNotIn('private-', content)
        self.assertEqual((activity['model_calls'], activity['tool_calls']), (1, 1))
        self.assertIn('已结束', activity['events'][-1]['label'])


class JobIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hub-test-')
        self.state = patch.object(common, 'STATE_DIR', Path(self.temp.name))
        self.environment = patch.dict(os.environ, {'AGENT_HUB_CLAUDE_API_KEY': 'original-fixture'})
        self.state.start()
        self.environment.start()
        server.JOBS.clear()
        common.save_settings('https://original.example.com', 'original-model', 'claude', 'high')

    async def asyncTearDown(self):
        for job in list(server.JOBS.values()):
            if not job['future'].done():
                await server.cancel_job(job['job_id'])
        if server.PANEL:
            await asyncio.to_thread(server.PANEL.close)
            server.PANEL = None
        self.environment.stop()
        self.state.stop()
        self.temp.cleanup()

    async def test_submitted_profile_is_immutable_and_key_is_only_stdin(self):
        payloads, commands = [], []
        class Process:
            returncode = 0
            async def communicate(self, payload):
                payloads.append(json.loads(payload))
                return b'{"ok":true,"text":"finished"}', b''
        async def create(*args, **kwargs):
            commands.append((args, kwargs))
            return Process()
        with patch.object(asyncio, 'create_subprocess_exec', side_effect=create):
            title = 'Review source and security boundaries'
            description = 'Review ' + 'the project source and security boundaries ' * 8 + '\nDetailed instructions.'
            job = await server.submit_task(description, str(APP), backend='claude', title=title)
            common.save_settings('https://changed.example.com', 'changed-model', 'claude')
            os.environ['AGENT_HUB_CLAUDE_API_KEY'] = 'changed-fixture'
            result = await server.get_job(job['job_id'], wait_seconds=2)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['title'], title)
        self.assertEqual(progress.load_job(job['job_id'])['title'], title)
        self.assertEqual(result['task_description'], description)
        self.assertEqual(progress.load_job(job['job_id'])['task_description'], description)
        self.assertEqual(payloads[0]['task'], description)
        self.assertEqual(payloads[0]['profile'], {'base_url': 'https://original.example.com', 'model': 'original-model', 'effort': 'high',
                                                'runtime_settings': app_settings.task_values()})
        self.assertEqual(payloads[0]['key'], 'original-fixture')
        self.assertNotIn('original-fixture', str(commands))
        self.assertNotIn('original-fixture', (progress.job_dir(job['job_id']) / 'job.json').read_text(encoding='utf-8'))

    async def test_invalid_configuration_does_not_replace_saved_key(self):
        common.save_key('old-key', 'deepseek')
        common.save_settings('https://old.example.com', 'old-model', 'deepseek')
        with self.assertRaises(common.RelayError):
            await server.panel_dispatch('POST', 'config', {'backend': 'deepseek', 'base_url': 'https://new.example.com',
                'key': 'new-key', 'model': '', 'effort': ''})
        self.assertEqual(common.load_key('deepseek'), 'old-key')
        self.assertEqual(common.load_settings('deepseek')['base_url'], 'https://old.example.com')

    async def test_timeout_stops_worker_and_persists_failure(self):
        class Process:
            returncode = None
            async def communicate(self, payload):
                raise TimeoutError()
        with patch.object(asyncio, 'create_subprocess_exec', new=AsyncMock(return_value=Process())), \
             patch.object(server, 'stop_process', new=AsyncMock()) as stop:
            job = await server.submit_task('review', str(APP), backend='claude')
            result = await server.get_job(job['job_id'], wait_seconds=2)
        self.assertEqual(result['status'], 'timed_out')
        stop.assert_awaited_once()
        self.assertEqual(progress.load_job(job['job_id'])['status'], 'timed_out')

    async def test_failed_initial_record_write_does_not_reserve_a_task_slot(self):
        with patch.object(progress, 'save_job', side_effect=PermissionError('fixture write failure')):
            for _ in range(2):
                with self.assertRaises(PermissionError):
                    await server.submit_task('review', str(APP), backend='claude')
                self.assertFalse(server.JOBS)

        async def finish(job, *_args):
            job.update(status='completed', result='OK')
            progress.save_job(job)

        with patch.object(server, 'execute', new=finish):
            job = await server.submit_task('review', str(APP), backend='claude')
            self.assertEqual((await server.get_job(job['job_id'], wait_seconds=1))['status'], 'completed')

    async def test_claude_denies_write_and_paths_outside_review_workspace(self):
        root = Path(self.temp.name)
        options = make_options(root, 'review', {'base_url': 'https://relay.example.com', 'model': 'test'},
                               'fake', root, EventWriter(root))
        before = options.hooks['PreToolUse'][0].hooks[0]
        for tool, values in [('Write', {'file_path': str(root / 'new.txt')}),
                             ('Read', {'file_path': str(root.parent / 'outside.txt')})]:
            reply = await before({'tool_name': tool, 'tool_input': values}, 'test-call', {})
            self.assertEqual(reply['hookSpecificOutput']['permissionDecision'], 'deny')


class WorktreeTests(unittest.TestCase):
    def test_edits_are_separate_and_untracked_files_are_reviewable(self):
        with tempfile.TemporaryDirectory(prefix='hub-worktree-') as tmp:
            root = Path(tmp).resolve()
            project = root / 'source'
            project.mkdir()
            with patch.object(common, 'STATE_DIR', root / 'state'):
                workspaces.git(project, 'init', '-b', 'main')
                (project / 'file.txt').write_text('original\n')
                workspaces.git(project, 'add', 'file.txt')
                workspaces.git(project, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-m', 'fixture')
                first = workspaces.prepare(project, '1' * 16, 'claude', 'edit', 'worktree')
                second = workspaces.prepare(project, '2' * 16, 'deepseek', 'edit', 'worktree')
                assigned = Path(first['path'])
                (assigned / 'file.txt').write_text('modified\n')
                (assigned / 'new.txt').write_text('new\n')
                self.assertEqual((project / 'file.txt').read_text(), 'original\n')
                self.assertEqual((Path(second['path']) / 'file.txt').read_text(), 'original\n')
                diff = workspaces.changes(first)
                self.assertIn('+modified', diff['diff'])
                self.assertEqual(diff['untracked_files'], ['new.txt'])
                (project / 'file.txt').write_text('user change\n')
                with self.assertRaises(common.RelayError):
                    workspaces.prepare(project, '3' * 16, 'gemini', 'edit', 'worktree')
                self.assertEqual((project / 'file.txt').read_text(), 'user change\n')


if __name__ == '__main__':
    unittest.main()
