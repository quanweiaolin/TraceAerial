import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from artifact_registration import detect_git_info, register_artifact  # noqa: E402


def _init_repo(repo: Path) -> None:
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.email', 'a@b.c'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.name', 'a'], cwd=repo, check=True)
    (repo / 'f.txt').write_text('x')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=repo, check=True)


class TestDetectGitInfo(unittest.TestCase):
    def test_clean_repo_returns_commit_and_not_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            commit, dirty = detect_git_info(repo / 'f.txt')
            self.assertEqual(len(commit), 40)
            self.assertFalse(dirty)

    def test_dirty_repo_returns_dirty_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            (repo / 'f.txt').write_text('changed')
            _, dirty = detect_git_info(repo / 'f.txt')
            self.assertTrue(dirty)

    def test_no_git_repo_returns_none_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            commit, dirty = detect_git_info(Path(tmp) / 'f.txt')
            self.assertIsNone(commit)
            self.assertIsNone(dirty)


def _mock_urlopen_success():
    cm = mock.MagicMock()
    cm.__enter__.return_value.read.return_value = b'{"id": 1}'
    return cm


class TestRegisterArtifact(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pending_file = Path(self.tmp.name) / 'pending_artifacts.jsonl'

    def tearDown(self):
        self.tmp.cleanup()

    @mock.patch('artifact_registration.urlopen')
    def test_backend_unreachable_does_not_raise(self, mock_urlopen):
        mock_urlopen.side_effect = OSError('connection refused')
        register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                           pending_file=self.pending_file)  # 不能抛异常

    @mock.patch('artifact_registration.urlopen')
    def test_network_error_appends_to_pending_file(self, mock_urlopen):
        mock_urlopen.side_effect = OSError('connection refused')
        register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                           pending_file=self.pending_file)
        lines = self.pending_file.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])['output_path'], '/b.csv')

    @mock.patch('artifact_registration.urlopen')
    def test_422_logs_error_and_is_not_added_to_pending(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError('url', 422, 'Unprocessable', {}, None)
        with self.assertLogs('artifact_registration', level='ERROR') as log_ctx:
            register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                               pending_file=self.pending_file)
        self.assertTrue(any('422' in m or 'Unprocessable' in m for m in log_ctx.output))
        self.assertFalse(self.pending_file.exists())

    @mock.patch('artifact_registration.urlopen')
    def test_network_error_logs_below_error_level(self, mock_urlopen):
        mock_urlopen.side_effect = OSError('timeout')
        with self.assertLogs('artifact_registration') as log_ctx:
            register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                               pending_file=self.pending_file)
        self.assertFalse(any(r.levelname == 'ERROR' for r in log_ctx.records))

    def test_next_call_flushes_pending_entry_once_backend_recovers(self):
        self.pending_file.parent.mkdir(parents=True, exist_ok=True)
        self.pending_file.write_text(json.dumps({
            'stage_code': 'ad_perception_tracking', 'input_video_paths': ['/old.MP4'],
            'output_path': '/old.csv', 'metadata': {},
        }) + '\n')
        with mock.patch('artifact_registration.urlopen') as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_success()
            register_artifact('ad_perception_tracking', ['/new.MP4'], '/new.csv', {},
                               pending_file=self.pending_file)
            self.assertEqual(mock_urlopen.call_count, 2)  # 一次补发/old.csv，一次发/new.csv
        self.assertFalse(self.pending_file.exists())


class TestCodeCommitOverride(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pending_file = Path(self.tmp.name) / 'pending_artifacts.jsonl'
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        _init_repo(self.repo)
        self.real_commit = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    def tearDown(self):
        self.tmp.cleanup()

    def _capture_body(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['body'] = json.loads(request.data)
            return _mock_urlopen_success()

        return captured, fake_urlopen

    def test_no_override_autodetects_current_head(self):
        captured, fake_urlopen = self._capture_body()
        with mock.patch('artifact_registration.urlopen', side_effect=fake_urlopen):
            with mock.patch('artifact_registration._GIT_SEARCH_ROOT', self.repo / 'f.txt'):
                register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                                   pending_file=self.pending_file)
        self.assertEqual(captured['body']['code_commit'], self.real_commit)

    def test_explicit_none_override_leaves_commit_and_dirty_blank(self):
        captured, fake_urlopen = self._capture_body()
        with mock.patch('artifact_registration.urlopen', side_effect=fake_urlopen):
            register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                               pending_file=self.pending_file, code_commit_override=None)
        self.assertIsNone(captured['body']['code_commit'])
        self.assertIsNone(captured['body']['code_dirty'])

    def test_resolvable_short_hash_override_fills_full_commit(self):
        short_hash = self.real_commit[:8]
        captured, fake_urlopen = self._capture_body()
        with mock.patch('artifact_registration.urlopen', side_effect=fake_urlopen):
            with mock.patch('artifact_registration._GIT_SEARCH_ROOT', self.repo / 'f.txt'):
                register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                                   pending_file=self.pending_file, code_commit_override=short_hash)
        self.assertEqual(captured['body']['code_commit'], self.real_commit)
        self.assertIsNone(captured['body']['code_dirty'])

    def test_unresolvable_override_warns_and_still_registers(self):
        with mock.patch('artifact_registration.urlopen') as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_success()
            with mock.patch('artifact_registration._GIT_SEARCH_ROOT', self.repo / 'f.txt'):
                with self.assertLogs('artifact_registration', level='WARNING'):
                    register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                                       pending_file=self.pending_file,
                                       code_commit_override='not-a-real-hash')
        self.assertEqual(mock_urlopen.call_count, 1)  # 仍然发出了注册请求


if __name__ == '__main__':
    unittest.main()
