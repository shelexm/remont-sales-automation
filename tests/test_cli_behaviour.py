"""Exercise operator-facing commands without touching a running installation."""
import io
import json
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import patch

import psycopg

from app import analytics, diagnostics, reconcile


class CommandTests(unittest.TestCase):
    def run_command(self, module, args, env, patches=()):
        with patch.object(module.sys, 'argv', [module.__name__, *args]), \
             patch.dict(module.os.environ, env, clear=True), \
             redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            with ExitStack() as stack:
                mocks = [stack.enter_context(p) for p in patches]
                code = module.main()
            return code, out.getvalue(), err.getvalue(), mocks

    def test_diagnostics_exit_status_and_sanitized_database_error(self):
        for status, expected in [('ok', 0), ('attention', 1)]:
            with self.subTest(status=status):
                code, out, _, mocks = self.run_command(
                    diagnostics, ['--overdue-minutes', '17'], {'DATABASE_URL': 'test'},
                    [patch.object(diagnostics, 'queue_report', return_value={'status': status})])
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(out)['status'], status)
                mocks[0].assert_called_once_with('test', 17)
        for exception in (psycopg.OperationalError('secret-db-details'), ValueError('sensitive')):
            code, _, err, _ = self.run_command(diagnostics, [], {'DATABASE_URL': 'test'},
                [patch.object(diagnostics, 'queue_report', side_effect=exception)])
            self.assertEqual(code, 2)
            self.assertNotIn('secret-db-details', err)
            self.assertNotIn('sensitive', err)
        self.assertEqual(self.run_command(diagnostics, [], {})[0], 2)

    def test_analytics_json_text_and_errors(self):
        data = {'window_days': 3, 'from_utc': 'start', 'until_utc': 'end',
                'events': dict(accepted=2, completed=1, pending=1, processing=0, failed=0,
                               first_case_events=1, repeat_case_events=0, without_case=1),
                'services': {'capital': 2}, 'budget_classes': {'high': 1},
                'integration_jobs': {'crm': {'done': 1}}, 'completed_without_crm_job': 0}
        for args in (['--days', '3'], ['--days', '3', '--json']):
            code, out, _, mocks = self.run_command(analytics, args, {'DATABASE_URL': 'test'},
                                                  [patch.object(analytics, 'report', return_value=data)])
            self.assertEqual(code, 0)
            mocks[0].assert_called_once_with('test', 3)
            if '--json' in args:
                self.assertEqual(json.loads(out), data)
            else:
                self.assertIn('Принято уникальных событий: 2', out)
                self.assertIn('CRM: выполнено: 1', out)
        for failure in (psycopg.OperationalError('secret-db'), ValueError('invalid period')):
            code, _, err, _ = self.run_command(analytics, [], {'DATABASE_URL': 'test'},
                                               [patch.object(analytics, 'report', side_effect=failure)])
            self.assertEqual(code, 2)
            self.assertNotIn('secret-db', err)
        self.assertEqual(self.run_command(analytics, [], {})[0], 2)
        with self.assertRaises(ValueError):
            analytics.report('unused', 366)

    def test_analytics_empty_and_unknown_destinations_are_readable(self):
        data = {'window_days': 1, 'from_utc': 'a', 'until_utc': 'b',
                'events': dict.fromkeys(('accepted', 'completed', 'pending', 'processing', 'failed',
                                         'first_case_events', 'repeat_case_events', 'without_case'), 0),
                'services': {}, 'budget_classes': {}, 'integration_jobs': {'other': {'odd': 1}},
                'completed_without_crm_job': 0}
        with redirect_stdout(io.StringIO()) as output:
            analytics.print_report(data)
        self.assertIn('нет данных', output.getvalue())
        self.assertIn('неизвестное назначение: неизвестный статус: 1', output.getvalue())

    def test_reconciliation_requires_explicit_chat_confirmation_and_hides_errors(self):
        with patch.object(reconcile.sys, 'argv', ['reconcile', 'confirm-telegram', '--job', '1', '--operator', 'me']), \
             redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            reconcile.main()
        args = ['confirm-telegram', '--job', '7', '--operator', 'me', '--confirmed-in-chat']
        code, out, _, mocks = self.run_command(reconcile, args, {'DATABASE_URL': 'test'},
                                                [patch.object(reconcile, 'confirm_telegram')])
        self.assertEqual(code, 0)
        self.assertIn('no message was sent', out)
        mocks[0].assert_called_once_with('test', 7, 'me')
        code, out, _, _ = self.run_command(reconcile, ['list'], {'DATABASE_URL': 'test'},
                                           [patch.object(reconcile, 'list_uncertain', return_value=[{'job_id': 7}])])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [{'job_id': 7}])
        for failure in (psycopg.OperationalError('db-secret'), ValueError('invalid job')):
            code, _, err, _ = self.run_command(reconcile, ['list'], {'DATABASE_URL': 'test'},
                                               [patch.object(reconcile, 'list_uncertain', side_effect=failure)])
            self.assertEqual(code, 2)
            self.assertNotIn('db-secret', err)
        self.assertEqual(self.run_command(reconcile, ['list'], {})[0], 2)


if __name__ == '__main__':
    unittest.main()
