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
  serve.py           optional runner that drains live calls on SIGTERM (not used by the service)
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
deploy/                    setup.sh, update.sh, systemd unit, Apache (Webuzo) rules
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
| Audio path | 8 kHz end to end, so nothing is resampled. Audio goes out in Exotel's minimum 200 ms chunks. | ~0 |

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

## Providers

| Kind | Provider ids |
|---|---|
| STT | `sarvam`, `elevenlabs`, `deepgram`, `google`, `azure`, `gladia`, `assemblyai` |
| LLM | `openai`, `sarvam`, `gemini` (Vertex AI), `azure_openai`, `groq`, `anthropic` |
| TTS | `sarvam`, `elevenlabs`, `azure`, `google`, `cartesia`, `deepgram` (Aura, English only) |

Each one is a Pipecat streaming service, built for 8 kHz. Every STT finalises on Pipecat's
VAD, so turn detection is the same whichever STT you pick. Notes:

- `gemini` runs on Vertex AI in `GOOGLE_LOCATION` (default `asia-south1`, Mumbai), with
  thinking off by default (`thinking_budget=0` on 2.5 Flash, `minimal` on 3 Flash). To turn
  it on, set `models.llm.options.thinking`.
- `azure_openai`: `models.llm.model` (or the credential's `deployment`) is your Azure
  **deployment name**. An endpoint ending in `/openai/v1` uses Azure's v1 API.
- `languages` in `/catalog` come from each Pipecat service's own language map, not from
  guesses. For example, Google STT has no Odia, AssemblyAI has hi/en/mr and Deepgram Aura
  is English only.

**Ravan isn't a provider here.** Its public API (Agni, `docs.ravan.ai/openapi.json`)
covers agents, tools, RAG, calling and call sessions. It has no streaming STT, TTS or LLM
endpoints that a pipeline could call. It's a hosted-agent platform like ElevenLabs
Conversational AI, so any integration belongs in Laravel next to that one, not in this engine.

### Adding a provider

Add one file and one registry line. The file needs `build(choice, ctx)` and an `INFO`
for `/catalog`. A TTS can also have `async voices(creds)`.

```python
# app/providers/tts_example.py
from pipecat.services.example.tts import ExampleTTSService
from app.providers.base import CallContext, ProviderInfo, Tuning, model_list

def build(choice, ctx: CallContext):
    return ExampleTTSService(
        api_key=ctx.secret("tts", "api_key", ctx.settings.example_api_key),  # credential, else .env
        sample_rate=ctx.sample_rate,
        settings=ExampleTTSService.Settings(model=choice.model or "v1", voice=choice.voice),
    )

INFO = ProviderInfo(name="Example", models=model_list(("v1", "Example v1")), languages=["en-IN"],
                    credentials=["api_key"], tuning=[Tuning("speed", "Speed", 0.5, 2, 1, step=0.1)])

async def voices(creds: dict[str, str]) -> list[dict]:   # creds["api_key"] from .env
    ...  # return [{"id": ..., "name": ...}, ...]
```

```python
# app/providers/__init__.py
TTS = {
    ...
    "example": "app.providers.tts_example:build",
}
```

Also add the Pipecat extra (for example `pipecat-ai[example]`) in `pyproject.toml`. For an
STT, use the provider's streaming service and let it finalise on Pipecat's VAD (manual
commit), like the existing ones do.

## GET /catalog

This endpoint is for Laravel's admin. It's off (404) unless `ENGINE_ADMIN_TOKEN` is set,
and it needs `Authorization: Bearer <ENGINE_ADMIN_TOKEN>` (401 otherwise).

```bash
curl -H "Authorization: Bearer $ENGINE_ADMIN_TOKEN" https://voice.sakhii.io/catalog
```

```json
{
  "version": 1,
  "generated_at": 1791527367,
  "stt": [
    {"id": "deepgram", "kind": "stt", "name": "Deepgram",
     "models": [{"id": "nova-3-general", "name": "Nova-3"}, {"id": "nova-2-general", "name": "Nova-2"}],
     "languages": ["hi-IN", "en-IN"], "credentials": ["api_key"], "streaming": true,
     "tuning": [], "notes": "", "voices": null, "voices_error": null}
  ],
  "llm": [ ... ],
  "tts": [
    {"id": "sarvam", "kind": "tts", "name": "Sarvam AI",
     "models": [{"id": "bulbul:v3", "name": "Bulbul v3"}],
     "languages": ["hi-IN", "en-IN", "bn-IN", "gu-IN", "kn-IN", "ml-IN", "mr-IN", "or-IN", "pa-IN", "ta-IN", "te-IN"],
     "credentials": ["api_key"], "streaming": true,
     "tuning": [{"key": "speed", "label": "Speaking speed", "min": 0.5, "max": 2.0, "default": 1.0, "unit": "x", "step": 0.05},
                {"key": "temperature", "label": "Expressiveness", "min": 0.01, "max": 1.0, "default": 0.6, "unit": "", "step": 0.01}],
     "notes": "", "voices": [{"id": "aditya", "name": "Aditya"}, {"id": "ritu", "name": "Ritu"}], "voices_error": null},
    {"id": "cartesia", "...": "...", "voices": null, "voices_error": "no credentials configured on the engine"}
  ]
}
```

- `models[].id` goes into `models.<kind>.model`, `voices[].id` into `models.tts.voice`, and
  `tuning[].key` into the matching agent field (`speed`, `temperature`) or into `options`.
- `languages: ["*"]` means the model takes any language (LLMs).
- `credentials` lists the fields a credential for this provider can hold (see below).
- Voices are fetched live with the engine's `.env` keys and cached for 1 hour per
  provider. If a provider has no key, or its API fails, it gets `voices: null` and a
  `voices_error` (only the error type, never request details). The rest of the catalogue
  still loads.

## Provider credentials

By default, every provider uses the engine's `.env` keys. To have an agent use its own
account (a tenant's key, or a different Azure deployment), Laravel stores a credential and
points the agent's model choice at it with `"credential_id": "<id>"`.

**Key:** `sakhii:voice:cred:<id>`, with the same `REDIS_KEY_PREFIX` as other keys.

**Value:** AES-256-GCM, encrypted with `SAKHII_VOICE_CRED_KEY`. This is base64 of 32 random
bytes (`openssl rand -base64 32`), and must be identical in Laravel and the engine:

```json
{"v": 1, "iv": "<base64, 12 bytes>", "ct": "<base64 ciphertext>", "tag": "<base64, 16 bytes>"}
```

The additional authenticated data is the string `sakhii:voice:cred:<id>`, so a value copied
to another id won't decrypt. The plaintext is
`{"provider": "deepgram", "fields": {"api_key": "..."}}`, with the field names from the
provider's `credentials` list in `/catalog`.

```php
$key   = base64_decode(config('services.sakhii_voice.cred_key'));
$iv    = random_bytes(12);
$plain = json_encode(['provider' => 'azure', 'fields' => ['api_key' => $apiKey, 'region' => 'centralindia']]);
$ct    = openssl_encrypt($plain, 'aes-256-gcm', $key, OPENSSL_RAW_DATA, $iv, $tag, "sakhii:voice:cred:{$id}", 16);
Redis::set("sakhii:voice:cred:{$id}", json_encode([
    'v' => 1, 'iv' => base64_encode($iv), 'ct' => base64_encode($ct), 'tag' => base64_encode($tag),
]));
```

The engine decrypts in memory when a call starts, caches the result for 5 minutes and never
logs the values. Errors name only the credential id. Any field a credential doesn't
have falls back to `.env`. If the credential is missing or won't decrypt, the call is
refused (stream closed with 1011) instead of silently using the platform's key.

## Call recording

**What Exotel offers:** the Voicebot applet has its own "record" checkbox, and the
recording URL then comes back through Passthru and the Call Details API
(`/v1/Accounts/<sid>/Calls/<CallSid>.json`, `RecordingUrl`). However, it's a beta feature,
it's set per flow in the Exotel dashboard rather than per call, and since October 2024
the URLs need your API credentials (or `PreSignedRecordingUrl`). So it can't follow a
per-agent switch. If a whole number should always be recorded, that checkbox is a valid
alternative and needs no engine setup.

**What the engine does:** for agents with `"recording_enabled": true`, it records the
call in stereo at 8 kHz, with the caller on the left channel and the agent on the right.
Audio is appended to a temp file in 10-second chunks from a worker thread, so nothing on
the audio path waits for disk. After hang-up, it's encoded to MP3 (`lameenc`, no ffmpeg,
32 kbps by default, about 240 KB a minute) and uploaded to S3-compatible storage. Then
the temp files are deleted. If the upload fails, the recording is lost but the
call log isn't (`recording_*` fields are `null`).

Setup (`.env`):

```
RECORDING_S3_BUCKET=sakhii-calls
RECORDING_S3_KEY=...
RECORDING_S3_SECRET=...
RECORDING_S3_ENDPOINT=https://blr1.digitaloceanspaces.com   # empty for AWS S3; R2/MinIO/Spaces endpoint otherwise
RECORDING_S3_REGION=ap-south-1                               # "auto" for R2
RECORDING_S3_PREFIX=recordings/
RECORDING_PUBLIC_BASE_URL=                                   # empty = private bucket
```

Objects are stored at `<prefix>YYYY/MM/DD/<CallSid>.mp3`. With a private bucket (the
default and recommended), `call.ended` carries only `recording_key`, and Laravel signs a
URL when someone plays it. With `RECORDING_PUBLIC_BASE_URL` set, `recording_url` is
`<base>/<key>`. If an agent has recording on but storage isn't configured, the engine
logs a warning and doesn't record.

## After-call analysis

After hang-up, one LLM request (`ANALYSIS_MODEL`, default `gpt-4o-mini`, JSON mode, 30 s
timeout) reads the transcript and produces:

- a 2–3 line summary in English, plus one in the call's language when it isn't English;
- an outcome from the agent's `analysis.outcomes`;
- the caller's sentiment;
- the agent's `analysis.fields`.

It runs as a background task after `call.ended` and has no effect on the call. Any failure
is logged (as the error type only) and dropped. Calls where the caller never spoke are skipped.
To turn it off for one agent, set `"analysis": {"enabled": false}`, or set
`ANALYSIS_ENABLED=false` for the whole engine.

## Redis contract with Laravel

All keys are prefixed with `REDIS_KEY_PREFIX`. Set it to Laravel's `REDIS_PREFIX`
(by default `"<app-name-slug>-database-"`) so `Redis::get('sakhii:voice:...')` in Laravel
reads the same key.

**Laravel writes:**

| Key | Type | When |
|---|---|---|
| `sakhii:voice:agent:{agent_id}` | string (JSON below) | On agent save/deploy |
| `sakhii:voice:number:{+91XXXXXXXXXX}` | string: agent_id | When an ExoPhone is assigned to a pipeline agent. E.164 format. |
| `sakhii:voice:settings` / `sakhii:voice:secrets` | string | From the admin panel, then PUBLISH `sakhii:voice:settings:changed` (see "Settings") |
| `sakhii:voice:call:{CallSid}:init` | string: `{"agent_id": "...", "variables": {"customer_name": "...", "amount_due": "...", "due_date": "...", "loan_id": "..."}}` | Before placing an outbound call. Use the Exotel CallSid from the Connect API response. Expire after ~1 h. |

The agent is resolved in this order: `?agent_id=` on the Voicebot URL, then `agent_id` in
Exotel custom parameters, then the call's `:init` key, then the dialled ExoPhone, then the
caller-id number. If nothing matches, the stream closes with code 1011.

Exotel's Voicebot applet drops query parameters, so `?agent_id=` only works for tools
like curl or the latency probe. On real calls, the agent comes from custom parameters,
the `:init` key or the ExoPhone mapping.

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
    "tts": {"provider": "sarvam", "model": "bulbul:v3", "voice": "priya", "speed": 1.0, "options": {}, "credential_id": null}
  },
  "recording_enabled": false,
  "analysis": {
    "enabled": true,
    "outcomes": ["resolved", "follow_up_needed", "transferred", "not_interested", "no_conversation"],
    "fields": [{"key": "promised_date", "description": "Date the customer promised to pay"}]
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
- `models.*.credential_id` (optional) points to an encrypted `sakhii:voice:cred:<id>`
  (see "Provider credentials"). If it's missing or null, the engine's `.env` keys are used.
- `recording_enabled` (default `false`) and `analysis`: see "Call recording" and
  "After-call analysis". `analysis.outcomes` defaults to the list shown above.
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
| `sakhii:voice:call:{CallSid}` | hash, 24 h TTL | `status` (`in_progress` / `completed`), `agent_id`, `tenant_id`, `from`, `to`, `started_at`, `ended_at`, `duration_secs`, `end_reason`, `next_action`, `transfer_to`, `latency_ms`, `tool_calls`, `transcript`, `recording_key`, `recording_url`, `recording_duration`, `analysis` (non-string values JSON-encoded; null fields left out) |
| `sakhii:voice:status` | string, 90 s TTL | Engine status every 30 s (see "Settings") |
| `sakhii:voice:active` | set | CallSids live right now (for a concurrency view or limit) |
| `sakhii:voice:events` | stream | `call.started`, `call.ended`, `call.analyzed` and `settings.rejected` entries with fields `type`, `call_sid` and `data` (a JSON string). Laravel should consume them with a consumer group (`XREADGROUP`) for CallLogs, billing minutes, and the post-call webhook. |

`end_reason` is one of `caller_hung_up`, `agent_ended`, `transferred`, `max_duration`,
or `caller_silent`.

**`call.ended`**: stream entry `{"type": "call.ended", "call_sid": "<CallSid>", "data": "<JSON>"}`,
where `data` is:

```json
{
  "agent_id": "42",
  "tenant_id": "7",
  "duration_secs": 84.4,
  "end_reason": "caller_hung_up",
  "transfer_to": null,
  "latency_ms": {"turns": 6, "greeting": 410, "p50": 820, "p90": 1010, "max": 1180},
  "turn_ends": {"smart_turn": 5, "smart_turn_silence_fallback": 1},
  "tool_calls": [],
  "transcript": [
    {"role": "assistant", "text": "Namaste, main Sakhii bol rahi hoon Acme Finance se..."},
    {"role": "user", "text": "Haan ji, boliye."}
  ],
  "recording_key": "recordings/2026/10/09/CA0123456789abcdef.mp3",
  "recording_url": null,
  "recording_duration": 84.2,
  "status": "completed",
  "ended_at": 1791527369.29
}
```

The `recording_*` fields are `null` when the agent doesn't record or the upload failed.
`recording_url` is set only with `RECORDING_PUBLIC_BASE_URL`. `recording_duration` is in
seconds.

**`call.analyzed`**: comes a few seconds after `call.ended`, for the same `call_sid`. It's
also stored as `analysis` on the call hash. Stream entry:
`{"type": "call.analyzed", "call_sid": "<CallSid>", "data": "<JSON>"}`, where `data` is:

```json
{
  "agent_id": "42",
  "summary": "Ramesh called about his overdue EMI of Rs 5,000.\nHe agreed to pay by the 15th via UPI; a payment link was sent.",
  "summary_local": "Ramesh ji ne 5,000 rupaye ki EMI 15 tarikh tak UPI se bharne ka vaada kiya.",
  "language": "hi-IN",
  "outcome": "resolved",
  "sentiment": "positive",
  "fields": {"promised_date": "15th"}
}
```

- `summary_local` is `null` for English calls.
- `outcome` is one of the agent's `analysis.outcomes`, or `null` if the model answered
  outside the list.
- `sentiment` is `positive`, `neutral` or `negative` (or `null`).
- `fields` has every configured key, with `null` for the ones that weren't mentioned.
- No `call.analyzed` is sent when analysis is off, the caller never spoke, or the
  request failed.

## Settings (admin panel)

Everything except a few bootstrap values is set from Sakhii's admin panel (Super Admin →
Voice Engine Settings) through Redis, and takes effect without a restart. `/opt/sakhii-voice/shared/.env` only needs the
bootstrap values.

