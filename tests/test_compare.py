"""The tool comparison's plumbing (benchmark/compare), without any model."""
import json

from worth_your_read import answer
from worth_your_read.benchmark.compare import common, judge_pairs, manual, run_paperqa, score_compare


def test_sentences_split_without_breaking_on_et_al_or_decimals():
    text = ("Iron limits growth in HNLC regions (pqac-abcd1234). Martin et al. found a 2.5-fold rise [1, 2]. "
            "See Fig. 2 for details.\n- A bullet point\n## Heading")
    assert common.split_sentences(text) == [
        "Iron limits growth in HNLC regions (pqac-abcd1234).", "Martin et al. found a 2.5-fold rise [1, 2].",
        "See Fig. 2 for details.", "A bullet point", "Heading"]


def test_a_passage_is_found_in_its_paper_despite_reflowing():
    paper = "Results. Iron addition increased the photosynthetic effi-\nciency (Fv/Fm) by 65% at station HNL."
    assert common.in_paper("iron addition increased the photosynthetic efficiency (Fv/Fm) by 65%", paper)
    assert not common.in_paper("iron addition increased chlorophyll by 65%", paper)


def test_paperqa_citations_map_to_their_chunks():
    raw = ("Iron limits growth (pqac-aaaaaaaa, pqac-bbbbbbbb). Nitrogen limits the gyres (pqac-zzzzzzzz). "
           "No citation here.")
    ctx = [{"id": "pqac-aaaaaaaa", "n": 2, "text": "chunk A", "summary": "s"},
           {"id": "pqac-bbbbbbbb", "n": 5, "text": "chunk B", "summary": "s"}]
    out = run_paperqa.pqa_to_shared("q", "Q?", raw, ctx, {}, 12.0)
    assert [s["text"] for s in out["sentences"]] == ["Iron limits growth.", "Nitrogen limits the gyres.",
                                                     "No citation here."]
    assert [s["paper"] for s in out["sentences"][0]["sources"]] == [2, 5]
    assert out["sentences"][1]["sources"] == [] and out["unknown_citations"] == 1


def test_a_filled_manual_template_parses():
    md = ("<!-- tool: notebooklm -->\n<!-- seconds: 40 -->\n# Q?\n\n## Answer\n\n"
          "Iron limits growth [1][2]. Gyres are N-limited [3-4]. Uncited.\n\n## Sources\n\n"
          "[1] P2 | passage one\n[2] P5 | passage two\n[3] P1 | three\n")
    out = manual.parse_md(md, "notebooklm", "q", "Q?")
    assert out["seconds"] == 40 and out["unknown_citations"] == 1
    assert out["sentences"][0] == {"text": "Iron limits growth.", "sources": [
        {"paper": 2, "passage": "passage one"}, {"paper": 5, "passage": "passage two"}]}
    assert [s["paper"] for s in out["sentences"][1]["sources"]] == [1]
    assert out["sentences"][2]["sources"] == []


def test_judge_reads_labels_and_ignores_junk(monkeypatch):
    monkeypatch.setattr(answer, "_complete", lambda *a, **k: json.dumps({
        "passages": [{"id": "S2", "label": "partly"}, {"id": "S1", "label": "Backs"}, {"id": "S9", "label": "no"},
                     {"id": "S3", "label": "maybe"}], "together": "Yes", "unsupported": ""}))
    j = judge_pairs.judge_sentence("A sentence.", ["p1", "p2", "p3"])
    assert j == {"labels": ["backs", "partly", None], "together": "yes", "unsupported": ""}


def test_scores_count_uncited_wrong_and_made_up_citations(monkeypatch):
    monkeypatch.setattr(score_compare.common, "paper_text", lambda qid, n: {1: "iron limits growth in hnlc regions"}.get(n, ""))
    monkeypatch.setattr(score_compare.common, "manifest", lambda qid: {"papers": [{}, {}, {}]})
    ans = {"question_id": "q", "seconds": 120, "unknown_citations": 1, "sentences": [
        {"text": "A.", "sources": [{"paper": 1, "passage": "Iron limits growth in HNLC regions"},
                                   {"paper": 2, "passage": "not in paper two"}]},
        {"text": "B.", "sources": []}]}
    judged = {"sentences": [{"text": "A.", "labels": ["backs", "no"], "together": "yes"},
                            {"text": "B.", "labels": [], "together": "no"}]}
    t = score_compare.tally(ans, judged)
    assert (t["sentences"], t["uncited"], t["citations"], t["good"], t["wrong"], t["supported"]) == (2, 1, 2, 1, 1, 1)
    assert t["not in paper"] == 2 and t["papers cited"] == 2
    assert score_compare.row(t)[3] == "50%" and score_compare.row(t)[5] == "50%"
