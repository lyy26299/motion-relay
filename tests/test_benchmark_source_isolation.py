"""A benchmark must not silently mix baseline and editable-install sources."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.benchmark_architecture import source_module_manifest


class BenchmarkSourceTests(unittest.TestCase):
    def test_manifest_records_only_bound_coach_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'coach.py'
            path.write_text('# fixture\n')
            result = source_module_manifest(root, {
                'coach': SimpleNamespace(__file__=str(path)),
                'other': SimpleNamespace(__file__='/outside/other.py'),
            })
            self.assertEqual(result, {'coach': {
                'path': 'coach.py', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            }})

    def test_editable_fallback_outside_source_root_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, 'outside --source-root'):
                source_module_manifest(root / 'baseline', {
                    'coach.voice_state': SimpleNamespace(__file__=str(root/'current.py')),
                })

    def test_missing_module_origin_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'has no file'):
            source_module_manifest(Path('.'), {'coach.voice_state': SimpleNamespace()})

    def test_symlink_cannot_disguise_foreign_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / 'baseline'
            baseline.mkdir()
            foreign = root / 'current.py'
            foreign.write_text('# fixture\n')
            link = baseline / 'voice.py'
            link.symlink_to(foreign)
            with self.assertRaisesRegex(RuntimeError, 'outside --source-root'):
                source_module_manifest(baseline, {'coach.voice_state': SimpleNamespace(__file__=str(link))})