- **Bootstrap** (`.env` only; a change needs a restart): `REDIS_HOST`, `REDIS_PORT`,
  `REDIS_PASSWORD`, `REDIS_DB` (or the older `REDIS_URL`), `REDIS_KEY_PREFIX`,
  `SAKHII_VOICE_CRED_KEY`, plus `HOST`/`PORT`, the address the engine listens on.
- **Runtime**: `sakhii:voice:settings`, plain JSON, no secrets.
- **Secret**: `sakhii:voice:secrets`, encrypted.

**Precedence:** a Redis value beats the `.env` value, which beats the code default. A
server with only a `.env` works exactly as before. `null` or `""` in Redis means "not set
here", so a blank admin field falls back to `.env` and never wipes a key. To stop using a
`.env` value, set a different value in Redis or delete the line from `.env`.

**`sakhii:voice:settings`** (string, JSON):

```json
{"version": 7, "values": {"VAD_STOP_SECS": 0.25, "TURN_DETECTION": "smart", "LOG_LEVEL": "INFO",
                          "DEFAULT_MODELS": {"stt.deepgram": "nova-3-general"}, "RECORDING_S3_BUCKET": "sakhii-calls"}}
```

**`sakhii:voice:secrets`** (string): the same envelope as provider credentials (see
"Provider credentials"), with additional data `sakhii:voice:secrets` (no id) and plaintext
`{"version": 3, "fields": {"OPENAI_API_KEY": "...", "EXOTEL_WS_TOKEN": "..."}}`.

