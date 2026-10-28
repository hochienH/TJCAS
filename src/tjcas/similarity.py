"""Stage 3: pick the cited judgment among candidates by full-text similarity.

All three methods are character-based, so no Chinese word segmenter is needed;
they differ only in how much context a feature carries and how features are
weighted:

* ``bow``   — character unigram counts (a pure bag of words)
* ``ngram`` — character 2-4 gram counts, so phrasing survives
* ``tfidf`` — the same 2-4 grams, IDF-weighted against the candidate set

The vectorizer is fitted on the citing context plus its own candidates only.
IDF is therefore local to the decision being made, which is what we want: the
discriminative features are the ones that differ *between the candidates*.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import chain

import numpy as np
from sklearn.feature_extraction.text import (
    CountVectorizer,
    TfidfTransformer,
    TfidfVectorizer,
)
from sklearn.metrics.pairwise import cosine_similarity

METHODS: dict[str, Callable[[], CountVectorizer]] = {
    "bow": lambda: CountVectorizer(analyzer="char", ngram_range=(1, 1)),
    "ngram": lambda: CountVectorizer(analyzer="char", ngram_range=(2, 4), min_df=1),
    "tfidf": lambda: TfidfVectorizer(
        analyzer="char", ngram_range=(2, 4), sublinear_tf=True
    ),
}


@dataclass(frozen=True)
class Choice:
    method: str
    index: int
    score: float
    margin: float


def rank(context: str, candidate_texts: Sequence[str]) -> dict[str, Choice]:
    """Score ``context`` against each candidate with every method.

    This is the definition; ``CandidateSet`` computes the same thing faster
    when many contexts share one set of candidates.
    """
    if len(candidate_texts) < 2:
        raise ValueError("similarity ranking needs at least two candidates")
    choices: dict[str, Choice] = {}
    documents = [context, *candidate_texts]
    for name, factory in METHODS.items():
        try:
            matrix = factory().fit_transform(documents)
        except ValueError:  # empty vocabulary
            choices[name] = Choice(name, -1, 0.0, 0.0)
            continue
        choices[name] = _choose(name, matrix)
    return choices


def _choose(name: str, matrix) -> Choice:
    """The candidate row closest to the context row (row 0), and by how much."""
    scores = cosine_similarity(matrix[0], matrix[1:])[0]
    order = scores.argsort()[::-1]
    best = int(order[0])
    margin = float(scores[best] - scores[int(order[1])])
    return Choice(name, best, float(scores[best]), margin)


@dataclass(frozen=True)
class _Plan:
    name: str
    tokens: tuple  # what decides the n-grams; methods that agree on it share them
    dtype: type
    tfidf: dict | None  # TfidfTransformer parameters, None for raw counts


def _plan(name: str, vectorizer: CountVectorizer) -> _Plan:
    """What ``CandidateSet`` must reproduce of one method's vectorizer.

    It rebuilds only plain character n-gram counts, optionally tf-idf weighted;
    a method configured beyond that is refused here rather than scored wrongly.
    """
    params = vectorizer.get_params()
    plain = {
        "input": "content",
        "analyzer": "char",
        "preprocessor": None,
        "tokenizer": None,
        "binary": False,
        "max_df": 1.0,
        "min_df": 1,
        "max_features": None,
        "vocabulary": None,
    }
    for key, value in plain.items():
        if params[key] != value:
            raise NotImplementedError(f"CandidateSet cannot reproduce {name}: {key}={params[key]!r}")
    tokens = tuple(
        params[key] for key in ("ngram_range", "lowercase", "strip_accents", "encoding", "decode_error")
    )
    tfidf = None
    if isinstance(vectorizer, TfidfVectorizer):
        tfidf = {key: params[key] for key in ("norm", "use_idf", "smooth_idf", "sublinear_tf")}
    return _Plan(name, tokens, vectorizer.dtype, tfidf)


_PLANS = [_plan(name, factory()) for name, factory in METHODS.items()]
_ANALYZERS = {}
for _name, _factory in METHODS.items():
    _ANALYZERS.setdefault(next(p.tokens for p in _PLANS if p.name == _name), _factory().build_analyzer())
# the sparse class CountVectorizer hands back under the current configuration
_SPARSE = type(CountVectorizer(analyzer="char").fit_transform(["ab"]))


class _Tokenized:
    """The candidates under one tokenization, kept in the shape CountVectorizer
    would give them once a context is put in front.

    CountVectorizer numbers terms in order of first appearance across the
    documents — the context first, then each candidate — sorts every row by that
    number, and only then relabels the columns alphabetically, without sorting
    again. So a row is stored in first-appearance order under alphabetical
    labels, and that order is not cosmetic: row norms and dot products are summed
    in it, which fixes the last bits of every score. A candidate row therefore
    lists the terms it shares with the context, in the context's order, then its
    own terms in the order the candidates first used them.
    """

    def __init__(self, analyze: Callable[[str], list[str]], texts: Sequence[str]) -> None:
        self.analyze = analyze
        counts = [Counter(analyze(text)) for text in texts]
        first_seen = dict.fromkeys(chain.from_iterable(counts))
        self.index = dict(zip(first_seen, range(len(first_seen))))
        terms = list(first_seen)
        alphabetical = sorted(range(len(terms)), key=terms.__getitem__)
        self.sorted_terms = [terms[i] for i in alphabetical]
        self.rank_of = np.empty(len(terms), dtype=np.int64)
        self.rank_of[alphabetical] = np.arange(len(terms))
        self.rows: list[tuple[np.ndarray, np.ndarray]] = []
        for counter in counts:
            ids = np.fromiter(map(self.index.__getitem__, counter), np.int64, len(counter))
            values = np.fromiter(counter.values(), np.int64, len(counter))
            order = np.argsort(ids)
            self.rows.append((ids[order], values[order]))

    def matrix(self, context: str):
        """The arrays of the count matrix of ``[context, *candidates]`` as
        ``(data, indices, indptr, shape)``, or None if it has no columns."""
        counter = Counter(self.analyze(context))
        terms = list(counter)
        ids = np.fromiter((self.index.get(term, -1) for term in terms), np.int64, len(terms))
        known = ids >= 0
        new_terms = sorted(term for term, known_term in zip(terms, known) if not known_term)
        width = len(self.sorted_terms) + len(new_terms)
        if width == 0:
            return None
        # a new term is never equal to a candidate term, so each lands strictly
        # between two of them, and every candidate term shifts right by the
        # number of new terms sorting before it
        slots = np.fromiter(
            (bisect_left(self.sorted_terms, term) for term in new_terms), np.int64, len(new_terms)
        )
        old = np.arange(len(self.sorted_terms))
        column_of_id = (old + np.searchsorted(slots, old, side="right"))[self.rank_of]
        new_column = dict(zip(new_terms, (slots + np.arange(len(new_terms))).tolist()))

        context_columns = np.empty(len(terms), dtype=np.int64)
        context_columns[known] = column_of_id[ids[known]]
        context_columns[~known] = [new_column[term] for term, k in zip(terms, known) if not k]
        position = np.full(len(self.sorted_terms), -1, dtype=np.int64)
        position[ids[known]] = np.nonzero(known)[0]

        columns = [context_columns]
        values = [np.fromiter(counter.values(), np.int64, len(counter))]
        for row_ids, row_values in self.rows:
            where = position[row_ids]
            shared = np.nonzero(where >= 0)[0]
            take = np.concatenate([shared[np.argsort(where[shared])], np.nonzero(where < 0)[0]])
            columns.append(column_of_id[row_ids[take]])
            values.append(row_values[take])
        indptr = np.zeros(len(columns) + 1, dtype=np.int32)
        np.cumsum([len(c) for c in columns], out=indptr[1:])
        return (
            np.concatenate(values),
            np.concatenate(columns).astype(np.int32),
            indptr,
            (len(columns), width),
        )


class CandidateSet:
    """The candidate side of ``rank``, tokenized once for every context.

    ``CandidateSet(texts).rank(context)`` equals ``rank(context, texts)`` down to
    the last bit of every score and margin. ``rank`` refits each vectorizer on
    ``[context, *texts]``, cutting every candidate judgment into n-grams again
    for each context; the judgments dwarf the contexts and a key is cited nine
    times on average, so almost all of that work repeats. Here the candidates
    are cut once, the matrix CountVectorizer would build is assembled from them
    and each context, and the tf-idf weighting and cosine scoring that follow
    are scikit-learn's own.
    """

    def __init__(self, candidate_texts: Sequence[str]) -> None:
        if len(candidate_texts) < 2:
            raise ValueError("similarity ranking needs at least two candidates")
        self._tokenized = {
            tokens: _Tokenized(analyze, candidate_texts) for tokens, analyze in _ANALYZERS.items()
        }

    def rank(self, context: str) -> dict[str, Choice]:
        counts = {tokens: t.matrix(context) for tokens, t in self._tokenized.items()}
        choices: dict[str, Choice] = {}
        for plan in _PLANS:
            arrays = counts[plan.tokens]
            if arrays is None:  # empty vocabulary, where rank() catches a ValueError
                choices[plan.name] = Choice(plan.name, -1, 0.0, 0.0)
                continue
            data, indices, indptr, shape = arrays
            # Built in the method's own dtype, never cast afterwards: scipy's
            # astype sorts every row first, and a float matrix is scored in the
            # order it was built in, so a cast would move the last bits of tf-idf.
            # Fresh index arrays per method, since scoring may sort them in place.
            matrix = _SPARSE(
                (data.astype(plan.dtype), indices.copy(), indptr.copy()), shape=shape
            )
            if plan.tfidf is not None:
                # as TfidfVectorizer.fit_transform: fit, then transform in place
                transformer = TfidfTransformer(**plan.tfidf)
                transformer.fit(matrix)
                matrix = transformer.transform(matrix, copy=False)
            choices[plan.name] = _choose(plan.name, matrix)
        return choices


def majority(decision: Mapping[str, object]) -> tuple[object | None, str]:
    """The pick more than half the methods made, and how it was reached.

    Returns ``(pick, "unanimous")`` when every method landed on it,
    ``(pick, "majority")`` when it merely cleared half of them, and
    ``(None, "none")`` when nothing did — the same threshold ``agreement``
    counts as ``no_majority``.
    """
    names = sorted(METHODS)
    picks: Counter[object] = Counter(decision[name] for name in names)
    winner, count = picks.most_common(1)[0]
    if count * 2 <= len(names):
        return None, "none"
    return winner, "unanimous" if len(picks) == 1 else "majority"


def agreement(decisions: Sequence[Mapping[str, object]]) -> dict[str, float]:
    """Pairwise, unanimous and no-majority top-1 rates across the three methods.

    Each decision maps a method name to whatever identifies its pick (an index,
    a filename, ...); only equality between methods matters.

    ``no_majority`` is the share of decisions no candidate wins: the most
    popular pick is chosen by at most half the methods, so three methods
    splitting 1-1-1 count but 2-1 does not. Written as a share of the method
    count rather than hard-coded to three, so adding a method to ``METHODS``
    keeps the definition.
    """
    names = sorted(METHODS)
    pairs = [(a, b) for i, a in enumerate(names) for b in names[i + 1 :]]
    hits: Counter[str] = Counter()
    for decision in decisions:
        for left, right in pairs:
            if decision[left] == decision[right]:
                hits[f"{left}~{right}"] += 1
        picks: Counter[object] = Counter(decision[name] for name in names)
        if len(picks) == 1:
            hits["unanimous"] += 1
        if max(picks.values()) * 2 <= len(names):
            hits["no_majority"] += 1
    total = len(decisions) or 1
    labels = [f"{a}~{b}" for a, b in pairs] + ["unanimous", "no_majority"]
    return {label: hits[label] / total for label in labels}
