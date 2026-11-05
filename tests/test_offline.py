"""Offline tests for the guard, focus, local-library and sidecar plumbing.
No model and no network: run with `python -m pytest worth_your_read/tests`."""
import json
import re

import pytest

from worth_your_read import answer, fetch, focus, judge, library, store

QUOTE = ("for most study designs and settings, it is more likely for a research "
         "claim to be false than true")
PAGES = ["Essay\nWhy Most Published Research Findings Are False\nSummary " + QUOTE + ".",
         "Results\nThe probability that a finding is true depends on power and bias.",
         "Corollary 1: The smaller the studies conducted, the less likely the "
         "research findings are to be true."]


def _fetched():
    out = {"sections": {"abstract": PAGES[0], "results": PAGES[1] + " " + PAGES[2]},
           "source": "zotero_fulltext", "open_access": False}
    return fetch.assemble(out)


def _supported(quote=QUOTE):
    return {"verdict": "SUPPORTED", "confidence": 1.0,
            "aspects": [{"type": "outcome", "text": "findings false",
                         "status": "ENTAILED", "quote": quote}],
            "evidence": [{"quote": quote, "stance": "ENTAILS"}]}


# --- scope backstop -------------------------------------------------------------

def test_universal_claim_backed_by_most_is_overstated():
    r = judge.finalize(_supported(), _fetched(),
                       "Most findings are false, regardless of study design or field.")
    assert r["verdict"] == "OVERSTATED"
    assert r["confidence"] <= 0.6
    assert any(a["type"] == "scope" and a["status"] == "ABSENT" for a in r["aspects"])


def test_faithful_scope_stays_supported():
    r = judge.finalize(_supported(), _fetched(),
                       "For most study designs, a claim is more likely false than true.")
    assert r["verdict"] == "SUPPORTED"


def test_scope_backstop_ignores_all_cause():
    assert judge._UNIVERSAL_CLAIM_RE.search("Statins reduce all-cause mortality") is None


def test_invented_scope_on_plain_claim_is_ignored():
    data = _supported()
    data["aspects"].append({"type": "scope", "text": "essential", "status": "ABSENT", "quote": ""})
    r = judge.finalize(data, _fetched(), "For most study designs, claims are likely false.")
    assert r["verdict"] == "SUPPORTED"


def test_model_scope_contradiction_is_kept():
    data = _supported()
    data["aspects"].append({"type": "scope", "text": "regardless of design",
                            "status": "CONTRADICTED", "quote": QUOTE})
    r = judge.finalize(data, _fetched(), "Findings are false regardless of design.")
    assert r["verdict"] == "OVERSTATED"


def test_fabricated_quote_still_insufficient():
    r = judge.finalize(_supported("a sentence that is nowhere in this paper at all"),
                       _fetched(), "Most findings are false.")
    assert r["verdict"] == "INSUFFICIENT"


# --- revise unwrapping --------------------------------------------------------------

@pytest.mark.parametrize("raw", ['{"rewritten_claim": "X holds for most designs."}',
                                 '```\nX holds for most designs.\n```',
                                 '"X holds for most designs."'])
def test_clean_revision(raw):
    assert judge._clean_revision(raw) == "X holds for most designs."


# --- focus ---------------------------------------------------------------------------

def test_focus_keeps_relevant_passage_and_fits_budget():
    filler = " ".join(f"Unrelated sentence number {i} about methods." for i in range(400))
    text = (f"## ABSTRACT\nShort abstract.\n## RESULTS\n{filler} "
            f"Corollary 1: The smaller the studies, the less likely findings are true. {filler}")
    out = focus.focus_text("smaller studies less likely true", text, 3000)
    assert len(out) <= 3200
    assert "Corollary 1" in out and "Short abstract." in out


def test_focus_returns_short_text_unchanged():
    assert focus.focus_text("q", "## ABSTRACT\nshort", 12000) == "## ABSTRACT\nshort"


# --- local library ------------------------------------------------------------------

def test_library_reads_zotero_cache_and_locates_pages(tmp_path, monkeypatch):
    key = "ABCD1234"
    d = tmp_path / "storage" / key
    d.mkdir(parents=True)
    (d / library.FT_CACHE).write_text("\f".join(PAGES), encoding="utf-8")
    monkeypatch.setenv("WYR_ZOTERO_DIR", str(tmp_path))
    f = library.load(attachment_key=key)
    assert f["source"] == "zotero_fulltext" and len(f["pages"]) == 3
    assert library.page_of(QUOTE, f["pages"]) == 1
    assert library.page_of("the less likely the research findings are to be true", f["pages"]) == 3
    assert library.page_of("never written anywhere in these pages", f["pages"]) is None


# --- grounded answers ------------------------------------------------------------------

