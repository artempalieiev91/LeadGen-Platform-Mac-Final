"""
Підбір еталонних пар Title → Right Title для промпта: exact lookup + семантичний пошук (embeddings).
Повний `title_training.csv` не вставляється в кожен запит chat.completions.
"""

from __future__ import annotations

import csv
import math
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import OpenAI

_DATA_DIR = Path(__file__).resolve().parent / "sheets_prep_data"
_TITLE_TRAINING_CSV = _DATA_DIR / "title_training.csv"

COL_IN = "Title"
COL_OUT = "Right Title"

EMBEDDING_MODEL = "text-embedding-3-small"
EMBED_BATCH = 256
RETRIEVAL_TOP_K = 6
MAX_PAIRS_IN_PROMPT = 40
EMBED_TIMEOUT_SEC = 120.0


def normalize_title_key(raw: str) -> str:
    return " ".join((raw or "").split())


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _nlargest_indices(scores: list[float], k: int) -> list[int]:
    if k <= 0 or not scores:
        return []
    indexed = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return indexed[:k]


class TitleTrainingRetrievalIndex:
    """
    corpus: унікальні вхідні Title з канонічним Right Title (при дублях left — найчастіший right).
    exact_map: normalize_title_key(left) -> canonical right
    """

    def __init__(self, csv_path: Path) -> None:
        self._path = csv_path
        self.corpus: list[tuple[str, str]] = []
        self.exact_map: dict[str, str] = {}
        self._vectors: list[list[float]] | None = None
        self._mtime: float = 0.0
        if csv_path.is_file():
            self._mtime = csv_path.stat().st_mtime
            self._load_rows()

    def _load_rows(self) -> None:
        left_to_rights: dict[str, list[str]] = {}
        with self._path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return
            for row in reader:
                left = (row.get(COL_IN) or "").strip()
                right = (row.get(COL_OUT) or "").strip()
                if not left:
                    continue
                left_to_rights.setdefault(left, []).append(right)

        exact: dict[str, str] = {}
        corpus_pairs: list[tuple[str, str]] = []
        for left, rights in left_to_rights.items():
            canon = Counter(rights).most_common(1)[0][0]
            key = normalize_title_key(left)
            exact[key] = canon
            corpus_pairs.append((left, canon))

        self.exact_map = exact
        self.corpus = corpus_pairs

    @property
    def is_empty(self) -> bool:
        return len(self.corpus) == 0

    def reset_embeddings(self) -> None:
        self._vectors = None

    def embed_corpus(self, client: OpenAI) -> None:
        if self._vectors is not None or not self.corpus:
            return
        texts = [p[0] for p in self.corpus]
        all_emb: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            chunk = texts[i : i + EMBED_BATCH]
            r = client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=chunk,
                timeout=EMBED_TIMEOUT_SEC,
            )
            for d in sorted(r.data, key=lambda x: x.index):
                all_emb.append(list(d.embedding))
        self._vectors = all_emb

    def build_few_shot_for_queries(self, client: OpenAI, query_titles: list[str]) -> str:
        """Пари для промпта: top-K сусідів на кожен унікальний непорожній тайтл з батча."""
        if not self.corpus or self._vectors is None:
            return ""
        uniq: list[str] = []
        seen: set[str] = set()
        for t in query_titles:
            n = normalize_title_key(t)
            if not n or n in seen:
                continue
            seen.add(n)
            uniq.append(t)
        if not uniq:
            return ""

        r = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=uniq,
            timeout=EMBED_TIMEOUT_SEC,
        )
        qvecs = [list(d.embedding) for d in sorted(r.data, key=lambda x: x.index)]

        pair_best: dict[tuple[str, str], float] = {}
        for qvec in qvecs:
            scores = [_cosine(qvec, cvec) for cvec in self._vectors]
            for i in _nlargest_indices(scores, RETRIEVAL_TOP_K):
                left, right = self.corpus[i]
                pair = (left, right)
                s = scores[i]
                prev = pair_best.get(pair)
                if prev is None or s > prev:
                    pair_best[pair] = s

        ordered_pairs = sorted(pair_best.keys(), key=lambda p: pair_best[p], reverse=True)[:MAX_PAIRS_IN_PROMPT]
        lines = [
            "Еталонні пари (підібрані за схожістю до тайтлів у цьому батчі; застосовуй ту саму логіку, "
            "канони — як у Right Title):\n\n",
        ]
        for left, right in ordered_pairs:
            lines.append(f"- {left!r} → {right!r}\n")
        return "".join(lines)


_index_cache: TitleTrainingRetrievalIndex | None = None
_index_cache_mtime: float | None = None


def get_title_training_index(csv_path: Path | None = None) -> TitleTrainingRetrievalIndex:
    path = csv_path or _TITLE_TRAINING_CSV
    global _index_cache, _index_cache_mtime
    mtime = path.stat().st_mtime if path.is_file() else 0.0
    if _index_cache is not None and _index_cache_mtime == mtime and _index_cache._path == path:
        return _index_cache
    idx = TitleTrainingRetrievalIndex(path)
    _index_cache = idx
    _index_cache_mtime = mtime
    return idx


def clear_title_index_cache() -> None:
    global _index_cache, _index_cache_mtime
    _index_cache = None
    _index_cache_mtime = None
