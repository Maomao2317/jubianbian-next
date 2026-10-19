"""Exercise release metadata and preservation without Docker or a live server."""
from contextlib import ExitStack
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class ReleaseDeploymentTests(unittest.TestCase):
    def test_deploy_preserves_published_revision_and_configuration(self):
        spec = importlib.util.spec_from_file_location(
            'repair_deployment', Path(__file__).with_name('deploy-repair-recovery.py'))
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'fcntl': types.SimpleNamespace()}):
            spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as mocks:
            base = Path(tmp)/'release'
            root = Path(tmp)/'staging'
            base.mkdir()
            root.mkdir()
            relative = 'backend/app/config.py'
            for parent, text in [(base, 'new'), (root, 'old')]:
                target = parent/relative
                target.parent.mkdir(parents=True)
                target.write_text(text)
            (root/'fangzhou.env').write_text('JBB_BUILD_VERSION=old\nKEEP=unchanged\n')
            image = 'sha256:new'
            previous = {'Image': 'sha256:old', 'Config': {'Env': [
                'JBB_ENVIRONMENT=staging', 'JBB_BUILD_VERSION=old', 'KEEP=unchanged']},
                'Mounts': [{'Source': '/data/staging', 'Destination': '/app/data', 'RW': True}],
                'HostConfig': {'PortBindings': {'8000/tcp': [{'HostPort': '18000'}]}}}
            current = dict(previous, Image=image, Config={'Env': [
                'JBB_ENVIRONMENT=staging', 'JBB_BUILD_VERSION='+module.BUILD, 'KEEP=unchanged']})
            digest = hashlib.sha256(b'frontend').hexdigest()
            manifest = {'baseCommit': 'b'*40, 'patch': 'digest', 'files': {
                'web-staging/js/app.js': digest, 'web-staging/js/config.js': digest}}
            published = {'commit': 'a'*40, 'ref': 'refs/heads/codex/repair-batch-content'}
            for name, value in [('BASE', base), ('ALLOWED', {relative})]:
                mocks.enter_context(patch.object(module, name, value))
            mocks.enter_context(patch.object(module, 'Path', side_effect=lambda p:
                root if str(p) == '/opt/jubianbian-staging' else Path(p)))
            for name in ['baseline', 'idle', 'compose', 'run']:
                mocks.enter_context(patch.object(module, name))
            mocks.enter_context(patch.object(module, 'inspect', side_effect=[previous, current]))
            mocks.enter_context(patch.object(module, 'health', return_value={'status': 'ok'}))
            mocks.enter_context(patch.object(module, 'urlopen', side_effect=lambda *a, **k: io.BytesIO(b'frontend')))
            mocks.enter_context(patch.object(module.subprocess, 'check_output',
                                           return_value=json.dumps([{'Id': image}]).encode()))
            module.deploy('jubianbian-staging', 18000, 'staging', image, manifest, published)
            record = json.loads((base/'staging-deployment.json').read_text())
            self.assertEqual(record['sourceCommit'], published['commit'])
            self.assertEqual(record['sourceRef'], published['ref'])
            self.assertTrue(record['configurationPreserved'])
            self.assertTrue(record['dataMountsPreserved'])
            self.assertEqual((root/relative).read_text(), 'new')
            self.assertEqual((Path(record['backup'])/relative).read_text(), 'old')
            self.assertIn('KEEP=unchanged', (root/'fangzhou.env').read_text())
            self.assertEqual(published, {'commit': 'a'*40, 'ref': 'refs/heads/codex/repair-batch-content'})


if __name__ == '__main__':
    unittest.main()