def test_answer_marks_ungrounded_claims(monkeypatch):
    raw = json.dumps({"claims": [
        {"claim": "Most claims are likely false for most designs.", "quotes": [QUOTE]},
        {"claim": "The author recommends banning p-values.", "quotes": ["p-values must be banned outright"]},
    ], "not_addressed": ["funding"]})
    monkeypatch.setattr(answer, "_complete", lambda s, u: raw)
    r = answer.ask("What does it argue?", _fetched())
    assert [c["status"] for c in r["claims"]] == ["GROUNDED", "UNGROUNDED"]
    assert r["grounded"] == 1 and r["ungrounded"] == 1
    assert 0 < r["confidence"] <= 0.7
    html = answer.note_html(r, "Ioannidis 2005")
    assert "Not verified against the text" in html and "banning" in html


def _paper(label, text, full=True):
    return {"label": label, "fetched": {"full_text": text, "has_fulltext": full}}


_A_TEXT = "Metformin lowered all-cause mortality by 12% in the treated arm. Adverse events were mild."
_B_TEXT = "Among older adults, metformin users lived longer than matched controls."


def _fake_model(merge_reply):
    """Per-paper answers keyed on the paper text; the merge returns `merge_reply`."""
    def complete(system, user, max_tokens=1500):
        if system == answer._MERGE_SYSTEM:
            return merge_reply
        if "lowered all-cause mortality" in user:
            return json.dumps({"claims": [
                {"claim": "Mortality fell by 12%.", "quotes": ["Metformin lowered all-cause mortality by 12%"]},
                {"claim": "It doubled lifespan.", "quotes": ["metformin doubled lifespan"]}]})
        return json.dumps({"claims": [
            {"claim": "Users lived longer.", "quotes": ["metformin users lived longer than matched controls"]}]})
    return complete


def test_ask_many_asks_each_paper_then_merges_by_claim_id(monkeypatch):
    merge = json.dumps({"claims": [
        {"claim": "Metformin was linked to lower mortality and longer life [P1, P2].", "from": ["C1", "C2"]},
        {"claim": "It cures ageing.", "from": ["C9"]}],             # points at nothing
        "not_addressed": ["cost"]})
    monkeypatch.setattr(answer, "_complete", _fake_model(merge))
    seen = []
    r = answer.ask_many("Does metformin extend life?",
                        [_paper("Trial A", _A_TEXT), _paper("Cohort B", _B_TEXT), _paper("Empty", "")],
                        progress=seen.append)
    first = r["claims"][0]
    assert first["status"] == "GROUNDED" and {s["paper"] for s in first["sources"]} == {1, 2}
    assert first["claim"] == "Metformin was linked to lower mortality and longer life."
    assert r["claims"][1]["status"] == "UNGROUNDED"
    # the fabricated per-paper quote ("doubled lifespan") never reaches the answer
    assert "doubled" not in json.dumps(r["claims"])
    assert r["merged"] and r["skipped"] == ["Empty"] and all(p["used"] for p in r["papers"])
    assert [p["stage"] for p in seen] == ["draft", "paper", "paper", "merge", "check", "wording"] and r["not_addressed"] == ["cost"]
    assert 0 < r["confidence"] <= 0.7


def test_ask_many_keeps_findings_the_merge_drops_or_garbles(monkeypatch):
    # The merge uses only C1 and is cut off mid-way: C2 must still be in the answer.
    cut = '{"claims": [{"claim": "Mortality fell.", "from": ["C1"]}, {"claim": "Users li'
    monkeypatch.setattr(answer, "_complete", _fake_model(cut))
    r = answer.ask_many("Does metformin extend life?", [_paper("Trial A", _A_TEXT), _paper("Cohort B", _B_TEXT)])
    assert r["truncated"] and [c["claim"] for c in r["claims"]] == ["Mortality fell.", "Users lived longer."]
    assert [c["extra"] for c in r["claims"]] == [False, True]     # the dropped one is marked as extra
    # Unreadable merge output: the per-paper findings are the answer.
    monkeypatch.setattr(answer, "_complete", _fake_model("not json at all"))
    r = answer.ask_many("Does metformin extend life?", [_paper("Trial A", _A_TEXT), _paper("Cohort B", _B_TEXT)])
    assert not r["merged"] and len(r["claims"]) == 2 and r["grounded"] == 2


def test_ask_many_caps_papers_and_reports_the_rest(monkeypatch):
    monkeypatch.setattr(answer, "_complete", lambda s, u, max_tokens=1500: "{}")
    papers = [_paper(f"P{i}", "metformin lifespan " * (i + 1)) for i in range(answer.MAX_PAPERS + 3)]
    r = answer.ask_many("metformin lifespan", papers, max_papers=answer.MAX_PAPERS)
    assert len(r["papers"]) == answer.MAX_PAPERS and len(r["skipped"]) == 3
    assert [x["reason"] for x in r["left_out"]] == ["limit"] * 3
    # The limit is the caller's to set; unreadable papers are reported apart.
    r = answer.ask_many("metformin lifespan", papers + [_paper("Empty", "")], max_papers=4)
    assert len(r["papers"]) == 4
    assert sorted(x["reason"] for x in r["left_out"]) == ["limit"] * (len(papers) - 4) + ["no_text"]


