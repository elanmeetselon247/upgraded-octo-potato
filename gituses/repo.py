"""On-disk repository store used by the ``gituses`` CLI and server."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time

# Repository names are intentionally permissive, but they must stay a single
# safe path component so that a request can never escape the store root.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

CONFIG_NAME = "gituses.json"


class GitusesError(RuntimeError):
    """Raised for user facing errors (bad name, missing store, git failure)."""


def validate_name(name: str) -> str:
    """Return the canonical repository name (without ``.git``).

    Raises :class:`GitusesError` for anything that is not a plain, safe name.
    """
    if not isinstance(name, str):
        raise GitusesError("repository name must be a string")
    canonical = name[:-4] if name.endswith(".git") else name
    if (
        not NAME_RE.match(canonical)
        or canonical in (".", "..")
        or ".." in canonical
        or "/" in canonical
        or "\\" in canonical
    ):
        raise GitusesError(
            "invalid repository name %r: use letters, digits, dot, dash or "
            "underscore, starting with a letter or digit (max 100 chars)" % (name,)
        )
    return canonical


def _run_git(args, cwd=None):
    """Run git, raising GitusesError with git's own message on failure."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - git is a hard dep
        raise GitusesError("git executable not found on PATH") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitusesError("git %s failed: %s" % (" ".join(args), detail))
    return proc


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Store:
    """A directory of bare repositories plus its ``gituses.json`` config."""

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))

    # -- config ---------------------------------------------------------
    @property
    def config_path(self) -> str:
        return os.path.join(self.root, CONFIG_NAME)

    def exists(self) -> bool:
        return os.path.isdir(self.root)

    def load_config(self) -> dict:
        try:
            with open(self.config_path, encoding="utf-8") as handle:
                config = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise GitusesError("cannot read %s: %s" % (self.config_path, exc)) from exc
        if not isinstance(config, dict):
            raise GitusesError("%s is not a JSON object" % self.config_path)
        return config

    def save_config(self, config: dict) -> None:
        os.makedirs(self.root, exist_ok=True)
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.config_path)

    def token(self) -> str:
        """The shared access token, or "" when the store is anonymous."""
        env_token = os.environ.get("GITUSES_TOKEN")
        if env_token is not None:
            return env_token.strip()
        return str(self.load_config().get("token") or "").strip()

    def init(self, token: str | None = None, anonymous: bool = False) -> str:
        """Initialise the store. Returns the token in use ("" if anonymous)."""
        os.makedirs(self.root, exist_ok=True)
        if os.path.exists(self.config_path):
            raise GitusesError(
                "store already initialised at %s (config: %s)"
                % (self.root, self.config_path)
            )
        if anonymous:
            token = ""
        elif token is None:
            token = secrets.token_urlsafe(24)
        config = {
            "version": 1,
            "token": token,
            "anonymous": not token,
            "created_at": utcnow(),
        }
        self.save_config(config)
        return token

    def require_initialised(self) -> None:
        if not os.path.isdir(self.root):
            raise GitusesError(
                "no repository store at %s; run 'gituses init --root %s' first"
                % (self.root, self.root)
            )
        if not os.path.exists(self.config_path):
            # A bare directory of repos is still usable; record an anonymous
            # config so the store is self describing from now on.
            self.save_config(
                {"version": 1, "token": "", "anonymous": True, "created_at": utcnow()}
            )

    # -- repositories ---------------------------------------------------
    def repo_path(self, name: str) -> str:
        return os.path.join(self.root, validate_name(name) + ".git")

    def exists_repo(self, name: str) -> bool:
        try:
            path = self.repo_path(name)
        except GitusesError:
            return False
        return os.path.isdir(path)

    def create(self, name: str, description: str = "") -> str:
        """Create a bare repository and return its path."""
        canonical = validate_name(name)
        path = self.repo_path(canonical)
        if os.path.exists(path):
            raise GitusesError("repository %r already exists" % canonical)
        os.makedirs(self.root, exist_ok=True)
        _run_git(["init", "--bare", "--quiet", path])
        # Pushing over HTTP is opt-in for git-http-backend; set it explicitly
        # so hand-created repositories behave the same way.
        _run_git(["config", "http.receivepack", "true"], cwd=path)
        _run_git(["config", "uploadpack.allowFilter", "true"], cwd=path)
        _run_git(["config", "uploadpack.allowAnySHA1InWant", "true"], cwd=path)
        # A predictable initial branch avoids the "master" hint noise.
        _run_git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=path)
        if description:
            with open(os.path.join(path, "description"), "w", encoding="utf-8") as fh:
                fh.write(description.strip() + "\n")
        return path

    def list(self) -> list[dict]:
        """All repositories, sorted by name."""
        repos = []
        if not os.path.isdir(self.root):
            return repos
        for entry in sorted(os.listdir(self.root)):
            if not entry.endswith(".git"):
                continue
            path = os.path.join(self.root, entry)
            if not os.path.isdir(path):
                continue
            repos.append(
                {
                    "name": entry[:-4],
                    "path": path,
                    "description": self._description(path),
                }
            )
        return repos

    @staticmethod
    def _description(path: str) -> str:
        try:
            with open(
                os.path.join(path, "description"), encoding="utf-8", errors="replace"
            ) as fh:
                text = fh.read().strip()
        except OSError:
            return ""
        default = (
            "Unnamed repository; edit this file 'description' to name the repository."
        )
        return "" if text == default else text

    def delete(self, name: str) -> None:
        canonical = validate_name(name)
        path = self.repo_path(canonical)
        if not os.path.isdir(path):
            raise GitusesError("repository %r does not exist" % canonical)
        shutil.rmtree(path)
