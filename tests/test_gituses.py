"""Tests for gituses: unit tests plus real git clone/push integration tests."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from gituses.repo import GitusesError, Store, validate_name
from gituses.server import GitusesServer

TOKEN = "test-token-123"


def git(*args, cwd=None, check=True):
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Tester",
            "GIT_AUTHOR_EMAIL": "tester@example.com",
            "GIT_COMMITTER_NAME": "Tester",
            "GIT_COMMITTER_EMAIL": "tester@example.com",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GITUSES_QUIET": "1",
        }
    )
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=check,
    )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="gituses-store-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = Store(self.root)

    def test_init_creates_config_and_token(self):
        self.assertFalse(os.path.exists(self.store.config_path))
        self.store.init(token=TOKEN)
        with open(self.store.config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        self.assertEqual(config["token"], TOKEN)
        self.assertFalse(config["anonymous"])
        self.assertEqual(self.store.token(), TOKEN)

    def test_init_random_token_and_anonymous(self):
        token = self.store.init()
        self.assertGreaterEqual(len(token), 20)
        # Re-initialising is a friendly error, not a destructive overwrite.
        with self.assertRaises(GitusesError):
            self.store.init()
        anon_root = tempfile.mkdtemp(prefix="gituses-anon-")
        self.addCleanup(shutil.rmtree, anon_root, True)
        anon = Store(anon_root)
        self.assertEqual(anon.init(anonymous=True), "")
        self.assertEqual(anon.token(), "")

    def test_create_list_delete(self):
        self.store.init(anonymous=True)
        path = self.store.create("demo", description="hello")
        self.assertTrue(os.path.isdir(os.path.join(path, "objects")))
        self.assertEqual(
            self.store.list(),
            [{"name": "demo", "path": path, "description": "hello"}],
        )
        with self.assertRaises(GitusesError):
            self.store.create("demo")
        self.store.delete("demo")
        self.assertEqual(self.store.list(), [])
        with self.assertRaises(GitusesError):
            self.store.delete("demo")

    def test_name_validation(self):
        for good in ("a", "demo", "my.repo", "a_b-c", "A1", "x" * 100):
            self.assertEqual(validate_name(good), good)
            self.assertEqual(validate_name(good + ".git"), good)
        for bad in ("", ".", "..", "../evil", "a/b", "a\\b", "-lead", ".hidden", "x" * 101):
            with self.assertRaises(GitusesError):
                validate_name(bad)

    def test_existing_repo_dir_is_still_served(self):
        self.store.create("orphan")
        self.store.require_initialised()
        self.assertTrue(os.path.exists(self.store.config_path))
        self.assertEqual(self.store.token(), "")
        self.assertEqual([r["name"] for r in self.store.list()], ["orphan"])


class BaseServerCase(unittest.TestCase):
    """Shared fixture: a live server backed by a temporary store."""

    token = TOKEN

    def setUp(self):
        os.environ.pop("GITUSES_TOKEN", None)
        os.environ["GITUSES_QUIET"] = "1"  # keep unittest output readable
        self.root = tempfile.mkdtemp(prefix="gituses-root-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = Store(self.root)
        self.store.init(token=self.token)
        self.store.create("demo", description="demo repository")
        self.server = GitusesServer(("127.0.0.1", 0), self.store, token=self.token)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.host, self.port = self.server.server_address[:2]
        self.work = tempfile.mkdtemp(prefix="gituses-work-")
        self.addCleanup(shutil.rmtree, self.work, True)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # -- helpers --------------------------------------------------------
    def url(self, name="demo", token=None, user="git"):
        token = self.token if token is None else token
        if token:
            return "http://%s:%s@%s:%d/%s.git" % (user, token, self.host, self.port, name)
        return "http://%s:%d/%s.git" % (self.host, self.port, name)

    def anonymous_url(self, name="demo"):
        return "http://%s:%d/%s.git" % (self.host, self.port, name)

    def http(self, path, method="GET", token=None, headers=None):
        request = urllib.request.Request(
            "http://%s:%d%s" % (self.host, self.port, path), method=method
        )
        if token is not None:
            import base64

            encoded = base64.b64encode(("git:%s" % token).encode()).decode()
            request.add_header("Authorization", "Basic " + encoded)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def make_work_repo(self, name="work"):
        path = os.path.join(self.work, name)
        os.makedirs(path)
        git("init", "-q", "-b", "main", cwd=path)
        return path

    def commit_file(self, repo, relpath, content, message):
        full = os.path.join(repo, relpath)
        os.makedirs(os.path.dirname(full) or repo, exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(full, mode) as handle:
            handle.write(content)
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", message, cwd=repo)
        return git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    def ls_remote(self, url):
        return git("ls-remote", url).stdout


class ServerTests(BaseServerCase):
    """End-to-end tests against a live server with real git clients."""

    # -- tests ----------------------------------------------------------
    def test_health_endpoint(self):
        status, headers, body = self.http("/_health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["repositories"], 1)
        self.assertFalse(payload["anonymous"])

    def test_index_lists_repositories(self):
        status, headers, body = self.http("/")
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn("demo", text)
        self.assertIn("demo repository", text)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))

    def test_authentication_is_enforced(self):
        status, headers, _ = self.http("/demo.git/info/refs?service=git-upload-pack")
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])

        status, _, _ = self.http(
            "/demo.git/info/refs?service=git-upload-pack", token="wrong-token"
        )
        self.assertEqual(status, 401)

        status, _, body = self.http(
            "/demo.git/info/refs?service=git-upload-pack", token=self.token
        )
        self.assertEqual(status, 200)
        self.assertIn(b"git-upload-pack", body)

        result = git("ls-remote", self.anonymous_url(), check=False)
        self.assertNotEqual(result.returncode, 0)
        combined = (result.stderr + result.stdout).lower()
        self.assertTrue(
            "authentication" in combined
            or "could not read username" in combined
            or "terminal prompts disabled" in combined,
            combined,
        )

    def test_bearer_and_username_token_are_accepted(self):
        status, _, _ = self.http(
            "/demo.git/info/refs?service=git-upload-pack",
            headers={"Authorization": "Bearer " + self.token},
        )
        self.assertEqual(status, 200)
        # Some clients put the token in the username field, with no password.
        import base64

        encoded = base64.b64encode((self.token + ":").encode()).decode()
        status, _, _ = self.http(
            "/demo.git/info/refs?service=git-upload-pack",
            headers={"Authorization": "Basic " + encoded},
        )
        self.assertEqual(status, 200)

    def test_missing_and_invalid_paths(self):
        status, _, _ = self.http(
            "/nope.git/info/refs?service=git-upload-pack", token=self.token
        )
        self.assertEqual(status, 404)
        status, _, _ = self.http("/not-a-repo/info/refs", token=self.token)
        self.assertEqual(status, 404)
        status, _, _ = self.http("/..%2F..%2Fetc/passwd", token=self.token)
        self.assertIn(status, (400, 404))

    def test_clone_push_clone_roundtrip(self):
        work = self.make_work_repo()
        first = self.commit_file(work, "README.md", "hello gituses\n", "initial commit")
        self.commit_file(work, "src/app.py", "print('hi')\n", "add app")
        push = git("push", self.url(), "main", cwd=work)
        self.assertIn("main", push.stdout + push.stderr)

        clone_dir = os.path.join(self.work, "clone")
        git("clone", "-q", self.url(), clone_dir)
        self.assertEqual(git("rev-parse", "HEAD", cwd=clone_dir).stdout.strip(),
                         git("rev-parse", "HEAD", cwd=work).stdout.strip())
        with open(os.path.join(clone_dir, "README.md")) as handle:
            self.assertEqual(handle.read(), "hello gituses\n")
        with open(os.path.join(clone_dir, "src/app.py")) as handle:
            self.assertEqual(handle.read(), "print('hi')\n")
        log = git("log", "--format=%s", cwd=clone_dir).stdout.split()
        self.assertEqual(log, ["add", "app", "initial", "commit"])
        self.assertTrue(first)

    def test_history_rewrite_and_force_push(self):
        work = self.make_work_repo()
        self.commit_file(work, "file.txt", "one\n", "one")
        git("push", self.url(), "main", cwd=work)
        # Amend the tip and force push: no artificial restriction on rewrites.
        self.commit_file(work, "file.txt", "rewritten\n", "rewritten")
        git("reset", "-q", "--hard", "HEAD~1", cwd=work)
        self.commit_file(work, "file.txt", "rewritten\n", "rewritten")
        git("push", "--force", self.url(), "main", cwd=work)

        clone_dir = os.path.join(self.work, "clone")
        git("clone", "-q", self.url(), clone_dir)
        with open(os.path.join(clone_dir, "file.txt")) as handle:
            self.assertEqual(handle.read(), "rewritten\n")
        subjects = git("log", "--format=%s", cwd=clone_dir).stdout.splitlines()
        self.assertEqual(subjects, ["rewritten", "one"])

    def test_many_repositories_and_branches(self):
        names = ["repo-%02d" % index for index in range(25)]
        for name in names:
            self.store.create(name)
        self.assertEqual(len(self.store.list()), len(names) + 1)
        for name in names:
            self.assertEqual(self.ls_remote(self.url(name)), "")

        work = self.make_work_repo()
        self.commit_file(work, "a.txt", "a\n", "base")
        for index in range(10):
            git("checkout", "-q", "-b", "feature-%d" % index, "main", cwd=work)
            self.commit_file(work, "b.txt", "branch %d\n" % index, "branch %d" % index)
            git("push", "-q", self.url(), "feature-%d" % index, cwd=work)
            git("checkout", "-q", "main", cwd=work)
        remote = self.ls_remote(self.url())
        for index in range(10):
            self.assertIn("refs/heads/feature-%d" % index, remote)

    def test_large_push_is_streamed_and_intact(self):
        work = self.make_work_repo("large")
        payload = os.urandom(4 * 1024 * 1024)
        self.commit_file(work, "blob.bin", payload, "add 4 MiB blob")
        git("push", "-q", self.url(), "main", cwd=work)

        clone_dir = os.path.join(self.work, "large-clone")
        git("clone", "-q", self.url(), clone_dir)
        path = os.path.join(clone_dir, "blob.bin")
        with open(path, "rb") as handle:
            clone_payload = handle.read()
        self.assertEqual(len(clone_payload), len(payload))
        self.assertEqual(hashlib.sha256(clone_payload).hexdigest(),
                         hashlib.sha256(payload).hexdigest())

    def test_concurrent_clones(self):
        work = self.make_work_repo()
        self.commit_file(work, "shared.txt", "shared\n", "shared")
        git("push", "-q", self.url(), "main", cwd=work)
        expected = git("rev-parse", "HEAD", cwd=work).stdout.strip()

        def clone(index):
            target = os.path.join(self.work, "parallel-%d" % index)
            git("clone", "-q", self.url(), target)
            return git("rev-parse", "HEAD", cwd=target).stdout.strip()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            heads = list(pool.map(clone, range(8)))
        self.assertEqual(heads, [expected] * 8)

    def test_deleted_repository_stops_serving(self):
        self.store.delete("demo")
        status, _, _ = self.http(
            "/demo.git/info/refs?service=git-upload-pack", token=self.token
        )
        self.assertEqual(status, 404)


class AnonymousServerTests(BaseServerCase):
    """An anonymous store must work without credentials in the URL."""

    token = ""

    def test_anonymous_read_and_write(self):
        status, _, body = self.http("/demo.git/info/refs?service=git-upload-pack")
        self.assertEqual(status, 200)
        self.assertIn(b"git-upload-pack", body)

        work = self.make_work_repo()
        self.commit_file(work, "README.md", "anonymous\n", "initial")
        git("push", "-q", self.anonymous_url(), "main", cwd=work)
        clone_dir = os.path.join(self.work, "anon-clone")
        git("clone", "-q", self.anonymous_url(), clone_dir)
        with open(os.path.join(clone_dir, "README.md")) as handle:
            self.assertEqual(handle.read(), "anonymous\n")

    def test_health_reports_anonymous(self):
        status, _, body = self.http("/_health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["anonymous"])


if __name__ == "__main__":
    unittest.main()


