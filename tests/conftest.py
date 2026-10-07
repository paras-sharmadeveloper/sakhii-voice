import json

import pytest

AGENT = {
    "schema_version": 1,
    "agent_id": 42,
    "tenant_id": 7,
    "name": "EMI Recovery",
    "display_name": "Sakhii",
    "greeting": {
        "opening": "Namaste, main {agent_name} bol rahi hoon {company} se. Kya main {customer_name} ji se baat kar rahi hoon?",
        "closing": "Dhanyavaad {customer_name} ji.",
    },
    "business": {"name": "Acme Finance", "phone": "+911234567890"},
    "transfer": {"enabled": True, "number": "+919999999999"},
    "models": {
        "stt": {"provider": "sarvam", "model": "saarika:v2.5"},
        "llm": {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0.35},
        "tts": {"provider": "sarvam", "model": "bulbul:v2", "voice": "Anushka", "speed": 1.0},
    },
    "system_prompt": "You are {agent_name} for {company}. Amount due: {amount_due} by {due_date}.",
    "languages": {"primary": "Hindi", "also": ["English"], "code_switching": True, "auto_detect": True},
    "language_overrides": {"Hindi": "Hamesha ji ka istemaal karein."},
    "fillers": [{"text": "ji haan", "language": "Hindi", "context": "Acknowledgement"}],
    "pronunciations": [
        {"term": "EMI", "say": "ee-em-eye", "language": "English"},
        {"term": "EMI", "say": "ee-em-aai", "language": "Hindi"},
        {"term": "UPI", "say": "yoo-pee-eye", "language": "All"},
    ],
    "knowledge": {"faqs": [{"question": "Can I pay using UPI?", "answer": "Yes."}]},
    "tools": [
        {
            "name": "send_payment_link",
            "description": "Send a UPI payment link by SMS.",
            "parameters": {"type": "object", "properties": {"amount": {"type": "number"}}, "required": ["amount"]},
            "url": "https://example.invalid/pay",
        }
    ],
}


@pytest.fixture
def agent_dict():
    return json.loads(json.dumps(AGENT))
