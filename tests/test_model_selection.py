"""Name heuristics and the opt-in boundary from configuration to immutable tasks."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import common
import model_selection
import progress
import server


class NamePolicyTests(unittest.TestCase):
    def test_simple_task_uses_exact_id_from_the_low_end_of_the_live_ladder(self):
        chosen = model_selection.choose_model(
            ['gemini-4-argon', 'gemini-4-flash', 'models/gemini-3.8-flash', 'gpt-5-mini'],
            'gemini', 'gemini-4-argon', 'simple')
        self.assertEqual(chosen['model'], 'models/gemini-3.8-flash')
        self.assertEqual(chosen['mode'], 'automatic')
        self.assertEqual((chosen['rank'], chosen['rank_count']), (1, 2))
        self.assertTrue(model_selection.describe_model('3.8flash', 'gemini')['eligible'])
        self.assertEqual(model_selection.describe_model('4argon', 'gemini')['tier'], 'strong')

    def test_provider_specific_families_and_ambiguous_names(self):
        cases = [('claude-haiku-4-5', 'claude', 'fast'), ('claude-sonnet-4-6', 'claude', 'balanced'),
                 ('claude-opus-4-6', 'claude', 'strong'), ('claude-fable-5', 'claude', 'strong'),
                 ('deepseek-v4-flash', 'deepseek', 'fast'), ('gemini-4-flash-lite', 'gemini', 'fast'),
                 ('gemini-4-pro', 'gemini', 'strong'), ('gemini-4-ultra', 'gemini', 'strong')]
        for name, backend, tier in cases:
            with self.subTest(name=name):
                self.assertEqual(model_selection.describe_model(name, backend)['tier'], tier)
        for name in ('custom-pro', 'fast-private-alias', 'vendor-model-123'):
            self.assertFalse(model_selection.describe_model(name, 'gemini')['eligible'])

    def test_foreign_and_specialized_models_are_not_routed(self):
        for name in ('claude-haiku-4-5', 'gpt-5-mini', 'gemma-3-pro', 'gemini-3-flash-image',
                     'gemini-2.5-flash-tts', 'gemini-embedding-2', 'gemini-3-live', 'qwen/gemini-flash'):
            with self.subTest(name=name):
                self.assertFalse(model_selection.describe_model(name, 'gemini')['eligible'])

    def test_gemini_matches_relative_positions_in_different_live_catalogs(self):
        catalogs = [
            ['gemini3.8flash', 'gemini4flash', 'gemini4argon'],
            ['models/gemini-7-flash', 'gemini-7-pro', 'gemini-8-flash'],
            ['gemini-3.8-flash', 'gemini-3.10-flash', 'gemini-4-ultra'],
        ]
        for models in catalogs:
            for complexity, expected in zip(('simple', 'standard', 'complex'), models):
                with self.subTest(models=models, complexity=complexity):
                    chosen = model_selection.choose_model(list(reversed(models)), 'gemini', 'saved', complexity)
                    self.assertEqual(chosen['model'], expected)
        models = ['gemini-4-flash', 'gemini-3.8-pro', 'gemini-2.5-flash-20991231']
        self.assertEqual(model_selection.choose_model(models, 'gemini', 'saved', 'standard')['model'], models[1])
        self.assertEqual(model_selection.choose_model(models, 'gemini', 'saved', 'complex')['model'], models[0])
        self.assertEqual(model_selection._version('claude-sonnet-4-6-20261103-1m'), (4, 6, 0))
        self.assertEqual(model_selection._version('gemini-2.5-flash-8b'), (2, 5, 0))

    def test_claude_keeps_tier_preferences_and_uses_newest_within_the_tier(self):
        models = ['claude-haiku-5-preview', 'claude-haiku-4-5', 'vendor/claude-haiku-4-5', 'claude-opus-6']
        self.assertEqual(model_selection.choose_model(models, 'claude', models[2], 'simple')['model'], models[0])
        self.assertEqual(model_selection.choose_model([models[0]], 'claude', 'old', 'simple')['model'], models[0])
        models += ['claude-haiku-5', 'vendor/claude-haiku-5']
        self.assertEqual(model_selection.choose_model(models, 'claude', models[-1], 'simple')['model'], models[-1])
        self.assertEqual(model_selection.choose_model(
            ['claude-sonnet-5', 'claude-sonnet-4-6', 'claude-haiku-6'], 'claude', 'saved', 'standard')['model'],
            'claude-sonnet-5')
        self.assertEqual(model_selection.choose_model(
            ['claude-fable-5', 'claude-opus-6'], 'claude', 'saved', 'complex')['model'], 'claude-opus-6')

    def test_gemini_group_ties_prefer_stable_then_saved_model(self):
        models = ['gemini-4-flash-preview', 'gemini-3.8-flash']
        self.assertEqual(model_selection.choose_model(models, 'gemini', models[1], 'complex')['model'], models[0])
        models.append('gemini-4-flash')
        self.assertEqual(model_selection.choose_model(models, 'gemini', models[0], 'complex')['model'], models[2])
        models.append('models/gemini-4-flash')
        chosen = model_selection.choose_model(models, 'gemini', models[3], 'complex')
        self.assertEqual(chosen['model'], models[3])
        self.assertEqual(chosen['rank_count'], 2)

    def test_gemini_two_groups_use_lower_middle_and_single_group_is_available_for_all_tasks(self):
        models = ['gemini-3.8-flash', 'gemini-4-argon']
        for complexity, expected in [('simple', models[0]), ('standard', models[0]), ('complex', models[1])]:
            self.assertEqual(model_selection.choose_model(models, 'gemini', 'saved', complexity)['model'], expected)
        for complexity in model_selection.COMPLEXITIES:
            chosen = model_selection.choose_model([models[0]], 'gemini', 'saved', complexity)
            self.assertEqual(chosen['model'], models[0])
            self.assertEqual(chosen['mode'], 'automatic')

    def test_gemini_aliases_and_previews_do_not_change_the_middle_position(self):
        models = ['gemini-3.8-flash', 'models/gemini-3.8-flash', 'gemini-3.8-flash-preview',
                  'gemini-4-flash', 'gemini-4-flash-exp', 'gemini-4-pro', 'gemini-4-argon',
                  'gemini-5-flash-image', 'claude-opus-9']
        chosen = model_selection.choose_model(models, 'gemini', 'saved', 'standard')
        self.assertEqual(chosen['model'], 'gemini-4-flash')
        self.assertEqual(chosen['rank_count'], 3)

    def test_gemini_matching_positions_are_customizable(self):
        with patch.dict(model_selection.COMPLEXITY_POSITIONS, {'standard': 1.0}):
            chosen = model_selection.choose_model(
                ['gemini-3.8-flash', 'gemini-4-argon'], 'gemini', 'saved', 'standard')
        self.assertEqual(chosen['model'], 'gemini-4-argon')

    def test_gemini_handles_unversioned_aliases_without_inventing_versions(self):
        models = ['gemini-flash-latest', 'gemini-4-pro', 'gemini-5-flash-image']
        self.assertEqual(model_selection.choose_model(models, 'gemini', models[0], 'simple')['model'], models[0])
        models = ['gemini-flash-latest', 'gemini-pro-latest']
        self.assertEqual(model_selection.choose_model(models, 'gemini', models[1], 'simple')['model'], models[0])
        self.assertEqual(model_selection.choose_model(models, 'gemini', models[0], 'complex')['model'], models[1])

    def test_deepseek_only_routes_flash_for_every_complexity(self):
        models = ['deepseek-v5-pro', 'deepseek-v4-flash', 'deepseek-v3.2', 'deepseek-r1', 'deepseek-chat',
                  'deepseek-lite', 'deepseek-reasoner', 'deepseek-v5-flash-preview']
        for model in models:
            self.assertEqual(model_selection.describe_model(model, 'deepseek')['eligible'], 'flash' in model)
        for complexity in model_selection.COMPLEXITIES:
            with self.subTest(complexity=complexity):
                chosen = model_selection.choose_model(models, 'deepseek', models[0], complexity)
                self.assertEqual(chosen['model'], 'deepseek-v4-flash')
                self.assertEqual(chosen['tier'], 'fast')
        fallback = model_selection.choose_model(['deepseek-v5-pro'], 'deepseek', 'deepseek-v5-pro', 'simple')
        self.assertEqual(fallback['mode'], 'fallback')

    def test_policy_exposes_provider_specific_order_and_customization(self):
        for backend in ('gemini', 'claude', 'deepseek'):
            policy = model_selection.policy(True, backend)
            self.assertEqual(policy['backend'], backend)
            self.assertEqual(policy['version_first'], backend == 'gemini')
            self.assertEqual(policy['latest_in_tier'], backend == 'claude')
            self.assertEqual(policy['tier_order'], model_selection.PREFERENCES[backend])
            self.assertEqual(policy['rank_positions'],
                             model_selection.COMPLEXITY_POSITIONS if backend == 'gemini' else {})
            for setting in ('VERSION_FIRST', 'LATEST_IN_TIER', 'TIER_ORDER', 'COMPLEXITY_POSITIONS'):
                self.assertIn(setting, policy['customization'])
        for model in ('gemini-nano', 'gemini-4-lite'):
            self.assertFalse(model_selection.describe_model(model, 'gemini')['eligible'])

    def test_empty_and_unknown_catalogs_fall_back(self):
        for models in ([], ['private-alias']):
            chosen = model_selection.choose_model(models, 'gemini', 'private-default', 'complex')
            self.assertEqual(chosen['model'], 'private-default')
            self.assertEqual(chosen['mode'], 'fallback')

    def test_simple_prefers_basic_tier_over_an_older_flagship(self):
        models = ['gemini-3-pro', 'gemini-4-flash', 'gemini-4-argon']
        self.assertEqual(model_selection.choose_model(models, 'gemini', models[-1], 'simple')['model'], models[1])
        # Actual complex work still follows the full capability ladder.
        self.assertEqual(model_selection.choose_model(models, 'gemini', models[1], 'complex')['model'], models[-1])

    def test_catalog_ids_are_not_rewritten_and_namespace_numbers_are_not_versions(self):
        for model in (' gemini-3-flash', 'gemini-3-flash\n', 'gemini-flash-' + 'x' * 512):
            self.assertFalse(model_selection.describe_model(model, 'gemini')['eligible'])
        models = ['vendor-99/gemini-3.8-flash', 'vendor-1/gemini-4-flash']
        chosen = model_selection.choose_model(models, 'gemini', models[0], 'complex')
        self.assertEqual(chosen['model'], models[1])
        self.assertEqual(model_selection.describe_model('lite-relay/gemini-4-pro', 'gemini')['tier'], 'strong')


class AutomaticSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hub-routing-')
        self.state = patch.object(common, 'STATE_DIR', Path(self.temp.name))
        self.env = patch.dict(os.environ, {'AGENT_HUB_' + name.upper() + '_API_KEY': 'routing-fixture'
                                          for name in common.PROVIDERS})
        self.state.start()
        self.env.start()
        server.JOBS.clear()
        self.payloads = []

        async def finish(job, task, timeout, profile, key, workspace):
            self.payloads.append(profile.copy())
            job.update(status='completed', result='finished')
            progress.save_job(job)

        self.worker = patch.object(server, 'execute', new=finish)
        self.worker.start()
        common.save_settings('https://relay.example.com', 'gemini-4-argon')

    async def asyncTearDown(self):
        for job in list(server.JOBS.values()):
            if not job['future'].done():
                await server.cancel_job(job['job_id'])
        if server.PANEL:
            await asyncio.to_thread(server.PANEL.close)
            server.PANEL = None
        self.worker.stop()
        self.env.stop()
        self.state.stop()
        self.temp.cleanup()

    def enable(self, backend='gemini', model='gemini-4-argon', effort=''):
        common.save_settings('https://relay.example.com', model, backend, effort, auto_select_model=True)

    async def submit(self, **kwargs):
        job = await server.submit_task('Make the requested change', str(APP), **kwargs)
        return await server.get_job(job['job_id'], wait_seconds=1)

    async def test_old_profiles_default_off_and_settings_are_independent(self):
        path = common.STATE_DIR / 'settings.json'
        path.write_text(json.dumps({'profiles': {'gemini': {'base_url': 'https://relay.example.com',
                                                            'model': 'gemini-4-argon'}}}))
        self.assertFalse(common.load_settings()['auto_select_model'])
        self.enable()
        config = server.get_configuration()
        self.assertTrue(config['auto_select_model'])
        self.assertTrue(config['model_selection_policy']['enabled'])
        self.assertFalse(config['profiles']['claude']['auto_select_model'])
        self.assertFalse(config['profiles']['deepseek']['auto_select_model'])
        common.save_settings('https://relay.example.com', 'gemini-4-pro')
        self.assertTrue(common.load_settings()['auto_select_model'])

    async def test_disabled_uses_default_without_network_or_complexity(self):
        with patch.object(common, 'fetch_models') as fetch:
            job = await self.submit(task_complexity='simple')
        fetch.assert_not_called()
        self.assertEqual(job['model'], 'gemini-4-argon')
        self.assertEqual(job['model_selection']['mode'], 'default')

    async def test_automatic_model_is_temporary_visible_and_persisted(self):
        self.enable()
        with patch.object(common, 'fetch_models', return_value=['gemini-4-argon', 'gemini-3.8-flash']):
            job = await self.submit(task_complexity='simple')
        self.assertEqual(job['model'], 'gemini-3.8-flash')
        self.assertEqual(self.payloads[-1]['model'], job['model'])
        self.assertEqual(common.load_settings()['model'], 'gemini-4-argon')
        self.assertEqual(progress.load_job(job['job_id'])['model_selection']['mode'], 'automatic')
        self.assertEqual(progress.list_jobs()[0]['model_selection']['complexity'], 'simple')
        self.assertNotIn('routing-fixture', json.dumps(progress.load_job(job['job_id'])))

    async def test_each_task_adapts_to_the_latest_catalog(self):
        self.enable()
        with patch.object(common, 'fetch_models', side_effect=[
            ['gemini-3.8-flash', 'gemini-4-argon'],
            ['gemini-3.8-flash', 'gemini-4-flash', 'gemini-4-argon'],
        ]) as fetch:
            first = await self.submit(task_complexity='standard')
            second = await self.submit(task_complexity='standard')
        self.assertEqual(first['model'], 'gemini-3.8-flash')
        self.assertEqual(second['model'], 'gemini-4-flash')
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(common.load_settings()['model'], 'gemini-4-argon')

    async def test_explicit_model_wins_with_either_setting(self):
        for enabled in (False, True):
            common.save_settings('https://relay.example.com', 'gemini-4-argon', auto_select_model=enabled)
            with patch.object(common, 'fetch_models') as fetch:
                job = await self.submit(model='my-explicit-alias', model_override_reason='用户要求本次使用 my-explicit-alias')
            self.assertEqual(job['model'], 'my-explicit-alias')
            self.assertEqual(job['model_selection']['mode'], 'explicit')
            fetch.assert_not_called()

    async def test_copying_a_saved_model_cannot_silently_bypass_routing(self):
        self.enable()
        prompt = '这是原生 SDK 调用连通性测试。不要读取文件、运行命令、调用工具或修改文件。最终回复必须严格为两个大写英文字母：OK。'
        with patch.object(common, 'fetch_models') as fetch:
            with self.assertRaisesRegex(common.RelayError, 'model_override_reason'):
                await server.submit_task(prompt, str(APP), model='gemini-4-argon')
        fetch.assert_not_called()
        self.assertFalse(server.JOBS)
        for invalid in ('', ' ' * 10, True, 'x' * 301):
            with self.assertRaisesRegex(common.RelayError, 'model_override_reason'):
                await self.submit(model='gemini-4-argon', model_override_reason=invalid)
        with self.assertRaisesRegex(common.RelayError, 'model_override_reason'):
            await self.submit(model_override_reason='no model')

    async def test_connectivity_uses_basic_models_for_all_backends_without_changing_defaults(self):
        cases = [
            ('gemini', 'gemini-4-argon', 'gemini-3.8-flash'),
            ('claude', 'claude-sonnet-5-5', 'claude-haiku-4-5'),
            ('deepseek', 'deepseek-v4-pro', 'deepseek-v4-flash'),
        ]
        for backend, saved, basic in cases:
            self.enable(backend, saved)
            for complexity in (None, 'complex'):
                with patch.object(common, 'fetch_models', return_value=[saved, basic]):
                    job = await self.submit(backend=backend, task_kind='connectivity', task_complexity=complexity)
                self.assertEqual(job['model'], basic)
                self.assertEqual(job['model_selection']['complexity'], 'simple')
                self.assertEqual(job['task_kind'], 'connectivity')
                self.assertEqual(job['title'], '原生 SDK 连通性检测')
                self.assertEqual(self.payloads[-1]['model'], basic)
                self.assertEqual(common.load_settings(backend)['model'], saved)

    async def test_connectivity_honors_explicit_user_model_and_disabled_automation(self):
        self.enable()
        with patch.object(common, 'fetch_models') as fetch:
            job = await self.submit(task_kind='connectivity', model='gemini-4-argon',
                                    model_override_reason='用户要求测试 gemini-4-argon routing-fixture')
        fetch.assert_not_called()
        self.assertEqual(job['model_selection']['mode'], 'explicit')
        self.assertNotIn('routing-fixture', json.dumps(progress.load_job(job['job_id'])))
        common.save_settings('https://relay.example.com', 'gemini-4-argon', auto_select_model=False)
        with patch.object(common, 'fetch_models') as fetch:
            job = await self.submit(task_kind='connectivity')
        fetch.assert_not_called()
        self.assertEqual(job['model_selection']['mode'], 'default')

    async def test_connectivity_cannot_be_an_edit_and_general_debugging_is_not_downgraded(self):
        with self.assertRaisesRegex(common.RelayError, 'mode=review'):
            await self.submit(task_kind='connectivity', mode='edit')
        self.enable()
        with patch.object(common, 'fetch_models', return_value=['gemini-3.8-flash', 'gemini-4-argon']):
            job = await server.submit_task('Debug why the connectivity check never replies OK', str(APP),
                                           task_complexity='complex')
            job = await server.get_job(job['job_id'], wait_seconds=1)
        self.assertEqual(job['model'], 'gemini-4-argon')
        self.assertEqual(job['model_selection']['complexity'], 'complex')

    async def test_panel_manual_model_is_explicit_but_automatic_connectivity_is_simple(self):
        self.enable()
        payload = {'task': 'Only reply OK', 'project_dir': str(APP), 'task_kind': 'connectivity',
                   'task_complexity': 'complex', 'title': '检查连接'}
        with patch.object(common, 'fetch_models', return_value=['gemini-3.8-flash', 'gemini-4-argon']):
            job = await server.panel_dispatch('POST', 'tasks', payload)
            job = await server.get_job(job['job_id'], wait_seconds=1)
        self.assertEqual(job['model'], 'gemini-3.8-flash')
        self.assertEqual(job['title'], payload['title'])
        with patch.object(common, 'fetch_models') as fetch:
            manual = await server.panel_dispatch('POST', 'tasks', {**payload, 'model': 'gemini-4-argon'})
            await server.get_job(manual['job_id'], wait_seconds=1)
        fetch.assert_not_called()
        self.assertEqual(manual['model_selection']['mode'], 'explicit')
        self.assertIn('手动选择', manual['model_selection']['override_reason'])

    async def test_enabled_requires_codex_judgment_before_network_or_job(self):
        self.enable()
        with patch.object(common, 'fetch_models') as fetch:
            with self.assertRaisesRegex(common.RelayError, 'task_complexity'):
                await self.submit()
        self.assertFalse(server.JOBS)
        fetch.assert_not_called()

    async def test_failed_catalog_falls_back_without_leaking_upstream_error(self):
        self.enable()
        for error in (common.RelayError('routing-fixture'), RuntimeError('routing-fixture')):
            with patch.object(common, 'fetch_models', side_effect=error):
                job = await self.submit(task_complexity='simple')
            self.assertEqual(job['model'], 'gemini-4-argon')
            self.assertEqual(job['model_selection']['mode'], 'fallback')
            self.assertNotIn('routing-fixture', json.dumps(job))

    async def test_claude_selection_preserves_configured_effort(self):
        self.enable('claude', 'claude-opus-4-6', 'high')
        with patch.object(common, 'fetch_models', return_value=['claude-opus-4-6', 'claude-sonnet-4-6']):
            job = await self.submit(backend='claude', task_complexity='standard')
        self.assertEqual(job['model'], 'claude-sonnet-4-6')
        self.assertEqual(self.payloads[-1]['effort'], 'high')
        self.assertEqual(common.load_settings('claude')['model'], 'claude-opus-4-6')

    async def test_invalid_flag_does_not_replace_saved_credentials(self):
        os.environ['AGENT_HUB_GEMINI_API_KEY'] = ''
        common.save_key('old-fixture')
        with self.assertRaises(common.RelayError):
            await server.panel_dispatch('POST', 'config', {'base_url': 'https://relay.example.com',
                'model': 'gemini-4-argon', 'key': 'new-fixture', 'auto_select_model': 'false'})
        self.assertEqual(common.load_key(), 'old-fixture')
        for value in ('simple-ish', [], True):
            with self.assertRaises(common.RelayError):
                await self.submit(task_complexity=value)

    async def test_select_model_preserves_automatic_setting(self):
        self.enable()
        with patch.object(common, 'fetch_models', return_value=['gemini-4-pro']):
            await server.select_model('gemini-4-pro')
        self.assertTrue(common.load_settings()['auto_select_model'])

    async def test_parallel_catalog_requests_cannot_overbook_task_limit(self):
        self.enable()
        ready = threading.Barrier(3)
        release = asyncio.Event()

        def fetch(*_args):
            ready.wait(timeout=5)
            return ['gemini-3.8-flash']

        async def hold(*_args):
            await release.wait()

        with patch.object(common, 'fetch_models', side_effect=fetch), patch.object(server, 'execute', new=hold):
            results = await asyncio.gather(*(server.submit_task('review', str(APP), task_complexity='simple')
                                            for _ in range(3)), return_exceptions=True)
            self.assertEqual(sum(isinstance(item, dict) for item in results), 2)
            self.assertEqual(sum(isinstance(item, common.RelayError) for item in results), 1)
            release.set()
            await asyncio.gather(*(job['future'] for job in server.JOBS.values()))


if __name__ == '__main__':
    unittest.main()
