"""Intent registry for note routing.

Adding a new intent is a two-line change:

1. add a member to ``Intent``,
2. add a keyword tuple to ``INTENT_KEYWORDS`` (the keyword fast path).

The classification prompt in ``prompts.py`` is generated from the enum, so the
LLM fallback learns about the new intent automatically. The configured webhook
receiver can branch on the intent (that integration lives outside this codebase).
"""

import re
from enum import StrEnum


class Intent(StrEnum):
    """Intent labels emitted by the classifier and included in webhook payloads.

    Attributes
    ----------
    JOURNAL : str
        Personal journal entry.
    TODO : str
        Actionable task or reminder.
    MEMO : str
        General note or fallback when no more specific category fits.
    RESEARCH : str
        Request for research and a summary.
    """

    JOURNAL = "journal"
    TODO = "todo"
    MEMO = "memo"
    RESEARCH = "research"


# Lowercase triggers matched against the start of a transcript. General notes
# without a specific keyword fall through to the LLM, where ``memo`` is the default.
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
