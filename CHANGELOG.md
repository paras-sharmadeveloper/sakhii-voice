# Changelog

Any change that needs a step on the server goes here, under
**Server action required**. Deploys are manual (`deploy/update.sh`), so a
server step that isn't listed here won't happen.

## 2026-10-09: status right after a reload; case-insensitive choices

- After every settings reload (applied or rejected), the engine rewrites
  `sakhii:voice:status` immediately instead of at the next 30 s tick. Laravel's Voice
  Engine Settings page uses this to show the result within a second.
- `LOG_LEVEL` and `TURN_DETECTION` accept any case (`info`, `Smart`). Before this, an
  older `.env` with `LOG_LEVEL=info` would have stopped the engine from starting.

### Server action required

1. `sudo bash /opt/sakhii-voice/app/deploy/update.sh` (no new dependencies).
2. Laravel: set `SAKHII_VOICE_CRED_KEY` in Laravel's `.env` to **the same value** as in
   `/opt/sakhii-voice/shared/.env`, run `php artisan migrate` (new table
   `voice_engine_settings_revisions`), then `php artisan config:clear`.
3. Open Super Admin → Voice Engine Settings. Until the first save, the engine runs on
   its `.env`. The first save (or "Send again") writes everything the panel knows:
   - Panel Keys values, including Laravel's own `.env` keys for OpenAI, ElevenLabs and
     Deepgram;
   - the non-secret settings you've set.
   Check that "Settings version in use" shows the new version before trimming the
   engine's `.env`.

## 2026-10-09: settings from the admin panel (Redis, hot-reloaded)

- Runtime settings come from `sakhii:voice:settings` (JSON) and `sakhii:voice:secrets`
  (AES-256-GCM with `SAKHII_VOICE_CRED_KEY`). Precedence: Redis > `.env` > default.
- The engine reloads on pub/sub `sakhii:voice:settings:changed` and every 60 s, with no
  restart. Live calls keep the settings they started with. An invalid change is rejected
  as a whole, and the reason is published as `settings.rejected`.
- `sakhii:voice:status` is written every 30 s: git commit, uptime, active calls, settings
  version, last reload and provider health.
- New bootstrap variables `REDIS_HOST`/`REDIS_PORT`/`REDIS_PASSWORD`/`REDIS_DB`. The
  existing `REDIS_URL` still works when `REDIS_HOST` is empty.
- `LOG_LEVEL` now changes live. Tracebacks no longer print local variable values
  (loguru `diagnose=False`), so a key held in a variable can't end up in the log.
- New runtime setting `DEFAULT_MODELS`. Every setting is documented in the README table.

### Server action required

1. `sudo bash /opt/sakhii-voice/app/deploy/update.sh` (no new dependencies). The current
   `.env` keeps working unchanged, because nothing is in Redis yet.
2. After the deploy, check that `redis-cli -n <db> GET '<REDIS_KEY_PREFIX>sakhii:voice:status'`
   shows the new commit as `version`.
3. Once Laravel's settings page writes `sakhii:voice:settings`/`sakhii:voice:secrets` and the
   status shows its `settings_version` (with `last_reload.result` = `applied`), you can
   remove everything except the bootstrap lines from `/opt/sakhii-voice/shared/.env`:
   `REDIS_*`, `REDIS_KEY_PREFIX`, `SAKHII_VOICE_CRED_KEY`, `HOST`, `PORT`. Do this one
   last time, and only after the values are in Redis: a value that's in neither falls
   back to its default (for example, an empty `EXOTEL_WS_TOKEN` turns the token check off).
   Then restart once with `update.sh`, so the process no longer holds the removed values
   from the environment.
4. `SAKHII_VOICE_CRED_KEY` must be set (same value as in Laravel) before Laravel writes
   `sakhii:voice:secrets`. Otherwise every change is rejected with "SAKHII_VOICE_CRED_KEY
   must be base64 of 32 bytes", and the engine keeps running on `.env`.

## 2026-10-09: provider registry, /catalog, encrypted credentials, call recording, after-call analysis

