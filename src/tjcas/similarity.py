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

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from sklearn.feature_extraction.text import CountVectorizer, TfidfVectorizer
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
    """Score ``context`` against each candidate with every method."""
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
        scores = cosine_similarity(matrix[0], matrix[1:])[0]
        order = scores.argsort()[::-1]
        best = int(order[0])
        margin = float(scores[best] - scores[int(order[1])])
        choices[name] = Choice(name, best, float(scores[best]), margin)
    return choices


def agreement(decisions: Sequence[Mapping[str, object]]) -> dict[str, float]:
    """Pairwise and unanimous top-1 agreement rates across the three methods.

    Each decision maps a method name to whatever identifies its pick (an index,
    a filename, ...); only equality between methods matters.
    """
    names = sorted(METHODS)
    pairs = [(a, b) for i, a in enumerate(names) for b in names[i + 1 :]]
    hits: Counter[str] = Counter()
    for decision in decisions:
        for left, right in pairs:
            if decision[left] == decision[right]:
                hits[f"{left}~{right}"] += 1
        if len({decision[name] for name in names}) == 1:
            hits["unanimous"] += 1
    total = len(decisions) or 1
    labels = [f"{a}~{b}" for a, b in pairs] + ["unanimous"]
    return {label: hits[label] / total for label in labels}
