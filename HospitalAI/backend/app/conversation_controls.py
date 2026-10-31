"""Conservative vetoes for a model-proposed destructive call control."""

import re


def end_requires_clarification(text: str) -> bool:
    """Block inferred hangup on a question, explicit continuation, or hold request.

    This never recognizes an end request: the contextual model still owns that
    decision. Keeping a call open is safer than acting on its contradictory end.
    """
    compact = re.sub(r"\s+", "", text)
    return bool(
        re.search(r"[?？]", compact)
        or re.search(r"(?:끊|마치|끝내|종료하|그만하)지(?:는)?(?:마|말|않)", compact)
        or re.search(r"(?:끊|마치|끝내|종료하|그만하)면안", compact)
        or re.search(r"(?:끊어|마쳐|끝내|종료해|그만해)도(?:되|돼|될)", compact)
        or re.search(r"(?:잠깐|잠시|조금)만(?:요|기다려|기다리)", compact)
        or re.search(r"(?:기다려주세요|기다려요|기다려줘)", compact)
    )
