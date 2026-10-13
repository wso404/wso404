import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile

HELPER = Path(__file__).parents[1] / '.github/scripts/private_ci.py'
spec = importlib.util.spec_from_file_location('private_ci', HELPER)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class PrivateLogging(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = self.root / 'engine'
        self.engine.mkdir()
        self.environment = mock.patch.dict(os.environ, {
            'NEOM_LOG_DIR': str(self.root / 'logs'),
            'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
            'GITHUB_REPOSITORY': 'wso404/wso404',
            'TELEGRAM_OWNER_ID': '123456',
            'TELEGRAM_BOT_TOKEN': '999999:PRIVATE-BOT-TOKEN',
            'NEOM_JOB_STEPS': '{}',
        }, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def write_config(self, name='gdrive'):
        token = {'access_token': 'ya29.ACCESS_PRIVATE', 'refresh_token': '1//REFRESH_PRIVATE'}
        text = '[' + name + ']\ntype = drive\nscope = drive\ntoken = ' + json.dumps(token) + '\n'
        p = self.engine / 'rclone.conf'
        p.write_text(text)
        os.environ['RCLONE_CONFIG_B64'] = base64.b64encode(text.encode()).decode()
        os.environ['RCLONE_REMOTE_NAME'] = name
        return p

    def test_stage_output_is_private_and_failure_code_survives(self):
        public = io.StringIO()
        with contextlib.redirect_stdout(public):
            code = ci.capture('build', [sys.executable, '-c',
                'import sys; print("SYSTEM_PARTITION_DETAIL"); print("hidden stderr",file=sys.stderr);sys.exit(7)'])
        self.assertEqual(code, 7)
        self.assertNotIn('SYSTEM_PARTITION_DETAIL', public.getvalue())
        self.assertNotIn('hidden stderr', public.getvalue())
        private = (ci.log_root() / 'full-build.log').read_text()
        self.assertIn('SYSTEM_PARTITION_DETAIL', private)
        self.assertIn('hidden stderr', private)
        self.assertIn('exit=7', private)

    def test_workflow_heredoc_and_tee_remain_private(self):
        body = 'set -o pipefail\nif bash -c "echo FULL_ENGINE_OUTPUT; exit 9" 2>&1 | tee "$NEOM_LOG_DIR/engine.log"; then exit 0; else exit 9; fi\n'
        result = subprocess.run([sys.executable, str(HELPER), 'run', 'build', '--',
                                 'bash', '-e', '-u', '-o', 'pipefail'],
                                input=body, capture_output=True, text=True, env=os.environ)
        self.assertEqual(result.returncode, 9)
        self.assertNotIn('FULL_ENGINE_OUTPUT', result.stdout + result.stderr)
        self.assertIn('FULL_ENGINE_OUTPUT', (ci.log_root() / 'full-build.log').read_text())

    def test_remote_name_mismatch_is_specific_without_network(self):
        p = self.write_config()
        os.environ['RCLONE_REMOTE_NAME'] = 'drive'
        output = io.StringIO()
        with mock.patch.object(ci.subprocess, 'run') as command, contextlib.redirect_stdout(output):
            self.assertEqual(ci.check_drive(p), 1)
        command.assert_not_called()
        self.assertIn('does not match', output.getvalue())
        self.assertIn('gdrive', output.getvalue())
        self.assertNotIn('REFRESH_PRIVATE', output.getvalue())

    def test_invalid_config_does_not_dump_token_line(self):
        p = self.engine / 'rclone.conf'
        p.write_text('{"access_token":"PRIVATE_VALUE"}')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(ci.check_drive(p), 1)
        self.assertIn('Invalid rclone configuration', output.getvalue())
        self.assertNotIn('PRIVATE_VALUE', output.getvalue())

    def test_original_rclone_stderr_and_exit_are_preserved(self):
        p = self.write_config()
        results = [subprocess.CompletedProcess([], 0, 'gdrive:\n', 'CONFIG_WARNING\n'),
                   subprocess.CompletedProcess([], 8)]
        output, error = io.StringIO(), io.StringIO()
        with mock.patch.object(ci.subprocess, 'run', side_effect=results) as command, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(ci.check_drive(p), 8)
        self.assertIn('CONFIG_WARNING', error.getvalue())
        self.assertIn('about', command.call_args_list[1].args[0])

    def test_archive_contains_full_log_state_and_redacts_credentials(self):
        self.write_config()
        secret = 'github_pat_HIGHLY_PRIVATE'
        os.environ['ENGINE_READ_TOKEN'] = secret
        encoded = base64.b64encode(('x-access-token:' + secret).encode()).decode()
        (ci.log_root() / 'build.log').write_text('SECTION SIZES\n' + secret + '\nAUTHORIZATION: basic ' + encoded + '\n')
        (ci.log_root() / 'drive-check.log').write_text('invalid_grant\nya29.ACCESS_PRIVATE\n1//REFRESH_PRIVATE\n')
        state = self.engine / 'bin/ddevice'
        state.mkdir(parents=True)
        (state / 'superSize.txt').write_text('9126805504')
        (self.engine / 'do-not-send-secret.conf').write_text('SECRET_CONFIG_FILE')
        archive = ci.archive_logs(self.engine, 'failure')[0]
        with zipfile.ZipFile(archive) as package:
            text = '\n'.join(package.read(name).decode() for name in package.namelist())
            self.assertIn('SECTION SIZES', text)
            self.assertIn('invalid_grant', text)
            self.assertIn('9126805504', text)
            for value in (secret, encoded, 'ya29.ACCESS_PRIVATE', '1//REFRESH_PRIVATE', 'SECRET_CONFIG_FILE'):
                self.assertNotIn(value, text)
            self.assertNotIn('rclone.conf', package.namelist())

    def test_successful_build_logs_are_also_delivered(self):
        (ci.log_root() / 'full-build.log').write_text('PACKING_DETAIL')
        responses = [{'type': 'private', 'id': 123456}, {'message_id': 9}]
        with mock.patch.object(ci, 'telegram_request', side_effect=responses) as request, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ci.deliver(self.engine, 'success'), 0)
        self.assertEqual(request.call_args_list[1].args[1], 'sendDocument')
        self.assertEqual(request.call_args_list[1].args[2]['chat_id'], '123456')
        self.assertIn('SUCCESS', request.call_args_list[1].args[2]['caption'])

    def test_checkout_failure_delivers_even_without_engine_directory(self):
        self.engine.rmdir()
        (ci.log_root() / 'full-build.log').write_text('checkout failed with HTTP 403')
        with mock.patch.object(ci, 'telegram_request', side_effect=[{'type': 'private', 'id': 123456}, {}]) as request, \
                contextlib.redirect_stdout(io.StringIO()):
            ci.deliver(self.engine, 'failure')
        with zipfile.ZipFile(request.call_args_list[1].args[3]) as package:
            self.assertIn('HTTP 403', package.read('full-build.log').decode())

    def test_recipient_must_match_the_configured_private_owner(self):
        for chat in ({'type': 'group', 'id': -123456}, {'type': 'private', 'id': 654321}):
            with mock.patch.object(ci, 'telegram_request', return_value=chat) as request:
                with self.assertRaises(ValueError):
                    ci.deliver(self.engine, 'failure')
            self.assertEqual(request.call_count, 1)

    def test_large_zip_parts_restore_the_complete_archive(self):
        (ci.log_root() / 'full-build.log').write_text(base64.b64encode(os.urandom(4096)).decode())
        with mock.patch.object(ci, 'MAX_DOCUMENT_BYTES', 1000):
            archives = ci.archive_logs(self.engine, 'failure')
        self.assertGreater(len(archives), 1)
        self.assertTrue(all(p.stat().st_size <= 1000 for p in archives))
        with zipfile.ZipFile(io.BytesIO(b''.join(p.read_bytes() for p in archives))) as restored:
            self.assertIsNone(restored.testzip())
            self.assertIn('full-build.log', restored.namelist())

    def test_telegram_rate_limit_retry_and_multipart_upload(self):
        document = self.root / 'logs.zip'
        document.write_bytes(b'ZIP_DATA')
        error = urllib.error.HTTPError('https://example.test', 429, 'rate limit', {},
                                       io.BytesIO(b'{"ok":false,"error_code":429,"parameters":{"retry_after":1}}'))
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true,"result":{"message_id":7}}'
        with mock.patch.object(ci.urllib.request, 'urlopen', side_effect=[error, response]) as send, \
                mock.patch.object(ci.time, 'sleep') as sleep:
            result = ci.telegram_request('FAKE_TOKEN', 'sendDocument', {'chat_id': '123456'}, document)
        self.assertEqual(result['message_id'], 7)
        sleep.assert_called_once_with(1)
        self.assertIn(b'ZIP_DATA', send.call_args.args[0].data)
        self.assertIn(b'123456', send.call_args.args[0].data)

    def test_checkout_authentication_is_ephemeral(self):
        os.environ['ENGINE_REPO'] = 'Mostafameesr/NeomOS'
        os.environ['ENGINE_READ_TOKEN'] = 'PRIVATE_GIT_TOKEN'
        with mock.patch.object(ci.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            ci.checkout(self.engine)
        fetch = next(call for call in run.call_args_list if 'fetch' in call.args[0])
        self.assertIn('GIT_CONFIG_VALUE_0', fetch.kwargs['env'])
        for call in run.call_args_list:
            self.assertNotIn('PRIVATE_GIT_TOKEN', ' '.join(call.args[0]))
        self.assertNotIn('GIT_CONFIG_VALUE_0', os.environ)


if __name__ == '__main__':
    unittest.main(verbosity=2)
