"""Resolve real Compose configuration without contacting Docker or SES."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent


class EmailConfigurationTests(unittest.TestCase):
    def test_private_email_settings_survive_both_compose_invocations(self):
        settings = {
            'TENCENTCLOUD_SECRET_ID': 'dummy-id',
            'TENCENTCLOUD_SECRET_KEY': 'dummy-key',
            'TENCENTCLOUD_REGION': 'ap-hongkong',
            'TENCENTCLOUD_SES_ENDPOINT': 'ses.example.invalid',
            'TENCENTCLOUD_SES_FROM_EMAIL': 'sender@example.invalid',
            'TENCENTCLOUD_SES_FROM_NAME': 'Test Sender',
            'TENCENTCLOUD_SES_TEMPLATE_ID': '12345',
            'JBB_AUTH_ALLOW_DEV_CODE': '1',
        }
        # Do not let the operator's shell or real private files affect this test.
        clean_env = {k: v for k, v in os.environ.items()
                     if k.upper() not in settings and not k.startswith('COMPOSE_')}
        clean_env['COMPOSE_DISABLE_ENV_FILE'] = '1'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'fangzhou.env').write_text(
                ''.join(f'{k}={v}\n' for k, v in settings.items()), encoding='utf-8')
            for name in ['docker-compose.yml', 'docker-compose.staging.yml']:
                target = root / name
                target.write_bytes((ROOT / name).read_bytes())
                for explicit in [False, True]:
                    with self.subTest(compose=name, explicit_env_file=explicit):
                        command = ['docker', 'compose', '-p', 'jbb-mail-regression']
                        if explicit:
                            command += ['--env-file', str(root / 'fangzhou.env')]
                        command += ['-f', str(target), 'config', '--format', 'json']
                        result = subprocess.run(command, env=clean_env, cwd=root,
                                                capture_output=True, check=True)
                        actual = json.loads(result.stdout)['services']['app']['environment']
                        for key, value in settings.items():
                            self.assertEqual(str(actual[key]),
                                             '0' if key == 'JBB_AUTH_ALLOW_DEV_CODE' else value)


if __name__ == '__main__':
    unittest.main()
