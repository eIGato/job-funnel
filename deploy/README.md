# Scheduling the funnel (Phase 7)

A **user** systemd timer, not a system one: the funnel runs as you, out of your checkout, and
needs no root. It calls `docker compose run --rm app uv run funnel run-funnel` — the container
starts, does the batch, and exits.

`run-funnel` is `ingest → match → draft`. It **never sends** (invariant 2); it leaves drafts in
the database for you to review in the admin.

## Install

```bash
mkdir -p ~/.config/systemd/user
cp deploy/funnel.service deploy/funnel.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now funnel.timer
```

`WorkingDirectory` in the unit is `%h/src/common/job-funnel`. If your checkout lives elsewhere,
edit that line before copying.

## Two things that will bite you otherwise

**Linger.** A user timer only runs while you have a login session, so it will not fire on a
machine you are not logged into. To let it run regardless:

```bash
sudo loginctl enable-linger "$USER"
```

**Docker permissions.** The unit calls `docker` as you, so your user must be in the `docker`
group (`groups | grep docker`). If it is not, the run fails with a socket permission error.

## Check on it

```bash
systemctl --user list-timers funnel.timer   # when it next fires, when it last did
systemctl --user status funnel.service      # the last run's result
journalctl --user -u funnel.service -n 50   # its output
systemctl --user start funnel.service       # run once, right now, without waiting
```

## What is deliberately not scheduled

`funnel check-replies` (Phase 6) is **not** part of `run-funnel` and has no timer of its own:

- it only does anything once you have marked applications `sent` by hand, and
- it reads your mailbox and calls the LLM, so it should not fire unattended, and a Gmail token
  that needs re-authorizing should not fail your nightly ingest.

Run it yourself when you are expecting answers:

```bash
docker compose run --rm app uv run funnel check-replies
```

Add a second timer for it later if the manual run becomes a chore — the unit above is an easy
template, just swap the `ExecStart` command.