```php
$plain = json_encode(['version' => $version, 'fields' => $secrets]);
$ct = openssl_encrypt($plain, 'aes-256-gcm', $key, OPENSSL_RAW_DATA, $iv, $tag, 'sakhii:voice:secrets', 16);
Redis::set('sakhii:voice:secrets', json_encode(['v' => 1, 'iv' => base64_encode($iv), 'ct' => base64_encode($ct), 'tag' => base64_encode($tag)]));
Redis::publish('sakhii:voice:settings:changed', (string) $version);
```

**Reload:** after writing, publish anything on `sakhii:voice:settings:changed`. The engine
listens on both the prefixed and the unprefixed channel name, because Laravel's Redis
clients differ on whether they prefix channels. It also checks every 60 s, so a missed
message only delays a change. `version` is any string or number. It's echoed back in the
status, and if it's left out, a hash of the content is used.

- **New calls** use the new settings. **Calls already running keep the settings they
  started with**, including their end-of-call recording upload and analysis.
- **Validation:** names must be known and in the right store (a secret in `settings`, or a
  bootstrap value in either, is rejected). Types and ranges are checked as in the table
  below. If anything fails, the whole change is rejected: the engine keeps its current
  settings and adds one `settings.rejected` entry to `sakhii:voice:events`. The reason
  gives setting names and rules only, never values:

