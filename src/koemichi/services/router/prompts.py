"""Classification prompt for the intent agent.

The prompt is generated from the ``Intent`` enum so adding an intent updates
the LLM's answer space automatically.
"""

from .intents import Intent

_EXAMPLES = """\
Examples:
- "Journal: today the trains were late again" -> journal
- "I have a doctor's appointment tomorrow" -> todo
- "The bakery on the corner now sells sourdough" -> memo
- "Research how fast sea levels are rising" -> research
- "I'm feeling tired after the long drive" -> other
"""


def build_system_prompt() -> str:
    """Build the system prompt listing exactly the supported intents.

    Returns
    -------
    str
        Prompt text containing the intent choices and classification examples.
    """
    intents = ", ".join(f"`{intent.value}`" for intent in Intent)
    return (
        "You classify a single voice note transcript into exactly one of the "
        f"following intents: {intents}.\n"
        "The transcript is raw speech-to-text output, so punctuation and casing "
        "may be missing; ignore filler words such as 'uh', 'um'.\n"
        "Choose `other` when the note clearly does not fit any specific intent.\n"
        "Return only the classification JSON; the intent field must be one of "
        "the intents listed above.\n" + _EXAMPLES
    )
