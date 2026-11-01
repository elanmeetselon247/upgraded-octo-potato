# Running gituses locally (and using it from VS Code)

This machine is already set up: `gituses` is on `PATH` and a server is running.
This file records exactly how, so you can restart it or rebuild it anywhere.

## What is already installed

| Thing | Where | Notes |
| ----- | ----- | ----- |
| `gituses` command | `~/.local/bin/gituses` | editable install of this repo; on `PATH` via `~/.profile` |
| Repository store | `~/gituses` | contains `gituses.json` (config + token) and `*.git` bare repos |
| Running server | <http://127.0.0.1:8082/> | detached, logs in `~/gituses-serve.log` |
| Access token | `gituses token` | currently as printed below |

Port **8082** is used because 8080 and 8081 were already occupied on this host.

## Check it is running

```sh
curl -s http://127.0.0.1:8082/_health
# {"status": "ok", "repositories": N, "anonymous": false}
```

Open <http://127.0.0.1:8082/> in a browser for the repository index.

## Restart the server

```sh
# Stop any running instance by pid record.  Do not use `pkill -f 'gituses
# serve'`: the pattern also matches the shell running pkill, which kills
# itself before finishing.
if [ -f ~/gituses-serve.pid ]; then
  kill "$(cat ~/gituses-serve.pid)" 2>/dev/null || true
  sleep 1
fi

# Start it detached so it keeps running after the shell exits.
setsid gituses serve --root ~/gituses --host 127.0.0.1 --port 8082 \
  > ~/gituses-serve.log 2>&1 < /dev/null &

sleep 1
# setsid forks, so $! is not the server; record the python process instead.
ps -eo pid,cmd \
  | awk '/\/usr\/bin\/python3 .*gituses serve/ {print $1; exit}' \
  > ~/gituses-serve.pid

curl -s http://127.0.0.1:8082/_health
```

`gituses stop` does not exist; `kill $(cat ~/gituses-serve.pid)` is the way
to stop it.

## Rebuild the install from scratch

```sh
cd /workspace
python3 -m pip install --no-build-isolation --user -e .
hash -r          # forget the old command lookup table
gituses --version
```

If `gituses: command not found` in a new shell, `~/.profile` is missing or was
overwritten. It needs to contain:

```sh
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="$HOME/.local/bin:$PATH" ;;
esac
export PATH
```

## Use it from VS Code

Git identity and a credential helper are already configured globally, so
commits work and pushes do not prompt:

```sh
git config --global user.name      # Local User
git config --global user.email     # local@example.com
git config --global credential.helper   # store
```

### Option A - open in the terminal, then use the Source Control panel

```sh
cd ~
git clone http://git:TOKEN@127.0.0.1:8082/my-project.git
```

Replace `TOKEN` with the value of `gituses token`. Then
**File > Open Folder** and pick the cloned directory. The Source Control panel
now shows commits, branches, staging, push and pull against gituses.

Because `credential.helper=store` is set, the token is saved the first time
you authenticate. You can then drop it from the remotes URL:

```sh
git remote set-url origin http://127.0.0.1:8082/my-project.git
git push            # no prompt
```

### Option B - VS Code's "Clone Repository" command

Press <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>P</kbd> and run
**Git: Clone**, then paste `http://git:TOKEN@127.0.0.1:8082/my-project.git`.
VS Code asks for the password once; give it the token.

## Create more repositories

```sh
gituses create --root ~/gituses another-project --description "what it is"
gituses list   --root ~/gituses
```

Then clone `http://git:TOKEN@127.0.0.1:8082/another-project.git` the same way.

## Troubleshooting

| Symptom | Cause / fix |
| ------- | ----------- |
| `cannot bind 127.0.0.1:8082: Address already in use` | another instance is running; `pkill -f 'gituses serve'` first |
| `gituses: command not found` | shell has not sourced `~/.profile`; run `hash -r`, or open a new terminal |
| `Authentication failed` / repeated 401 | token is wrong or rotated: run `gituses token --root ~/gituses` |
| Push rejected after a token rotation | re-clone, or clear the cached credential for the host from `~/.git-credentials` |
| `Author identity unknown` | `git config --global user.name "..."` and `user.email "..."` |
