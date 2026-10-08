"""Seed a test agent into Redis so you can make a real call before Laravel
writes agents itself.

Writes exactly what Laravel will write (see "Redis contract" in the README):
    sakhii:voice:agent:{agent_id}   agent config JSON
    sakhii:voice:number:{+91...}    agent_id for the ExoPhone
using the engine's own settings, key builder and config model, so the keys
and fields are the ones the engine looks up.

On the server (Redis + REDIS_KEY_PREFIX come from /opt/sakhii-voice/shared/.env):

    cd /opt/sakhii-voice/app
    sudo -u sakhii /opt/sakhii-voice/venv/bin/python scripts/seed_test_agent.py --exophone 08047112233
    sudo -u sakhii /opt/sakhii-voice/venv/bin/python scripts/seed_test_agent.py --exophone 08047112233 \
        --provider-preset elevenlabs --voice-id <ElevenLabs voice_id>
    sudo -u sakhii /opt/sakhii-voice/venv/bin/python scripts/seed_test_agent.py --exophone 08047112233 --delete
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import store  # noqa: E402
from app.agent_config import LANGUAGE_NAMES, AgentConfig, to_language_code  # noqa: E402
from app.settings import get_settings  # noqa: E402

SERVER_ENV = Path("/opt/sakhii-voice/shared/.env")

DEFAULT_GREETING = "Namaste! Main Sakhii bol rahi hoon. Bataiye, main aapki kya madad kar sakti hoon?"
DEFAULT_CLOSING = "Dhanyavaad! Aapka din shubh ho."
DEFAULT_PROMPT = """You are Sakhii, a friendly receptionist for {company}, answering a test phone call.

- Speak natural Hinglish (Hindi-English mix), warm and polite, using "ji".
- Keep every reply to one short sentence, then let the caller speak.
- You can answer simple questions, take the caller's name and the reason for calling,
  and offer a callback.
