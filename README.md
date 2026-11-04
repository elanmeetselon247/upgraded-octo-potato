# gituses

**Unlimited git uses.** A self-hosted git server with no limits on repositories,
pushes, repository size, or concurrent clients — plus a CLI to manage it.

`gituses` speaks git's smart HTTP protocol (the same protocol GitHub, GitLab and
Gitea serve), so every normal git command works against it:

```sh
git clone http://git:TOKEN@127.0.0.1:8080/my-project.git
```

No database, no Node runtime, no containers, no external Python packages. It is
one small Python package plus the `git` binary you already have.

## Why "unlimited"?

| Limit you hit elsewhere              | gituses                                  |
| ------------------------------------ | ---------------------------------------- |
| Repository count caps                | none — one bare repo per directory       |
| Repo size caps / LFS paywalls        | none — git-http-backend streams to disk   |
| Push size limits                     | none — `receive.maxInputSize=0`           |
| Object-count push limits             | none — `receive.unpackLimit=0`            |
| Force-push / branch-deletion blocks  | none — `receive.deny*` disabled           |
| Requests-per-hour / API rate limits  | none                                      |
| Concurrent clone/push caps           | none — one thread per connection, backlog = `SOMAXCONN` |
| Per-seat pricing                     | none — one shared token (or anonymous)   |

The only limits are your disk, CPU and network. On a 16 GiB disk you can host
roughly 16 GiB of git history.

Every repository gituses tracks carries the same policy, defined once in
`gituses/repo.py` as `UNLIMITED_REPO_CONFIG`:

```ini
[http]
	receivepack = true                  # accept pushes over HTTP
[receive]
	maxInputSize = 0                    # 0 = no push size limit
	unpackLimit = 0                     # 0 = never unpack into loose objects
	denyDeletes = false                 # allow ref deletion
	denyNonFastForwards = false         # allow force pushes / rewrites
	keepAlive = 0                       # no keepalive packets mid-transfer
[uploadpack]
	allowFilter = true                  # partial clones (--filter)
	allowAnySHA1InWant = true           # fetch any object by id
```

`gituses create` writes it for new repositories, and `gituses serve` (which runs
the same check at startup) re-asserts it for repositories that already exist —
so a repo created by an older gituses, or one that arrived with provider-style
caps, loses those caps instead of having to be recreated. Re-applying it is a
no-op, so it is safe on every start. From git's `receive.maxInputSize` docs:
*"If not set or set to 0, then the size is unlimited."*

## Quick start

Requires Python 3.10+ and git (with `git-http-backend`, included in the normal
git packages).

```sh
# 1. create a store (prints an access token; keep it)
python3 -m gituses init --root ~/gituses

# 2. create an empty repository
python3 -m gituses create --root ~/gituses my-project --description "my first repo"

# 3. serve it
python3 -m gituses serve --root ~/gituses --host 127.0.0.1 --port 8080
```

Then from another terminal:

```sh
git clone http://git:TOKEN@127.0.0.1:8080/my-project.git
cd my-project
echo hello > README.md
git add README.md && git commit -m "hello"
git push origin main
```

Prefer an installed command? `pip install -e .` gives you `gituses` on your
`PATH`, so the commands become `gituses init`, `gituses serve`, and so on.

Open <http://127.0.0.1:8080/> in a browser for a repository index with clone
commands.

## Commands

| Command | What it does |
| ------- | ------------ |
| `gituses init [--root DIR] [--token T] [--anonymous]` | Create a store and its `gituses.json` config |
| `gituses create NAME [--description TEXT]` | Create an empty bare repository |
| `gituses list [--json]` | List repositories (name, path, description) |
| `gituses delete NAME [-y]` | Delete a repository and all of its history |
| `gituses token [--rotate]` | Print or rotate the access token |
| `gituses serve [--host H] [--port P] [--token T]` | Serve all repositories over smart HTTP |

`--root` defaults to `$GITUSES_ROOT` or `~/.gituses`.

