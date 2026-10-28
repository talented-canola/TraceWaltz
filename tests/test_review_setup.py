import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('review_setup', ROOT / 'tools/pin-review-packages.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class ReviewSetup(unittest.TestCase):
    def make_source(self, root):
        source = root / 'source'
        plugin = source / 'harness/plugins/orchestrator.mjs'
        plugin.parent.mkdir(parents=True)
        plugin.write_text('', encoding='utf8')
        modules = root / 'review/node_modules'
        modules.mkdir(parents=True)
        return source, modules

    @unittest.skipIf(os.name == 'nt', 'Installer creates Linux dependency links')
    def test_link_is_correct_and_repeat_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, modules = self.make_source(Path(tmp))
            first = setup.link_modules(source, modules)
            self.assertTrue(first.is_symlink())
            self.assertEqual(first.resolve(), modules)
            self.assertEqual(setup.link_modules(source, modules), first)

    def test_existing_dependency_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, modules = self.make_source(Path(tmp))
            (source / 'node_modules').mkdir()
            marker = source / 'node_modules/owned.txt'
            marker.write_text('existing', encoding='utf8')
            with self.assertRaisesRegex(RuntimeError, 'preserved'):
                setup.link_modules(source, modules)
            self.assertEqual(marker.read_text(), 'existing')

    def test_missing_review_dependencies_do_not_create_a_broken_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, modules = self.make_source(Path(tmp))
            with self.assertRaisesRegex(RuntimeError, 'Prepare'):
                setup.link_modules(source, modules / 'absent')
            self.assertFalse((source / 'node_modules').is_symlink())
