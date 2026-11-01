# Deploy gituses on Windows

One script, run once in PowerShell. It installs gituses, creates a repository
store, starts the server, and tells you the clone command.

## Prerequisites

Run these two checks first. If either fails, install it before continuing:

```powershell
git --version      # install git for Windows from https://git-scm.com/download/win
python --version   # install Python 3.10+ from https://python.org (tick "Add to PATH")
```

## Run the deploy

```powershell
cd $HOME
powershell -ExecutionPolicy Bypass -File .\deploy-windows.ps1
```

If PowerShell blocks scripts, that `-ExecutionPolicy Bypass` is what gets
around it for this one run only; nothing is changed permanently.

## What it does

1. Installs this directory as a `gituses` command (`pip install --user -e .`).
2. Creates a store at `%USERPROFILE%\gituses` with a random access token.
3. Creates a sample repository called `my-project`.
4. Starts the server on <http://127.0.0.1:8082/> in the background.
5. Prints the exact `git clone` command to paste into VS Code.

Re-running it is safe: an existing store is kept, the server is restarted.

## After it finishes

Open VS Code, then **Terminal > New Terminal**, and paste the clone command the
script printed. Then **File > Open Folder** and pick the cloned directory.

## Stop / restart later

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy-windows.ps1 -Stop
powershell -ExecutionPolicy Bypass -File .\deploy-windows.ps1
```

## Troubleshooting

| Symptom | Fix |
| ------- | --- |
| `git : command not found` | install git for Windows and reopen PowerShell |
| `python : command not found` | install Python and tick "Add Python to PATH" |
| `Address already in use` | another server is on 8082; pass `-Port 8083` |
| Server exits immediately | read `%USERPROFILE%\gituses-serve.log` |
| `git-http-backend not found` | git install is incomplete; reinstall git for Windows |
