"""Bounded lexical retrieval from a user-supplied, unverified knowledge file."""

import json
import re
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

MAX_BYTES: Final = 5 * 1024 * 1024
MAX_RECORDS: Final = 5000
MAX_RESULTS: Final = 5
STOPWORDS: Final = frozenset({"있어요", "있습니다", "무엇", "어떻게", "증상", "관련단어", "해결방안", "준비물"})
GENERIC_SYMPTOMS: Final = frozenset({
    "기침", "가래", "발열", "고열", "근육통", "관절통", "오한", "두통", "통증",
    "구토", "어지럼증", "피로", "호흡곤란", "설사", "인후통", "콧물", "코막힘",
    "복통", "현기증", "몸살", "결막염",
})


class KnowledgeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    category: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=4000)


class KnowledgeError(Exception):
    """Knowledge source cannot safely be read."""


class KnowledgeBase:
    def __init__(self, path: str | Path) -> None:
        try:
            with Path(path).open("rb") as source:
                raw = source.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise KnowledgeError("Knowledge file exceeds size limit")
            records = TypeAdapter(list[KnowledgeRecord]).validate_python(json.loads(raw.decode("utf-8-sig")))
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
            raise KnowledgeError("Knowledge file could not be read or validated") from exc
        if len(records) > MAX_RECORDS or len({record.id for record in records}) != len(records):
            raise KnowledgeError("Knowledge file has too many records or duplicate IDs")
        self.records = records

    def search(self, query: str) -> list[dict[str, str]]:
        words = {word for word in re.findall(r"[가-힣A-Za-z0-9]+", query.casefold())
                 if len(word) >= 2 and word not in STOPWORDS}
        if not words:
            return []
        scores: list[tuple[int, KnowledgeRecord]] = []
        for record in self.records:
            # Require every significant word in a keyword phrase to match. A
            # generic cough must not match "cough when hernia protrudes".
            first_section = record.content.split('.', 1)[0].partition(':')[2].casefold()
            phrases = [(part, 1) for part in first_section.split(',')]
            phrases += [(part, 3) for part in re.split(r'[/()]', record.category.casefold())]
            score = 0
            matched_signals: set[str] = set()
            category_matched = False
            for phrase, weight in phrases:
                tokens = [token for token in re.findall(r"[가-힣A-Za-z0-9]+", phrase)
                          if len(token) >= 2 and token not in STOPWORDS]
                if tokens and all(any(word.startswith(token) for word in words) for token in tokens):
                    score += sum(len(token) for token in tokens) * weight
                    matched_signals.update(tokens)
                    category_matched = category_matched or weight == 3
            # A special-case record should not be selected from a single broad
            # symptom alone; it needs context, a category, or multiple signals.
            if (record.id.startswith("spec_") and not category_matched
                    and len(matched_signals) < 2 and matched_signals <= GENERIC_SYMPTOMS):
                continue
            if score:
                scores.append((score, record))
        scores.sort(key=lambda item: (-item[0], item[1].id))
        return [{"id": f"clinic:json:{record.id}", "type": "clinic",
                 "text": f"{record.category}: {record.content}",
                 "source": "사용자 제공 JSON (의료 검증 미확인)", "updated_at": ""}
                for _, record in scores[:MAX_RESULTS]]
