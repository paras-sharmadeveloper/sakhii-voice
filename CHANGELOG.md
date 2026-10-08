# Changelog

**Rule:** any change to the deploy topology (systemd units, ports, sudoers, the
Apache/nginx config, bootstrap steps) must list the manual server steps under
**Server action required** in its entry. A push to `main` deploys the code, but it
cannot change root-owned server config. Without these steps the deploy fails or
misroutes calls.

## 2026-10-08: single-instance deploy restored

Reverts the two-instance deploy topology from `9a4c20d` and `94f65e0`. Keeps every
app-level change since, including the `ef33b49` token changes.

- One systemd unit again, `deploy/sakhii-voice.service`: uvicorn on 127.0.0.1:8000
  only. `deploy/sakhii-voice@.service` is removed.
- `deploy/remote_deploy.sh` restarts `sakhii-voice` and health-checks
  `127.0.0.1:8000/healthz`. It rolls back `current` if the restart or the health check
  fails. Before building or switching anything, it checks that
  `sudo -n systemctl restart sakhii-voice` is allowed. It uses `sudo -n -l`, falling
  back to `sudo -n systemctl start sakhii-voice` (same sudoers rule, no-op on a running
  service), because on this server `sudo -n -l` was refused while the same NOPASSWD
  restart had worked hours earlier, most likely sudo's `listpw` default.
  If neither is allowed, it fails early, prints the sudo rules and the exact
  `bootstrap.sh` command to run, and leaves the live release untouched.
- `deploy/bootstrap.sh`:
  - the sudo rule allows only `systemctl restart|start sakhii-voice`;
  - it stops, disables and removes `sakhii-voice@8000`/`@8001` if present, then
    installs, enables and starts `sakhii-voice.service`. A running `sakhii-voice` is
    left running;
  - `shared/.env` stays untouched.
- `deploy/apache-webuzo.conf`: no balancer. It proxies `/ws/exotel` and
  `/ws/exotel/<token>` to `ws://127.0.0.1:8000` (mod_proxy_wstunnel), and `/health` to
  `http://127.0.0.1:8000/healthz`, with `ProxyTimeout 600`.
- `deploy/nginx-voice.conf` (non-Webuzo servers): single upstream. It now uses a
  prefix match on `/ws/exotel`, because the old exact match returned 404 for
  `/ws/exotel/<token>`.
- `app/serve.py` (drain on shutdown) stays in the repo with its tests, but the unit
  doesn't use it. Deploys end live calls again (see README, Deploy).

### Server action required

Run once, as root, on the server:

1. Re-run bootstrap from the newest uploaded release. It's idempotent and keeps
   `shared/.env`:
   `sudo bash /opt/sakhii-voice/releases/<newest>/deploy/bootstrap.sh --no-webserver "$(head -1 /home/sakhii/.ssh/authorized_keys)"`
2. Re-copy the Apache config:
   `cp /opt/sakhii-voice/current/deploy/apache-webuzo.conf /var/webuzo-data/apache2/custom/domains/<voice domain>.conf`
3. Restart Apache (Webuzo panel).
4. If not already done for `ef33b49`: set the Exotel Voicebot URL to
   `wss://<voice domain>/ws/exotel/<EXOTEL_WS_TOKEN>`.

## 2026-10-08: Exotel token as a path segment (`ef33b49`)

- `/ws/exotel/{token}` accepted alongside `/ws/exotel?token=`, because Exotel's applet
  drops query strings. Both use a constant-time comparison.
- The token is masked in uvicorn's handshake logs. Apache marks these requests
  `dontlog`.
- `scripts/seed_test_agent.py` for test calls before the Laravel integration.

### Server action required

- Exotel flow: Voicebot URL `wss://<voice domain>/ws/exotel/<EXOTEL_WS_TOKEN>`.
- Re-copy `apache-webuzo.conf` and restart Apache. This is superseded by the entry
  above, which includes it.

## 2026-10-08: two-instance zero-downtime deploy (`9a4c20d`, `94f65e0`), reverted

Two units (`sakhii-voice@8000`/`@8001`) behind an Apache balancer. The server steps
(re-run bootstrap, balancer config) were never performed. Every deploy failed at the
sudo restart, and the server stayed on the single `sakhii-voice.service`. Reverted by
the first entry above.

## 2026-10-07: Webuzo/Apache support (`33fba64`)

- `bootstrap.sh --no-webserver`, apt/dnf detection, Python 3.11+, a single
  `sakhii-voice.service` on 127.0.0.1:8000, and `deploy/apache-webuzo.conf`.

### Server action required

- First-time setup: `bootstrap.sh --no-webserver`, fill `shared/.env`, copy
  `apache-webuzo.conf`, restart Apache, and add the GitHub deploy secrets.

## 2026-10-07: initial engine (`57d8793`)

Exotel Voicebot WebSocket, STT, LLM and TTS on Pipecat, with the Redis contract with
Laravel and the GitHub Actions test-and-deploy pipeline.
