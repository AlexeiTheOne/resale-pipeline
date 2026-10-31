---
name: deploy-status
description: Check what is live on the production bot (Person B's PC) - live commit vs GitHub, did the last auto-deploy work, is the bot running/busy, any failed or blocked deploy, and how to roll back. Use when asked "is it deployed", "what's live", "did the deploy work", "why no ✅", or before/after pushing to main.
---

# Deploy status

Production runs on Person B's PC. Pushing to `main` deploys within ~5 min via
task `EbayBotDeploy` running `scripts/deploy.ps1` (see CLAUDE.md).

## 1. Get the status (read-only)

Work out where you are first:
- **On the production PC** (`C:\Users\boris\Projects\ebay-list` exists):
  `powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\boris\Projects\ebay-list\scripts\prod_status.ps1`
- **Anywhere else**, over Tailscale:
  `ssh -o ConnectTimeout=15 boris@100.73.116.76 "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\boris\Projects\ebay-list\scripts\prod_status.ps1"`
  The remote shell is cmd.exe, so keep the whole command in one pair of double quotes.

If SSH times out, check Tailscale is up. If it is, sshd on the production PC
may be stopped; Person B fixes it there in an admin PowerShell:
`Start-Service sshd; Set-Service sshd -StartupType Automatic`.

## 2. Read it

- **`status: up to date`** and the heartbeat commit matches the live copy:
  everything is deployed.
- **GitHub is ahead:** the next run (every 5 min) picks it up. If it stays
  ahead for more than ~15 min, read the last deploy log lines for the reason.
- **`failed: <sha>`:** tests failed or the bot didn't come up on that commit,
  and it was rolled back. It won't retry that commit. Fix it and push a new
  commit. Use `scripts\deploy.ps1 -Force` on the production PC only if the
  failure wasn't the code's fault.
- **`WARNING: live copy has local changes`:** someone edited files on prod,
  which blocks deploys. Ask both people before discarding anything.
- **`busy`:** a publish/review is in flight, and the deploy waits for it.
- **Heartbeat `NOT running`, or 0 processes:** the restart loop should bring
  it back within ~15 s. Check `prod-logs` for a crash.

## 3. Roll back

Preferred: `git revert <bad sha>` on a laptop, run the tests, push. The
auto-deploy ships the revert like any other commit.

Never `git reset` or commit on the production copy, and never restart except
via `C:\Users\boris\Projects\restart_ebay_bot.ps1`.

Report in two or three plain lines: what's live, whether the last deploy
worked, and anything that needs a human.
