"""Semantic closeness of texts to a query: TF-IDF + latent semantic analysis
(TruncatedSVD). Classical, local, deterministic — no model calls. Unlike plain
word overlap it relates papers that describe the same thing in different words
("ageing", "all-cause mortality", "live longer" vs "lifespan").

Ported from OpenBioNews (openbionews/mlrank.py, MIT). scikit-learn is optional
(the `sidecar` extra); `available()` says whether it's installed, and callers
fall back to word-overlap scoring when it isn't.
"""


def available() -> bool:
    try:
        import sklearn  # noqa: F401
        return True
    except ImportError:
        return False


def semantic_scores(query: str, docs: list) -> list:
    """Cosine similarity (0-1) of each doc to `query` in LSA space. The SVD is
    pinned (random_state=0); on a corpus too small to project it falls back to
    raw TF-IDF cosine."""
    if not docs:
        return []
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    matrix = TfidfVectorizer(stop_words="english", max_features=4096).fit_transform(
        [query] + list(docs))
    n_components = min(128, matrix.shape[1] - 1, matrix.shape[0] - 1)
    if n_components >= 2:
        reduced = TruncatedSVD(n_components=n_components, random_state=0).fit_transform(matrix)
        sims = cosine_similarity(reduced[0:1], reduced[1:])[0]
    else:
        sims = cosine_similarity(matrix[0:1], matrix[1:])[0]
    return [max(0.0, min(1.0, float(s))) for s in sims]
