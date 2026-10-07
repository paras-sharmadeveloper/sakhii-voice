# sakhii-voice

The real-time voice engine for Sakhii's pipeline agents. It handles live calls only:

```
Exotel Voicebot applet ──wss──▶ /ws/exotel ──▶ STT ──▶ LLM ──▶ TTS ──▶ back to Exotel
                                     ▲
                Redis (shared with Laravel): agent config in, call state + events out
```

Laravel stays the control plane (agents, billing, numbers, KYC, knowledge). ElevenLabs
Conversational AI agents keep running through Laravel as they do today. This engine is
only the new pipeline path.

Stack: Python 3.12 (3.11+ supported), FastAPI, uvicorn, Pipecat 1.12 (pinned), Redis.

## Layout

```
app/
  main.py            FastAPI app: /ws/exotel, /healthz (public as /health)
  pipeline.py        one call: transport, turn detection, greeting, limits, logging
  agent_config.py    the agent JSON Laravel writes (mirrors the Agent Builder tabs)
  prompt.py          system prompt, {placeholders}, pronunciation rules
  tools.py           end_call, transfer_call, search_knowledge, Integrations HTTP tools
  store.py           Redis contract (keys documented at the top)
  settings.py        env config
  providers/
    __init__.py      the registry
    base.py          CallContext + helpers
    stt_sarvam.py  stt_elevenlabs.py  llm_openai.py  llm_sarvam.py  tts_sarvam.py  tts_elevenlabs.py
scripts/latency_probe.py   fake Exotel client that measures real latency
deploy/                    systemd unit + nginx vhost
tests/                     unit tests + end-to-end over a real WebSocket and Redis
```

## Latency

Target: under ~1.1 s from the caller going quiet to Sakhii's voice on the line.

| Stage | How it's kept short | Typical |
|---|---|---|
| End of turn | Silero VAD with 200 ms of silence, then the local Smart Turn v3 model decides whether the caller is finished (~10–60 ms CPU). No fixed 600–1000 ms silence wait. | 250–300 ms |
| STT final | Streaming websocket STT only (Sarvam `saaras:v3-realtime`, ElevenLabs `scribe_v2_realtime`). It finalises on our end-of-turn signal. | 100–250 ms |
| LLM first token | Streamed. A short prompt, a reply cap of `LLM_MAX_TOKENS`, Sarvam reasoning effort `low`, and OpenAI `service_tier: priority` available per agent. | 250–500 ms |
| TTS first audio | Websocket TTS that starts on the first sentence. ElevenLabs uses `auto_mode`. | 100–250 ms |
| Audio path | 8 kHz end to end, so nothing is resampled. nginx has buffering off. Audio goes out in Exotel's minimum 200 ms chunks. | ~0 |

Also:
- The greeting goes straight to TTS, with no LLM call first.
- The agent lookup in Redis and the VAD/turn model loads run concurrently while the call connects.
- Providers are imported at startup, and HTTP tool calls share one keep-alive connection pool.

Every turn's voice-to-voice time is logged. Each call's p50/p90/max goes into the
`call.ended` event as `latency_ms`, so you can chart it in Laravel. To measure from
outside, run `scripts/latency_probe.py`.

### Turn detection

| Env | Default | What it does |
|---|---|---|
| `VAD_STOP_SECS` | `0.2` | Silence after speech before the turn is evaluated. If Hindi/Hinglish callers get cut off mid-sentence, raise it (0.3–0.5). Each extra 100 ms adds 100 ms to every reply. |
| `VAD_START_SECS` | `0.2` | Speech needed before the VAD reports that the caller started talking. |
| `VAD_CONFIDENCE` | `0.7` | Silero speech probability threshold. |
| `TURN_DETECTION` | `smart` | `smart`: after each pause, the local Smart Turn v3 model decides whether the caller is finished. `off`: a fixed `USER_SPEECH_TIMEOUT` of silence ends the turn. |
| `SMART_TURN_STOP_SECS` | `3.0` | With `smart`: how long to wait when the model thinks the caller isn't finished. |
| `USER_SPEECH_TIMEOUT` | `0.6` | With `off`: the silence that ends a turn. |

