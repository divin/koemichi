"""Intent registry for note routing.

Adding a new intent is a two-line change:

1. add a member to ``Intent``,
2. add a keyword tuple to ``INTENT_KEYWORDS`` (the keyword fast path).

The classification prompt in ``prompts.py`` is generated from the enum, so the
LLM fallback learns about the new intent automatically. The n8n dispatch
workflow then needs a matching branch (that lives outside this codebase).
"""

import re
from enum import StrEnum


class Intent(StrEnum):
    """Intent labels emitted by the classifier and consumed by n8n.

    Attributes
    ----------
    JOURNAL : str
        Personal journal entry.
    TODO : str
        Actionable task or reminder.
    MEMO : str
        General note without a more specific category.
    RESEARCH : str
        Request for research and a summary.
    OTHER : str
        Content that does not fit the other categories.
    """

    JOURNAL = "journal"
    TODO = "todo"
    MEMO = "memo"
    RESEARCH = "research"
    OTHER = "other"


# Lowercase triggers matched against the start of a transcript. ``other``
# deliberately has no keywords: it is the fallback intent.
INTENT_KEYWORDS: dict[Intent, tuple[str, ...]] = {
    Intent.JOURNAL: ("journal", "diary", "tagebuch"),
    Intent.TODO: ("todo", "to-do", "task", "aufgabe"),
    Intent.MEMO: ("memo", "note", "notiz", "remember"),
    Intent.RESEARCH: ("research", "recherche", "look up", "lookup", "search"),
}


def keyword_intent(transcript: str | None) -> Intent | None:
    """Run the keyword fast path: first phrase of a transcript maps to an intent.

    Returns ``None`` when no keyword matches, so the caller can fall back to
    the LLM classifier.

    Parameters
    ----------
    transcript : str or None
        Normalized transcript text, or None when no transcript exists.

    Returns
    -------
    Intent or None
        The matched intent, or None when no keyword matches.
    """
    if not transcript:
        return None

    text = " ".join(transcript.strip().lower().split())
    for intent, keywords in INTENT_KEYWORDS.items():
        for keyword in keywords:
            if re.match(rf"{re.escape(keyword)}\b", text):
                return intent
    return None
