"""Coordinator-observed objectives, independent of worker prose and exit codes."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from omcp.controller import Config, Principal, TaskController, Worker


class ObjectiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.launch = Mock(return_value=Mock(pid=os.getpid(), poll=Mock(return_value=None)))
        self.config = Config(
            'owner/repo', self.root, Path(self.temp.name) / 'state', 'origin/main',
            {'codex': Worker('codex', ('codex',)),
             'response': Worker('response', (), adapter='deepseek')},
            Principal('test', frozenset({'tasks:submit', 'tasks:read', 'workers:read', 'results:read'}), 'test'),
            task_approval_mode='disabled',
        )
        self.controller = TaskController(self.config, popen=self.launch,
                                         workspace_preparer=lambda *_: self.root)
        self.addCleanup(self.controller.close)

    def submit(self, title='Inspect repository', instructions='Explain the findings',
               worker='response', intent='analysis', request_id='test'):
        return self.controller.submit_task(request_id, title, instructions, worker,
                                           consequence_class='routine_development', task_intent=intent)

    def finish(self, task_id, contract='success'):
        task = self.controller.state['tasks'][task_id]
        Path(task['result_path']).write_text('All done, implemented and deployed!')
        Path(task['completion_path']).write_text(json.dumps({
            'exit_code': 0, 'finished_at': '2026-10-27T00:00:00+00:00',
            'completion_contract': contract,
        }))
        self.controller.reconcile()
        return self.controller._public(task)

    def test_exact_response_only_hotfix_misroute_and_legacy(self):
        for intent in ('code_change', None):
            with self.subTest(intent=intent), self.assertRaisesRegex(ValueError, 'worker does not support'):
                self.submit('Hotfix dashboard', 'Fix the code and add tests', intent=intent)
        self.launch.assert_not_called()
        self.assertEqual(self.controller.state['tasks'], {})

    def test_deployment_cannot_use_routine_or_response_only(self):
        for intent in ('analysis', 'code_change', 'deployment', None):
            with self.subTest(intent=intent), self.assertRaises(ValueError):
                self.submit('Deploy hotfix', 'Deploy to production', intent=intent)
        self.launch.assert_not_called()

    def test_analysis_response_delivered_without_implementation_claim(self):
        task = self.submit()
        final = self.finish(task['task_id'])
        self.assertEqual(final['status'], 'succeeded')
        self.assertEqual(final['objective_status'], 'response_delivered')
        self.assertEqual(final['response_status'], 'delivered')
        self.assertNotIn('artifact_evidence', final)

    def test_code_requires_real_artifact_even_with_contract_and_prose(self):
        task = self.submit('Implement hotfix', 'Change code', 'codex', 'code_change')
        (self.root / '.orchestra-contract').mkdir()
        (self.root / '.orchestra-contract' / 'orchestra-completion.json').write_text('{"status":"success"}')
        final = self.finish(task['task_id'])
        self.assertEqual(final['status'], 'failed')
        self.assertEqual(final['failure']['type'], 'MissingCodingArtifacts')
        self.assertEqual(final['objective_status'], 'unverified')

    def test_code_with_artifact_and_contract_needs_no_commit(self):
        task = self.submit('Implement hotfix', 'Change code', 'codex', 'code_change')
        (self.root / 'fix.py').write_text('fixed = True\n')
        final = self.finish(task['task_id'])
        self.assertEqual(final['status'], 'succeeded')
        self.assertEqual(final['objective_status'], 'artifact_verified')
        self.assertEqual(final['artifact_evidence']['changed_files'], ['fix.py'])
        self.assertTrue(self.controller._worker_health('codex')['proven_healthy'])

    def test_artifact_cannot_replace_contract(self):
        task = self.submit('Implement hotfix', 'Change code', 'codex', 'code_change')
        (self.root / 'fix.py').write_text('fixed = True\n')
        final = self.finish(task['task_id'], contract=None)
        self.assertEqual(final['status'], 'failed')
        self.assertEqual(final['objective_status'], 'unverified')

    def test_legacy_analysis_and_idempotency(self):
        task = self.submit(intent=None)
        replay = self.submit(intent=None)
        self.assertTrue(replay['idempotent_replay'])
        self.assertEqual(task['task_intent'], 'analysis')
        self.assertEqual(task['intent_source'], 'legacy_inferred')
        self.assertEqual(self.finish(task['task_id'])['objective_status'], 'response_delivered')

    def test_legacy_historical_retry_preserves_identity_without_launch(self):
        import hashlib
        content = ['Hotfix dashboard', 'Fix the code', 'response', 'default', 'routine_development']
        digest = hashlib.sha256(json.dumps(content, separators=(',', ':')).encode()).hexdigest()
        historical = {'task_id': 'd88d24ad-ddff-4f43-8ed3-ae3c0e9fd983',
                      'status': 'succeeded', 'request_digest': digest}
        self.controller.state['tasks'][historical['task_id']] = historical
        self.controller.state['requests']['legacy'] = historical['task_id']
        replay = self.submit('Hotfix dashboard', 'Fix the code', intent=None, request_id='legacy')
        self.assertTrue(replay['idempotent_replay'])
        self.assertEqual(replay['objective_status'], 'unverified')
        self.launch.assert_not_called()

    def test_historical_annotation_is_read_only(self):
        historical = {'task_id': 'd88d24ad-ddff-4f43-8ed3-ae3c0e9fd983', 'status': 'succeeded'}
        self.assertEqual(TaskController._public(historical)['objective_status'], 'unverified')
        self.assertIn('not executed', TaskController._public(historical)['objective_note'])
        self.assertEqual(set(historical), {'task_id', 'status'})

    def test_health_requires_artifact_evidence(self):
        task = self.submit(worker='codex')
        self.finish(task['task_id'])
        health = self.controller._worker_health('codex')
        self.assertFalse(health['proven_healthy'])