def test_ask_many_merges_many_findings_in_parts(monkeypatch):
    # 30 papers x 1 finding, batches of 8: no merge call ever sees more than 8
    # claims, every paper is still read, and the final claim rests on papers
    # from different parts.
    monkeypatch.setattr(answer, "MERGE_BATCH", 8)
    sizes, checks_with_parts = [], []
    def complete(system, user, max_tokens=1500):
        if system == answer._PART_SYSTEM:
            checks_with_parts.append(user)
        if system != answer._MERGE_SYSTEM:
            return json.dumps({"claims": [{"claim": "Users lived longer.",
                                           "quotes": ["metformin users lived longer than matched controls"]}]})
        n = len(re.findall(r"\nC\d+ ", user))
        sizes.append(n)
        # merge the first two claims of whatever it is given
        return json.dumps({"claims": [{"claim": "Merged.", "from": ["C1", "C2"]}]})
    monkeypatch.setattr(answer, "_complete", complete)
    papers = [_paper(f"P{i}", _B_TEXT) for i in range(30)]
    r = answer.ask_many("Did users live longer?", papers, method="merge_whole")
    assert len(r["papers"]) == 30 and not r["skipped"] and max(sizes) <= 8
    assert not checks_with_parts          # merging in parts must not switch the check variant
    main = [c for c in r["claims"] if not c["extra"]]
    assert len(main) == 1 and len({s["paper"] for s in main[0]["sources"]}) == 4
    # nothing is lost: every paper's finding is in the answer, merged or as a further finding
    assert {s["paper"] for c in r["claims"] for s in c["sources"]} == set(range(1, 31))


def test_ask_many_prunes_findings_that_do_not_back_the_merged_claim(monkeypatch):
    # The merge lumps both findings under one sentence; the check keeps only C1.
    merge = json.dumps({"claims": [{"claim": "Mortality fell by 12%.", "from": ["C1", "C2"]},
                                   {"claim": "Something neither says.", "from": ["C1", "C2"]}]})
    base = _fake_model(merge)
    asked = []
    def complete(system, user, max_tokens=1500):
        if system == answer._SUPPORT_SYSTEM:
            asked.append(user)
            return json.dumps({"support": ["C1"] if "12%" in user.split("FINDINGS")[0] else []})
        return base(system, user, max_tokens)
    monkeypatch.setattr(answer, "_complete", complete)
    seen = []
    r = answer.ask_many("Does metformin extend life?", [_paper("Trial A", _A_TEXT), _paper("Cohort B", _B_TEXT)],
                        progress=seen.append, method="merge_whole")
    first, second = r["claims"][0], r["claims"][1]
    assert {s["paper"] for s in first["sources"]} == {1}
    assert second["status"] == "UNGROUNDED" and not second["sources"]
    # Cohort B's finding, pruned from both, is still in the answer as a further finding
    assert [c["claim"] for c in r["claims"] if c["extra"]] == ["Users lived longer."]
    assert len(asked) == 2 and [p["stage"] for p in seen].count("check") == 2
    # A check that doesn't answer leaves the merge's ids as they were.
    monkeypatch.setattr(answer, "_complete", lambda s, u, max_tokens=1500:
                        "garbled" if s == answer._SUPPORT_SYSTEM else base(s, u, max_tokens))
    r = answer.ask_many("Does metformin extend life?", [_paper("Trial A", _A_TEXT), _paper("Cohort B", _B_TEXT)],
                        method="merge_whole")
    assert {s["paper"] for s in r["claims"][0]["sources"]} == {1, 2}


def test_ask_many_grouped_cites_exactly_each_group(monkeypatch):
    base = _fake_model("{}")
    def complete(system, user, max_tokens=1500):
        if system == answer._GROUP_SYSTEM:      # C1 = trial mortality, C2 = cohort; C9 is junk
            return json.dumps({"groups": [{"ids": ["C1", "C9"]}, {"ids": ["C1"]}]})
        if system == answer._WRITE_SYSTEM:
            return json.dumps({"claims": [{"group": "G1", "claim": "Mortality fell by 12% [P1]."},
                                          {"group": "G1", "claim": "Written twice."}]})
        return base(system, user, max_tokens)
    monkeypatch.setattr(answer, "_complete", complete)
    r = answer.ask_many("Does metformin extend life?", [_paper("Trial A", _A_TEXT), _paper("Cohort B", _B_TEXT)],
                        method="grouped")
    main = [c for c in r["claims"] if not c["extra"]]
    assert [c["claim"] for c in main] == ["Mortality fell by 12%."]
    assert {s["paper"] for s in main[0]["sources"]} == {1}
    # C2, left out of every group by the model, became its own group; unwritten, it stays a further finding
    assert [c["claim"] for c in r["claims"] if c["extra"]] == ["Users lived longer."]