- New providers. STT: Deepgram, Google, Azure, Gladia, AssemblyAI. LLM: Gemini on Vertex AI
  (asia-south1, thinking off), Azure OpenAI, Groq, Anthropic. TTS: Azure, Google, Cartesia,
  Deepgram Aura. Existing agents (Sarvam, ElevenLabs, OpenAI) behave as before.
- `GET /catalog` (Bearer `ENGINE_ADMIN_TOKEN`) lists providers, models, live voices (cached
  1 h), languages, tuning ranges and credential fields.
- `models.*.credential_id` makes an agent use an encrypted `sakhii:voice:cred:<id>` instead
  of the `.env` key.
- `recording_enabled` per agent: stereo MP3 uploaded to S3-compatible storage after
  hang-up. `call.ended` gains `recording_key`, `recording_url` and `recording_duration`.
- After-call analysis: new `call.analyzed` event with summary, outcome, sentiment and fields.
- Ravan: hosted-agent only (no streaming STT/LLM/TTS API), so there's no adapter. See the README.
- New Python dependencies: Pipecat extras for the new providers, `cryptography`, `boto3` and
  `lameenc`. `update.sh` installs them.

### Server action required

1. Add the new variables to `/opt/sakhii-voice/shared/.env` (see `.env.example`). Each one
   is optional: if it's missing, that feature is off and calls work as before.
   - `ENGINE_ADMIN_TOKEN`, to enable `/catalog` (`openssl rand -hex 24`).
   - `SAKHII_VOICE_CRED_KEY` (`openssl rand -base64 32`), only when Laravel starts writing
     credentials. Put **the same value** in Laravel's `.env`.
   - Keys for any new provider you want to offer: `DEEPGRAM_API_KEY`, `AZURE_SPEECH_KEY`
     + `AZURE_SPEECH_REGION`, `GOOGLE_CREDENTIALS_PATH` (service-account JSON readable by
     the `sakhii-voice` user, for example `/opt/sakhii-voice/shared/google.json`, mode 600),
     and so on.
   - Recording: `RECORDING_S3_BUCKET`, `RECORDING_S3_KEY`, `RECORDING_S3_SECRET`, plus
     `RECORDING_S3_ENDPOINT`/`RECORDING_S3_REGION` for non-AWS storage. Create the bucket
     first, keep it private, and give the key write access to it only.
2. `sudo bash /opt/sakhii-voice/app/deploy/update.sh`. It installs the new dependencies
   (several minutes the first time; the Pipecat extras pull in the Google and Azure SDKs)
   and restarts. Check that `/healthz` returns `{"ok": true}`.
3. If `ENGINE_ADMIN_TOKEN` is set, check
   `curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/catalog | head -c 300`.
   If Laravel runs on the same server, it can call `http://127.0.0.1:8000/catalog` and
   needs no Apache change. If it calls `https://voice.sakhii.io/catalog`, add the two new
   `/catalog` lines from `deploy/apache-webuzo.conf` to
   `/var/webuzo-data/apache2/custom/domains/voice.sakhii.io.conf`. Then run
   `/usr/local/apps/apache2/bin/httpd -t` and restart Apache from Webuzo.
4. Laravel follow-ups (not in this change):
   - Read `/catalog` for the provider, model and voice pickers.
   - Write `sakhii:voice:cred:<id>` in the format in the README.
   - Publish `recording_enabled`, `analysis` and `credential_id` in the agent JSON.
   - Have `sakhii-voice:sync-calls` handle `call.analyzed` (it currently acks and skips
     it) and store `recording_key`/`recording_duration` on the call log.

## 2026-10-09: Laravel publishes agents to Redis and imports engine calls

The Laravel backend writes `sakhii:voice:agent:{id}` and `sakhii:voice:number:{+91…}`
(see "Redis contract with Laravel" in the README) when an agent is deployed, switched on
or off, deleted, or gets a new number. It also takes an account off the engine when its
minutes run out. Finished engine calls become call logs in Laravel: Enquiries, billed
minutes, email, Zapier and the post-call webhook. No engine code changed.

### Server action required

1. Laravel `.env` on the server: `SAKHII_VOICE_ENABLED=true`, pointing at the Redis the
   engine uses (default: `REDIS_HOST`/`REDIS_PORT`, DB 0; override with
   `SAKHII_VOICE_REDIS_HOST`/`_PORT`/`_DB`/`_PASSWORD`). Laravel needs the PHP `redis`
   extension (`php -m | grep redis`) or `REDIS_CLIENT=predis` with predis installed.
