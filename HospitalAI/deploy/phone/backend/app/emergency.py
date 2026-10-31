"""Limited urgent-phrase detection shared by the consultation pipeline."""
from __future__ import annotations

import re


def _affirmed_occurrence(compact: str, phrase: str) -> bool:
    """Ignore only an explicit, immediately attached negation of this occurrence."""
    offset = 0
    while (position := compact.find(phrase, offset)) >= 0:
        suffix = compact[position + len(phrase):]
        prior = compact[max(0, position - 20):position]
        past = re.search(
            r"(?:작년|재작년|몇년전|수년전|십년전|오래전|과거)(?:에|에는|쯤)?"
            r"(?:(?:우리|저희)?(?:어머니|아버지|할머니|할아버지|엄마|아빠|남편|아내)(?:가|께서|는|이)?)?"
            r"(?:(?:농약|살충제|제초제)(?:을|를)?)?$",
            prior,
        )
        # A later distinct complaint does not make this old occurrence current.
        # Explicit continuation remains urgent; a repeated occurrence is checked separately.
        history_only = past is not None and not re.search(r"(?:지금도|현재도|여전히|아직도)", suffix)
        # Do not interpret double negatives as absent symptoms.
        prefix_negated = prior.endswith("안") and not prior.endswith(("동안", "불안", "편안"))
        double_negative = (
            re.match(r"^(?:은|는|이|가|도)?없(?:지|지는|진)(?:않|안)", suffix)
            or re.match(r"^지(?:는|도)?않(?:은|는)?(?:건|게|것은|것이|것도)(?:아니|아닙)", suffix)
            or re.match(r"^(?:운|은|는)?(?:건|게|것은|것이|것도)(?:아니|아닙)", suffix)
            or (prefix_negated and re.match(r"^(?:지|지는|진)(?:않|안)", suffix))
        )
        negated = (
            re.match(r"^(?:은|는|이|가|도|을|를)?(?:전혀|아예)?(?:없|적(?:이|은)?없|아니|아닌|아닙|아냐|안뿌리|안뿌렸|안썼|안써|사용하지않|뿌리지않|(?:있|계시)?지(?:는|도)?(?:않|안)|진(?:않|안))", suffix)
            or re.match(r"^(?:다는|다고)(?:말|말씀|뜻|이야기)(?:은|는|이)?(?:아니|아닙)", suffix)
            or (any(word in phrase for word in ("농약", "살충제", "제초제"))
                and re.match(r"^는지(?:몰라|모르|모르겠)", suffix))
            or prefix_negated
        )
        bleeding_stopped = (
            phrase == "피를흘"
            and re.match(r"^(?:렸|리던|리다|리다가).{0,15}(?:멈췄|멎었|그쳤)", suffix) is not None
            and not re.search(r"(?:다시|또).{0,5}흘", suffix)
        )
        if double_negative or (not negated and not history_only and not bleeding_stopped):
            return True
        offset = position + len(phrase)
    return False


def pesticide_dizziness(text: str) -> bool:
    compact = re.sub(r"\s+", "", text.lower())
    pesticide = any(_affirmed_occurrence(compact, word) for word in ("농약", "살충제", "제초제"))
    dizziness = any(_affirmed_occurrence(compact, word) for word in ("어지럽", "어지러", "어질", "현기증"))
    return pesticide and dizziness


def _pesticide_ingestion_or_symptoms(text: str) -> bool:
    """Escalate explicit oral exposure or exposure with symptoms; not a diagnosis."""
    compact = re.sub(r"\s+", "", text.lower())
    pesticide = any(_affirmed_occurrence(compact, word) for word in ("농약", "살충제", "제초제"))
    ingestion_pattern = (
        r"(?:농약|살충제|제초제)(?:이|가|은|는|을|를)?(?:조금|실수로|잘못|모르고)?"
        r"(?:(?:입에|입으로|입안에)들어(?:갔|간|가)|마셨|마신|마시|먹었|먹은|먹|삼켰|삼키|음독|흡입했|흡입하|들이마셨|들이마신|들이마시)"
    )
    ingested = any(_affirmed_occurrence(compact, match.group()) for match in re.finditer(ingestion_pattern, compact))
    symptoms = any(_affirmed_occurrence(compact, word) for word in (
        "구토", "토하고", "토해", "토했", "토합니다", "구역질", "메스껍", "메스꺼",
        "몸이떨", "손이떨", "몸이흔들", "숨이차", "숨차",
    ))
    return ingested or (pesticide and symptoms)


def pesticide_risk(text: str) -> bool:
    """Recognize limited pesticide risk patterns without claiming a diagnosis."""
    return pesticide_dizziness(text) or _pesticide_ingestion_or_symptoms(text)


def emergency_detected(text: str) -> bool:
    """Detect a limited set of urgent phrases; not a clinical triage system."""
    compact = re.sub(r"\s+", "", text.lower())
    if pesticide_risk(text):
        return True
    phrases = (
        "숨을쉴수없", "숨을쉴수가없", "숨을못쉬", "숨못쉬", "숨이안쉬어", "호흡곤란", "숨이막혀", "의식이없",
        "의식을잃", "쓰러졌", "쓰러져있", "말을못하", "심한가슴통증", "가슴이쥐어짜",
        "피가멈추지않", "심한출혈", "죽고싶", "자살", "목숨을끊",
        "한쪽마비", "한쪽이마비", "얼굴이마비", "심한흉통",
        "가슴통증", "가슴이아파", "가슴이아프", "가슴아파", "가슴아프", "흉통", "피를토", "피를흘",
        "피를많이흘", "피가많이나", "피가안멈",
    )
    return any(_affirmed_occurrence(compact, phrase) for phrase in phrases)


