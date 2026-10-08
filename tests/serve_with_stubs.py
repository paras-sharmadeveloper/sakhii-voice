"""`python -m app.serve`, with the stub providers registered (for tests that
run the engine as a real process)."""

import sys

from app import providers, serve

providers.STT["stub"] = "tests.stub_providers:build_stt"
providers.LLM["stub"] = "tests.stub_providers:build_llm"
providers.TTS["stub"] = "tests.stub_providers:build_tts"

if __name__ == "__main__":
    serve.main(sys.argv[1:])
