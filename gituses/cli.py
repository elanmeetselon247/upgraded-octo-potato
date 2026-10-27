"""Command line interface for ``gituses``."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys

from . import __version__
from .repo import GitusesError, Store
from .server import serve as make_server

DEFAULT_ROOT = os.environ.get("GITUSES_ROOT") or os.path.expanduser("~/.gituses")


def _store(args) -> Store:
    return Store(getattr(args, "root", None) or DEFAULT_ROOT)


def cmd_init(args) -> int:
    store = _store(args)
    token = store.init(token=args.token, anonymous=args.anonymous)
    print("initialised gituses store at %s" % store.root)
    if token:
        print("access token: %s" % token)
        print("(store it safely; clients use it as the git password)")
    else:
        print("anonymous access enabled: anyone who can reach the port can push")
    print("next: gituses create my-project")
    return 0


def cmd_create(args) -> int:
    store = _store(args)
    store.require_initialised()
    path = store.create(args.name, description=args.description or "")
    print("created repository %s" % path)
    token = store.token()
    name = args.name[:-4] if args.name.endswith(".git") else args.name
    if token:
        print("clone with: git clone http://git:%s@HOST:PORT/%s.git" % (token, name))
    else:
        print("clone with: git clone http://HOST:PORT/%s.git" % name)
    return 0


def cmd_list(args) -> int:
    store = _store(args)
    store.require_initialised()
    repos = store.list()
    if args.json:
        print(json.dumps(repos, indent=2))
        return 0
    if not repos:
        print("no repositories in %s" % store.root)
        return 0
    width = max(len(repo["name"]) for repo in repos)
    for repo in repos:
        line = repo["name"].ljust(width)
        if repo["description"]:
            line += "  " + repo["description"]
        print(line)
    return 0


def cmd_delete(args) -> int:
    store = _store(args)
    store.require_initialised()
    if not args.yes:
        answer = input("delete repository %r and all its history? [y/N] " % args.name)
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    store.delete(args.name)
    print("deleted repository %s" % args.name)
    return 0


def cmd_token(args) -> int:
    store = _store(args)
    store.require_initialised()
    if args.rotate:
        config = store.load_config()
        config["token"] = secrets.token_urlsafe(24)
        config["anonymous"] = False
        store.save_config(config)
        print("rotated token: %s" % config["token"])
        return 0
    token = store.token()
    print(token if token else "(anonymous: no token configured)")
    return 0


def cmd_serve(args) -> int:
    store = _store(args)
    store.require_initialised()
    token = args.token if args.token is not None else store.token()
    try:
        server = make_server(store, host=args.host, port=args.port, token=token)
    except OSError as exc:
        raise GitusesError(
            "cannot bind %s:%d: %s" % (args.host, args.port, exc)
        ) from exc
    host, port = server.server_address[:2]
    # A wildcard bind is not a usable URL; show something clickable instead.
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    print("gituses %s serving %d repositories" % (__version__, len(store.list())))
    print("root:  %s" % store.root)
    print("url:   http://%s:%d/" % (display_host, port))
    print("auth:  %s" % ("token required" if token else "anonymous"))
    if token:
        print("token: %s" % token)
    print("press Ctrl+C to stop")
    sys.stdout.flush()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gituses",
        description="Unlimited self-hosted git over smart HTTP.",
    )
    parser.add_argument("--version", action="version", version="gituses " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_root(p, help_text="repository store directory"):
        p.add_argument(
            "--root",
            default=None,
            help="%s (default: %s)" % (help_text, DEFAULT_ROOT),
        )

    init = sub.add_parser("init", help="initialise a repository store")
    add_root(init, "store directory to create")
    init.add_argument(
        "--token", default=None, help="shared access token (default: random)"
    )
    init.add_argument(
        "--anonymous",
        action="store_true",
        help="allow unauthenticated reads and writes",
    )
    init.set_defaults(func=cmd_init)

    create = sub.add_parser("create", help="create an empty repository")
    add_root(create, "store directory")
    create.add_argument("name", help="repository name (letters, digits, . _ -)")
    create.add_argument("--description", default="", help="short description")
    create.set_defaults(func=cmd_create)

    listing = sub.add_parser("list", help="list repositories")
    add_root(listing, "store directory")
    listing.add_argument("--json", action="store_true", help="machine readable output")
    listing.set_defaults(func=cmd_list)

    delete = sub.add_parser("delete", help="delete a repository")
    add_root(delete, "store directory")
    delete.add_argument("name")
    delete.add_argument("-y", "--yes", action="store_true", help="do not prompt")
    delete.set_defaults(func=cmd_delete)

    server = sub.add_parser("serve", help="serve repositories over smart HTTP")
    add_root(server, "store directory")
    server.add_argument(
        "--host", default="127.0.0.1", help="bind address (default 127.0.0.1)"
    )
    server.add_argument("--port", type=int, default=8080, help="bind port (default 8080)")
    server.add_argument(
        "--token",
        default=None,
        help="override the stored token (empty string = anonymous)",
    )
    server.set_defaults(func=cmd_serve)

    token = sub.add_parser("token", help="print or rotate the access token")
    add_root(token, "store directory")
    token.add_argument("--rotate", action="store_true", help="generate a new token")
    token.set_defaults(func=cmd_token)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except GitusesError as exc:
        print("gituses: error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
