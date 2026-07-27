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

- it is a no-op until you have marked applications `sent` by hand, so the clock is the wrong
  trigger for it — the trigger is a human action, and
- every run re-reads the inbox window and re-bills the classifier, so three ticks a day would
  mostly pay to re-classify mail it has already seen.

"It calls the LLM" is **not** a reason: `draft` calls the LLM too and is on the timer. Nor is a
stale Gmail token: `ingest` catches a source's failure per source, so it survives one anyway.

Run it yourself when you are expecting answers:

```bash
docker compose run --rm app uv run funnel check-replies
```

Add a second timer for it later if the manual run becomes a chore — the unit above is an easy
template, just swap the `ExecStart` command.
