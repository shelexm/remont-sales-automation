"""Verify delivery decisions when providers or the database fail."""
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import psycopg

from app.integrations import worker
from app.integrations.providers import Permanent, Retryable, Uncertain


class StopLoop(BaseException):
    pass


class DeliveryTests(unittest.TestCase):
    def test_no_destination_and_empty_queue_do_not_finish_jobs(self):
        with patch.object(worker, 'claim') as claim, patch.object(worker, 'finish') as finish:
            self.assertFalse(worker.deliver_once('db'))
            claim.assert_not_called()
            claim.return_value = None
            self.assertFalse(worker.deliver_once('db', crm=Mock()))
            finish.assert_not_called()

    def test_failures_are_classified_without_leaking_remote_data(self):
        cases = [(Retryable('rate', 100), 1, 'pending', 'rate', 100),
                 (Retryable('rate', 1), 5, 'failed', 'rate', 320),
                 (Permanent('private error'), 1, 'failed', 'remote_rejected', None),
                 (Uncertain('private error'), 1, 'uncertain', 'remote_result_unknown', None),
                 (RuntimeError('private error'), 1, 'uncertain', 'internal_error', None)]
        for error, attempts, state, reason, delay in cases:
            with self.subTest(state=state, error=type(error).__name__):
                crm = Mock()
                crm.sync.side_effect = error
                job = (11, 22, 'crm', attempts, {'name': 'private'})
                with patch.object(worker, 'claim', return_value=job), patch.object(worker, 'finish') as finish:
                    self.assertTrue(worker.deliver_once('db', crm=crm))
                    args = finish.call_args.args
                    self.assertEqual(args[3:5], (state, reason))
                    self.assertNotIn('private', str(args))
                    if delay is not None:
                        self.assertEqual(args[5], delay)

    def test_telegram_and_demo_followup_are_sent_only_for_new_cases(self):
        telegram = Mock()
        for destination, status, count in [('telegram', 'new', 0), ('telegram_reminder', 'new', 1),
                                            ('telegram_reminder', 'closed', 1), ('telegram_reminder', None, 1)]:
            job = (1, 2, destination, 1, {'service': 'capital'})
            conn = Mock()
            conn.execute.return_value.fetchone.return_value = (status,) if status else None
            context = Mock()
            context.__enter__ = Mock(return_value=conn)
            context.__exit__ = Mock(return_value=False)
            with patch.object(worker, 'claim', return_value=job), patch.object(worker, 'finish') as finish, \
                 patch.object(worker.psycopg, 'connect', return_value=context):
                worker.deliver_once('db', telegram=telegram)
                self.assertEqual(finish.call_args.args[3], 'done')
                self.assertEqual(telegram.remind.call_count, count)
        telegram.notify.assert_called_once()

    def test_refresh_due_ignores_rejected_stages_and_retries(self):
        context = unittest.mock.MagicMock()
        context.__enter__.return_value.execute.return_value.fetchone.return_value = (42,)
        for failure in (Retryable('temporary'), Permanent('rejected')):
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(worker.psycopg, 'connect', return_value=context), \
                 patch.object(worker, 'refresh_stage', side_effect=failure):
                worker.refresh_one_due('db', Mock())

    def test_invalid_reminder_interval_is_rejected_before_database_access(self):
        with patch.object(worker.psycopg, 'connect') as connect:
            for minutes in (0, 10081):
                with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                    worker.enqueue_reminders('db', minutes)
            connect.assert_not_called()

    def test_missing_crm_tracking_delays_reminder(self):
        context = unittest.mock.MagicMock()
        context.__enter__.return_value.execute.return_value.fetchone.return_value = None
        with patch.object(worker.psycopg, 'connect', return_value=context), self.assertRaises(Retryable):
            worker.refresh_stage('db', Mock(), 1)

    def test_worker_binds_before_delivery_and_handles_database_outage(self):
        settings = {'DATABASE_URL': 'isolated', 'CRM_PROVIDER': 'demo', 'TELEGRAM_ENABLED': 'false'}
        with patch.dict(worker.os.environ, settings, clear=True), \
             patch.object(worker, 'ensure_provider_binding') as bind, \
             patch.object(worker, 'deliver_once', side_effect=psycopg.OperationalError('secret')), \
             patch.object(worker.time, 'sleep', side_effect=StopLoop), redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(StopLoop):
                worker.main()
        bind.assert_called_once()
        self.assertIn('database_unavailable', out.getvalue())
        self.assertNotIn('secret', out.getvalue())

    def test_unknown_provider_and_missing_bitrix_stage_are_rejected(self):
        base = {'DATABASE_URL': 'test', 'CRM_PROVIDER': 'other'}
        with patch.dict(worker.os.environ, base, clear=True), self.assertRaises(ValueError):
            worker.main()
        env = {'DATABASE_URL': 'test', 'CRM_PROVIDER': 'bitrix24', 'BITRIX_WEBHOOK_URL':
               'https://example.invalid/rest/1/exampletoken/', 'BITRIX_CONTACT_FIELD': 'UF_CRM_CONTACT_KEY',
               'BITRIX_DEAL_FIELD': 'UF_CRM_DEAL_KEY', 'BITRIX_INSTALLATION_ID': 'example_installation',
               'TELEGRAM_ENABLED': 'true', 'TELEGRAM_BOT_TOKEN': 'test', 'TELEGRAM_CHAT_ID': '1'}
        with patch.dict(worker.os.environ, env, clear=True), patch.object(worker, 'ensure_provider_binding') as bind:
            with self.assertRaisesRegex(ValueError, 'BITRIX_UNPROCESSED_STAGE'):
                worker.main()
            bind.assert_not_called()

    def test_worker_provider_configuration_and_reminder_scheduling(self):
        common = {'DATABASE_URL': 'test', 'TELEGRAM_ENABLED': 'true',
                  'TELEGRAM_BOT_TOKEN': 'test', 'TELEGRAM_CHAT_ID': '1'}
        configurations = [
            ('demo', {}, 'Prospecting'),
            ('espocrm', {'ESPO_URL': 'https://example.invalid', 'ESPO_API_KEY': 'test',
                         'ESPO_ASSIGNED_USER_ID': 'owner123', 'ESPO_UNPROCESSED_STAGE': 'New'}, 'New'),
            ('bitrix24', {'BITRIX_WEBHOOK_URL': 'https://example.invalid/rest/1/exampletoken/',
                          'BITRIX_CONTACT_FIELD': 'UF_CRM_CONTACT_KEY', 'BITRIX_DEAL_FIELD': 'UF_CRM_DEAL_KEY',
                          'BITRIX_INSTALLATION_ID': 'example_installation', 'BITRIX_UNPROCESSED_STAGE': 'NEW'}, 'NEW'),
        ]
        for provider, settings, stage in configurations:
            with self.subTest(provider=provider), patch.dict(worker.os.environ, {**common, **settings, 'CRM_PROVIDER': provider}, clear=True), \
                 patch.object(worker, 'ensure_provider_binding') as bind, \
                 patch.object(worker, 'refresh_one_due') as refresh, \
                 patch.object(worker, 'enqueue_reminders') as enqueue, \
                 patch.object(worker, 'deliver_once', return_value=False) as deliver, \
                 patch.object(worker.time, 'sleep', side_effect=StopLoop):
                with self.assertRaises(StopLoop):
                    worker.main()
                bind.assert_called_once()
                self.assertEqual(bind.call_args.args[1], provider)
                self.assertEqual(enqueue.call_args.args[2:], (provider, stage))
                self.assertEqual(deliver.call_args.args[3], stage)
                self.assertEqual(refresh.call_count, 0 if provider == 'demo' else 1)


if __name__ == '__main__':
    unittest.main()