Every turn logs why it ended, so you can review false cut-offs from real calls:

```
turn ended: reason=smart_turn p_complete=0.97
```

| `reason` | Meaning |
|---|---|
| `smart_turn` | VAD silence, then the model judged the turn complete (`p_complete` is its confidence). |
| `smart_turn_silence_fallback` | The model judged the turn incomplete, then `SMART_TURN_STOP_SECS` of silence passed. |
| `silence_timeout` | `TURN_DETECTION=off`: `USER_SPEECH_TIMEOUT` of silence. |
| `stop_timeout` | Pipecat's 5 s safety net: the turn never resolved otherwise. |

Per-call counts go into the `call.ended` event as `turn_ends`. A `smart_turn` end with
a high `p_complete` that is followed straight away by the caller talking again is a
false cut-off. One example from testing: the model gave p=0.97 on the 220 ms pause
after "Wait,".

### Barge-in

When the caller talks over Sakhii:

1. The interruption is broadcast through the pipeline. The LLM's streaming request
   for that turn is closed, TTS stops generating, and any audio already queued in
   the output transport is dropped.
2. We send Exotel `{"event": "clear", "stream_sid": "<stream sid>"}`, the documented
   format, so audio already sent but not yet played on Exotel's side is discarded too.
3. What Sakhii said before being cut off stays in the conversation context, and the
   caller's speech starts the next turn.

To ignore coughs, "hmm" and line noise, a turn only interrupts the bot after
`INTERRUPT_MIN_SPEECH_SECS` (default `0.4`) of continuous speech, or
`INTERRUPT_MIN_WORDS` (default `3`) transcribed words. The bot therefore stops about
`INTERRUPT_MIN_SPEECH_SECS + VAD_STOP_SECS` (≈0.6 s) after the caller starts talking;
the VAD only reports "stopped" after `VAD_STOP_SECS` of silence. While the bot is
silent, the normal VAD start applies, so short answers like "haan" are still heard.

Pipecat also broadcasts an interruption, and so sends a `clear`, at the start of every
caller turn, even when Sakhii is silent. Exotel then has nothing to clear, so this is
harmless.

### Exotel media chunk rules

