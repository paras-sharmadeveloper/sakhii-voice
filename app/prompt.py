"""Turn an AgentConfig into the system prompt, greeting and TTS text rules."""

import re

from pipecat.utils.text.base_text_filter import BaseTextFilter

from app.agent_config import LANGUAGE_NAMES, AgentConfig

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_SPACES = re.compile(r"[ \t]{2,}")

# Hard cap on knowledge text in the prompt: every extra token adds to the
# LLM's time-to-first-token on every turn.
MAX_INLINE_KNOWLEDGE_CHARS = 12_000


def call_variables(agent: AgentConfig, per_call: dict[str, str]) -> dict[str, str]:
    primary = agent.languages.primary
    base = {
        "agent_name": agent.display_name or agent.name,
        "company": agent.business.name,
        "language": LANGUAGE_NAMES.get(primary, primary),
    }
    return {**base, **agent.variables, **per_call}


def render(template: str, variables: dict[str, str]) -> str:
    """Fill {placeholders}. Unknown ones become empty rather than being read
    out loud as "curly brace customer name"."""
    out = _PLACEHOLDER.sub(lambda m: variables.get(m.group(1), ""), template)
    return _SPACES.sub(" ", out).strip()


def _language_rules(agent: AgentConfig) -> str:
    langs = agent.languages
    primary = LANGUAGE_NAMES.get(langs.primary, langs.primary)
    others = [LANGUAGE_NAMES.get(c, c) for c in langs.also if c != langs.primary]
    lines = [f"- Start in {primary}."]
    if others:
        lines.append(
            f"- You also speak {', '.join(others)}. If the caller speaks one of these, "
            "reply in that language from then on."
        )
    else:
        lines.append(f"- Reply only in {primary}.")
    if langs.code_switching:
        lines.append(
            "- Mixing languages mid-sentence (e.g. Hinglish) is fine; mirror how the caller talks."
        )
    lines.append(
        "- Write Hindi and other Indian languages in their native script, "
        "English words in English."
    )
    return "\n".join(lines)


def system_prompt(agent: AgentConfig, variables: dict[str, str]) -> str:
    parts = [render(agent.system_prompt, variables)]

    override = agent.language_overrides.get(agent.languages.primary)
    if override:
        parts.append(render(override, variables))

    parts.append(
        "## How to speak on this call\n"
        "You are on a live phone call. Everything you write is spoken aloud.\n"
        "- Keep each reply to one or two short sentences, then let the caller talk.\n"
        "- No markdown, lists, emojis, URLs or symbols. Say numbers and amounts the way "
        "a person would say them.\n"
        "- Ask one question at a time.\n"
        f"- Tone: {agent.persona.base_tone}."
        + (
            " Notice how the caller feels and adjust your tone to it."
            if agent.persona.emotion_awareness.lower().startswith(("adaptive", "always emp"))
            else ""
        )
        + "\n"
        + _language_rules(agent)
    )

    if agent.fillers:
        fillers = ", ".join(f'"{f.text}"' for f in agent.fillers)
        parts.append(f"Natural fillers you may use sparingly: {fillers}.")

    b = agent.business
    business = [
        f"{label}: {value}"
        for label, value in (
            ("Business", b.name),
            ("About", b.description),
            ("Address", b.address),
            ("Phone", b.phone),
            ("Email", b.email),
            ("Website", b.website),
        )
        if value
    ]
    if business:
        parts.append("## Business details\n" + "\n".join(business))

    if agent.knowledge.faqs:
        faqs = "\n".join(f"Q: {f.question}\nA: {f.answer}" for f in agent.knowledge.faqs)
        parts.append(
            "## FAQs\nWhen the caller asks one of these, answer with this content first.\n" + faqs
        )

    if agent.knowledge.inline_text:
        parts.append(
            "## Reference information\n"
            + agent.knowledge.inline_text[:MAX_INLINE_KNOWLEDGE_CHARS]
        )

    tools = []
    if agent.transfer.enabled and agent.transfer.number:
        tools.append(
            "- If the caller asks for a human, disputes, or you cannot help, "
            "use transfer_call."
        )
    tools.append(
        "- When the conversation is complete and the caller has nothing else, "
        "use end_call. Do not say goodbye yourself; end_call does that."
    )
    parts.append("## Call controls\n" + "\n".join(tools))

    return "\n\n".join(p for p in parts if p)


class PronunciationFilter(BaseTextFilter):
    """Swap terms for how they should be said, right before TTS.

    Rules for the agent's primary language win over "all" rules for the same
    term (EMI is "ee-em-eye" in English but "ee-em-aai" in Hindi).
    """

    def __init__(self, agent: AgentConfig):
        primary = agent.languages.primary
        chosen: dict[str, str] = {}
        for rule in agent.pronunciations:
            lang = rule.language.strip().lower()
            if lang in ("", "all"):
                chosen.setdefault(rule.term, rule.say)
            elif _language_matches(lang, primary):
                chosen[rule.term] = rule.say
        self._rules = chosen
        self._pattern = (
            re.compile(
                r"(?<!\w)("
                + "|".join(re.escape(t) for t in sorted(chosen, key=len, reverse=True))
                + r")(?!\w)"
            )
            if chosen
            else None
        )

    @property
    def active(self) -> bool:
        return self._pattern is not None

    async def update_settings(self, settings):
        pass

    async def filter(self, text: str) -> str:
        if self._pattern is None:
            return text
        return self._pattern.sub(lambda m: self._rules[m.group(1)], text)

    async def handle_interruption(self):
        pass

    async def reset_interruption(self):
        pass


def _language_matches(rule_lang: str, code: str) -> bool:
    name = LANGUAGE_NAMES.get(code, code).lower()
    return rule_lang in (code.lower(), code.split("-")[0].lower(), name)
