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

# Everything git would otherwise cap on a push or a fetch, switched off.  These
# are applied to every repository gituses tracks (including ones that already
# existed), so the "unlimited" promise is enforced by git itself and not just
# by our own plumbing.  Each value is the one git's documentation defines as
# "no limit"; see the comments for the reason each key is here.
UNLIMITED_REPO_CONFIG = {
    # Accept pushes over HTTP (opt-in in git-http-backend).
    "http.receivepack": "true",
    # Pack streams of any size: "If not set or set to 0, then the size is
    # unlimited." (Documentation/config/receive.txt). Hosting providers use
    # this key for push-size caps; we set it to 0 instead.
    "receive.maxInputSize": "0",
    # Always keep an incoming push as a pack instead of exploding it into one
    # loose object file per object. 0 disables the unpack heuristic entirely,
    # which is what makes pushes with millions of objects fast and predictable.
    "receive.unpackLimit": "0",
    # Allow force pushes, history rewrites and branch/tag deletion.
    "receive.denyDeletes": "false",
    "receive.denyNonFastForwards": "false",
    # Do not inject keepalive packets while a large pack is being written.
    "receive.keepAlive": "0",
    # Partial clones (--filter=blob:none) and arbitrary object fetches.
    "uploadpack.allowFilter": "true",
    "uploadpack.allowAnySHA1InWant": "true",
}


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


def _parse_config_file(config_path: str) -> dict:
    """Read a git config file into ``{lowercased dotted key: value}``.

    This is deliberately a small, direct reader rather than ``git config
    --get-regexp``: a dotted key name has to be escaped for git's regex engine,
    so reading the file avoids that whole class of bug.  Only the flat
    ``[section]`` / ``key = value`` form git writes is handled, which is all a
    repository's own ``config`` file ever uses.
    """
    values = {}
    section = None
    with open(config_path, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line[0] in "#;":
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip().strip('"').lower()
                continue
            if section is None:
                # The old ``[section.subsection]`` style: only the first dot
                # separates the section from the variable name.
                continue
            key, sep, value = line.partition("=")
            if not sep:
                key, value = line, "true"  # ``key`` alone means true
            values["%s.%s" % (section, key.strip().lower())] = value.strip()
    return values


def _policy_already_local(repo_path: str) -> bool:
    """True when every policy key is set, with the right value, in this repo.

    Only the repository's own config file counts: a matching value inherited
    from a system or global config does not make this repository unlimited, so
    the policy must still be written locally.
    """
    config_path = os.path.join(repo_path, "config")
    if not os.path.isfile(config_path):
        return False
    try:
        local = _parse_config_file(config_path)
    except OSError:
        return False
    return all(
        local.get(key.lower()) == value
        for key, value in UNLIMITED_REPO_CONFIG.items()
    )


def _write_config_values(repo_path: str, values: dict) -> None:
    """Append the missing keys in the same format ``git config`` writes.

    ``git config`` accepts only one key per process, so writing a policy of a
    dozen keys would cost a dozen spawns; appending to ``config`` is what git
    itself does.  Because a *later* value wins in git, every section that gets
    a new key is first rewritten without its old copies of those keys, so the
    file never accumulates stale duplicates that shadow the policy.

    The write is atomic (temp file + ``os.replace``) so an interrupted run
    cannot leave a half-written config behind.
    """
    config_path = os.path.join(repo_path, "config")
    sections = {}
    for key, value in values.items():
        # git compares section and variable names case-insensitively and writes
        # them back lowercased, so canonicalise them here: the file then reads
        # back exactly the way _policy_already_local() parses it.
        name, _, variable = key.rpartition(".")
        sections.setdefault(name.lower(), {})[variable.lower()] = value

    kept = []
    section = None
    skip_section = False
    try:
        with open(config_path, encoding="utf-8") as handle:
            existing = handle.read()
    except FileNotFoundError:
        existing = ""
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().strip('"').lower()
            skip_section = section in sections
        if skip_section:
            continue
        kept.append(line)

    for name, items in sections.items():
        if kept and kept[-1].strip():
            kept.append("")
        kept.append("[%s]" % name)
        for variable, value in items.items():
            kept.append("\t%s = %s" % (variable, value))

    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("\n".join(kept).rstrip("\n") + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, config_path)


def apply_unlimited_config(repo_path: str, *, quiet: bool = False) -> None:
    """Make sure a bare repository carries :data:`UNLIMITED_REPO_CONFIG`.

    Idempotent: once the policy is in place this costs a single read-only
    ``git`` process and performs no writes.

    With ``quiet`` set, failures are swallowed -- used for pre-existing
    repositories that may be read-only, or not be git repositories at all.
    """
    if _policy_already_local(repo_path):
        return
    try:
        _write_config_values(repo_path, UNLIMITED_REPO_CONFIG)
    except OSError as exc:
        # Covers a read-only store and repos that are missing a ``config``
        # file; with ``quiet`` the caller decided that is not fatal.
        if not quiet:
            raise GitusesError(
                "cannot update the config of %s: %s" % (repo_path, exc)
            ) from exc


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

    def apply_unlimited_policy(self) -> int:
        """Re-assert the unlimited policy on every repository in the store.

        Repositories created by an older gituses, or that arrived with
        provider-style caps, get those caps removed instead of having to be
        recreated.  Each repository that already complies costs a single
        read-only ``git`` process, so this is cheap enough to run on every
        ``serve``.  Returns the number of repositories that changed.
        """
        changed = 0
        for repo in self.list():
            path = repo["path"]
            if _policy_already_local(path):
                continue
            apply_unlimited_config(path, quiet=True)
            changed += 1
        return changed

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
        # Apply the full unlimited policy (receive-pack over HTTP, no push size
        # or object-count caps, force pushes and deletions allowed, partial
        # clone support) so every repository behaves the same way.
        apply_unlimited_config(path)
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