2. Run `php artisan config:clear && php artisan sakhii-voice:publish` in the Laravel app.
   It prints the key prefix Laravel uses.
3. Set `REDIS_KEY_PREFIX` in `/opt/sakhii-voice/shared/.env` to that prefix (for example
   `laravel-database-`), then `sudo bash /opt/sakhii-voice/app/deploy/update.sh`.
4. To move an ExoPhone to the engine: in its Exotel flow, use the Voicebot applet with
   `wss://voice.sakhii.io/ws/exotel/<EXOTEL_WS_TOKEN>`. Numbers whose flows don't route
   to the engine keep working as before.
5. Engine calls reach Enquiries through `php artisan sakhii-voice:sync-calls`, which is
   scheduled every minute. The server's Laravel cron (`* * * * * php artisan schedule:run`)
   must be running; it already runs `quota:check` and the other jobs. Each call is
   imported once, from the engine's `sakhii:voice:events` stream (consumer group
   `laravel`). Calls answered before step 2 are imported too, as long as they're still
   in the stream.

## 2026-10-08: manual deploy replaces GitHub Actions deploys

- GitHub Actions now only runs the tests (`.github/workflows/test.yml`). Removed: the
  deploy job, `deploy/remote_deploy.sh`, `deploy/bootstrap.sh`, the `releases/` +
  `current` layout, the deploy SSH key and sudoers logic, `sakhii-voice@.service` and
  `deploy/nginx-voice.conf`.
- New `deploy/setup.sh` (once): clones to `/opt/sakhii-voice/app`, creates the Python
  3.12 venv in `/opt/sakhii-voice/venv`, removes all older units and the old sudoers
  rule, and installs and starts one `sakhii-voice.service` (uvicorn on 127.0.0.1:8000,
  user `sakhii`, `Restart=always`). It keeps `/opt/sakhii-voice/shared/.env`.
- New `deploy/update.sh` (each update): git pull, reinstall deps, restart, and health
  check. If unhealthy, it resets to the previous commit, restarts, and prints the last
  50 journal lines.
- `deploy/apache-webuzo.conf`: single backend. `/ws/exotel` and `/ws/exotel/<token>` go
  to `ws://127.0.0.1:8000`, and `/health` to `http://127.0.0.1:8000/healthz`, with
  `ProxyTimeout 600`.
- App code is unchanged: path-segment token, log redaction and `seed_test_agent.py`
  all stay.

### Server action required

1. `git clone https://github.com/paras-sharmadeveloper/sakhii-voice.git /opt/sakhii-voice/app`
2. `sudo bash /opt/sakhii-voice/app/deploy/setup.sh`
3. `cp /opt/sakhii-voice/app/deploy/apache-webuzo.conf /var/webuzo-data/apache2/custom/domains/voice.sakhii.io.conf`,
   then restart Apache (Webuzo panel).
4. Exotel flow, if not done yet: set the Voicebot URL to
   `wss://voice.sakhii.io/ws/exotel/<EXOTEL_WS_TOKEN>` (the token goes in the path).
5. Optional cleanup:
   - delete the `DEPLOY_HOST`/`DEPLOY_SSH_KEY` GitHub secrets;
   - remove the GitHub deploy key from `/home/sakhii/.ssh/authorized_keys`.

## Earlier entries

These describe deploy mechanisms that the entry above replaced. They're kept for
history; their server steps no longer apply.

### 2026-10-08: deploy restarts whatever the server's sudo rule allows

`deploy/remote_deploy.sh` no longer assumes a unit layout. It reads `sudo -n -l` and
restarts exactly the units that rule allows, with the `systemctl` path spelled the
way the rule spells it:
- `restart sakhii-voice`: the single unit, health check on 8000;
- `restart sakhii-voice@<port>`: each instance in turn, with a health check on each
  port.

This makes deploys work again on the current server, whose rule is the two-instance
one (`/bin/systemctl restart sakhii-voice@8000/@8001`), with no root step. Rollback
and the "no rule at all" early exit are unchanged.

