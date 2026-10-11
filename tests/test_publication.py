"""Exercise the release script against a real local Git remote."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / 'extensions/scripts/publish_pages.sh'


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote.git'
        self.seed = self.root / 'seed'
        self.module = self.root / 'extensions'
        self.publish = self.root / 'pages'
        self.env = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.com',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.com',
                        PAGES_REPOSITORY_URL=str(self.remote), PUBLISH_DIR=str(self.publish),
                        PAGES_BRANCH='gh-pages')
        self.run_cmd('git', 'init', '--bare', str(self.remote))
        self.run_cmd('git', 'init', '-b', 'gh-pages', str(self.seed))
        (self.seed / 'obsolete.txt').write_text('old')
        self.run_cmd('git', '-C', str(self.seed), 'add', '.')
        self.run_cmd('git', '-C', str(self.seed), 'commit', '-m', 'Initial release')
        self.run_cmd('git', '-C', str(self.seed), 'push', str(self.remote), 'gh-pages')
        self.initial = self.run_cmd('git', '-C', str(self.seed), 'rev-parse', 'HEAD').stdout.strip()
        (self.module / 'scripts').mkdir(parents=True)
        (self.module / 'scripts/publish_pages.sh').write_bytes(SCRIPT.read_bytes())
        (self.module / 'repo').mkdir()
        (self.module / 'repo/index.min.json').write_text(json.dumps([]))
        (self.module / 'repo/repo.json').write_text(json.dumps({'meta': {}}))

    def run_cmd(self, *args, check=True):
        return subprocess.run(args, env=self.env, text=True, capture_output=True, check=check)

    def publish_repo(self):
        return self.run_cmd('bash', str(self.module / 'scripts/publish_pages.sh'), check=False)

    def test_publish_and_repeat_preserve_release_history(self):
        result = self.publish_repo()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.publish / 'obsolete.txt').exists())
        self.assertTrue((self.publish / '.nojekyll').exists())
        parent = self.run_cmd('git', '-C', str(self.publish), 'rev-parse', 'HEAD^').stdout.strip()
        self.assertEqual(parent, self.initial)
        first = self.run_cmd('git', '-C', str(self.publish), 'rev-parse', 'HEAD').stdout
        result = self.publish_repo()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No changes to publish.', result.stdout)
        self.assertEqual(first, self.run_cmd('git', '-C', str(self.publish), 'rev-parse', 'HEAD').stdout)
        self.assertEqual(first.strip(), self.run_cmd('git', '--git-dir', str(self.remote), 'rev-parse', 'gh-pages').stdout.strip())

    def test_reject_dirty_directory(self):
        self.assertEqual(self.publish_repo().returncode, 0)
        (self.publish / 'local.txt').write_text('keep')
        result = self.publish_repo()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('uncommitted changes', result.stderr)
        self.assertEqual((self.publish / 'local.txt').read_text(), 'keep')

    def test_reject_wrong_remote(self):
        self.assertEqual(self.publish_repo().returncode, 0)
        self.run_cmd('git', '-C', str(self.publish), 'remote', 'set-url', 'origin', 'wrong')
        result = self.publish_repo()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('independent clone', result.stderr)

    def test_missing_artifact_does_not_clone(self):
        (self.module / 'repo/repo.json').unlink()
        result = self.publish_repo()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing repo artifact', result.stderr)
        self.assertFalse(self.publish.exists())

    def test_reject_wrong_branch(self):
        self.assertEqual(self.publish_repo().returncode, 0)
        self.run_cmd('git', '-C', str(self.publish), 'checkout', '-b', 'main')
        result = self.publish_repo()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('independent clone', result.stderr)

    def test_reject_existing_non_repository(self):
        self.publish.mkdir()
        (self.publish / 'keep.txt').write_text('keep')
        result = self.publish_repo()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.publish / 'keep.txt').read_text(), 'keep')