def test_ask_many_parts_keeps_a_finding_that_backs_half_the_sentence(monkeypatch):
    merge = json.dumps({"claims": [{"claim": "Mortality fell by 12% and users lived longer.", "from": ["C1", "C2"]}]})
    base = _fake_model(merge)
    def complete(system, user, max_tokens=1500):
        if system == answer._PART_SYSTEM:
            return json.dumps({"findings": [{"id": "C1", "backs": "part", "part": "Mortality fell by 12%"},
                                            {"id": "C2", "backs": "part", "part": "words not in the sentence"}]})
        if system == answer._SUPPORT_SYSTEM:
            return json.dumps({"support": []})     # the whole-sentence check keeps neither
        return base(system, user, max_tokens)
    monkeypatch.setattr(answer, "_complete", complete)
    papers = [_paper("Trial A", _A_TEXT), _paper("Cohort B", _B_TEXT)]
    strict = answer.ask_many("Does metformin extend life?", papers, method="merge_whole")
    assert strict["claims"][0]["status"] == "UNGROUNDED"
    r = answer.ask_many("Does metformin extend life?", papers)            # the part check is the default
    first = r["claims"][0]
    assert first["status"] == "GROUNDED" and {s["paper"] for s in first["sources"]} == {1, 2}
    # a part the model made up isn't placed; the source is still cited (at the end)
    assert {s["paper"]: s["part"] for s in first["sources"]} == {1: "Mortality fell by 12%", 2: None}


def test_an_over_long_merge_is_merged_once_more(monkeypatch):
    monkeypatch.setattr(answer, "MAX_ANSWER_CLAIMS", 1)
    merges = []
    def complete(system, user, max_tokens=1500):
        if system == answer._MERGE_SYSTEM:
            merges.append(user)
            if len(merges) == 1:      # ignores the limit: one sentence per finding
                return json.dumps({"claims": [{"claim": "Mortality fell.", "from": ["C1"]},
                                              {"claim": "Users lived longer.", "from": ["C2"]}]})
            return json.dumps({"claims": [{"claim": "Metformin users lived longer.", "from": ["C1", "C2"]}]})
        return _fake_model("{}")(system, user, max_tokens)
    monkeypatch.setattr(answer, "_complete", complete)
    r = answer.ask_many("Does metformin extend life?", [_paper("Trial A", _A_TEXT), _paper("Cohort B", _B_TEXT)])
    main = [c for c in r["claims"] if not c["extra"]]
    assert len(merges) == 2 and [c["claim"] for c in main] == ["Metformin users lived longer."]
    assert {s["paper"] for s in main[0]["sources"]} == {1, 2}


def _found_items(papers):
    found = [(n, f"finding {i}", ["q"]) for i, n in enumerate(papers)]
    return found, [(t, {i}) for i, (_, t, _) in enumerate(found)]


def test_a_merge_reference_to_a_paper_is_not_read_as_an_item_number():
    found, items = _found_items([1, 2, 3, 23, 23])
    assert answer._refs(["C2"], items, found) == {1}
    assert answer._refs(["P23"], items, found) == {3, 4}     # not out of range, so not lost
    assert answer._refs(["P3"], items, found) == {2}         # paper 3, which is the third item only by chance
    assert answer._refs(["P1", "C5"], items, found) == {0, 4}
    assert answer._refs(["C9"], items, found) == set() and answer._refs(None, items, found) == set()


def test_a_bad_merge_reply_does_not_lose_the_findings_it_skipped():
    found, items = _found_items([1, 2, 3])
    assert answer._survivors([("All.", {0})], {}, items) == [("All.", {0})]      # a clean reply may drop some
    cut_off = answer._survivors([("All.", {0})], {"truncated": True}, items)
    assert cut_off == [("All.", {0}), items[1], items[2]]
    no_refs = answer._survivors([("All.", {0}), ("Nothing cited.", set())], {}, items)
    assert no_refs == [("All.", {0}), items[1], items[2]]
    assert answer._survivors([], {}, items) == items


def test_later_papers_reach_the_answer_when_a_merge_names_papers(monkeypatch):
    monkeypatch.setattr(answer, "MERGE_BATCH", 2)
    found = [(n, f"finding of paper {n}", ["q"]) for n in range(1, 9)]
    chosen = [{"label": f"Paper {n}"} for n in range(1, 9)]
    def complete(system, user, max_tokens=1500):
        papers = re.findall(r"\[(P\d+)\] Paper", user)
        return json.dumps({"claims": [{"claim": f"About {p}.", "from": [p]} for p in papers]})
    monkeypatch.setattr(answer, "_complete", complete)
    monkeypatch.setattr(answer, "_supporting_parts", lambda text, found, ids: (ids, {}))
    final, _, _ = answer._combine_merge("Q?", chosen, found, lambda **k: None)
    assert {found[i][0] for _, ids, _ in final for i in ids} == set(range(1, 9))