#### Server action required (obsolete)

None for deploys to work.

Optional, to move the server to the single-instance layout described below: run that
entry's steps when convenient. They end live calls. Deploys keep working before and
after.

### 2026-10-08: single-instance deploy restored

Reverts the two-instance deploy topology from `9a4c20d` and `94f65e0`. Keeps every
app-level change since, including the `ef33b49` token changes.

- One systemd unit again, `deploy/sakhii-voice.service`: uvicorn on 127.0.0.1:8000
  only. `deploy/sakhii-voice@.service` is removed.
- `deploy/remote_deploy.sh` restarts `sakhii-voice` and health-checks
  `127.0.0.1:8000/healthz`. It rolls back `current` if the restart or the health check
  fails. Before building or switching anything, it checks with `sudo -n -l` that
  `sudo -n systemctl restart sakhii-voice` is allowed. If not, it fails early, prints
  the server's sudo rules and the exact `bootstrap.sh` command to run, and leaves the
  live release untouched.
- **Fixed the cause of the failed deploys:** a `systemctl` path mismatch. sudo matches
  the command path as written. Bootstrap, run via `sudo bash` (secure_path, `/bin`
  first), wrote `/bin/systemctl` into the rule, while the deploy user ran
  `/usr/bin/systemctl`. On AlmaLinux these are the same file, but sudo still refused.
  Bootstrap now allows every spelling that exists (`/usr/bin/systemctl`,
  `/bin/systemctl`), and the deploy uses the real path (`readlink -f`).
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

#### Server action required (obsolete)

Run once, as root, on the server:

1. Re-run bootstrap from the newest uploaded release. It's idempotent and keeps
   `shared/.env`:
   `sudo bash /opt/sakhii-voice/releases/<newest>/deploy/bootstrap.sh --no-webserver "$(head -1 /home/sakhii/.ssh/authorized_keys)"`
2. Re-copy the Apache config:
   `cp /opt/sakhii-voice/current/deploy/apache-webuzo.conf /var/webuzo-data/apache2/custom/domains/<voice domain>.conf`
3. Restart Apache (Webuzo panel).
4. If not already done for `ef33b49`: set the Exotel Voicebot URL to
   `wss://<voice domain>/ws/exotel/<EXOTEL_WS_TOKEN>`.

### 2026-10-08: Exotel token as a path segment (`ef33b49`)

- `/ws/exotel/{token}` accepted alongside `/ws/exotel?token=`, because Exotel's applet
  drops query strings. Both use a constant-time comparison.
- The token is masked in uvicorn's handshake logs. Apache marks these requests
  `dontlog`.
- `scripts/seed_test_agent.py` for test calls before the Laravel integration.

#### Server action required (obsolete)

- Exotel flow: Voicebot URL `wss://<voice domain>/ws/exotel/<EXOTEL_WS_TOKEN>`.
- Re-copy `apache-webuzo.conf` and restart Apache. This is superseded by the entry
  above, which includes it.

### 2026-10-08: two-instance zero-downtime deploy (`9a4c20d`, `94f65e0`), reverted

Two units (`sakhii-voice@8000`/`@8001`) behind an Apache balancer. Bootstrap was
re-run on the server (its sudo rule names the `@` units), but the rule said
`/bin/systemctl` while the deploy called `/usr/bin/systemctl`, so every deploy failed
at the sudo restart. The server was left on a `9a4c20d` release. Reverted by the first
entry above.

### 2026-10-07: Webuzo/Apache support (`33fba64`)

- `bootstrap.sh --no-webserver`, apt/dnf detection, Python 3.11+, a single
  `sakhii-voice.service` on 127.0.0.1:8000, and `deploy/apache-webuzo.conf`.

#### Server action required (obsolete)

- First-time setup: `bootstrap.sh --no-webserver`, fill `shared/.env`, copy
  `apache-webuzo.conf`, restart Apache, and add the GitHub deploy secrets.

### 2026-10-07: initial engine (`57d8793`)

Exotel Voicebot WebSocket, STT, LLM and TTS on Pipecat, with the Redis contract with
Laravel and the GitHub Actions test-and-deploy pipeline.