```json
{"type": "settings.rejected", "data": "{\"reason\": \"VAD_STOP_SECS: Input should be less than or equal to 2\", \"settings_version\": \"8\", \"at\": 1791537256.8, \"settings_version_in_use\": \"7\", \"secrets_version_in_use\": \"3\"}"}
```

**`sakhii:voice:status`** (string, JSON). It's rewritten every 30 s and also right after
every reload attempt, so the admin panel can show the result within a second. It has a
90 s TTL, so a missing key means the engine is down:

```json
{
  "version": "3f2a9c1d04b7",
  "host": "voice-1", "pid": 4121,
  "started_at": 1791520000.0, "uptime_secs": 17256,
  "active_calls": 3,
  "settings_version": "7", "secrets_version": "3",
  "last_reload": {"at": 1791537000.1, "result": "applied", "reason": null},
  "providers": {
    "stt.sarvam": {"calls": 412, "errors": 0, "last_error": null, "last_error_at": null, "last_ok_at": 1791537250.2},
    "tts.cartesia": {"calls": 18, "errors": 2, "last_error": "ConnectionClosedError", "last_error_at": 1791530011.0, "last_ok_at": 1791537101.9}
  },
  "updated_at": 1791537256.8
}
```

- `version` is the deployed git commit.
- `settings_version`/`secrets_version` are `null` while nothing is in Redis (`.env` only).
- `last_reload.result` is `applied` or `rejected`, the latter with a `reason`.
- `providers` counts calls since the engine started, per `<kind>.<provider>`. Only the
  error's type is recorded, never its message.