def test_a_cut_off_second_merge_does_not_replace_the_answer(monkeypatch):
    monkeypatch.setattr(answer, "MAX_ANSWER_CLAIMS", 1)
    found = [(n, f"finding of paper {n}", ["q"]) for n in (1, 2, 3)]
    replies = [json.dumps({"claims": [{"claim": f"Sentence {k}.", "from": [f"C{k}"]} for k in (1, 2, 3)]}),
               '{"claims": [{"claim": "Only this.", "from": ["C1"]}, {"claim": "Cut o']
    monkeypatch.setattr(answer, "_complete", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(answer, "_supporting_parts", lambda text, found, ids: (ids, {}))
    final, _, _ = answer._combine_merge("Q?", [{"label": "P"}] * 3, found, lambda **k: None)
    assert [t for t, _, _ in final] == ["Sentence 1.", "Sentence 2.", "Sentence 3."]


def test_merged_sentence_numbers_must_come_from_its_findings(monkeypatch):
    assert answer._unsourced_numbers("a 2ly% to 61% excess risk",
                                     ["a significant 24% to 61% excess risk (P<0.001)"]) == ["2"]
    assert answer._unsourced_numbers("rose from 10,000 to 48.5 mg (\u22121.2)",
                                     ["10000 then 48.5", "-1.2"]) == []
    assert answer._strip_ids("Mortality fell [C6], and rose [C1, C17] [P2].") == "Mortality fell, and rose."
    assert answer._unsourced_numbers("In type 2 diabetes, CO2 fell by 30%", ["CO2 fell by 30%"]) == []
    merge = json.dumps({"claims": [{"claim": "Mortality fell by 21% [C1].", "from": ["C1"]}]})
    monkeypatch.setattr(answer, "_complete", _fake_model(merge))
    r = answer.ask_many("Does metformin extend life?", [_paper("Trial A", _A_TEXT), _paper("Cohort B", _B_TEXT)])
    first = r["claims"][0]
    assert first["claim"] == "Mortality fell by 21%." and first["unsourced_numbers"] == ["21"]


def test_overview_answers_from_the_merged_claims_and_is_checked_like_them(monkeypatch):
    def complete(system, user, max_tokens=1500):
        if system == answer._MERGE_SYSTEM:
            return json.dumps({"claims": [{"claim": "Mortality fell in the trial.", "from": ["C1"]},
                                          {"claim": "The cohort saw no change.", "from": ["C2"]}]})
        if system == answer._OVERVIEW_SYSTEM:
            assert "C1 [P1] Mortality fell in the trial." in user and "C2 [P2] The cohort saw no change." in user
            return json.dumps({"sentences": [
                {"text": "Not settled: mortality fell by 21% in one paper [C1].", "from": ["C1", "C2"]},
                {"text": "An invented sentence.", "from": ["C9"]},
                {"text": "Nobody said this.", "from": []}]})
        if system == answer._PART_SYSTEM:
            return json.dumps({"findings": [{"id": "C1", "backs": "part", "part": "mortality fell"},
                                            {"id": "C2", "backs": "none"}]})
        return _fake_model("{}")(system, user, max_tokens)
    monkeypatch.setattr(answer, "_complete", complete)
    r = answer.ask_many("Does metformin extend life?", [_paper("Trial A", _A_TEXT), _paper("Cohort B", _B_TEXT)])
    assert [c["claim"] for c in r["overview"]] == ["Not settled: mortality fell by 21% in one paper."]
    first = r["overview"][0]
    assert first["status"] == "GROUNDED" and {s["paper"] for s in first["sources"]} == {1}
    assert first["sources"][0]["part"] == "mortality fell" and first["unsourced_numbers"] == ["21"]
    assert all(c["claim"] != first["claim"] for c in r["claims"])


def test_draft_and_findings_go_out_with_progress_but_not_in_the_result(monkeypatch):
    def complete(system, user, max_tokens=1500):
        if system == answer._DRAFT_SYSTEM:
            assert "PASSAGES:" in user
            return json.dumps({"answer": "Quick unchecked answer [P1]."})
        return _fake_model("{}")(system, user, max_tokens)
    monkeypatch.setattr(answer, "_complete", complete)
    seen = []
    r = answer.ask_many("Did mortality fall with metformin?", [_paper("Trial A", _A_TEXT), _paper("Cohort B", _B_TEXT)],
                        progress=seen.append)
    assert seen[0]["stage"] == "draft" and "draft" not in seen[0]
    assert all(p["draft"]["answer"] == "Quick unchecked answer [P1]." for p in seen[1:])
    assert seen[1]["findings"] == [] and seen[2]["findings"] and seen[2]["findings"][0]["paper"] == 1
    assert "draft" not in r and "findings" not in r


def test_draft_failing_or_finding_nothing_changes_nothing(monkeypatch):
    def complete(system, user, max_tokens=1500):
        if system == answer._DRAFT_SYSTEM:
            raise TimeoutError
        return _fake_model("{}")(system, user, max_tokens)
    monkeypatch.setattr(answer, "_complete", complete)
    seen = []
    answer.ask_many("Did mortality fall with metformin?", [_paper("Trial A", _A_TEXT), _paper("Cohort B", _B_TEXT)],
                    progress=seen.append)
    assert seen[-1].get("draft") is None
    assert answer._draft("zebra quokka", [_paper("Trial A", _A_TEXT)]) is None


def test_draft_passages_are_spread_across_papers_and_skip_reference_lists():
    paper = lambda text: {"fetched": {"full_text": text}}
    long_paper = " ".join(f"Iron limits growth in region {i}. " * 20 + "." for i in range(8))
    refs = "Smith et al. (2001) Iron limits. Jones et al. (2002) Iron growth. Lee et al. (2003) Iron."
    picked = answer._draft_passages("does iron limit growth", [paper(long_paper), paper("Iron limits growth here."), paper(refs)])
    assert sum(n == 1 for n, _ in picked) == answer.DRAFT_PER_PAPER
    assert any(n == 2 for n, _ in picked) and not any(n == 3 for n, _ in picked)


def test_merge_batches_keep_a_papers_findings_together():
    found = [(n, "x", []) for n in (1, 1, 1, 2, 2, 3, 3, 3)]
    items = [("x", {i}) for i in range(len(found))]
    parts = answer._batches(items, found, 4)
    assert [[min(found[j][0] for j in it[1]) for it in p] for p in parts] == [[1, 1, 1], [2, 2], [3, 3, 3]]


def test_ask_many_drops_findings_about_the_paper_itself(monkeypatch):
    def complete(system, user, max_tokens=1500):
        if system == answer._MERGE_SYSTEM:
            return "{}"
        return json.dumps({"claims": [
            {"claim": "The study does not explicitly state where mortality fell.",
             "quotes": ["Metformin lowered all-cause mortality by 12%"]},
            {"claim": "Mortality fell by 12%.", "quotes": ["Metformin lowered all-cause mortality by 12%"]}]})
    monkeypatch.setattr(answer, "_complete", complete)
    r = answer.ask_many("Does metformin extend life?", [_paper("Trial A", _A_TEXT), _paper("Trial A2", _A_TEXT)])
    assert [c["claim"] for c in r["claims"]] == ["Mortality fell by 12%."] * 2


# --- disk cache + sidecar --------------------------------------------------------------

def test_store_roundtrip_skips_failed_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "_conn", None)
    store.enable()
    store.put("10.1/x", {"source": "pmc_bioc", "full_text": "t"})
    store.put("10.1/none", {"source": "none"})
    assert store.get("10.1/X")["full_text"] == "t"
    assert store.get("10.1/none") is None
    monkeypatch.setattr(store, "_conn", None)