From [Exotel's Stream/Voicebot applet doc](https://support.exotel.com/support/solutions/articles/3000108630-working-with-the-stream-and-voicebot-applet)
(updated 16 Sep 2025):
- Media is raw/slin: 16-bit, 8 kHz, mono PCM, little-endian, base64-encoded.
- "Minimum chunk size: 3.2k [100ms data]", "Maximum chunk size: 100k", and "Chunk
  size should always be in multiple of 320 bytes".
- A chunk that isn't a multiple of 320 bytes makes Exotel pause 20 ms, which causes
  audio gaps.

The doc contradicts itself: 3.2k bytes is 200 ms at 8 kHz, not 100 ms. We meet the
stricter byte count.

How we comply:
- Outbound audio goes out in 3,200-byte (200 ms) messages.
- The last chunk of each utterance is padded with silence to a full chunk.
- The longest message is the 2 s end-of-call silence (32,000 bytes).
- `app/exotel.py` pads anything else that would break the rules and logs an error
  above 100k.
- The tests assert these rules on every media message.

## Adding a provider

Add one file and one registry line. For example, Deepgram STT:

```python
# app/providers/stt_deepgram.py
from pipecat.services.deepgram.stt import DeepgramSTTService
from app.providers.base import CallContext, language_enum

def build(choice, ctx: CallContext):
    return DeepgramSTTService(
        api_key=ctx.settings.deepgram_api_key,   # add the key to settings.py/.env
        sample_rate=ctx.sample_rate,
        settings=DeepgramSTTService.Settings(model=choice.model or "nova-3",
                                             language=language_enum(ctx.language)),
    )
```

```python
# app/providers/__init__.py
STT = {
    ...
    "deepgram": "app.providers.stt_deepgram",
}
```

Also install the matching Pipecat extra, e.g. `pipecat-ai[deepgram]`. For STT, use the
provider's streaming service and let it finalise on Pipecat VAD (manual commit), the way
the existing STT providers do. Otherwise turn detection won't be the same across providers.

## Redis contract with Laravel

All keys are prefixed with `REDIS_KEY_PREFIX`. Set it to Laravel's `REDIS_PREFIX`
(by default `"<app-name-slug>-database-"`) so `Redis::get('sakhii:voice:...')` in Laravel
reads the same key.

**Laravel writes:**

| Key | Type | When |
|---|---|---|
| `sakhii:voice:agent:{agent_id}` | string (JSON below) | On agent save/deploy |
| `sakhii:voice:number:{+91XXXXXXXXXX}` | string: agent_id | When an ExoPhone is assigned to a pipeline agent. E.164 format. |
| `sakhii:voice:call:{CallSid}:init` | string: `{"agent_id": "...", "variables": {"customer_name": "...", "amount_due": "...", "due_date": "...", "loan_id": "..."}}` | Before placing an outbound call. Use the Exotel CallSid from the Connect API response. Expire after ~1 h. |

The agent is resolved in this order: `?agent_id=` on the Voicebot URL, then `agent_id` in
Exotel custom parameters, then the call's `:init` key, then the dialled ExoPhone, then the
caller-id number. If nothing matches, the stream closes with code 1011.

**Agent JSON** (`schema_version: 1`). Only `agent_id` and `models` are required; unknown
fields are ignored:

```json
{
  "schema_version": 1,
  "agent_id": "42", "tenant_id": "7",
  "name": "EMI Recovery", "display_name": "Sakhii", "use_case": "emi_collection",
  "greeting": {"opening": "Namaste, main {agent_name} bol rahi hoon {company} se...", "closing": "Dhanyavaad {customer_name} ji..."},
  "business": {"name": "", "phone": "", "email": "", "website": "", "address": "", "description": ""},
  "transfer": {"enabled": true, "number": "+919999999999", "message": ""},
  "max_call_duration_secs": 600,
  "models": {
    "stt": {"provider": "sarvam", "model": "saaras:v3-realtime", "options": {}},
    "llm": {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0.35, "max_tokens": null, "options": {"service_tier": "priority"}},
    "tts": {"provider": "sarvam", "model": "bulbul:v3", "voice": "priya", "speed": 1.0, "options": {}}
  },
  "system_prompt": "You are {agent_name}... {amount_due} ... {due_date}",
  "persona": {"base_tone": "Professional and empathetic", "emotion_awareness": "Adaptive"},
  "languages": {"primary": "hi-IN", "also": ["en-IN"], "code_switching": true, "auto_detect": true},
  "language_overrides": {"hi-IN": "Aap ek professional EMI collection agent hain..."},
  "fillers": [{"text": "ji haan", "language": "Hindi", "context": "Acknowledgement"}],
  "pronunciations": [{"term": "EMI", "say": "ee-em-aai", "language": "Hindi"}],
  "knowledge": {"faqs": [{"question": "...", "answer": "..."}], "inline_text": "", "search_url": null},
  "tools": [{"name": "send_payment_link", "description": "...", "parameters": {"type": "object", "properties": {"amount": {"type": "number"}}, "required": ["amount"]},
             "url": "https://...", "method": "POST", "headers": {}, "timeout_secs": 8, "wait_message": "Ek minute..."}],
  "variables": {"company": "Acme Finance"}
}
```

Notes on fields:
- `models.*.options` is passed through to the Pipecat service settings. Keys the service
  doesn't know are ignored. The ElevenLabs sliders accept 0–100 (`stability`,
  `similarity`, `style`).