- `recording.storage` is the recording uploads: `calls` counts the successful ones.
  `last_error` is `NotConfigured` (no bucket, key or secret) or `ClientError.<S3 code>`,
  for example `ClientError.AccessDenied` or `ClientError.NoSuchBucket`.

Every setting (the table is generated from `app/settings.py` with
`python scripts/settings_table.py`, and a test fails if it's out of date). "Where"
means `.env` only, `sakhii:voice:settings` or `sakhii:voice:secrets`. Any setting can
also stay in `.env` as a fallback.

| Name | Where | Secret | Type | Default | Range / values | Description |
|---|---|---|---|---|---|---|
| `HOST` | .env only | no | string | `127.0.0.1` |  | Listen address. |
| `PORT` | .env only | no | int | `8000` | 1 – 65535 | Listen port. |
| `REDIS_HOST` | .env only | no | string | (empty) |  | Redis host. Empty = use REDIS_URL. |
| `REDIS_PORT` | .env only | no | int | `6379` | 1 – 65535 | Redis port. |
| `REDIS_PASSWORD` | .env only | yes | string | (empty) |  | Redis password. |
| `REDIS_DB` | .env only | no | int | `0` | 0 – 15 | Redis database number. |
| `REDIS_URL` | .env only | yes | string | `redis://127.0.0.1:6379/0` |  | Used only when REDIS_HOST is empty (older .env files). |
| `REDIS_KEY_PREFIX` | .env only | no | string | (empty) |  | Laravel's REDIS_PREFIX, so both sides use the same keys. |
| `SAKHII_VOICE_CRED_KEY` | .env only | yes | string | (empty) |  | base64 of 32 bytes; decrypts sakhii:voice:secrets and sakhii:voice:cred:&lt;id&gt;. Same value as in Laravel. |
| `LOG_LEVEL` | settings | no | string | `INFO` | `TRACE` / `DEBUG` / `INFO` / `SUCCESS` / `WARNING` / `ERROR` | Engine log level. |
| `EXOTEL_ACCOUNT_SID` | settings | no | string | (empty) |  | If set, a stream's account_sid must match. |
| `EXOTEL_SAMPLE_RATE` | settings | no | int | `8000` | `8000` / `16000` | Exotel stream sample rate (Hz). |
| `CALL_STATE_TTL_SECS` | settings | no | int | `86400` | 3600 – 2592000 | TTL of sakhii:voice:call:&lt;CallSid&gt;. |
| `EVENTS_STREAM_MAXLEN` | settings | no | int | `100000` | 1000 – 10000000 | Approximate cap on sakhii:voice:events. |
| `AZURE_SPEECH_REGION` | settings | no | string | `centralindia` |  | Azure Speech region. |
| `AZURE_OPENAI_ENDPOINT` | settings | no | string | (empty) |  | Azure OpenAI endpoint (…/openai/v1 for the v1 API). |
| `AZURE_OPENAI_API_VERSION` | settings | no | string | `2024-10-21` |  | Azure OpenAI API version (non-v1 endpoints). |
| `GOOGLE_CREDENTIALS_PATH` | settings | no | string | (empty) |  | Path to a service-account JSON on the server (instead of GOOGLE_CREDENTIALS_JSON). |
| `GOOGLE_PROJECT_ID` | settings | no | string | (empty) |  | Vertex AI project. Empty = from the service-account JSON. |
| `GOOGLE_LOCATION` | settings | no | string | `asia-south1` |  | Vertex AI region for Gemini. |
| `DEFAULT_MODELS` | settings | no | object | (empty) |  | Model used when an agent leaves "model" empty, by "&lt;kind&gt;.&lt;provider&gt;", e.g. {"stt.deepgram": "nova-3-general"}. Otherwise each provider's built-in default. |
| `RECORDING_S3_ENDPOINT` | settings | no | string | (empty) |  | S3-compatible endpoint. Empty = AWS S3. |
| `RECORDING_S3_BUCKET` | settings | no | string | (empty) |  | Recording bucket. Empty = recording off. |
| `RECORDING_S3_REGION` | settings | no | string | `auto` |  | Storage region ("auto" for R2). |
| `RECORDING_S3_PREFIX` | settings | no | string | `recordings/` |  | Object key prefix. |
| `RECORDING_PUBLIC_BASE_URL` | settings | no | string | (empty) |  | Public/CDN base for recording_url. Empty = private bucket, only recording_key is sent. |
| `RECORDING_MP3_KBPS` | settings | no | int | `32` | 16 – 128 | Recording MP3 bitrate. |
| `ANALYSIS_ENABLED` | settings | no | bool | `true` |  | After-call analysis (call.analyzed) on/off for every agent. |
| `ANALYSIS_MODEL` | settings | no | string | `gpt-4o-mini` |  | OpenAI model for after-call analysis. |
| `VAD_STOP_SECS` | settings | no | float | `0.2` | 0.1 – 2.0 | Silence after speech before the turn is evaluated. Each 0.1 s adds 0.1 s to every reply. |
| `VAD_START_SECS` | settings | no | float | `0.2` | 0.05 – 1.0 | Speech needed before the caller counts as speaking. |
| `VAD_CONFIDENCE` | settings | no | float | `0.7` | 0.3 – 0.95 | VAD speech confidence threshold. |
| `TURN_DETECTION` | settings | no | string | `smart` | `smart` / `off` | smart = Smart Turn model judges each pause; off = fixed silence (USER_SPEECH_TIMEOUT). |
| `SMART_TURN_STOP_SECS` | settings | no | float | `3.0` | 0.5 – 10.0 | Smart turn fallback: max silence when the model thinks the caller isn't finished. |
| `USER_SPEECH_TIMEOUT` | settings | no | float | `0.6` | 0.2 – 3.0 | Turn detection off: silence that ends a turn. |
| `INTERRUPT_MIN_SPEECH_SECS` | settings | no | float | `0.4` | 0.0 – 3.0 | Barge-in: continuous caller speech needed to stop the bot. |
| `INTERRUPT_MIN_WORDS` | settings | no | int | `3` | 1 – 20 | Barge-in: or this many transcribed words. |
| `LLM_MAX_TOKENS` | settings | no | int | `220` | 32 – 2000 | Cap on LLM reply length when the agent doesn't set one. |
| `DEFAULT_MAX_CALL_SECS` | settings | no | int | `600` | 30 – 7200 | Call length limit when the agent doesn't set one. |
| `DRAIN_TIMEOUT_SECS` | settings | no | int | `615` | 0 – 7200 | On restart, how long live calls may continue. Keep above the longest call. |
| `EXOTEL_WS_TOKEN` | secrets | yes | string | (empty) |  | Token in the Voicebot URL (/ws/exotel/&lt;token&gt;). Empty = no check (local testing only). |
| `ENGINE_ADMIN_TOKEN` | secrets | yes | string | (empty) |  | Bearer token for GET /catalog. Empty = /catalog is off. |
| `SARVAM_API_KEY` | secrets | yes | string | (empty) |  | Platform Sarvam key (used when an agent has no credential_id). |
| `ELEVENLABS_API_KEY` | secrets | yes | string | (empty) |  | Platform ElevenLabs key. |
| `OPENAI_API_KEY` | secrets | yes | string | (empty) |  | Platform OpenAI key (also used by after-call analysis). |
| `DEEPGRAM_API_KEY` | secrets | yes | string | (empty) |  | Platform Deepgram key. |
| `GLADIA_API_KEY` | secrets | yes | string | (empty) |  | Platform Gladia key. |
| `ASSEMBLYAI_API_KEY` | secrets | yes | string | (empty) |  | Platform AssemblyAI key. |
| `GROQ_API_KEY` | secrets | yes | string | (empty) |  | Platform Groq key. |
| `ANTHROPIC_API_KEY` | secrets | yes | string | (empty) |  | Platform Anthropic key. |
| `CARTESIA_API_KEY` | secrets | yes | string | (empty) |  | Platform Cartesia key. |
| `AZURE_SPEECH_KEY` | secrets | yes | string | (empty) |  | Platform Azure Speech key. |
| `AZURE_OPENAI_API_KEY` | secrets | yes | string | (empty) |  | Platform Azure OpenAI key. |
| `GOOGLE_CREDENTIALS_JSON` | secrets | yes | string | (empty) |  | Google service-account JSON (STT, TTS, Gemini on Vertex). |
| `RECORDING_S3_KEY` | secrets | yes | string | (empty) |  | Recording storage access key. |
| `RECORDING_S3_SECRET` | secrets | yes | string | (empty) |  | Recording storage secret key. |

## Dashboard notifications (`/ws/dashboard`)

The Sakhii dashboard's bell, dashboard page and Enquiries update in real time
over `wss://voice.sakhii.io/ws/dashboard`:

1. The browser gets `{url, token}` from Laravel (`GET /api/user/realtime`). The token
   is valid for 10 minutes, for one account.
2. It opens the socket and sends `{"type": "auth", "token": "…"}` as its first message,
   within 5 s. The engine answers `{"type": "ready"}`, or closes with 1008 if the token
   is bad.
3. Notifications for that account follow as JSON messages.

Notifications travel over Redis pub/sub, channel `sakhii:voice:notify:<client_id>`
(with `REDIS_KEY_PREFIX`). Laravel publishes call logs as they're saved, and when a
summary or recording arrives later. The engine publishes live calls itself:

```json
{"id": "live-CA0123", "type": "live_call", "title": "Live call in progress", "body": "Caller: 09876543210", "call_sid": "CA0123", "agent_id": "42", "time": "2026-10-09T10:30:00+00:00"}
{"id": "live-CA0123", "type": "live_call_ended", "call_sid": "CA0123", "duration_secs": 84.2, "time": "2026-10-09T10:31:24+00:00"}
```

The token is `base64url(JSON {"c": client_id, "exp": unix}) + "." +
base64url(HMAC-SHA256(k, first part))`, with `k = HMAC-SHA256(SAKHII_VOICE_CRED_KEY
bytes, "sakhii:voice:dashboard")`. Each account can have at most 20 sockets open, and
`dashboard_sockets` in `sakhii:voice:status` counts them. These sockets don't count as
calls, so a restart doesn't wait for them.

## Test call before the Laravel integration

`scripts/seed_test_agent.py` writes one agent config and one ExoPhone mapping into
Redis. It writes the same keys and fields Laravel will (see the Redis contract above),
using the engine's own settings, key builder and config model, so the engine finds
them exactly as it will find Laravel's.

On the server, run it from the app checkout as the `sakhii` user. It reads
`REDIS_URL`, `REDIS_KEY_PREFIX` and the API keys from `/opt/sakhii-voice/shared/.env`,
the same file the service uses:

```bash
cd /opt/sakhii-voice/app

# Sarvam STT + gpt-4o-mini + Sarvam Bulbul v3 (voice "priya")
sudo -u sakhii /opt/sakhii-voice/venv/bin/python scripts/seed_test_agent.py \
    --exophone 08047112233

# Sarvam STT + gpt-4o-mini + ElevenLabs eleven_flash_v2_5
sudo -u sakhii /opt/sakhii-voice/venv/bin/python scripts/seed_test_agent.py \
    --exophone 08047112233 --provider-preset elevenlabs --voice-id <ElevenLabs voice_id>

# Remove the test agent and its mapping
sudo -u sakhii /opt/sakhii-voice/venv/bin/python scripts/seed_test_agent.py \
    --exophone 08047112233 --delete
```

Options:

| Option | Default | Meaning |
|---|---|---|
| `--exophone` | required | ExoPhone in any format (`0804…`, `91804…`, `+91804…`); stored as E.164 |
| `--agent-id` | `test-agent` | Agent id to write |
| `--provider-preset` | `sarvam` | `sarvam` or `elevenlabs` (needs `--voice-id`) |
| `--voice-id` | `priya` for Sarvam | ElevenLabs voice_id, or another Bulbul v3 speaker |
| `--language` | `hi-IN` | Primary language (code or name). English is added as a second language. |
| `--greeting`, `--system-prompt` | short Hinglish receptionist | Override the opening line and prompt |
| `--company` | `Sakhii` | Fills `{company}` |
| `--force` | off | Overwrite a number already mapped to a different agent (refused by default) |
| `--env-file` | `shared/.env` if present, else `./.env` | Env file to read |

The script prints the exact keys it wrote, the Redis URL (password masked) and the
prefix. It warns if an API key the preset needs is missing, then prints the number to
call. The number must be attached to an Exotel flow with the Voicebot applet (see
below). `--delete` only removes the number mapping if it still points to this test
agent. The keys have no expiry, so delete them when you're done.

## Exotel setup

Use the native Voicebot applet. No SIP trunk is involved.

1. In the call flow, add a **Voicebot** applet with URL

   ```
   wss://voice.YOURDOMAIN.com/ws/exotel/<EXOTEL_WS_TOKEN>
   ```

   The token goes in the **path**. Exotel's applet drops query parameters, so
   `/ws/exotel?token=...` reaches the engine without a token and is rejected with 403.
   That form still works from curl, the latency probe and other tools. Keep the default
   8 kHz.

   Use a URL-safe token, e.g. `openssl rand -hex 24`. A `/` or `?` in the token would
   break the path form. Both forms are checked with a constant-time comparison.
2. Put a **Passthru** applet after the Voicebot, pointing at Laravel. When the Voicebot
   stream ends, Exotel continues the flow, and Laravel reads `sakhii:voice:call:{CallSid}`:
   - If `next_action = transfer`, Laravel returns the route to a **Connect** applet that
     dials `transfer_to`.
   - Otherwise it hangs up.

   This is how "Transfer to human agent" works: the engine speaks the hand-off line,
   writes `next_action`, then closes the stream.

### Keeping the token out of logs

The token is part of the URL, so any component that logs URLs can leak it.

- **Engine:** never logs the token. uvicorn logs every WebSocket handshake at INFO with
  the full path and query string. `app/redact.py` masks both forms before anything is
  written: `"WebSocket /ws/exotel/***" [accepted]` and `?token=***`. The tests check
  this against uvicorn's real log output, for good and bad tokens.
- **Apache access log:** Apache writes the request line, including the token, unless
  told not to. `deploy/apache-webuzo.conf` marks every `/ws/exotel` request with
  `SetEnvIf ... dontlog`. That only takes effect if the vhost's `CustomLog` line ends in
  `env=!dontlog`. Webuzo generates that line, so check the domain's vhost and add the
  condition if your Webuzo version allows a custom log directive. Otherwise:
  - restrict who can read the domain's access logs (root and Webuzo only);
  - keep log rotation short;
  - rotate `EXOTEL_WS_TOKEN` if the logs are ever shared or shipped elsewhere.
- **Apache error log:** mod_proxy can include the backend URL, and so the token, when it
  fails to reach the engine (e.g. both instances down). Treat it like the access log.
- **Rotating the token:** set the new value in `/opt/sakhii-voice/shared/.env`, restart
  both instances, then update the Voicebot URL in the Exotel flow. Calls fail with 403
  between those two steps.

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

Deploys are manual: you run a script on the server as root. GitHub Actions only runs
the tests. Any change that needs a step on the server is listed in
[CHANGELOG.md](CHANGELOG.md) under **Server action required**, so check it before
updating.

Server layout (AlmaLinux 9 + Webuzo Apache):

| Path | What |
|---|---|
| `/opt/sakhii-voice/app` | git clone of this repo (`main`) |
| `/opt/sakhii-voice/venv` | Python 3.12 venv (`/bin/python3.12`) |
| `/opt/sakhii-voice/shared/.env` | config and secrets. The scripts never overwrite it. |
| `sakhii-voice.service` | uvicorn on **127.0.0.1:8000 only**, as user `sakhii`, `Restart=always` |

```
Exotel ──wss://voice.sakhii.io/ws/exotel/<token>──▶ Apache (Webuzo, :443) ──ws://127.0.0.1:8000──▶ sakhii-voice
```

The repo is private. git asks for your GitHub username and a token as the password,
or you can export `GITHUB_TOKEN=<token>` first. The token only needs read-only
"Contents" access, and the scripts never write it to disk.

### First time: `deploy/setup.sh`

```bash
git clone https://github.com/paras-sharmadeveloper/sakhii-voice.git /opt/sakhii-voice/app
sudo bash /opt/sakhii-voice/app/deploy/setup.sh
```

It does the following, and is safe to re-run:
- pulls the repo and builds the venv while any old service keeps answering;
- stops, disables and removes the old units (`sakhii-voice.service`,
  `sakhii-voice@8000`, `sakhii-voice@8001`) and `/etc/sudoers.d/sakhii-voice`;
- installs `deploy/sakhii-voice.service`, then starts it and checks
  `http://127.0.0.1:8000/healthz`;
- deletes `/opt/sakhii-voice/releases` and `current`, which are copies of old code
  from the earlier GitHub deploy.

If `shared/.env` doesn't exist yet, it creates it from `.env.example` and stops so you
can fill it in.

Then add the Apache rules. Webuzo includes per-domain directives in both the HTTP and
HTTPS vhosts ([docs](https://webuzo.com/docs/developers/custom-virtualhost-config/)):

```bash
mkdir -p /var/webuzo-data/apache2/custom/domains
cp /opt/sakhii-voice/app/deploy/apache-webuzo.conf /var/webuzo-data/apache2/custom/domains/voice.sakhii.io.conf
/usr/local/apps/apache2/bin/httpd -M | grep -E 'proxy_(http|wstunnel)|setenvif'   # all three must be listed
```

Restart Apache from the Webuzo panel. The rules proxy `/ws/exotel` and
`/ws/exotel/<token>` to `ws://127.0.0.1:8000` (mod_proxy_wstunnel), and `/health` to
`http://127.0.0.1:8000/healthz`, with `ProxyTimeout 600`.

If SELinux is enforcing (`getenforce`), allow Apache to proxy to localhost once:
`setsebool -P httpd_can_network_connect 1`.

### Each update: `deploy/update.sh`

```bash
sudo bash /opt/sakhii-voice/app/deploy/update.sh
```

1. `git pull`, reinstall the dependencies, and restart `sakhii-voice`.
2. Wait up to 30 s for `/healthz`.
3. If it isn't healthy: `git reset --hard` to the previous commit, reinstall, restart
   again, and print the last 50 journal lines.

**A restart ends live calls** (uvicorn closes open WebSockets) and refuses new ones
for the few seconds it takes to come back, so update when it's quiet.

### On the server

- `systemctl status sakhii-voice`
- `journalctl -u sakhii-voice -f`
- `curl -s 127.0.0.1:8000/healthz` and `curl -s https://voice.sakhii.io/health`
- Deployed version: `git -C /opt/sakhii-voice/app log -1 --oneline`

For the lowest provider round trips, host in Mumbai (e.g. AWS `ap-south-1`). Sarvam and
Exotel are in India, and every hop to Europe or the US adds 100–250 ms per turn.