def test_sidecar_requires_token_and_runs_plan_job(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from worth_your_read import sidecar
    monkeypatch.setattr(store.config, "DATA_DIR", str(tmp_path))
    c = TestClient(sidecar.app)
    assert c.get("/health").status_code == 401
    h = {"X-WYR-Token": sidecar.token()}
    assert c.get("/health", headers=h).json()["ok"] is True
    assert c.post("/jobs", json={"kind": "nope"}, headers=h).status_code == 400
    job = c.post("/jobs", json={"kind": "plan", "args": {"question": "Does metformin slow aging?"}},
                 headers=h).json()
    for _ in range(50):
        j = c.get(f"/jobs/{job['job_id']}", headers=h).json()
        if j["status"] in ("done", "error"):
            break
    assert j["status"] == "done" and j["result"]["mode"] == "clarify"


# --- locating quotes on the page -----------------------------------------------------

def test_find_span_exact_fuzzy_and_missing():
    toks = "the smaller the studies conducted in a field the less likely findings are true".split()
    q = "smaller the studies conducted in a field".split()
    assert library._find_span(q, toks) == (1, 8)
    # one word differs (extraction noise) -> still found by the fuzzy window
    assert library._find_span("smaller the studys conducted in a field".split(), toks) is not None
    assert library._find_span("nothing like this appears here".split(), toks) is None


def test_locate_quotes_without_pdf_reports_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("WYR_ZOTERO_DIR", str(tmp_path))
    assert library.locate_quotes(["any quote here"], attachment_key="NOPE") == \
        [{"text": "any quote here", "found": False}]


# --- research snowball (fake citation graph, no network) -----------------------------

from worth_your_read import research

_TOPIC = "statistical power and false positive research findings"
_GRAPH = {   # id: (title, refs, cited_by)
    "A": ("Statistical power and false positive research findings", ["C", "D"], []),
    "B": ("Why false positive findings dominate underpowered research", [], ["E"]),
    "C": ("Low statistical power undermines research findings reliability", ["F"], []),
    "D": ("A history of the printing press in Europe", [], []),
    "E": ("Replication shows false positive rates rise with low power", [], []),
    "F": ("Power analysis and the false positive rate of published findings", [], []),
}


def _w(i):
    t, refs, _ = _GRAPH[i]
    return {"id": i, "doi": f"10.1/{i.lower()}", "title": t, "year": 2020, "cited_by": 1,
            "type": "article", "is_oa": True, "retracted": False, "abstract": t, "refs": list(refs)}


@pytest.fixture
def fake_graph(monkeypatch):
    monkeypatch.setattr(research, "seed_works", lambda q, dois, n: [_w("A"), _w("B"), _w("D")])
    monkeypatch.setattr(research, "fetch_works", lambda ids=(), dois=(): [_w(i) for i in ids if i in _GRAPH])
    # Batched like OpenAlex: works citing any of the ids; a citing work's own
    # reference list says which of them it cites.
    monkeypatch.setattr(research, "fetch_citing",
                        lambda ids, k: [{**_w(c), "refs": _w(c)["refs"] + [pid]}
                                        for pid in ids for c in _GRAPH[pid][2]])
    # The "model": relevant unless it's about printing presses.
    return lambda q, papers: {i for i, p in enumerate(papers) if "printing" not in p["title"]}


def test_snowball_follows_refs_and_citations_until_exhausted(fake_graph):
    seen_progress = []
    r = research.snowball(_TOPIC, screen=fake_graph, progress=seen_progress.append)
    ids = [p["id"] for p in r["papers"]]
    assert set(ids) == {"A", "B", "C", "E", "F"}          # D screened out
    assert r["stopped"] == "exhausted"
    by = {p["id"]: p for p in r["papers"]}
    assert by["C"]["round"] == 1 and by["F"]["round"] == 2  # paper -> ref -> paper -> ref
    assert by["E"]["via"] == [{"from": "B", "rel": "cites"}]
    assert by["F"]["via"] == [{"from": "C", "rel": "referenced by"}]
    assert [x["accepted"] for x in r["rounds"]][-1] == 0      # last round found nothing new
    assert seen_progress and seen_progress[0]["stage"] == "seeds"


def test_snowball_reports_cap_instead_of_exhaustion(fake_graph):
    r = research.snowball(_TOPIC, screen=fake_graph, max_papers=3)
    assert r["stopped"] == "max_papers" and len(r["papers"]) == 3
    assert "cap" in r["summary"]


def test_snowball_admits_heavily_cocited_paper_with_low_model_score(fake_graph, monkeypatch):
    # G is referenced by A, B, C and F (4 accepted papers) but the model rates it 1.
    for k in "ABCF":
        _GRAPH[k][1].append("G") if k != "B" else _GRAPH["B"][2].append("G")
    _GRAPH["G"] = ("Targeting aging with a drug: trial rationale", [], [])
    try:
        screen = lambda q, ps: {i: (1 if p["id"] == "G" else 0 if "printing" in p["title"] else 3)
                                for i, p in enumerate(ps)}
        r = research.snowball(_TOPIC, screen=screen)
        assert "G" in {p["id"] for p in r["papers"]}
    finally:
        for k in "ACF":
            _GRAPH[k][1].remove("G")
        _GRAPH["B"][2].remove("G")
        del _GRAPH["G"]


def test_chain_of_weak_matches_is_not_followed_forever(fake_graph, monkeypatch):
    # A -> C -> F -> H, every paper rated 2 (related, not the question itself):
    # C is 1 hop from the seeds and expanded; F (2 hops, one citation link) is set
    # aside, so H is never fetched.
    _GRAPH["F"][1].append("H")
    _GRAPH["H"] = ("Power and false positive findings in a further field", ["I"], [])
    _GRAPH["I"] = ("Power and false positive findings, further still", [], [])
    try:
        screen = lambda q, ps: {i: (0 if "printing" in p["title"] else 2) for i, p in enumerate(ps)}
        r = research.snowball(_TOPIC, screen=screen)
        by = {p["id"]: p for p in r["papers"]}
        assert by["C"]["hops"] == 1 and "F" not in by and "H" not in by
        assert r["stopped"] == "exhausted" and "set aside" in r["summary"]
    finally:
        _GRAPH["F"][1].remove("H")
        del _GRAPH["H"], _GRAPH["I"]


def test_duplicate_records_and_correction_notices_are_collapsed(fake_graph, monkeypatch):
    # OpenAlex holds A again as a reprint (A2) plus a correction notice (A3).
    _GRAPH["A2"] = (_GRAPH["A"][0], [], [])
    _GRAPH["A3"] = ("Correction: " + _GRAPH["A"][0], [], [])
    _GRAPH["C"][1].extend(["A2", "A3"])
    try:
        r = research.snowball(_TOPIC, screen=fake_graph)
        ids = {p["id"] for p in r["papers"]}
        assert "A" in ids and not {"A2", "A3"} & ids
    finally:
        _GRAPH["C"][1].remove("A2"); _GRAPH["C"][1].remove("A3")
        del _GRAPH["A2"], _GRAPH["A3"]


def test_semantic_score_relates_synonyms_that_word_overlap_misses():
    from worth_your_read import semantic
    if not semantic.available():
        pytest.skip("scikit-learn not installed")
    docs = ["Metformin reduces all-cause mortality and diseases of ageing in older adults",
            "Metformin lowers all-cause mortality in an ageing cohort followed for a decade",
            "Metformin inhibits melanoma cell growth through autophagy",
            "A history of the printing press in Europe"]
    query = "Does metformin extend lifespan? " + docs[0]
    s = semantic.semantic_scores(query, docs[1:])
    assert s[0] > s[1] > s[2] and all(0 <= x <= 1 for x in s)


def test_snowball_same_result_with_semantic_stage_or_word_overlap(fake_graph):
    from worth_your_read import semantic
    runs = [research.snowball(_TOPIC, screen=fake_graph, semantic=False)]
    if semantic.available():
        runs.append(research.snowball(_TOPIC, screen=fake_graph, semantic=True))
    for r in runs:
        assert {p["id"] for p in r["papers"]} == {"A", "B", "C", "E", "F"}


def test_rate_limit_is_never_reported_as_exhausted(fake_graph, monkeypatch):
    from worth_your_read import discover
    def limited(ids, k):
        raise discover.RateLimited("https://api.openalex.org/works", 1200)
    monkeypatch.setattr(research, "fetch_citing", limited)
    r = research.snowball(_TOPIC, screen=fake_graph)
    assert r["stopped"] == "rate_limited" and r["retry_after"] == 1200
    assert "exhausted" not in r["summary"].lower() and "20 min" in r["summary"]
    assert {p["id"] for p in r["papers"]} >= {"A", "B"}      # partial results kept


def test_failed_requests_mark_search_incomplete(fake_graph, monkeypatch):
    def flaky(ids=(), dois=()):
        research._errors.append("HTTPError: 500")
        return [_w(i) for i in ids if i in _GRAPH]
    monkeypatch.setattr(research, "fetch_works", flaky)
    r = research.snowball(_TOPIC, screen=fake_graph)
    assert r["stopped"] == "incomplete"


def test_unfaithful_keeps_problems_and_drops_words_not_in_the_sentence(monkeypatch):
    seen = {}
    def complete(system, user, max_tokens=1500):
        seen["user"] = user
        return json.dumps({"problems": [
            {"words": "in placebo trials, RR 0.94", "why": "0.94 is across all trials"},
            {"words": "another study", "why": "same paper"},
            {"words": "made up words", "why": "not in the sentence"},
            {"words": "x", "why": ""}]})
    monkeypatch.setattr(answer, "_complete", complete)
    found = [(1, "RR 0.94 across all trials", []), (1, "RR 0.91 in placebo trials", [])]
    chosen = [{"label": "Smith 2020 — Metformin trials"}]
    probs = answer._unfaithful("In placebo trials, RR 0.94 and 0.91; another study agreed.", found, {0, 1}, chosen)
    assert probs == [{"words": "", "why": "0.94 is across all trials"},
                     {"words": "another study", "why": "same paper"},
                     {"words": "", "why": "not in the sentence"}]
    assert "C1 (Smith 2020) RR 0.94" in seen["user"] and "C2 (Smith 2020)" in seen["user"]
    monkeypatch.setattr(answer, "_complete", lambda *a, **k: "not json")
    assert answer._unfaithful("x", found, {0}, chosen) == []


def test_a_sentence_naming_more_studies_than_it_cites_is_flagged():
    s = "One meta-analysis found a benefit; another study found no effect, and another study found harm."
    assert answer._more_studies_than_papers(s, 1)[0]["words"] == "another study found"
    assert answer._more_studies_than_papers(s, 3) == []
    assert answer._more_studies_than_papers("Requiring replication in a second study reduces false positives.", 1) == []


def test_ask_many_flags_merged_sentences_that_say_more_than_their_findings(monkeypatch):
    merge = json.dumps({"claims": [
        {"claim": "Mortality fell by 21%; another study found longer life.", "from": ["C1", "C2"]}]})
    fake = _fake_model(merge)
    def complete(system, user, max_tokens=1500):
        if system == answer._FAITHFUL_SYSTEM:
            return json.dumps({"problems": [{"words": "by 21%", "why": "the finding says 12%"},
                                            {"words": "longer life", "why": "the finding says users lived longer"}]})
        if system == answer._PART_SYSTEM:
            return json.dumps({"findings": [{"id": "C1", "backs": "part"}, {"id": "C2", "backs": "part"}]})
        return fake(system, user, max_tokens)
    monkeypatch.setattr(answer, "_complete", complete)
    seen = []
    r = answer.ask_many("Does metformin extend life?", [_paper("Trial A", _A_TEXT), _paper("Cohort B", _B_TEXT)],
                        progress=seen.append)
    first = r["claims"][0]
    assert first["unsourced_numbers"] == ["21"]
    # the number guard already flags 21: no second warning for it; two papers, so "another study" is fine
    assert first["problems"] == [{"words": "longer life", "why": "the finding says users lived longer"}]
    assert "wording" in [p["stage"] for p in seen]