- For ElevenLabs, `tts.voice` must be the ElevenLabs **voice_id**, not a display name.
- Languages can be codes (`hi-IN`) or builder names (`Hindi`).
- `{placeholders}`: `agent_name`, `company` and `language` are built in. Everything else
  comes from `variables` and then from the per-call `:init` variables. Unknown
  placeholders become empty, so a template never reads out "curly brace".
- Integrations tools receive the LLM's arguments plus `_call: {call_sid, agent_id, from, to}`.
- `knowledge.search_url` (optional) is POSTed `{agent_id, call_sid, query}` with a 3 s
  timeout. It's only called when the FAQs/prompt don't cover the question.

**This engine writes:**

| Key | Type | Content |
|---|---|---|
| `sakhii:voice:call:{CallSid}` | hash, 24 h TTL | `status` (`in_progress` / `completed`), `agent_id`, `tenant_id`, `from`, `to`, `started_at`, `ended_at`, `duration_secs`, `end_reason`, `next_action`, `transfer_to`, `latency_ms`, `tool_calls`, `transcript` |
| `sakhii:voice:active` | set | CallSids live right now (for a concurrency view or limit) |
| `sakhii:voice:events` | stream | `call.started` and `call.ended` entries with `{type, call_sid, data}`. Laravel should consume them with a consumer group (`XREADGROUP`) for CallLogs, billing minutes, and the post-call webhook. |

`end_reason` is one of `caller_hung_up`, `agent_ended`, `transferred`, `max_duration`,
or `caller_silent`.

## Exotel setup

Use the native Voicebot applet. No SIP trunk is involved.

1. In the call flow, add a **Voicebot** applet with URL
   `wss://voice.YOURDOMAIN.com/ws/exotel?token=<EXOTEL_WS_TOKEN>`. To pin a specific
   agent, add `&agent_id=42`. Keep the default 8 kHz.
2. Put a **Passthru** applet after the Voicebot, pointing at Laravel. When the Voicebot
   stream ends, Exotel continues the flow, and Laravel reads `sakhii:voice:call:{CallSid}`:
   - If `next_action = transfer`, Laravel returns the route to a **Connect** applet that
     dials `transfer_to`.
   - Otherwise it hangs up.

   This is how "Transfer to human agent" works: the engine speaks the hand-off line,
   writes `next_action`, then closes the stream.

## Run locally

```bash
uv venv -p 3.12 .venv && source .venv/bin/activate
uv pip install -r pyproject.toml --extra dev
cp .env.example .env            # add API keys
uvicorn app.main:app --port 8000 --loop uvloop
pytest                          # needs a local redis-server for the e2e tests
```

Measure real latency against a running engine (agent mapped to the `--to` number):

```bash
python scripts/latency_probe.py --url "ws://127.0.0.1:8000/ws/exotel?token=$EXOTEL_WS_TOKEN" \
    --to 08047112233 --say question.wav --out reply.wav
```

## Deploy

The engine runs as one systemd service, `sakhii-voice`. It listens on
**127.0.0.1:8000 only**, so the web server already on the box is the only public
entry point:

```
Exotel ──wss://voice.YOURDOMAIN.com/ws/exotel──▶ Apache (Webuzo, :443, Let's Encrypt)
                                                   └─ws://127.0.0.1:8000/ws/exotel──▶ sakhii-voice
```

### Pipeline

GitHub Actions (`.github/workflows/deploy.yml`) runs the tests on every push and PR.
When a push to `main` passes, it deploys over SSH as the `sakhii` user:

1. The code is uploaded to `/opt/sakhii-voice/releases/<timestamp>-<sha>/`, and its
   own `.venv` is built there with the Python that bootstrap picked
   (`/opt/sakhii-voice/shared/python`).
2. `/opt/sakhii-voice/current` is switched to the new release, and `sakhii-voice` is
   restarted.
