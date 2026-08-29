"""Hybrid retrieval: keyword (BM25) + hard-constraint filter + embedding
similarity, merged per intent track.

Owner: Person A. This is the highest-leverage file — the baseline BM25-only
starter scores ~24% hit rate on Buying but only ~2.5% on Browsing (see
docs/baseline_results.json), because vague Browsing queries share no
vocabulary with product text. Adding real semantic_candidates() is the
single biggest score lever in this whole project.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "please", "some",
    "that", "the", "this", "to", "want", "with", "would", "you", "looking",
}


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(f"{key} {item}" for key, item in value.items())
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value)


def _terms(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text) if len(t) > 1 and t.lower() not in STOPWORDS]


class RetrievalIndex:
    """Keyword + product metadata index, built once at Agent startup
    (reused from the weak BM25 starter's SQLite FTS5 approach)."""

    def __init__(self, catalog_path: str | Path) -> None:
        self.catalog_path = Path(catalog_path)
        self.products: dict[str, dict] = {}
        self.connection = sqlite3.connect(":memory:")
        self._build_index()

    def _build_index(self) -> None:
        cursor = self.connection.cursor()
        cursor.execute(
            "CREATE VIRTUAL TABLE products USING fts5("
            "parent_asin UNINDEXED, title, categories, features, details, store, description, "
            "tokenize='unicode61 remove_diacritics 2')"
        )
        batch: list[tuple[str, str, str, str, str, str, str]] = []
        with self.catalog_path.open(encoding="utf-8") as handle:
            for line in handle:
                product = json.loads(line)
                parent_asin = str(product["parent_asin"])
                self.products[parent_asin] = product
                batch.append((
                    parent_asin,
                    _text(product.get("title")),
                    _text(product.get("categories")),
                    _text(product.get("features")),
                    _text(product.get("details")),
                    _text(product.get("store")),
                    _text(product.get("description")),
                ))
                if len(batch) >= 1000:
                    cursor.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
                    batch.clear()
        if batch:
            cursor.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
        self.connection.commit()

    def keyword_candidates(self, query: str, top_n: int = 50) -> list[tuple[str, float]]:
        """Returns [(parent_asin, score)], higher score = better match."""
        terms = list(dict.fromkeys(_terms(query)))[:40]
        if not terms:
            return []
        expression = " OR ".join(f'"{t}"' for t in terms)
        rows = self.connection.execute(
            "SELECT parent_asin, bm25(products, 0.0, 6.0, 4.0, 2.5, 2.5, 1.5, 1.0) as score "
            "FROM products WHERE products MATCH ? ORDER BY score LIMIT ?",
            (expression, top_n),
        ).fetchall()
        return [(str(asin), -float(score)) for asin, score in rows]  # sqlite bm25 is lower-is-better; flip sign

    def filter_candidates(self, slots: dict[str, str], candidate_ids: list[str]) -> list[str]:
        """Hard-constraint filter (price/brand/etc).

        TODO (Person A): implement real matching against product fields
        using `slots` — e.g. slots["budget"] -> product["price"] comparison,
        slots["brand"] -> product["store"]/title match. Placeholder: no-op,
        every candidate passes through unfiltered.
        """
        return candidate_ids

    def semantic_candidates(self, query: str, top_n: int = 50) -> list[tuple[str, float]]:
        """Dense embedding similarity search over the catalog.

        TODO (Person A): embed every product's searchable text once (at
        __init__ time, in-memory only — no external vector DB per the
        competition's scope rules) and embed the query per call, then rank
        by cosine similarity. Placeholder returns nothing, so Browsing
        currently falls back to keyword search alone until this is real.
        """
        return []


def retrieve(
    index: RetrievalIndex,
    query: str,
    slots: dict[str, str],
    track: str,
    top_n: int = 50,
) -> list[tuple[str, float]]:
    """Merge keyword + semantic candidates, then apply the hard filter.

    TODO (Person A): tune per-track weighting once semantic_candidates() is
    real — Buying should trust the filter/keyword route more, Browsing
    should trust semantic similarity more.
    """
    keyword_hits = dict(index.keyword_candidates(query, top_n))
    semantic_hits = dict(index.semantic_candidates(query, top_n))

    keyword_weight, semantic_weight = (0.7, 0.3) if track == "buying" else (0.3, 0.7)
    merged: dict[str, float] = {}
    for asin, score in keyword_hits.items():
        merged[asin] = merged.get(asin, 0.0) + keyword_weight * score
    for asin, score in semantic_hits.items():
        merged[asin] = merged.get(asin, 0.0) + semantic_weight * score

    candidate_ids = index.filter_candidates(slots, list(merged.keys()))
    ranked = sorted(((asin, merged[asin]) for asin in candidate_ids), key=lambda item: -item[1])
    return ranked[:top_n]
