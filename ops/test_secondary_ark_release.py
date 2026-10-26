"""Check private configuration preservation and Compose interpolation safety."""
import ast
import os
from pathlib import Path
import re
import tempfile
import textwrap
import unittest


class SecondaryArkReleaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(Path(__file__).with_name('enable-secondary-ark-account.py').read_text())
        # The deployment module imports Linux fcntl; its pure file/config helpers
        # are also checked here on the Windows release workstation.
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {'update_env', 'override', 'private_write'}]
        cls.helpers = {'os': os, 're': re}
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<release-helpers>', 'exec'), cls.helpers)
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                if getattr(node.value.func, 'attr', '') == 'dedent':
                    compile(textwrap.dedent(node.value.args[0].value), '<container-probe>', 'exec')

    def test_env_replaces_duplicates_and_keeps_other_values(self):
        source = '# comment\nARK_API_KEY=primary\nexport ARK_API_KEY_2=old\nARK_API_KEY_2=duplicate\nOTHER="a$b"\n'
        result = self.helpers['update_env'](source, 'ARK_API_KEY_2', 'test-secondary')
        self.assertEqual(result, '# comment\nARK_API_KEY=primary\nOTHER="a$b"\nARK_API_KEY_2=test-secondary\n')

    def test_env_rejects_multiline_and_interpolated_tokens(self):
        for token in ('', 'a\nINJECTED=value', '${OTHER}', 'a"b', 'a b'):
            with self.subTest(token=token), self.assertRaises(ValueError):
                self.helpers['update_env']('OTHER=kept\n', 'ARK_API_KEY_2', token)

    def test_override_preserves_literal_dollars_and_input(self):
        values = {'SECRET': 'test-$token-${UNSET}-$$', 'OTHER': 'unchanged'}
        result = self.helpers['override']('sha256:test', values)['services']['app']
        self.assertEqual(result['image'], 'sha256:test')
        restored = {k:v.replace('$$', '$') for k,v in result['environment'].items()}
        self.assertEqual(restored, values)
        self.assertEqual(values['SECRET'], 'test-$token-${UNSET}-$$')

    def test_private_write_replaces_and_refuses_existing_temp(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            path = Path(directory)/'private.env'
            self.helpers['private_write'](path, 'before\n')
            self.helpers['private_write'](path, 'after\n')
            self.assertEqual(path.read_text(), 'after\n')
            temporary = path.with_name(path.name+'.secondary-account.tmp')
            temporary.write_text('existing\n')
            with self.assertRaises(FileExistsError):
                self.helpers['private_write'](path, 'uncommitted\n')
            self.assertEqual(path.read_text(), 'after\n')


if __name__ == '__main__':
    unittest.main()
