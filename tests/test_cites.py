"""Following introductions to the papers they cite (cites.py), without a network."""
import json

from worth_your_read import answer, cites

_JATS = b"""<article><body>
<sec sec-type="intro"><title>Introduction</title>
<p>Ferroptosis is driven by lipid peroxidation.<xref ref-type="bibr" rid="B1">1</xref>,<xref ref-type="bibr" rid="B2">2</xref>
GPX4 restrains it <xref ref-type="bibr" rid="B1">1</xref>&#8211;<xref ref-type="bibr" rid="B3">3</xref>. Cells die.</p>
</sec>
<sec><title>Results</title><p>We found X <xref ref-type="bibr" rid="B3">3</xref>.</p></sec>
</body><back><ref-list>
<ref id="B1"><element-citation><article-title>Ferroptosis: an iron-dependent form</article-title>
<pub-id pub-id-type="doi">10.1016/J.CELL.2012.03.042</pub-id></element-citation></ref>
<ref id="B2"><element-citation><article-title>Lipid peroxidation</article-title></element-citation></ref>
<ref id="B3"><element-citation><article-title>GPX4 regulates</article-title>
<pub-id pub-id-type="doi">10.1016/j.cell.2013.12.010</pub-id></element-citation></ref>
</ref-list></back></article>"""


def test_jats_links_tie_introduction_sentences_to_their_references():
    links = cites.jats_links(_JATS)
    assert [link["sentence"] for link in links] == ["Ferroptosis is driven by lipid peroxidation.",
                                                     "GPX4 restrains it."]
    # citations after the full stop belong to the sentence before; DOIs are cleaned
    assert [r["doi"] for r in links[0]["refs"]] == ["10.1016/j.cell.2012.03.042", ""]
    # "1-3" cites 1, 2 and 3; results sections are never read for links
    assert [r["title"] for r in links[1]["refs"]] == ["Ferroptosis: an iron-dependent form", "Lipid peroxidation",
                                                       "GPX4 regulates"]
    assert cites.jats_links(b"not xml") == []


def test_match_links_needs_one_clear_reference():
    works = [{"doi": "10.1/a", "title": "Orange carotenoid protein photoactivation quenches phycobilisome fluorescence",
              "abstract": ""},
             {"doi": "10.1/b", "title": "Nitrogen fixation in marine diatoms", "abstract": ""},
             {"doi": "10.1/c", "title": "Iron limitation of ocean productivity", "abstract": ""}]
    works += [{"doi": f"10.1/x{i}", "title": f"Study {i} of soil bacteria growth", "abstract": ""} for i in range(30)]
    sentences = ["Photoactivation of the orange carotenoid protein quenches phycobilisome fluorescence.",
                 "Cells respond to stress."]
    assert [(link["sentence"][:14], link["refs"][0]["doi"]) for link in cites.match_links(sentences, works)] == [
        ("Photoactivatio", "10.1/a")]


def test_to_follow_ranks_by_how_many_papers_cite_it(monkeypatch):
    links = {"A": [{"sentence": "GPX4 restrains ferroptosis.", "how": "xref",
                    "refs": [{"doi": "10.1/gpx4", "title": "GPX4"}, {"doi": "10.1/have", "title": "Have"}]},
                   {"sentence": "Unrelated history of cell biology.", "how": "xref",
                    "refs": [{"doi": "10.1/history", "title": "History"}]}],
             "B": [{"sentence": "Ferroptosis needs iron; GPX4 stops it.", "how": "match",
                    "refs": [{"doi": "10.1/gpx4", "title": "GPX4"}, {"doi": "10.1/iron", "title": "Iron"}]}]}
    links["B"].append({"sentence": "GPX4 and iron in ferroptosis, a review.", "how": "xref",
                       "refs": [{"doi": "10.1/review", "title": "A review"}]})
    monkeypatch.setattr(cites, "intro_links", lambda fetched, doi="": links[fetched["name"]])
    monkeypatch.setattr(cites, "_reviews", lambda dois: {"10.1/review"} & set(dois))
    chosen = [{"fetched": {"name": "A"}}, {"fetched": {"name": "B"}}]
    rows = cites.to_follow("How do GPX4 and iron control ferroptosis?", chosen, {"10.1/have"}, 5)
    # the off-topic sentence's citation and the review are not followed
    assert [(r["doi"], r["via"], r["how"]) for r in rows] == [("10.1/gpx4", [1, 2], "xref"),
                                                              ("10.1/iron", [2], "match")]
    assert len(cites.to_follow("How do GPX4 and iron control ferroptosis?", chosen, set(), 1)) == 1


def test_ask_many_reads_papers_the_introductions_cite(monkeypatch):
    def complete(system, user, max_tokens=1500):
        if "lowered all-cause mortality" in user:
            return json.dumps({"claims": [{"claim": "Mortality fell.", "quotes": ["lowered all-cause mortality"]}]})
        if "users lived longer" in user:
            return json.dumps({"claims": [{"claim": "Users lived longer.", "quotes": ["users lived longer"]}]})
        return "{}"
    monkeypatch.setattr(answer, "_complete", complete)
    monkeypatch.setattr(cites, "to_follow", lambda q, chosen, have, limit: [
        {"doi": "10.1/left", "title": "Left out", "via": [1], "how": "xref"},
        {"doi": "10.1/new", "title": "New", "via": [1], "how": "xref"},
        {"doi": "10.1/gone", "title": "Unreadable", "via": [1], "how": "xref"}])
    papers = [{"label": "Trial A", "key": "10.1/a", "fetched": {"full_text": "Metformin lowered all-cause mortality.",
                                                               "has_fulltext": True}},
              {"label": "Left out", "key": "10.1/left", "fetched": {"full_text": "Unrelated.", "has_fulltext": True}}]
    loaded = []

    def load(doi, title):
        loaded.append(doi)
        if doi == "10.1/new":
            return {"label": title, "key": doi, "fetched": {"full_text": "Metformin users lived longer.",
                                                            "has_fulltext": True}}
        raise ValueError("no text")
    r = answer.ask_many("Does metformin lower mortality?", papers, max_papers=1, follow=load)
    assert len(r["papers"]) == 1 and loaded == []                # following is for deep research only
    r = answer.ask_many("Does metformin lower mortality?", papers, max_papers=1, follow=load, depth="deep")
    assert loaded == ["10.1/new", "10.1/gone"]                  # the left-out paper came back without a fetch
    assert [(p["label"], p.get("via")) for p in r["papers"]] == [("Trial A", None), ("Left out", [1]), ("New", [1])]
    assert r["left_out"] == [] and r["skipped"] == []
    assert {s["paper"] for c in r["claims"] for s in c["sources"]} == {1, 3}
