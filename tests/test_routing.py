"""Cross-runtime selection must enforce rules before native side effects."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import common
import progress
import routing
import server


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.object(common, 'STATE_DIR', self.root / 'state'))
        self.enterContext(patch.dict(os.environ, {'AGENT_HUB_' + b.upper() + '_API_KEY': b + '-credential-fixture' for b in common.PROVIDERS}))
        self.enterContext(patch.object(routing.runtimes, 'doctor', return_value={b: {'installed': True} for b in common.PROVIDERS}))
        self.models = {'claude': ['claude-sonnet-fixture', 'claude-opus-fixture'], 'gemini': ['gemini-flash-fixture'],
                       'deepseek': ['deepseek-v4-pro', 'deepseek-v4-flash']}
        for b, models in self.models.items():
            common.save_settings('https://relay.example.com', models[0], b)
        self.enterContext(patch.object(common, 'fetch_models', side_effect=lambda base, key, b: self.models[b]))
        self.panel = type('Panel', (), {'url': 'http://127.0.0.1/fixture/', 'register_context': lambda *args: 'http://127.0.0.1/fixture/'})()
        self.enterContext(patch.object(server, 'ensure_panel', return_value=self.panel))
        self.executed = []

        async def execute(job, task, timeout, profile, key, workspace):
            self.executed.append((job['backend'], profile.copy(), key))
            job.update(status='completed', result='done')
            progress.save_job(job)

        self.enterContext(patch.object(server, 'execute', new=execute))
        server.JOBS.clear()
        routing._contexts.clear()
        self.claude = routing.candidate_id('claude', self.models['claude'][0])
        self.opus = routing.candidate_id('claude', self.models['claude'][1])
        self.gemini = routing.candidate_id('gemini', self.models['gemini'][0])
        self.deepseek = routing.candidate_id('deepseek', self.models['deepseek'][0])

    async def asyncTearDown(self):
        await asyncio.gather(*(job['future'] for job in server.JOBS.values()), return_exceptions=True)
        server.JOBS.clear()

    def rule(self, identifier='first', pool=None, **kwargs):
        return {'id': identifier, 'name': identifier, 'candidates': pool or [self.claude], **kwargs}

    def save(self, rules=None, fallback=None):
        return routing.save({'enabled': True, 'rules': rules or [], 'fallback': fallback or []}, routing.policy()['revision'])

    async def context(self, **kwargs):
        return await server.get_routing_context('Review this project', str(self.root), **kwargs)

    async def submit(self, ctx, **decision):
        job = await server.submit_task('Review this project', str(self.root), task_complexity='standard',
                                       routing_decision={'token': ctx['token'], **decision})
        return await server.get_job(job['job_id'], wait_seconds=1)

    async def test_first_match_and_all_hard_conditions_are_enforced(self):
        self.save([self.rule('edit', conditions={'modes': ['edit']}),
                   self.rule('complex', conditions={'complexities': ['complex']}),
                   self.rule('project', project_dir=str(self.root / 'child')),
                   self.rule('intent', [self.deepseek], intent='large refactoring'),
                   self.rule('general', [self.gemini])])
        ctx = await self.context()
        self.assertEqual([r['id'] for r in ctx['rules']], ['intent', 'general'])
        selected = server.preview_routing(ctx['token'])
        self.assertEqual(selected['candidate_id'], self.gemini)
        selected = server.preview_routing(ctx['token'], ['intent'])
        self.assertEqual(selected['candidate_id'], self.deepseek)
        with self.assertRaises(common.RelayError):
            server.preview_routing(ctx['token'], ['edit'])
        with self.assertRaises(common.RelayError):
            server.preview_routing(ctx['token'], ['intent'], self.gemini)

    async def test_rules_choose_native_runtime_and_freeze_configuration(self):
        self.save([self.rule(pool=[self.deepseek], effort='high')])
        ctx = await self.context()
        self.assertNotIn('credential-fixture', json.dumps(ctx))
        job = await self.submit(ctx, candidate_id=self.deepseek, reason='fast enough deepseek-credential-fixture')
        self.assertEqual(job['backend'], 'deepseek')
        self.assertEqual(job['model'], 'deepseek-v4-pro')
        self.assertEqual(self.executed[0][1]['effort'], 'high')
        self.assertEqual(job['model_selection']['rule_id'], 'first')
        self.assertNotIn('credential-fixture', json.dumps(progress.load_job(job['job_id'])))
        self.assertNotIn('key', progress.load_job(job['job_id']))
        common.save_settings('https://changed.example.com', 'deepseek-other', 'deepseek')
        self.assertEqual(self.executed[0][1]['base_url'], 'https://relay.example.com')
        with self.assertRaises(common.ConfigurationConflict):
            await self.submit(ctx)

    async def test_current_model_list_includes_pro_and_excludes_wrong_family_or_specialist(self):
        self.models['deepseek'] += ['gpt-unknown', 'deepseek-image', 'relay-alias']
        listing = await routing.catalog()
        models = [c['model'] for c in listing['candidates'] if c['backend'] == 'deepseek']
        self.assertIn('deepseek-v4-pro', models)
        self.assertNotIn('gpt-unknown', models)
        self.assertNotIn('deepseek-image', models)
        self.assertNotIn('relay-alias', models)

    async def test_requested_backend_is_a_hard_constraint(self):
        self.save([self.rule(pool=[self.claude, self.gemini])])
        ctx = await self.context(requested_backend='gemini')
        self.assertEqual(server.preview_routing(ctx['token'])['backend'], 'gemini')
        with self.assertRaises(common.RelayError):
            server.preview_routing(ctx['token'], candidate_id=self.claude)

    async def test_availability_fallback_is_only_before_start(self):
        self.save([self.rule(pool=[self.claude]), self.rule('second', [self.gemini])])
        ctx = await self.context()
        self.models['claude'] = []
        job = await self.submit(ctx, candidate_id=self.claude, reason='original choice')
        self.assertEqual(job['backend'], 'gemini')
        self.assertTrue(job['model_selection']['fallback'])
        self.assertEqual(job['model_selection']['previous_candidate_id'], self.claude)
        self.assertEqual(len(self.executed), 1)

    async def test_no_fallback_stops_before_native_side_effects(self):
        self.save([self.rule(allow_fallback=False)], [self.gemini])
        ctx = await self.context()
        self.models['claude'] = []
        with self.assertRaisesRegex(common.RelayError, '不允许'):
            await self.submit(ctx, candidate_id=self.claude)
        self.assertFalse(self.executed)
        self.assertFalse(server.JOBS)

    async def test_recovered_earlier_rule_does_not_replace_a_still_available_decision(self):
        self.save([self.rule(), self.rule('second', [self.gemini])])
        self.models['claude'] = []
        ctx = await self.context()
        self.assertEqual(server.preview_routing(ctx['token'])['candidate_id'], self.gemini)
        self.models['claude'] = ['claude-sonnet-fixture']
        job = await self.submit(ctx, candidate_id=self.gemini)
        self.assertEqual(job['backend'], 'gemini')
        self.assertEqual(job['model_selection']['rule_id'], 'second')
        self.assertEqual(len(job['model_selection']['trace']), 2)

    async def test_newly_configured_backend_requires_a_new_context(self):
        self.save([self.rule()])
        os.environ.pop('AGENT_HUB_DEEPSEEK_API_KEY')
        ctx = await self.context()
        os.environ['AGENT_HUB_DEEPSEEK_API_KEY'] = 'new-fixture'
        with self.assertRaises(common.ConfigurationConflict):
            await self.submit(ctx)

    async def test_effort_filters_unsupported_backends_and_default_pool_works(self):
        self.save([self.rule(pool=[self.gemini, self.deepseek], effort='high')])
        ctx = await self.context()
        result = server.preview_routing(ctx['token'])
        self.assertEqual(result['backend'], 'deepseek')
        self.assertIn('推理强度', result['trace'][0]['unavailable'][0]['reason'])
        self.save([], [self.gemini])
        ctx = await self.context()
        self.assertEqual(server.preview_routing(ctx['token'])['rule_id'], '__fallback__')

    async def test_task_policy_profile_and_environment_changes_invalidate_context(self):
        self.save([self.rule()])
        ctx = await self.context()
        with self.assertRaises(common.ConfigurationConflict):
            await server.submit_task('A different task', str(self.root), task_complexity='standard', routing_decision={'token': ctx['token']})
        self.save([self.rule()])
        with self.assertRaises(common.ConfigurationConflict):
            server.preview_routing(ctx['token'])
        ctx = await self.context()
        os.environ['AGENT_HUB_CLAUDE_API_KEY'] = 'rotated-fixture'
        with self.assertRaises(common.ConfigurationConflict):
            server.preview_routing(ctx['token'])
        ctx = await self.context()
        common.save_settings('https://other.example.com', 'claude-sonnet-fixture', 'claude')
        with self.assertRaises(common.ConfigurationConflict):
            server.preview_routing(ctx['token'])

    async def test_expired_context_and_unknown_candidate_are_rejected(self):
        self.save([self.rule()])
        ctx = await self.context()
        with self.assertRaises(common.RelayError):
            server.preview_routing(ctx['token'], candidate_id='made-up-model')
        routing._contexts[ctx['token']]['expires'] = 0
        with self.assertRaises(common.ConfigurationConflict):
            server.preview_routing(ctx['token'])

    async def test_explicit_user_model_wins_but_rule_choices_do_not_impersonate_user(self):
        self.save([self.rule()])
        with self.assertRaisesRegex(common.RelayError, 'get_routing_context'):
            await server.submit_task('Review', str(self.root))
        job = await server.submit_task('Review', str(self.root), backend='gemini', model='gemini-flash-fixture',
                                       model_override_reason='User requested gemini-flash-fixture')
        await server.get_job(job['job_id'], wait_seconds=1)
        self.assertEqual(job['model_selection']['mode'], 'explicit')
        ctx = await self.context()
        with self.assertRaises(common.RelayError):
            await server.submit_task('Review', str(self.root), model='gemini-flash-fixture',
                model_override_reason='user specified', routing_decision={'token': ctx['token']})

    async def test_failed_discovery_does_not_leak_errors(self):
        self.save([], [self.gemini])
        with patch.object(common, 'fetch_models', side_effect=RuntimeError('private-fixture')):
            ctx = await self.context()
        self.assertNotIn('private-fixture', json.dumps(ctx))
        with self.assertRaises(common.RelayError):
            server.preview_routing(ctx['token'])

    async def test_rule_revision_validation_and_concurrent_limit(self):
        self.save([self.rule()])
        with self.assertRaises(common.ConfigurationConflict):
            routing.save({'enabled': True, 'rules': []}, 'initial')
        with self.assertRaises(common.RelayError):
            self.save([self.rule(), self.rule()])
        contexts = [await self.context() for _ in range(3)]
        hold = asyncio.Event()

        async def execute(*args):
            await hold.wait()

        with patch.object(server, 'execute', new=execute):
            results = await asyncio.gather(*(server.submit_task('Review this project', str(self.root), task_complexity='standard',
                        routing_decision={'token': ctx['token']}) for ctx in contexts), return_exceptions=True)
            self.assertEqual(sum(isinstance(r, dict) for r in results), 2)
            self.assertEqual(sum(isinstance(r, common.RelayError) for r in results), 1)
            hold.set()
