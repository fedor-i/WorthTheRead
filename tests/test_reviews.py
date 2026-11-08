"""The review benchmark's plumbing (benchmark/reviews), without any model."""
import json

import pytest

from worth_your_read import answer
from worth_your_read.benchmark.reviews import run


def _work(title, year=2019, doi="10.1/x", abstract=""):
    return {"title": title, "year": year, "doi": doi, "abstract": abstract}


def test_a_review_never_supplies_itself_or_anything_newer():
    review = {"title": "Iron: a review", "year": 2020, "question": "How does iron limit growth?"}
    works = [_work("Iron: A Review!"), _work("Iron limits growth", 2021), _work("No DOI", doi=None),
             _work("Iron limits growth in the sea"), _work("Iron limits growth in the sea"),
             _work("Unrelated topic"), _work("How iron limits growth", 2020)]
    assert [w["title"] for w in run.cited_papers(review, works)] == [
        "How iron limits growth", "Iron limits growth in the sea", "Unrelated topic"]


def test_coverage_labels_are_read_and_junk_is_none(monkeypatch):
    monkeypatch.setattr(answer, "_complete", lambda *a, **k: json.dumps(
        {"passages": "Yes", "findings": "partly", "evidence": "maybe", "short": "no"}))
    got = run.cover({"text": "S.", "kind": "finding"}, {"findings": "a", "evidence": "b", "short": "c"}, ["p"])
    assert got == {"reachable": "yes", "findings": "partly", "evidence": None, "short": "no"}


def test_levels_separate_the_short_answer_evidence_and_further_findings():
    ans = {"overview": [{"claim": "Short."}], "claims": [{"claim": "Main.", "extra": False},
                                                         {"claim": "Further.", "extra": True}]}
    assert run.levels(ans) == {"short": "Short.", "evidence": "Main.", "findings": "Main.\nFurther."}


def test_tally_counts_only_statements_in_the_papers_read():
    row = lambda kind, reach, f, e, s: {"kind": kind, "reachable": reach, "findings": f, "evidence": e, "short": s}
    t = run.tally([row("finding", "yes", "yes", "yes", "yes"), row("finding", "partly", "yes", "no", "partly"),
                   row("disagreement", "yes", "yes", "partly", "no"), row("finding", "no", "no", "no", "no")])
    assert (t["statements"], t["reachable"], t["conflicts"], t["conflicts_short"]) == (4, 3, 1, 0)
    assert round(t["findings"], 2) == 1.0 and round(t["evidence"], 2) == 0.33 and round(t["short"], 2) == 0.33
    assert round(t["short_or_partly"], 2) == 0.67


def test_an_unlabelled_statement_stops_the_table_rather_than_leaving_the_score():
    with pytest.raises(SystemExit):
        run.tally([{"kind": "finding", "reachable": None, "findings": None, "evidence": None, "short": None}])


def test_the_test_reviews_stay_apart_from_the_tuning_ones():
    assert {r["id"] for r in run.reviews(split="tune")} == {"ocp", "ferroptosis"}
    assert len(run.reviews(split="test")) == 3 and not {r["id"] for r in run.reviews(split="tune")} & {r["id"] for r in run.reviews(split="test")}
