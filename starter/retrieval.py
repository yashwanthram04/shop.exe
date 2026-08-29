"""Hybrid retrieval: keyword (BM25) + hard-constraint filter + embedding
similarity, merged per intent track.

Owner: Person A. This is the highest-leverage file — the baseline BM25-only
starter scores ~24% hit rate on Buying but only ~2.5% on Browsing (see
docs/baseline_results.json), because vague Browsing queries share no
vocabulary with product text. semantic_candidates() (a real pretrained
sentence-embedding model, cosine similarity, in-memory only — no external
vector DB) is the fix for that gap.

Requires `sentence-transformers` (see requirements.txt) as the always-on
local fallback. Catalog embeddings are computed once and cached to disk
under `<catalog_dir>/.embedding_cache/` (gitignored) so repeated evaluator
runs don't re-embed all 50k products every time — only the very first run
after a fresh clone or a catalog change pays that cost.

Optional: if an `OPENAI_API_KEY` environment variable is set, OpenAI
embeddings (`text-embedding-3-small`) are built and cached the same way and
tried FIRST on every semantic_candidates() call, falling back to the local
model on any failure (missing key, network error, rate limit, timeout) —
never on the local model *unavailable*, since it's always built. This
satisfies docs/submission_rules.md's requirement to document network
behavior: this file works fully offline, and transparently improves if a
key happens to be available. Never commit an API key — set it as an
environment variable only (`OPENAI_API_KEY`), per submission_rules.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

import numpy as np

TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "please", "some",
    "that", "the", "this", "to", "want", "with", "would", "you", "looking",
}

MATERIAL_WORDS = ("cotton", "polyester", "nylon", "leather", "wool", "spandex", "silk", "rayon", "denim")
COLOR_WORDS = ("black", "white", "blue", "red", "pink", "green", "brown", "gray", "grey", "purple", "yellow", "orange")
STYLE_WORDS = ("casual", "formal", "athletic", "classic", "vintage", "modern", "sporty", "elegant")

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
OPENAI_TIMEOUT_SECONDS = 8  # keep a single turn from hanging if the API is slow/unreachable
BUDGET_TOLERANCE = 1.15  # ~15% slack: intent text is often "budget around $X", not an exact cap


def _parse_budget(value: str | None) -> float | None:
    """`value` may be a "; "-joined multi-fact string (Person B's
    extract_slot_values format) even though a customer stating two budgets
    at once isn't really meaningful — take the first part that actually
    parses as a number, rather than failing outright on the whole string."""
    if not value:
        return None
    for part in value.split(";"):
        try:
            return float(part.strip())
        except ValueError:
            continue
    return None


def _split_slot_values(value: str) -> list[str]:
    """Split a filled_slots value into its individual facts. Person B's
    extract_slot_values joins multiple facts from one answer with "; "
    (e.g. "cotton; leather") rather than storing a list — a single-value
    slot just splits into one part, so this is safe to call unconditionally."""
    return [part.strip() for part in value.split(";") if part.strip()]


def _catalog_cache_key(catalog_path: Path, model_name: str) -> str:
    stat = catalog_path.stat()
    raw = f"{catalog_path.resolve()}::{stat.st_size}::{int(stat.st_mtime)}::{model_name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


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
        self._model = None  # lazy-loaded SentenceTransformer, see _get_model()
        self._embed_ids: list[str] = []
        self._embeddings: np.ndarray | None = None
        self._openai_client = None  # lazy-loaded, see _get_openai_client()
        self._openai_embed_ids: list[str] = []
        self._openai_embeddings: np.ndarray | None = None
        self._build_embeddings()
        self._build_openai_embeddings()

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
        """Hard-constraint filter on *structured* attributes only
        (budget/brand/size/category). Soft attributes (material/color/
        style/...) are deliberately NOT hard-excluded here — free-text
        metadata is too inconsistent to safely gate recall on, and those
        signals already influence keyword/semantic scoring in `retrieve()`.
        A false negative in a hard filter permanently removes a candidate
        before ranking ever sees it, so this stays conservative and falls
        back to "no filter" if it would zero out the whole pool.
        """
        budget = _parse_budget(slots.get("budget"))
        brand = slots.get("brand")
        size = slots.get("size")
        category = slots.get("category")
        if budget is None and not brand and not size and not category:
            return candidate_ids

        kept: list[str] = []
        for asin in candidate_ids:
            product = self.products.get(asin)
            if product is None:
                continue
            if budget is not None:
                price = product.get("price")
                if isinstance(price, (int, float)) and price > budget * BUDGET_TOLERANCE:
                    continue  # only exclude when price is known and clearly over budget
            if brand:
                haystack = f"{product.get('store', '')} {product.get('title', '')}".lower()
                # "; "-split so a joined multi-value slot (e.g. "nike; adidas")
                # matches if the product has ANY one of them, not the literal
                # joined string (which would never appear verbatim in text).
                if not any(part.lower() in haystack for part in _split_slot_values(brand)):
                    continue
            if size:
                haystack = (_text(product.get("details")) + " " + _text(product.get("features"))).lower()
                if not any(part.lower() in haystack for part in _split_slot_values(size)):
                    continue
            if category:
                # `category` is a loose customer phrase (e.g. "earrings
                # hoop"), not a catalog taxonomy path — matching the full
                # phrase as one contiguous substring against
                # product["categories"] would fail even for a correct
                # match (the words could appear as separate list entries).
                # Word-overlap containment instead: does ANY meaningful
                # word from the phrase show up in this product's own
                # categories/title text?
                haystack = (_text(product.get("categories")) + " " + _text(product.get("title"))).lower()
                words = [w for part in _split_slot_values(category) for w in part.split() if len(w) > 2]
                if words and not any(word in haystack for word in words):
                    continue
            kept.append(asin)

        return kept if kept else candidate_ids

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # lazy: avoid loading torch for callers that never need embeddings
            self._model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        return self._model

    def _embedding_text(self, product: dict) -> str:
        parts = [_text(product.get("title")), _text(product.get("categories")), _text(product.get("features"))]
        return " ".join(part for part in parts if part)[:1000]

    def _extract_attrs(self, product: dict) -> dict[str, str]:
        """Lightweight structured facts pulled from a product's own text —
        used by clarify.py's entropy-based attribute selection (which open
        attribute would split the current candidate pool the most) and
        rank.py's slot-fit scoring. Same simple word-list approach as
        router.py's extract_slots, but deliberately kept independent here:
        one parses customer messages, this parses product text, and neither
        owner needs to import the other's file for what's a few word lists.
        """
        haystack = f"{_text(product.get('title'))} {_text(product.get('features'))} {_text(product.get('details'))}".lower()
        attrs: dict[str, str] = {}
        for word in MATERIAL_WORDS:
            if word in haystack:
                attrs["material"] = word
                break
        for word in COLOR_WORDS:
            if word in haystack:
                attrs["color"] = word
                break
        for word in STYLE_WORDS:
            if word in haystack:
                attrs["style"] = word
                break
        if product.get("store"):
            attrs["brand"] = str(product["store"])
        categories = product.get("categories")
        if isinstance(categories, list) and categories:
            attrs["category"] = str(categories[-1])
        return attrs

    def _build_embeddings(self) -> None:
        """Embed every product once with the local model, cached to disk
        keyed by catalog file size/mtime + model name (see
        _catalog_cache_key) so the cache auto-invalidates if the catalog
        changes. This is the always-on fallback — no API key required."""
        cache_dir = self.catalog_path.parent / ".embedding_cache"
        cache_dir.mkdir(exist_ok=True)
        key = _catalog_cache_key(self.catalog_path, EMBEDDING_MODEL_NAME)
        ids_path = cache_dir / f"{key}_ids.json"
        vecs_path = cache_dir / f"{key}_vecs.npy"

        if ids_path.exists() and vecs_path.exists():
            self._embed_ids = json.loads(ids_path.read_text(encoding="utf-8"))
            self._embeddings = np.load(vecs_path)
            return

        model = self._get_model()
        self._embed_ids = list(self.products.keys())
        texts = [self._embedding_text(self.products[asin]) for asin in self._embed_ids]
        vectors = model.encode(texts, batch_size=256, show_progress_bar=True, normalize_embeddings=True)
        self._embeddings = np.asarray(vectors, dtype=np.float32)
        ids_path.write_text(json.dumps(self._embed_ids), encoding="utf-8")
        np.save(vecs_path, self._embeddings)

    def _get_openai_client(self):
        if self._openai_client is None:
            from openai import OpenAI  # lazy: avoid importing/requiring the package when no key is set
            self._openai_client = OpenAI(timeout=OPENAI_TIMEOUT_SECONDS)
        return self._openai_client

    def _build_openai_embeddings(self) -> None:
        """Optional upgrade path: if OPENAI_API_KEY is set, also embed the
        catalog with OpenAI and cache it separately from the local model's
        cache. Any failure here (bad key, no network, rate limit) is
        swallowed — self._openai_embeddings stays None and
        semantic_candidates() simply never tries the OpenAI path, using the
        always-available local embeddings instead. This must never raise:
        an agent crash counts as a miss for the whole session (see
        AGENTS.md), so a bad/expired key should degrade quietly, not break
        anything.
        """
        if not os.environ.get("OPENAI_API_KEY"):
            return
        try:
            cache_dir = self.catalog_path.parent / ".embedding_cache"
            cache_dir.mkdir(exist_ok=True)
            key = _catalog_cache_key(self.catalog_path, OPENAI_EMBEDDING_MODEL)
            ids_path = cache_dir / f"{key}_ids.json"
            vecs_path = cache_dir / f"{key}_vecs.npy"

            if ids_path.exists() and vecs_path.exists():
                self._openai_embed_ids = json.loads(ids_path.read_text(encoding="utf-8"))
                self._openai_embeddings = np.load(vecs_path)
                return

            client = self._get_openai_client()
            embed_ids = list(self.products.keys())
            texts = [self._embedding_text(self.products[asin]) for asin in embed_ids]
            vectors: list[list[float]] = []
            batch_size = 300
            for start in range(0, len(texts), batch_size):
                batch = texts[start:start + batch_size]
                response = client.embeddings.create(model=OPENAI_EMBEDDING_MODEL, input=batch)
                vectors.extend(item.embedding for item in response.data)

            embeddings = np.asarray(vectors, dtype=np.float32)
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            embeddings = embeddings / np.clip(norms, 1e-8, None)  # normalize so dot product == cosine similarity

            self._openai_embed_ids = embed_ids
            self._openai_embeddings = embeddings
            ids_path.write_text(json.dumps(embed_ids), encoding="utf-8")
            np.save(vecs_path, embeddings)
        except Exception:
            self._openai_embed_ids = []
            self._openai_embeddings = None

    def semantic_candidates(self, query: str, top_n: int = 50) -> list[tuple[str, float]]:
        """Dense embedding similarity search over the catalog. Tries OpenAI
        first (if its catalog embeddings were built successfully), and
        falls back to the always-available local model on any query-time
        failure — network drop, rate limit, timeout — so a mid-session API
        hiccup degrades gracefully instead of losing the whole session.
        """
        if not query.strip():
            return []

        if self._openai_embeddings is not None:
            try:
                client = self._get_openai_client()
                response = client.embeddings.create(model=OPENAI_EMBEDDING_MODEL, input=[query])
                query_vector = np.asarray(response.data[0].embedding, dtype=np.float32)
                query_vector = query_vector / max(np.linalg.norm(query_vector), 1e-8)
                scores = self._openai_embeddings @ query_vector
                top_indices = np.argsort(-scores)[:top_n]
                return [(self._openai_embed_ids[i], float(scores[i])) for i in top_indices]
            except Exception:
                pass  # fall through to the local model below

        if self._embeddings is None:
            return []
        model = self._get_model()
        query_vector = model.encode([query], normalize_embeddings=True)[0]
        scores = self._embeddings @ query_vector
        top_indices = np.argsort(-scores)[:top_n]
        return [(self._embed_ids[i], float(scores[i])) for i in top_indices]


def retrieve(
    index: RetrievalIndex,
    query: str,
    filled_slots: dict[str, str],
    track: str,
    top_n: int = 50,
) -> list[dict]:
    """Merge keyword + semantic candidates, apply the hard filter, and
    attach parsed attrs + score to each surviving candidate.

    `query` is expected to already be this turn's full search text — call
    with `state.durable_notes` (Person B's state.py: current message +
    running slot summary), not just the raw current-turn message. This
    closes the gap flagged in AGENTS.md where retrieval only ever saw the
    current turn in isolation; the context-folding itself now lives in
    state.py (`update_durable_notes`) rather than being duplicated here, so
    this function stays a plain (query string, slots dict) -> pool
    transformation with no dependency on the SessionState class shape.

    `filled_slots` is the flat {attribute: value} dict used for hard
    filtering — note some values may be "; "-joined multi-fact strings
    (e.g. "cotton; leather") per Person B's extract_slot_values; see
    filter_candidates below for how that's handled.

    Returns candidates as `{"parent_asin": str, "score": float, "attrs": dict}`,
    best-first, length <= top_n (the pool is always capped here — clarify.py's
    pool_is_too_broad threshold should be read relative to this cap, not the
    full 50k catalog). `attrs` is a handful of parsed product facts
    (material/color/style/brand/category) for clarify.py's entropy-based
    attribute selection and rank.py's slot-fit scoring.

    TODO (Person A): the 0.7/0.3 keyword/semantic weighting below is an
    untuned first guess — validate/tune it against the 200 dev sessions'
    scenario_metrics (Buying vs Browsing hit rate) once end-to-end runs are
    cheap to iterate.
    """
    keyword_hits = dict(index.keyword_candidates(query, top_n))
    semantic_hits = dict(index.semantic_candidates(query, top_n))

    keyword_weight, semantic_weight = (0.7, 0.3) if track == "buying" else (0.3, 0.7)
    merged: dict[str, float] = {}
    for asin, score in keyword_hits.items():
        merged[asin] = merged.get(asin, 0.0) + keyword_weight * score
    for asin, score in semantic_hits.items():
        merged[asin] = merged.get(asin, 0.0) + semantic_weight * score

    candidate_ids = index.filter_candidates(filled_slots, list(merged.keys()))
    ranked_ids = sorted(candidate_ids, key=lambda asin: -merged[asin])[:top_n]
    return [
        {
            "parent_asin": asin,
            "score": merged[asin],
            "attrs": index._extract_attrs(index.products[asin]),
        }
        for asin in ranked_ids
    ]
