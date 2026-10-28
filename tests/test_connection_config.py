import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evotrace_bridge.common import BridgeError
from evotrace_bridge.gateway import authorized_connection


class ConnectionConfiguration(unittest.TestCase):
    def test_environment_overrides_the_selected_utf8_file(self):
        with tempfile.TemporaryDirectory() as root:
            f = Path(root) / 'connection.env'
            f.write_text('DEEPSEEK_API_KEY=file-placeholder\nDEEPSEEK_MODEL=file-model\n', encoding='utf-8-sig')
            with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'environment-placeholder', 'DEEPSEEK_BASE_URL': 'http://localhost:12345/v1/', 'DEEPSEEK_MODEL': 'environment-model'}, clear=True):
                self.assertEqual(authorized_connection(f), ('http://localhost:12345/v1', 'environment-placeholder', 'environment-model'))

    def test_explicit_file_selection_does_not_need_a_personal_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            f = Path(root) / 'connection.env'
            f.write_text('DEEPSEEK_API_KEY="offline-placeholder"\nDEEPSEEK_MODEL=local-model\n', encoding='utf8')
            with patch.dict(os.environ, {'TRACEWALTZ_ENV_FILE': str(f)}, clear=True):
                self.assertEqual(authorized_connection(), ('https://api.deepseek.com', 'offline-placeholder', 'local-model'))

    def test_missing_or_example_key_is_rejected_before_any_model_call(self):
        with tempfile.TemporaryDirectory() as root:
            absent = Path(root) / 'absent.env'
            for values in [{}, {'DEEPSEEK_API_KEY': 'replace-me'}]:
                with self.subTest(values=values), patch.dict(os.environ, values, clear=True), self.assertRaises(BridgeError):
                    authorized_connection(absent)