- If you don't know something, say a team member will call back; never make things up.
- When the caller is done, end the call politely."""

PRESETS = {
    "sarvam": {
        "stt": {"provider": "sarvam", "model": "saaras:v3-realtime"},
        "llm": {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0.4},
        "tts": {"provider": "sarvam", "model": "bulbul:v3", "voice": "priya", "speed": 1.0},
    },
    "elevenlabs": {
        "stt": {"provider": "sarvam", "model": "saaras:v3-realtime"},
        "llm": {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0.4},
        "tts": {"provider": "elevenlabs", "model": "eleven_flash_v2_5", "voice": None, "speed": 1.0},
    },
}
REQUIRED_KEYS = {
    "sarvam": ("sarvam_api_key", "openai_api_key"),
    "elevenlabs": ("sarvam_api_key", "openai_api_key", "elevenlabs_api_key"),
}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed (or delete) a test agent for a real Exotel call.")
    p.add_argument("--exophone", required=True, help="ExoPhone to map, e.g. 08047112233 or +918047112233")
    p.add_argument("--agent-id", default="test-agent")
    p.add_argument("--provider-preset", choices=sorted(PRESETS), default="sarvam")
    p.add_argument("--voice-id", help="ElevenLabs voice_id (required for the elevenlabs preset); "
                                      "for sarvam, a Bulbul v3 speaker (default priya)")
    p.add_argument("--language", default="hi-IN", help="primary language code or name (default hi-IN)")
    p.add_argument("--greeting", default=DEFAULT_GREETING)
    p.add_argument("--system-prompt", default=DEFAULT_PROMPT)
    p.add_argument("--company", default="Sakhii", help="fills {company} in the prompt")
    p.add_argument("--delete", action="store_true", help="remove the test agent and its number mapping")
    p.add_argument("--force", action="store_true", help="overwrite a number already mapped to another agent")
    p.add_argument("--env-file", type=Path,
                   help=f"engine env file (default {SERVER_ENV} if present, else ./.env)")
    args = p.parse_args(argv)
    if args.provider_preset == "elevenlabs" and not args.voice_id and not args.delete:
        p.error("--voice-id is required with --provider-preset elevenlabs")
    return args


def load_engine_env(env_file: Path | None) -> Path | None:
    """Load the engine's env file into the environment, as systemd does for the
    service. Variables already set in the environment win."""
    if env_file is not None:
        if not env_file.is_file():
            sys.exit(f"env file not found: {env_file}")
        path = env_file
    elif SERVER_ENV.is_file():
        path = SERVER_ENV
    elif Path(".env").is_file():
        path = Path(".env")
    else:
        return None
    load_dotenv(path, override=False)
    get_settings.cache_clear()
    return path


def build_agent(args: argparse.Namespace) -> AgentConfig:
    models = json.loads(json.dumps(PRESETS[args.provider_preset]))
    if args.voice_id:
        models["tts"]["voice"] = args.voice_id
    primary = to_language_code(args.language)
    return AgentConfig.model_validate(
        {
            "schema_version": 1,
            "agent_id": args.agent_id,
            "name": f"Test agent ({args.provider_preset})",
            "display_name": "Sakhii",
            "use_case": "receptionist_test",
            "greeting": {"opening": args.greeting, "closing": DEFAULT_CLOSING},
            "business": {"name": args.company},
            "max_call_duration_secs": 300,
            "models": models,
            "system_prompt": args.system_prompt,
            "languages": {
                "primary": primary,
                "also": [] if primary == "en-IN" else ["en-IN"],
                "code_switching": True,
                "auto_detect": True,
            },
            "variables": {"company": args.company},
        }
    )


async def seed(args: argparse.Namespace) -> int:
    s = get_settings()
    number = store.normalize_number(args.exophone)
    if not number or len(number) != 13 or not number.startswith("+91"):
        print(f"--exophone {args.exophone!r} is not an Indian number (+91 and 10 digits)", file=sys.stderr)
        return 2
    agent_key, number_key = store.key("agent", args.agent_id), store.key("number", number)
    r = store.client()
    try:
        await r.ping()
        mapped_to = await r.get(number_key)

        if args.delete:
            removed = [agent_key] if await r.delete(agent_key) else []
            if mapped_to == args.agent_id:
                await r.delete(number_key)
                removed.append(number_key)
            elif mapped_to:
                print(f"left {number_key} alone: it points to agent {mapped_to!r}, not {args.agent_id!r}")
            print("deleted:" if removed else "nothing to delete")
            for k in removed:
                print(f"  {k}")
            return 0

        if mapped_to and mapped_to != args.agent_id and not args.force:
            print(f"{number_key} already maps to agent {mapped_to!r}; use --force to overwrite",
                  file=sys.stderr)
            return 1

        agent = build_agent(args)
        async with r.pipeline(transaction=True) as p:
            p.set(agent_key, agent.model_dump_json())
            p.set(number_key, agent.agent_id)
            await p.execute()
    finally:
        await store.close()

    tts = agent.models.tts
    print("wrote:")
    print(f"  {agent_key}  (agent config JSON)")
    print(f"  {number_key} = {agent.agent_id}")
    print(f"redis: {_mask(s.redis_url)}  prefix: {s.redis_key_prefix!r}")
    print(
        f"models: STT {agent.models.stt.provider}/{agent.models.stt.model}, "
        f"LLM {agent.models.llm.provider}/{agent.models.llm.model}, "
        f"TTS {tts.provider}/{tts.model} voice={tts.voice}"
    )
    print(f"language: {LANGUAGE_NAMES.get(agent.languages.primary, agent.languages.primary)}")
    missing = [k.upper() for k in REQUIRED_KEYS[args.provider_preset] if not getattr(s, k)]
    if missing:
        print(f"WARNING: {', '.join(missing)} not set in the engine env; the call will fail")
    print(f"\nCall the ExoPhone: 0{number[3:]}  ({number})")
    print("Its Exotel flow must route to the Voicebot applet at "
          "wss://<your domain>/ws/exotel/<EXOTEL_WS_TOKEN>.")
    return 0


def _mask(url: str) -> str:
    """Hide a password in redis://user:password@host."""
    head, sep, tail = url.rpartition("@")
    if not sep or ":" not in head.split("//", 1)[-1]:
        return url
    scheme, _, creds = head.partition("//")
    return f"{scheme}//{creds.split(':', 1)[0]}:***@{tail}"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env_path = load_engine_env(args.env_file)
    print(f"env: {env_path or 'process environment only'}")
    return asyncio.run(seed(args))


if __name__ == "__main__":
    sys.exit(main())