3. The engine has to pass the health check within 30 s. If it doesn't, `current` goes
   back to the previous release and the deploy fails. The last 5 releases are kept.

**Restarts refuse new calls while live calls finish.** On restart, the old process stops
accepting connections and lets live calls finish, for up to the max call length
(10 min). Calls arriving in that window are refused. Deploy outside busy hours.

### One-time server setup: AlmaLinux / RHEL with Webuzo (Apache)

Webuzo's Apache owns ports 80/443 and its SSL, so bootstrap must not touch nginx or
certbot.

1. Create the deploy key on your machine. The private key goes to GitHub, the public
   key to the server:

   ```bash
   ssh-keygen -t ed25519 -f sakhii-deploy -N ""
   ```

2. On the server, as root, with the repo checked out anywhere:

   ```bash
   sudo bash deploy/bootstrap.sh --no-webserver "$(cat sakhii-deploy.pub)"
   sudo -u sakhii vi /opt/sakhii-voice/shared/.env   # API keys, REDIS_*, EXOTEL_WS_TOKEN
   ```

   Bootstrap uses dnf or yum (or apt on Debian/Ubuntu). It does the following:
   - installs rsync, curl, sudo and Python 3.11+. It uses `python3.12`, or `python3.11`
     from dnf if the system Python is older;
   - creates the `sakhii` user with the deploy key;
   - installs uv;
   - lays out `/opt/sakhii-voice/{releases,shared}`;
   - installs and enables `sakhii-voice.service`;
   - allows `sakhii` to restart only that service;
   - if SELinux is on, sets `httpd_can_network_connect` so Apache may proxy to
     127.0.0.1:8000.

3. In Webuzo, add the domain `voice.YOURDOMAIN.com` and issue its Let's Encrypt
   certificate.

4. Add the proxy rules by copying them into the domain's custom config. Webuzo includes
   these directives in both the HTTP and HTTPS vhosts
   ([docs](https://webuzo.com/docs/developers/custom-virtualhost-config/)):

   ```bash
   mkdir -p /var/webuzo-data/apache2/custom/domains
   cp deploy/apache-webuzo.conf /var/webuzo-data/apache2/custom/domains/voice.YOURDOMAIN.com.conf
   /usr/local/apps/apache2/bin/httpd -M | grep -E 'proxy_(http|wstunnel)'   # both must be listed
   ```

   Then rebuild/restart Apache from the Webuzo panel. The rules proxy `/ws/exotel`
   with `mod_proxy_wstunnel` and `/health` over HTTP, with `ProxyTimeout 600`.

5. Add the GitHub secrets below, then push to `main`. Check with
   `curl https://voice.YOURDOMAIN.com/health`, which should return `{"ok":true}`.

### Other servers (own nginx + certbot)

```bash
sudo bash deploy/bootstrap.sh voice.YOURDOMAIN.com "$(cat sakhii-deploy.pub)"
```

This also installs nginx and certbot, installs `deploy/nginx-voice.conf`, and gets the
certificate.

### GitHub secrets

Set these in the repo under Settings, then Secrets and variables, then Actions:

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | server IP or hostname |
| `DEPLOY_SSH_KEY` | contents of the private key `sakhii-deploy` |
| `DEPLOY_PORT` | optional, SSH port if not 22 |

The deploy job uses the `production` environment, so you can add required reviewers
there if you want a manual approval step before each deploy.

### On the server

- `systemctl status sakhii-voice`
- `journalctl -u sakhii-voice -f`
- `curl -s 127.0.0.1:8000/healthz`
- Roll back by hand: `ln -sfn /opt/sakhii-voice/releases/<older> /opt/sakhii-voice/current`,
  then `systemctl restart sakhii-voice`.

For the lowest provider round trips, host in Mumbai (e.g. AWS `ap-south-1`). Sarvam and
Exotel are in India, and every hop to Europe or the US adds 100–250 ms per turn.
