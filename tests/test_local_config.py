"""Import real-format fixtures in an isolated home; never inspect developer credentials."""
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import common
import local_config


class LocalConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.enterContext(patch.object(common, 'STATE_DIR', self.root / 'state'))
        self.enterContext(patch.object(Path, 'home', return_value=self.home))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.object(common, '_dpapi', side_effect=lambda value, decrypt=False:
                                      base64.b64decode(value) if decrypt else base64.b64encode(value)))
        local_config._previews.clear()
        self.client = self.home / '.claude' / 'settings.json'
        self.write_client()

    def write_client(self, model='claude-sonnet-fixture', key='fixture-key', base='http://127.0.0.1:4567'):
        self.client.parent.mkdir(parents=True, exist_ok=True)
        self.client.write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': base, 'ANTHROPIC_API_KEY': key},
                                           'model': model, 'effortLevel': 'high', 'permissions': {'allow': ['Bash(*)']}}))

    def apply(self, mode='follow', fields=None, backend='claude'):
        preview = local_config.detect(backend)['sources'][0]
        return local_config.configure(backend, mode, fields or list(local_config.FIELDS), preview['token'],
                                      common.profile_revision(backend), local_config.status(backend)['revision'])

    def test_off_never_detects_or_reads_client(self):
        with patch.object(local_config, 'read_source') as read, patch.object(local_config, 'sources') as scan:
            self.assertEqual(local_config.status('claude')['mode'], 'off')
            local_config.refresh('claude')
            local_config.require_ready('claude')
        read.assert_not_called()
        scan.assert_not_called()

    def test_import_is_once_secrets_are_redacted_and_client_is_unchanged(self):
        before = self.client.read_bytes()
        detected = local_config.detect('claude')
        self.assertNotIn('fixture-key', json.dumps(detected))
        self.assertTrue(detected['sources'][0]['key_available'])
        self.apply('import')
        self.assertEqual(common.load_key('claude'), 'fixture-key')
        self.assertEqual(self.client.read_bytes(), before)
        self.write_client(model='claude-new-fixture', key='next-fixture')
        local_config.refresh('claude')
        self.assertEqual(common.load_settings('claude')['model'], 'claude-sonnet-fixture')
        public = json.dumps(local_config.status('claude'))
        self.assertNotIn('fixture-key', public)
        self.assertNotIn('permissions', (common.STATE_DIR / 'settings.json').read_text())

    def test_follow_rotates_credentials_preserves_automation_and_pins_source(self):
        common.save_settings('https://relay.example.com', 'claude-old', 'claude', auto_select_model=True)
        self.apply()
        first = common.profile_revision('claude')
        self.write_client(model='claude-new-fixture', key='next-fixture')
        os.environ['CLAUDE_CONFIG_DIR'] = str(self.root / 'another-client')
        local_config.require_ready('claude')
        self.assertNotEqual(first, common.profile_revision('claude'))
        self.assertEqual(common.load_settings('claude')['model'], 'claude-new-fixture')
        self.assertTrue(common.load_settings('claude')['auto_select_model'])
        self.assertEqual(common.load_key('claude'), 'next-fixture')

    def test_manual_edits_conflict_and_disabling_retains_them(self):
        self.apply()
        common.save_settings('https://manual.example.com', 'claude-manual', 'claude')
        self.write_client(model='claude-upstream')
        with self.assertRaisesRegex(common.RelayError, '手动修改'):
            local_config.require_ready('claude')
        local_config.configure('claude', 'off', [], '', common.profile_revision('claude'), local_config.status('claude')['revision'])
        local_config.require_ready('claude')
        self.assertEqual(common.load_settings('claude')['model'], 'claude-manual')

    def test_unmanaged_fields_do_not_conflict_with_following_selected_fields(self):
        common.save_settings('https://relay.example.com', 'claude-old', 'claude', 'low')
        self.apply('follow', ['model'])
        common.save_settings('https://manual.example.com', 'claude-sonnet-fixture', 'claude', 'max', True)
        self.write_client(model='claude-new-fixture')
        local_config.require_ready('claude')
        saved = common.load_settings('claude')
        self.assertEqual(saved['base_url'], 'https://manual.example.com')
        self.assertEqual(saved['model'], 'claude-new-fixture')
        self.assertEqual(saved['effort'], 'max')
        self.assertTrue(saved['auto_select_model'])

    def test_missing_source_blocks_new_tasks_without_clearing_saved_settings(self):
        self.apply()
        self.client.unlink()
        with self.assertRaises(common.RelayError):
            local_config.require_ready('claude')
        self.assertEqual(local_config.status('claude')['status'], 'blocked')
        self.assertEqual(common.load_settings('claude')['model'], 'claude-sonnet-fixture')

    def test_stale_preview_and_sync_revision_are_rejected(self):
        preview = local_config.detect('claude')['sources'][0]
        self.write_client(key='changed-fixture')
        with self.assertRaises(common.ConfigurationConflict):
            local_config.configure('claude', 'follow', list(local_config.FIELDS), preview['token'],
                                   common.profile_revision('claude'), 'initial')
        self.apply('import')
        with self.assertRaises(common.ConfigurationConflict):
            local_config.configure('claude', 'off', [], '', common.profile_revision('claude'), 'initial')

    def test_partial_import_preserves_other_fields_and_base_requires_matching_key(self):
        common.save_settings('https://relay.example.com', 'claude-old', 'claude', 'low')
        self.apply('import', ['model'])
        self.assertEqual(common.load_settings('claude')['effort'], 'low')
        with self.assertRaisesRegex(common.RelayError, '同时导入'):
            self.apply('import', ['base_url'])
        os.environ['AGENT_HUB_CLAUDE_API_KEY'] = 'different-fixture'
        with self.assertRaisesRegex(common.RelayError, '环境变量'):
            self.apply()
        self.assertEqual(common.load_settings('claude')['base_url'], 'https://relay.example.com')

    def test_oauth_helpers_dynamic_values_and_parse_errors_do_not_leak_or_execute(self):
        self.client.write_text(json.dumps({'model': 'claude-sonnet', 'apiKeyHelper': 'never-execute'}))
        result = local_config.detect('claude')['sources'][0]
        self.assertEqual(result['auth'], 'unsupported')
        with self.assertRaises(common.RelayError):
            self.apply()
        self.write_client(key='sk-ant-oat-fixture')
        self.assertFalse(local_config.detect('claude')['sources'][0]['key_available'])
        self.write_client(key='${CLIENT_SECRET}')
        self.assertIn('error', local_config.detect('claude')['sources'][0])
        self.client.write_text('{"bad":"fixture-key"')
        self.assertNotIn('fixture-key', json.dumps(local_config.detect('claude')))

    def test_importing_only_key_requires_the_same_destination(self):
        common.save_settings('https://other.example.com', 'claude-fixture', 'claude')
        common.save_key('original-fixture', 'claude')
        with self.assertRaisesRegex(common.RelayError, '来源地址'):
            self.apply('import', ['api_key'])
        self.assertEqual(common.load_key('claude'), 'original-fixture')
        self.assertEqual(local_config.status('claude')['mode'], 'off')

    def test_key_only_follow_rotates_at_same_address_but_blocks_provider_switch(self):
        self.apply('import')
        self.apply('follow', ['api_key'])
        self.write_client(key='rotated-fixture', base='http://127.0.0.1:4567/v1')
        local_config.require_ready('claude')
        self.assertEqual(common.load_key('claude'), 'rotated-fixture')
        self.write_client(key='other-provider-fixture', base='https://other.example.com')
        with self.assertRaises(common.RelayError):
            local_config.require_ready('claude')
        self.assertEqual(local_config.status('claude')['status'], 'blocked')
        self.assertEqual(common.load_settings('claude')['base_url'], 'http://127.0.0.1:4567')
        self.assertEqual(common.load_key('claude'), 'rotated-fixture')
        self.apply('follow', ['base_url', 'api_key'])
        local_config.require_ready('claude')
        self.assertEqual(common.load_settings('claude')['base_url'], 'https://other.example.com')
        self.assertEqual(common.load_key('claude'), 'other-provider-fixture')

    def test_failed_follow_refresh_restores_key_before_marking_blocked(self):
        self.apply('follow')
        self.write_client(key='replacement-fixture')
        with patch.object(common, 'save_settings', side_effect=OSError('disk failure')):
            local_config.refresh('claude')
        self.assertEqual(common.load_key('claude'), 'fixture-key')
        self.assertEqual(local_config.status('claude')['status'], 'blocked')

    def test_legacy_mismatched_follow_blocks_even_with_unchanged_fingerprint(self):
        self.apply('import')
        self.apply('follow', ['api_key'])
        common.save_settings('https://old-destination.example.com', 'claude-fixture', 'claude')
        state = local_config._state()
        state['profiles']['claude']['managed_version'] = local_config._managed_version('claude', ['api_key'])
        local_config._write(state)
        with self.assertRaises(common.RelayError):
            local_config.require_ready('claude')
        self.assertEqual(local_config.status('claude')['status'], 'blocked')
        self.assertEqual(common.load_key('claude'), 'fixture-key')

    def test_gemini_api_key_and_oauth_distinction(self):
        directory = self.home / '.gemini'
        directory.mkdir()
        (directory / '.env').write_text('GEMINI_API_KEY="gemini-fixture"\nGEMINI_MODEL=gemini-pro-fixture\nGOOGLE_GEMINI_BASE_URL=http://localhost:5678')
        settings = directory / 'settings.json'
        settings.write_text(json.dumps({'security': {'auth': {'selectedType': 'gemini-api-key'}}}))
        self.apply('import', backend='gemini')
        self.assertEqual(common.load_key('gemini'), 'gemini-fixture')
        self.assertEqual(common.load_settings('gemini')['base_url'], 'http://localhost:5678')
        settings.write_text(json.dumps({'security': {'auth': {'selectedType': 'oauth-personal'}}}))
        data = local_config.detect('gemini')['sources'][0]
        self.assertEqual(data['auth'], 'unsupported')
        self.assertFalse(data['key_available'])

    def test_dsh_profile_overlay_credentials_and_messages_only(self):
        directory = self.home / '.dsh'
        profile = directory / 'profiles' / 'custom'
        profile.mkdir(parents=True)
        (directory / 'cordis.patch.yml').write_text('''- id: agent-default-model
  config: {provider: relay, model: deepseek-v4-flash}
- id: llm-pi-ai
  config:
    providers:
      relay: {baseURL: 'http://127.0.0.1:5678/v1', apiKeyEnv: RELAY_CREDENTIAL, api: anthropic-messages}
- id: irrelevant-plugin
  config: {cwd: !!js process.cwd()}
''')
        (profile / 'cordis.patch.yml').write_text('- id: agent-default-model\n  config: {provider: relay, model: deepseek-v4-pro, reasoningEffort: high}')
        (directory / '.env').write_text('RELAY_CREDENTIAL=env-fixture')
        (directory / '.credentials.yaml').write_text('version: 1\nrefs: {RELAY_CREDENTIAL: store-fixture}')
        results = local_config.detect('deepseek')['sources']
        self.assertEqual(results[1]['values']['model'], 'deepseek-v4-pro')
        source = local_config.read_source(results[1]['source'])
        self.assertEqual(source['values']['api_key'], 'store-fixture')
        path = directory / 'cordis.patch.yml'
        path.write_text(path.read_text().replace('anthropic-messages', 'openai-completions'))
        self.assertEqual(local_config.detect('deepseek')['sources'][0]['auth'], 'unsupported')
        (directory / '.credentials.yaml').write_text('refs: {RELAY_CREDENTIAL: !!js process.env.SECRET}')
        (directory / '.env').unlink()
        self.assertFalse(local_config.detect('deepseek')['sources'][0]['key_available'])

    def test_failed_profile_write_restores_prior_credential(self):
        self.apply('import')
        self.write_client(key='replacement-fixture')
        with patch.object(common, 'save_settings', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.apply('import')
        self.assertEqual(common.load_key('claude'), 'fixture-key')

    def test_failed_sync_metadata_write_rolls_back_the_import(self):
        self.apply('follow')
        self.write_client(key='replacement-fixture')
        with patch.object(local_config, '_write', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.apply('follow')
        self.assertEqual(common.load_key('claude'), 'fixture-key')
        self.assertEqual(common.load_settings('claude')['model'], 'claude-sonnet-fixture')

    def test_only_http_loopback_is_allowed(self):
        for base in ('http://127.0.0.1:8080/v1', 'http://[::1]:8080', 'http://localhost:8080'):
            self.assertTrue(common.normalize_base(base).startswith('http:'))
        for base in ('http://relay.example.com', 'http://192.168.1.2', 'http://localhost.example.com',
                     'https://user:password@relay.example.com', 'http://127.0.0.1:8080?key=fixture'):
            with self.assertRaises(common.RelayError):
                common.normalize_base(base)
