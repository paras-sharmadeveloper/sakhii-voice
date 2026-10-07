from app.agent_config import AgentConfig
from app.prompt import PronunciationFilter, call_variables, render, system_prompt


def test_parses_builder_payload(agent_dict):
    a = AgentConfig.model_validate(agent_dict)
    assert a.agent_id == "42"
    assert a.languages.primary == "hi-IN"
    assert a.languages.also == ["en-IN"]
    assert "hi-IN" in a.language_overrides


def test_render_fills_and_blanks_unknown(agent_dict):
    a = AgentConfig.model_validate(agent_dict)
    v = call_variables(a, {"customer_name": "Ramesh"})
    assert render(a.greeting.opening, v) == (
        "Namaste, main Sakhii bol rahi hoon Acme Finance se. Kya main Ramesh ji se baat kar rahi hoon?"
    )
    assert "{" not in render("Due {amount_due} now", v)


def test_system_prompt_sections(agent_dict):
    a = AgentConfig.model_validate(agent_dict)
    p = system_prompt(a, call_variables(a, {"amount_due": "5000 rupees"}))
    assert "Amount due: 5000 rupees" in p
    assert "Hamesha ji" in p
    assert "Can I pay using UPI?" in p
    assert "transfer_call" in p and "end_call" in p
    assert "Start in Hindi" in p and "English" in p


async def test_pronunciation_prefers_primary_language(agent_dict):
    f = PronunciationFilter(AgentConfig.model_validate(agent_dict))
    out = await f.filter("Aapki EMI UPI se bhar dijiye. EMIs")
    assert out == "Aapki ee-em-aai yoo-pee-eye se bhar dijiye. EMIs"