## Authentication

* `gituses init` generates a random token and stores it in `<root>/gituses.json`.
* Clients send it as the **git password** (any username works, `git` is
  conventional): `http://git:TOKEN@host:port/repo.git`.
* `Authorization: Bearer TOKEN` is also accepted for non-git HTTP clients.
* `gituses init --anonymous` (or `serve --token ""`) disables authentication
  entirely — convenient on a trusted LAN, dangerous on the public internet.
* Environment variable `GITUSES_TOKEN` overrides the stored token, which is
  handy for containers and secret managers.
* Rotate with `gituses token --rotate`.

## HTTP endpoints

| Endpoint | Purpose |
| -------- | ------- |
| `/<repo>.git/...` | Git smart HTTP (upload-pack and receive-pack) |
| `/` | HTML repository index |
| `/_health` | JSON health check: `{"status":"ok","repositories":N,"anonymous":bool}` |

`GET /<repo>.git` also supports git's dumb-HTTP fallback because
`git-http-backend` handles it.

## How it works

```
git client  ──HTTP──▶  GitusesHandler (threaded)  ──CGI──▶  git http-backend
                                                     │
                                          GIT_PROJECT_ROOT=<root>
                                          GIT_HTTP_EXPORT_ALL=1
```

* Every request is delegated to `git http-backend`, so git's own authentication
  of pack data, hooks, `uploadpack`/`receivepack` settings and protocol v2
  support all behave exactly as they do over `git://` or SSH.
* Responses stream through the Python server in 64 KiB chunks. A clone of a
  10 GiB repository costs kilobytes of server memory.
* `Content-Length` uploads stream straight through from the socket to the
  backend. Chunked uploads (git switches to chunked above `http.postBuffer`,
  1 MiB by default) are decoded to an unlinked temporary file first, because
  `git-http-backend` insists on an exact `CONTENT_LENGTH`; the file is removed
  as soon as the push finishes.
* Repositories are created with the full `UNLIMITED_REPO_CONFIG` policy (see
  above), so partial clones, force pushes, branch deletion, history rewrites
  and pushes of any size all work. The policy is re-asserted for existing
  repositories on every `serve`, so it also applies to repos that predate it.
* The HTTP server accepts connections with a backlog of `SOMAXCONN` instead of
  Python's default of 5, so a burst of clones never turns into `ECONNREFUSED`.
  Socket buffers are left to the kernel's TCP autotuning on purpose: pinning a
  huge `SO_SNDBUF` disables autotuning and makes long-haul clones slower.
* All paths are validated against a single safe name component, so requests can
  never escape the store root.

## Tests

The suite drives real `git` clients against a live server: clone, push, force
push, branch deletion, branch pushes, 4 MiB chunked pushes, 25 repositories,
eight concurrent clones, authentication failures, path traversal attempts,
deletion, and assertions that the unlimited policy is actually in force (for
new and pre-existing repositories, and for the server's accept backlog).

```sh
python3 -m unittest discover -s tests -t . -v

## Run it as a service

```ini
# /etc/systemd/system/gituses.service
[Unit]
Description=gituses git server
After=network.target

[Service]
User=git
Environment=GITUSES_TOKEN=replace-with-a-long-random-token
ExecStart=/usr/bin/python3 -m gituses serve --root /srv/gituses --host 127.0.0.1 --port 8080
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Put a TLS-terminating reverse proxy (Caddy, nginx, Traefik) in front of it for
any traffic that leaves the machine: the token travels as HTTP basic auth.
Backups are just the store directory — `rsync`/`restic` the whole `--root`.

## Use it as a library

```python
from gituses.repo import Store
from gituses.server import GitusesServer

store = Store("/srv/gituses")
store.require_initialised()
store.create("demo")

server = GitusesServer(("127.0.0.1", 8080), store, token="secret")
server.serve_forever()
```

## License

Released into the public domain under the [Unlicense](LICENSE).

```
