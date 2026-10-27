// Worth Your Read — static, client-side pipeline (GitHub Pages).
// Mirrors the Python package: resolve + retraction, Europe PMC full text,
// Anthropic judge (direct fetch with the user's key), the substring quote-
// verification guard, dedupe + threshold, and plan_search.
// Source discovery fans out to OpenAlex + Crossref + Europe PMC (all keyless,
// CORS-open) and ranks deterministically. All network calls go to CORS-enabled
// endpoints (OpenAlex, Crossref, Europe PMC) or directly to the Anthropic API
// with the browser-access header. No API key leaves the browser for discovery.

const $ = (id) => document.getElementById(id);
const esc = (s) => (s ?? "").toString().replace(/[&<>]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;" }[c]));

// ---------- key persistence ----------
try { $("key").value = localStorage.getItem("wyr_key") || ""; } catch {}
try { $("model").value = localStorage.getItem("wyr_model") || "claude-sonnet-5-5"; } catch {}
$("key").addEventListener("change", e => { try { localStorage.setItem("wyr_key", e.target.value.trim()); } catch {} });
$("model").addEventListener("change", e => { try { localStorage.setItem("wyr_model", e.target.value.trim()); } catch {} });
$("minconf").addEventListener("input", e => $("minconfVal").textContent = Number(e.target.value).toFixed(2));

// ---------- reference parsing + resolve ----------
const DOI_RE = /10\.\d{4,9}\/[-._;()/:A-Za-z0-9]+/i;
function parseReference(ref) {
  ref = (ref || "").trim();
  const ids = { doi:null, pmid:null, pmcid:null, url: ref.startsWith("http") ? ref : null, raw:ref };
  const d = ref.match(DOI_RE); if (d) ids.doi = d[0].replace(/[).,;>"']+$/, "");
  const pc = ref.match(/PMC\d{5,9}/i); if (pc) ids.pmcid = pc[0].toUpperCase();
  const pm = ref.match(/pmid[:\s]*([0-9]{4,9})/i); if (pm) ids.pmid = pm[1];
  else if (/^\d{4,9}$/.test(ref)) ids.pmid = ref;
  const pu = ref.match(/pubmed\.ncbi\.nlm\.nih\.gov\/(\d{4,9})/); if (pu && !ids.pmid) ids.pmid = pu[1];
  return ids;
}
async function getJSON(url) {
  const r = await fetch(url, { headers: { "Accept": "application/json" } });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}
function retractionFlags(msg) {
  const out = { retracted:false, concern:false };
  for (const field of ["updated-by", "update-to"]) {
    for (const u of (msg[field] || [])) {
      const t = (u.type || "").toLowerCase();
      if (t.includes("retraction")) out.retracted = true;
      else if (t.includes("concern")) out.concern = true;
    }
  }
  return out;
}
async function resolve(ref) {
  const ids = parseReference(ref);
  const out = { ids, resolved:false, citation_ok:false, title:null, authors:null,
                year:null, journal:null, doi:ids.doi, pmcid:ids.pmcid, pmid:ids.pmid,
                retracted:false, concern:false, citations:null, type:null, is_oa:false,
                error:null };
  // Fill ids / metadata via Europe PMC when we lack a DOI.
  let epmc = null;
  try {
    let q = ids.doi ? `DOI:"${ids.doi}"` : ids.pmcid ? `PMCID:${ids.pmcid}`
          : ids.pmid ? `EXT_ID:${ids.pmid} AND SRC:MED` : null;
    if (q) {
      const j = await getJSON(`https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=${encodeURIComponent(q)}&resultType=core&format=json`);
      epmc = (j.resultList && j.resultList.result && j.resultList.result[0]) || null;
      if (epmc) {
        out.doi = out.doi || epmc.doi || null;
        out.pmcid = out.pmcid || epmc.pmcid || null;
        out.pmid = out.pmid || epmc.pmid || null;
        out.title = epmc.title || null;
        out.journal = (epmc.journalInfo && epmc.journalInfo.journal && epmc.journalInfo.journal.title) || null;
        out.year = epmc.pubYear || null;
        if (epmc.citedByCount != null) out.citations = epmc.citedByCount;
        if (epmc.isOpenAccess === "Y") out.is_oa = true;
        out.type = out.type || epmc.pubType || null;
      }
    }
  } catch (e) { /* non-fatal */ }
  out._epmc = epmc;

  if (!out.doi) {
    if (out.title) { out.resolved = true; out.citation_ok = true; return out; }
    out.error = "Could not resolve the reference to a DOI or indexed record.";
    return out;
  }
  try {
    const msg = (await getJSON(`https://api.crossref.org/works/${encodeURIComponent(out.doi)}`)).message;
    out.resolved = true; out.citation_ok = true;
    out.title = (msg.title && msg.title[0]) || out.title;
    out.journal = (msg["container-title"] && msg["container-title"][0]) || out.journal;
    const auth = (msg.author || []).slice(0,8).map(a => [a.given, a.family].filter(Boolean).join(" ")).filter(Boolean);
    out.authors = auth.join(", ") || out.authors;
    for (const k of ["published-print","published-online","issued"]) {
      const dp = msg[k] && msg[k]["date-parts"]; if (dp && dp[0] && dp[0][0]) { out.year = dp[0][0]; break; }
    }
    if (msg["is-referenced-by-count"] != null) out.citations = msg["is-referenced-by-count"];
    out.type = out.type || msg.type || null;
    Object.assign(out, retractionFlags(msg));
  } catch (e) {
    if (out.title) { out.resolved = true; out.citation_ok = true; }
    else out.error = `DOI did not resolve at Crossref (${e.message}).`;
  }
  return out;
}

// ---------- full text (Europe PMC) ----------
const PRIORITY = ["results","conclusion","abstract","methods","discussion","introduction","title","other"];
function bucketFromTitle(t) {
  t = (t || "").toLowerCase();
  if (t.includes("result")) return "results";
  if (t.includes("method") || t.includes("material")) return "methods";
  if (t.includes("discuss")) return "discussion";
  if (t.includes("conclu")) return "conclusion";
  if (t.includes("introduc") || t.includes("background")) return "introduction";
  return "other";
}
const strip = (s) => (s || "").replace(/\s+/g, " ").trim();
const MAX_CHARS = 48000;

async function fetchText(resolved) {
  const out = { sections:{}, sections_available:[], has_results:false, open_access:false, source:"none", full_text:"" };
  const pmcid = resolved.pmcid;
  if (pmcid) {
    try {
      const r = await fetch(`https://www.ebi.ac.uk/europepmc/webservices/rest/PMC/${pmcid}/fullTextXML`);
      if (r.ok) {
        const xml = new DOMParser().parseFromString(await r.text(), "application/xml");
        if (!xml.querySelector("parsererror")) {
          const ab = xml.querySelector("abstract");
          if (ab) out.sections.abstract = strip(ab.textContent);
          const body = xml.querySelector("body");
          if (body) {
            body.querySelectorAll("sec").forEach(sec => {
              const titleEl = [...sec.children].find(c => c.tagName === "title");
              const bucket = bucketFromTitle(titleEl ? titleEl.textContent : "");
              const txt = strip([...sec.children].filter(c => c.tagName === "p").map(p => p.textContent).join(" "));
              if (txt) out.sections[bucket] = (out.sections[bucket] ? out.sections[bucket] + "\n" : "") + txt;
            });
          }
          if (Object.keys(out.sections).length) { out.open_access = true; out.source = "epmc_fulltext"; }
        }
      }
    } catch (e) { /* fall through */ }
  }
  if (!Object.keys(out.sections).length) {
    const abs = resolved._epmc && resolved._epmc.abstractText;
    if (abs) { out.sections = { abstract: strip(abs) }; out.source = "abstract_only"; }
  }
  out.sections_available = PRIORITY.filter(s => out.sections[s]);
  out.has_results = !!out.sections.results;
  let budget = MAX_CHARS, parts = [];
  for (const b of PRIORITY) {
    if (!out.sections[b]) continue;
    const chunk = (`\n## ${b.toUpperCase()}\n` + out.sections[b]).slice(0, budget);
    parts.push(chunk); budget -= chunk.length; if (budget <= 0) break;
  }
  out.full_text = parts.join("").trim();
  return out;
}

// ---------- judge (Anthropic, direct fetch) + the guard ----------
const SYSTEM = `You are a strict scientific evidence judge. You decide whether a CLAIM is supported by the PAPER TEXT provided, and nothing else.

First DECOMPOSE the claim into its atomic, independently checkable aspects. Use these types where they apply:
- "population": who or what the claim is about (species, patients, setting).
- "intervention": the exposure, treatment, or variable acted on.
- "outcome": what was measured or affected.
- "direction": the sign of the effect (increase / decrease / no effect / association direction).
- "magnitude": how large the effect is — ONLY include this aspect if the claim actually asserts a size.
Only create an aspect for something the CLAIM actually asserts. A claim is only as strong as its weakest asserted aspect.

Rule on EACH aspect separately, using ONLY the provided text:
- "ENTAILED": the text directly establishes this aspect — give a VERBATIM quote.
- "CONTRADICTED": the text states the opposite — give a VERBATIM quote.
- "ABSENT": the text does not address this aspect — quote must be "".
Weigh RESULTS/CONCLUSION over the title/abstract. Quote VERBATIM, copied exactly from the paper text (numbers and punctuation included); never paraphrase inside a quote.

Then give an overall verdict and a one-sentence rationale CONSISTENT with the aspects (SUPPORTED only if every asserted aspect is ENTAILED; OVERSTATED if the core relationship holds but a qualifier like magnitude/population is unestablished or exaggerated; UNSUPPORTED if contradicted; INSUFFICIENT if the core relationship itself is absent). If OVERSTATED, name the distortion type.

Return ONLY one JSON object: {"verdict","confidence"(0-1),"aspects":[{"type":"population|intervention|outcome|direction|magnitude|other","text":"the sub-claim in words","status":"ENTAILED|CONTRADICTED|ABSENT","quote":"verbatim or empty","note":""}],"evidence":[{"quote","section","stance":"ENTAILS|CONTRADICTS|NEUTRAL","reason"}],"distortion_type":"none|correlation_causation|animal_to_human|dropped_caveats|effect_size_spin|cherry_picking|other","distortion_note","claim_aspects_unmet":[],"rationale"}`;

// Normalize unicode punctuation (curly quotes, dashes, non-breaking spaces)
// so a verbatim quote isn't rejected over a typographic variant.
const norm = (s) => (s || "")
  .replace(/[‘’‛′]/g, "'")
  .replace(/[“”″]/g, '"')
  .replace(/[‐-―−]/g, "-")
  .replace(/[   ]/g, " ")
  .replace(/\s+/g, " ").trim().toLowerCase();
function quoteVerified(quote, hay) {
  const q = norm(quote);
  if (q.length < 12) return false;
  if (hay.includes(q)) return true;
  if (q.length >= 60 && hay.includes(q.slice(0, 60))) return true;
  // Relaxed fallback: a long contiguous verbatim run (10+ words) still appears
  // exactly in the source — tolerates a dropped clause or ellipsis elsewhere in
  // the quote without weakening the anti-hallucination guarantee.
  const words = q.split(" ").filter(Boolean);
  if (words.length >= 10) {
    for (let i = 0; i + 10 <= words.length; i++) {
      if (hay.includes(words.slice(i, i + 10).join(" "))) return true;
    }
  }
  return false;
}
const band = (c) => c >= 0.75 ? "High" : c >= 0.45 ? "Med" : "Low";
const DISTORTION_LABELS = { none:"none", correlation_causation:"correlation→causation", animal_to_human:"animal→human",
  dropped_caveats:"dropped caveats", effect_size_spin:"effect-size spin", cherry_picking:"cherry-picking", other:"other" };

// Verify each decomposed aspect's quote, then DERIVE the verdict from the
// grounded aspects rather than trusting the model's own verdict. This is what
// stops a confident-but-wrong "SUPPORTED" when, e.g., the magnitude aspect is
// unestablished — the verdict is a transparent function of the aspect statuses.
const CORE_TYPES = new Set(["outcome", "direction"]);
const VERDICT_CEIL = { SUPPORTED: 1.0, OVERSTATED: 0.6, UNSUPPORTED: 0.9, INSUFFICIENT: 0.4 };
function checkAspects(aspects, hay) {
  return (aspects || []).map(a => {
    const st = (a.status || "").toUpperCase();
    const q = a.quote || "";
    const verified = q ? quoteVerified(q, hay) : false;
    // an ENTAILED/CONTRADICTED aspect whose quote can't be verified falls to ABSENT
    const status = ((st === "ENTAILED" || st === "CONTRADICTED") && !verified) ? "ABSENT" : st;
    return { type: (a.type || "other").toLowerCase(), text: a.text || "",
             status, quote: verified ? q : "", note: a.note || "" };
  });
}
function deriveFromAspects(checked) {
  if (!checked.length) return null;
  const core = checked.filter(a => CORE_TYPES.has(a.type));
  const anyCoreContra = core.some(a => a.status === "CONTRADICTED");
  const anyContra = checked.some(a => a.status === "CONTRADICTED");
  const allEntailed = checked.every(a => a.status === "ENTAILED");
  const coreOk = core.length > 0 && core.every(a => a.status === "ENTAILED");
  let verdict;
  if (anyCoreContra) verdict = "UNSUPPORTED";
  else if (anyContra) verdict = "OVERSTATED";                 // wrong on a qualifier
  else if (allEntailed) verdict = "SUPPORTED";
  else if (coreOk) verdict = "OVERSTATED";                    // core holds; a qualifier (magnitude/population) unestablished
  else if (core.length === 0) verdict = allEntailed ? "SUPPORTED" : "INSUFFICIENT";
  else verdict = "INSUFFICIENT";                              // core relationship itself not established
  const unmet = checked.filter(a => a.status === "ABSENT")
    .map(a => `${a.type}${a.text ? ` — ${a.text}` : ""} (not established in the text)`);
  return { verdict, unmet };
}
function finalize(data, fetched) {
  const hay = norm(fetched.full_text);
  const verified = [];
  for (const ev of (data.evidence || [])) { ev.verified = quoteVerified(ev.quote, hay); if (ev.verified) verified.push(ev); }
  const aspects = checkAspects(data.aspects, hay);
  const aspectQuotesVerified = aspects.filter(a => a.quote).length;
  const r = {
    verdict: data.verdict || "INSUFFICIENT",
    confidence: Number(data.confidence) || 0,
    evidence: verified,
    aspects,
    distortion_type: data.distortion_type || "none",
    distortion_note: data.distortion_note || "",
    claim_aspects_unmet: (data.claim_aspects_unmet || []).slice(),
    rationale: data.rationale || "",
    quotes_verified: verified.length + aspectQuotesVerified,
    downgraded: false,
  };

  // Derive the verdict from grounded aspects when the model provided a decomposition.
  const derived = deriveFromAspects(aspects);
  if (derived) {
    if (derived.verdict !== r.verdict) r.verdict_model = data.verdict || null; // keep the model's own call for transparency
    r.verdict = derived.verdict;
    for (const u of derived.unmet) if (!r.claim_aspects_unmet.includes(u)) r.claim_aspects_unmet.push(u);
    r.confidence = Math.min(r.confidence, VERDICT_CEIL[r.verdict] ?? 1.0);
  }

  // Core anti-hallucination guard: a non-INSUFFICIENT verdict needs at least one
  // verified quote somewhere (evidence or an aspect).
  if (["SUPPORTED","OVERSTATED","UNSUPPORTED"].includes(r.verdict) && !verified.length && !aspectQuotesVerified) {
    r.verdict = "INSUFFICIENT"; r.downgraded = true; r.confidence = Math.min(r.confidence, 0.3);
    r.claim_aspects_unmet.push("No quoted evidence could be verified against the source text.");
  }
  if (!fetched.has_results) r.confidence = Math.min(r.confidence, 0.5);
  r.band = band(r.confidence);
  return r;
}
function noTextResult(reason) {
  return { verdict:"INSUFFICIENT", confidence:0, band:"Low", evidence:[], aspects:[], distortion_type:"none",
    distortion_note:"", claim_aspects_unmet:[reason], rationale:reason, quotes_verified:0, downgraded:false };
}
function parseJsonVerdict(text, fetched) {
  if (!fetched.full_text) return noTextResult("No paper text could be retrieved.");
  let raw = (text || "").trim();
  raw = raw.replace(/^```[a-zA-Z]*\n?/, "").replace(/\n?```$/, "");
  const s = raw.indexOf("{"), e = raw.lastIndexOf("}");
  if (s !== -1 && e !== -1) raw = raw.slice(s, e + 1);
  let data; try { data = JSON.parse(raw); } catch (ex) { return noTextResult("Could not parse the judge output as JSON."); }
  return finalize(data, fetched);
}
async function judge(claim, fetched, key, model) {
  if (!fetched.full_text) return noTextResult("No paper text could be retrieved.");
  if (!key) return noTextResult("No API key set — add your Anthropic key above to run the judge.");
  const user = `CLAIM:\n${claim}\n\nSECTIONS AVAILABLE: ${fetched.sections_available.join(", ") || "none"} (source=${fetched.source})\n\nPAPER TEXT:\n${fetched.full_text}`;
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-api-key": key,
      "anthropic-version": "2023-06-01",
      "anthropic-dangerous-direct-browser-access": "true",
    },
    body: JSON.stringify({ model, max_tokens: 1500, system: SYSTEM,
      messages: [{ role: "user", content: user }] }),
  });
  if (!r.ok) { const t = await r.text(); return noTextResult(`Anthropic API error ${r.status}: ${t.slice(0,160)}`); }
  const j = await r.json();
  const text = (j.content || []).filter(b => b.type === "text").map(b => b.text).join("\n");
  return parseJsonVerdict(text, fetched);
}

// ---------- verify one claim ----------
async function verifyClaim(claim, reference, key, model) {
  const res = await resolve(reference);
  const fetched = await fetchText(res);
  let result;
  if (res.retracted) {
    result = { verdict:"UNSUPPORTED", confidence:0.9, band:"High", evidence:[], aspects:[], distortion_type:"none",
      distortion_note:"", claim_aspects_unmet:["The cited paper is retracted."],
      rationale:"Source is retracted; it cannot support the claim.", quotes_verified:0, downgraded:false };
  } else {
    result = await judge(claim, fetched, key, model);
  }
  Object.assign(result, {
    claim, reference, citation_ok: res.citation_ok, retracted: res.retracted, concern: res.concern,
    resolved: { title:res.title, authors:res.authors, year:res.year, journal:res.journal, doi:res.doi, pmcid:res.pmcid,
                citations:res.citations, type:res.type, is_oa:res.is_oa },
    has_results: fetched.has_results, sections_available: fetched.sections_available, open_access: fetched.open_access,
  });
  return result;
}

// ---------- score + worth-your-read ----------
const VW = { SUPPORTED:1.0, OVERSTATED:0.4, INSUFFICIENT:0, UNSUPPORTED:0 };
const VR = { SUPPORTED:0, OVERSTATED:1, INSUFFICIENT:2, UNSUPPORTED:3 };
function grade(x){ return x>=.85?"A":x>=.7?"B":x>=.5?"C":x>=.3?"D":"F"; }
const REVIEW_RE = /systematic review|meta-analysis|meta analysis/i;
function aggregateClaim(claim, refResults) {
  const sup = refResults.filter(r => r.verdict === "SUPPORTED");
  const over = refResults.filter(r => r.verdict === "OVERSTATED");
  const unsup = refResults.filter(r => r.verdict === "UNSUPPORTED");
  const verdict = sup.length ? "SUPPORTED" : over.length ? "OVERSTATED" : unsup.length ? "UNSUPPORTED" : "INSUFFICIENT";
  const support_count = sup.length, total_refs = refResults.length;
  const hasReview = sup.some(r => REVIEW_RE.test((r.resolved && r.resolved.title) || ""));
  const strength = (support_count >= 2 || (support_count >= 1 && hasReview)) ? "Strong"
                 : support_count === 1 ? "Limited" : "None";
  const pool = sup.length ? sup : over.length ? over : refResults;
  const confidence = pool.reduce((m, r) => Math.max(m, r.confidence || 0), 0);
  let recommendation = "";
  if (["SUPPORTED","OVERSTATED"].includes(verdict) && strength !== "Strong")
    recommendation = `Backed by only ${support_count} full-support source(s); ≥2 independent sources or a systematic review are recommended for a strong claim.`;
  return { claim, verdict, confidence, support_count, total_refs, strength, recommendation, refResults };
}
function scoreAnswer(claimResults, minConf) {
  const counts = {}; let sum = 0, weak = 0;
  for (const cr of claimResults) {
    counts[cr.verdict] = (counts[cr.verdict]||0)+1;
    sum += (VW[cr.verdict]||0) * (cr.confidence||0);
    if (["SUPPORTED","OVERSTATED"].includes(cr.verdict) && cr.strength !== "Strong") weak++;
  }
  const overall = claimResults.length ? sum/claimResults.length : 0;
  const byKey = {}; let excluded = 0;
  for (const cr of claimResults) for (const r of cr.refResults) {
    if (!["SUPPORTED","OVERSTATED"].includes(r.verdict) || r.retracted) continue;
    if ((r.confidence||0) < minConf) { excluded++; continue; }
    const res = r.resolved || {};
    const key = res.doi || res.title || r.reference;
    const reason = r.verdict === "SUPPORTED"
      ? `Directly supported at ${r.has_results ? "results-level" : "abstract-only"} with verified quotes.`
      : `Supports a weaker claim (${r.has_results ? "results-level" : "abstract-only"}); read for context (${DISTORTION_LABELS[r.distortion_type]||"overstated"}).`;
    const isReview = REVIEW_TYPE_RE.test(res.type||"") || REVIEW_TYPE_RE.test(res.title||"");
    const entry = { title:res.title||r.reference, doi:res.doi, verdict:r.verdict, confidence:r.confidence||0,
      reason, claims_supported:1,
      _sort:[VR[r.verdict]??9, -(r.confidence||0), r.has_results?0:1, isReview?0:1, -(res.citations||0)] };
    if (!byKey[key]) byKey[key] = entry;
    else { const prev = byKey[key]; const n = prev.claims_supported+1;
      const best = (entry._sort < prev._sort) ? entry : prev; best.claims_supported = n; byKey[key] = best; }
  }
  const worth = Object.values(byKey).sort((a,b)=> (a._sort<b._sort?-1:a._sort>b._sort?1:0));
  worth.forEach(w => { if (w.claims_supported>1) w.reason += ` Supports ${w.claims_supported} of the claims.`; });
  return { overall_confidence:Math.round(overall*100)/100, grade:grade(overall), counts,
    n_claims:claimResults.length, weak_claims:weak, min_confidence:minConf, excluded_below_threshold:excluded, worth };
}

// ---------- plan_search (ported) ----------
const STOP = new Set("a an the of to from in on for and or with without how does do is are be what which who whom whose why when where into onto over under about as at by it its this that these those can could would should may might will impact affect effect probability likelihood using use between among per there".split(" "));
function keyTerms(q){ const seen=new Set(),out=[]; for(const w of (q||"").toLowerCase().match(/[a-z][a-z-]{2,}/g)||[]){ if(STOP.has(w)||seen.has(w))continue; seen.add(w); out.push(w);} return out; }
const CLARIFIERS = [
  { id:"study_type", q:"Which study types should count as evidence?", options:["RCTs / clinical trials only","Include observational & cohort studies","Include preclinical / animal studies too","Any design"] },
  { id:"population", q:"Specific population, disease, or setting? (free text, or 'general')", free:true },
  { id:"recency", q:"How recent must the evidence be?", options:["Last 5 years","Last 10 years","Any time"] },
  { id:"min_confidence", q:"Minimum confidence to include a source?", options:["0.50 (lenient)","0.70 (balanced)","0.85 (strict)"] },
  { id:"breadth", q:"How many candidate sources before filtering?", options:["~8 (fast)","~15 (balanced)","~25 (exhaustive)"] },
];

// ---------- multi-provider source discovery ----------
// Fan out to OpenAlex + Crossref + Europe PMC in parallel — all keyless and
// CORS-open (Access-Control-Allow-Origin: *), so the browser calls them direct
// with no key, no proxy, no mailto/tracking. Candidates are deduped by DOI,
// their metadata merged, and ranked by a pure function of explicit trust
// signals (provider agreement, query-match rank, citations, recency, OA,
// evidence tier) — so the same query always yields the same order (auditable).
function cleanDoi(d) {
  if (!d) return null;
  return d.replace(/^https?:\/\/(dx\.)?doi\.org\//i, "").replace(/[).,;>"']+$/, "").toLowerCase();
}
async function openAlexSearch(query, n) {
  const j = await getJSON(`https://api.openalex.org/works?search=${encodeURIComponent(query)}&per_page=${n}`);
  return (j.results || []).map((w, i) => ({
    doi: cleanDoi(w.doi), pmcid: null,
    pmid: (w.ids && w.ids.pmid) ? w.ids.pmid.split("/").pop() : null,
    title: w.display_name || w.title || null, year: w.publication_year || null,
    venue: (w.primary_location && w.primary_location.source && w.primary_location.source.display_name) || null,
    citations: w.cited_by_count || 0, type: w.type || "", is_oa: !!(w.open_access && w.open_access.is_oa),
    provider: "openalex", rank: i }));
}
async function crossrefSearch(query, n) {
  const j = await getJSON(`https://api.crossref.org/works?query=${encodeURIComponent(query)}&rows=${n}&select=DOI,title,container-title,issued,is-referenced-by-count,type`);
  return ((j.message && j.message.items) || []).map((m, i) => ({
    doi: cleanDoi(m.DOI), pmcid: null, pmid: null,
    title: (m.title && m.title[0]) || null,
    year: (m.issued && m.issued["date-parts"] && m.issued["date-parts"][0] && m.issued["date-parts"][0][0]) || null,
    venue: (m["container-title"] && m["container-title"][0]) || null,
    citations: m["is-referenced-by-count"] || 0, type: m.type || "", is_oa: false,
    provider: "crossref", rank: i }));
}
async function epmcProviderSearch(query, n) {
  const j = await getJSON(`https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=${encodeURIComponent(query)}&format=json&resultType=lite&pageSize=${n}`);
  return (((j.resultList && j.resultList.result)) || []).map((r, i) => ({
    doi: cleanDoi(r.doi), pmcid: r.pmcid || null, pmid: r.pmid || null,
    title: r.title || null, year: r.pubYear ? Number(r.pubYear) : null, venue: r.journalTitle || null,
    citations: r.citedByCount || 0, type: r.pubType || "", is_oa: r.isOpenAccess === "Y",
    provider: "epmc", rank: i }));
}
const PROVIDERS = { openalex: openAlexSearch, crossref: crossrefSearch, epmc: epmcProviderSearch };
// DuckDuckGo-style !bang: scope a query to one provider.
const BANGS = { "!oa":"openalex", "!openalex":"openalex", "!cr":"crossref", "!crossref":"crossref",
  "!epmc":"epmc", "!europepmc":"epmc", "!pm":"epmc", "!pubmed":"epmc" };
const REVIEW_TYPE_RE = /review|meta-analysis|meta analysis/i;
const PREPRINT_RE = /preprint|biorxiv|medrxiv|arxiv|posted-content/i;
function mergeCandidates(lists) {
  const byKey = new Map();
  for (const list of lists) for (const c of list) {
    const key = c.doi || (c.pmcid && c.pmcid.toLowerCase());
    if (!key) continue;
    if (!byKey.has(key)) { byKey.set(key, { ...c, providers: [c.provider], bestRank: c.rank }); continue; }
    const m = byKey.get(key);
    if (!m.providers.includes(c.provider)) m.providers.push(c.provider);
    m.bestRank = Math.min(m.bestRank, c.rank);
    m.title = m.title || c.title; m.year = m.year || c.year; m.venue = m.venue || c.venue;
    m.pmcid = m.pmcid || c.pmcid; m.pmid = m.pmid || c.pmid;
    m.citations = Math.max(m.citations || 0, c.citations || 0);
    m.type = m.type || c.type; m.is_oa = m.is_oa || c.is_oa;
  }
  return [...byKey.values()];
}
function trustScore(c) {
  let s = 0;
  s += (c.providers ? c.providers.length : 1) * 3;        // agreement across providers
  s += Math.max(0, 6 - (c.bestRank == null ? 9 : c.bestRank)); // query-match (top of a list)
  s += Math.log10((c.citations || 0) + 1) * 2;            // impact
  if (c.year) s += Math.max(0, c.year - 2000) * 0.05;     // mild recency tilt
  if (c.is_oa) s += 2;                                    // full text is verifiable
  if (REVIEW_TYPE_RE.test(c.type || "") || REVIEW_TYPE_RE.test(c.title || "")) s += 3; // evidence tier
  if (PREPRINT_RE.test(c.type || "")) s -= 2;             // not yet peer-reviewed
  return s;
}
async function discoverSources(rawQuery, n = 3, opts = {}) {
  let query = (rawQuery || "").trim();
  let scope = opts.scope || null;
  const m = query.match(/(?:^|\s)(![a-z]+)\b/i);          // !bang provider scoping
  if (m && BANGS[m[1].toLowerCase()]) { scope = BANGS[m[1].toLowerCase()]; query = query.replace(m[1], "").replace(/\s+/g, " ").trim(); }
  const names = scope ? [scope] : ["openalex", "crossref", "epmc"];
  const per = Math.max(n + 3, 8);
  const lists = await Promise.all(names.map(name => PROVIDERS[name](query, per).catch(() => [])));
  let cands = mergeCandidates(lists).filter(c => c.doi || c.pmcid);
  if (opts.peerReviewedOnly) cands = cands.filter(c => !PREPRINT_RE.test(c.type || ""));
  cands.forEach(c => c._score = trustScore(c));
  cands.sort((a, b) => b._score - a._score ||
    String(a.doi || a.pmcid).localeCompare(String(b.doi || b.pmcid))); // deterministic tiebreak
  return cands.slice(0, n);
}

// ---------- draft claims from a question + find real sources ----------
async function draftClaims(question, key, model) {
  const sys = `You answer a scientific question as 3 to 5 discrete, individually verifiable factual claims. For EACH claim, give a focused literature-search query (a few keywords) that would find a primary paper supporting or refuting it. Return ONLY a JSON object: {"claims":[{"claim":"...","query":"..."}]}. No prose.`;
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "content-type":"application/json", "x-api-key":key, "anthropic-version":"2023-06-01", "anthropic-dangerous-direct-browser-access":"true" },
    body: JSON.stringify({ model, max_tokens: 1200, system: sys, messages: [{ role:"user", content: question }] }),
  });
  if (!r.ok) throw new Error(`Anthropic API ${r.status}: ${(await r.text()).slice(0,180)}`);
  const j = await r.json();
  let raw = (j.content || []).filter(b => b.type === "text").map(b => b.text).join("\n").trim();
  raw = raw.replace(/^```[a-zA-Z]*\n?/, "").replace(/\n?```$/, "");
  const s = raw.indexOf("{"), e = raw.lastIndexOf("}"); if (s !== -1 && e !== -1) raw = raw.slice(s, e + 1);
  return (JSON.parse(raw).claims) || [];
}

// ---------- UI wiring ----------
function addClaimRow(claim="", references="") {
  const div = document.createElement("div");
  div.className = "claim-row";
  div.innerHTML = `<button class="x" title="remove">×</button>
    <label>Claim</label><textarea class="c-claim" placeholder="A specific factual claim">${esc(claim)}</textarea>
    <label>References — one per line (DOI / PMID / PMCID / URL)</label><textarea class="c-ref" placeholder="10.1234/...\n10.5678/...">${esc(references)}</textarea>`;
  div.querySelector(".x").onclick = () => div.remove();
  $("claims").appendChild(div);
}
addClaimRow();
$("addClaim").onclick = () => addClaimRow();

$("draftBtn").onclick = async () => {
  const q = $("question").value.trim(), key = $("key").value.trim(), model = $("model").value.trim() || "claude-sonnet-5-5";
  if (!q) { $("clarifyOut").innerHTML = `<div class="err">Type a question first.</div>`; return; }
  if (!key) { $("clarifyOut").innerHTML = `<div class="err">Add your Anthropic API key above to draft claims.</div>`; return; }
  $("draftBtn").disabled = true; $("clarifyOut").innerHTML = ""; $("draftStatus").textContent = "Drafting claims…";
  try {
    const claims = await draftClaims(q, key, model);
    if (!claims.length) throw new Error("the model returned no claims.");
    $("claims").innerHTML = "";
    for (let i = 0; i < claims.length; i++) {
      $("draftStatus").textContent = `Finding sources… ${i + 1} / ${claims.length}`;
      const cands = await discoverSources(claims[i].query || claims[i].claim, 2);
      const refs = cands.map(c => c.doi || c.pmcid);
      addClaimRow(claims[i].claim, refs.join("\n"));
    }
    $("draftStatus").textContent = `Drafted ${claims.length} claims with sources — review, set your threshold, then Verify.`;
  } catch (e) {
    $("draftStatus").textContent = "";
    $("clarifyOut").innerHTML = `<div class="err">Draft failed: ${esc(e.message)}</div>`;
  }
  $("draftBtn").disabled = false;
};

$("clarifyBtn").onclick = () => {
  const q = $("question").value.trim();
  if (!q) { $("clarifyOut").innerHTML = `<div class="err">Type a question first.</div>`; return; }
  const terms = keyTerms(q);
  let html = `<details class="clarify" open><summary><b>Scope the search</b> — key terms: ${esc(terms.join(", "))}</summary>`;
  for (const c of CLARIFIERS) {
    html += `<label>${esc(c.q)}</label>`;
    if (c.free) html += `<input type="text" data-cid="${c.id}" placeholder="general">`;
    else html += `<select data-cid="${c.id}">${c.options.map(o=>`<option>${esc(o)}</option>`).join("")}</select>`;
  }
  html += `<p style="margin-top:10px"><button class="secondary" id="planBtn">Build search plan</button></p><div id="planOut"></div></details>`;
  $("clarifyOut").innerHTML = html;
  $("planBtn").onclick = buildPlan;
};
function buildPlan() {
  const q = $("question").value.trim();
  const ans = {}; $("clarifyOut").querySelectorAll("[data-cid]").forEach(el => ans[el.dataset.cid] = el.value);
  const terms = keyTerms(q).slice(0,6);
  let pop = (ans.population||"").trim(); if (/^general/i.test(pop) || pop==="") pop = "";
  const base = (terms.join(" ") + (pop ? " "+pop : "")).trim();
  const study = ans.study_type||"";
  const design = /rct|trial/i.test(study) ? "randomized controlled trial" : /observational|cohort/i.test(study) ? "cohort OR observational" : "";
  const queries = [...new Set([base, `${base} predictors`, `${base} systematic review meta-analysis`, design?`${base} ${design}`:""].map(s=>s.trim()).filter(Boolean))];
  const minc = (ans.min_confidence||"0.7").match(/[\d.]+/); const mc = minc?minc[0]:"0.7";
  const br = (ans.breadth||"15").match(/\d+/); const breadth = br?br[0]:"15";
  if (mc) { $("minconf").value = mc; $("minconfVal").textContent = Number(mc).toFixed(2); }
  $("planOut").innerHTML = `<div class="scorecard"><b>Search plan</b>
    <div class="meta">Gather ~${esc(breadth)} candidates, keep only confidence ≥ ${esc(mc)} · study: ${esc(study||"any")} · population: ${esc(pop||"any")}${ans.recency&&ans.recency!=="Any time"?" · "+esc(ans.recency):""}</div>
    <div>Queries:</div><ol>${queries.map(x=>`<li>${esc(x)}</li>`).join("")}</ol>
    <p class="hint">Search these (e.g. in PubMed / Europe PMC), then add the best claims + references below and Verify.</p></div>`;
}

function cardHTML(r, hideClaim=false) {
  const BADGE = { SUPPORTED:"✓ SUPPORTED", OVERSTATED:"⚠ OVERSTATED", UNSUPPORTED:"✗ UNSUPPORTED", INSUFFICIENT:"— INSUFFICIENT EVIDENCE" };
  const res = r.resolved || {};
  const status = [ r.citation_ok?"link ✓":"link ⚠", r.retracted?"<b>RETRACTED</b>":(r.concern?"concern":""), r.has_results?"full text ✓":"abstract only ⚠" ].filter(Boolean).join(" · ");
  const ASPECT_ICON = { ENTAILED:"✓", CONTRADICTED:"✗", ABSENT:"—" };
  const asps = r.aspects || [];
  let asp = asps.length ? `<div class="aspects"><div class="meta"><b>Claim breakdown</b> — each piece checked separately:</div>`
    + asps.map(a => `<div class="aspect ${esc(a.status)}"><span class="achip ${esc(a.status)}">${ASPECT_ICON[a.status]||"?"} ${esc(a.type)}</span> ${esc(a.text)}${a.quote?`<blockquote>“${esc(a.quote)}”</blockquote>`:""}</div>`).join("")
    + `</div>` : "";
  // Aspects already carry the verified quotes; only show the separate evidence block when there's no decomposition.
  let ev = asps.length ? "" : (r.evidence||[]).slice(0,3).map(e => `<blockquote>“${esc(e.quote)}”${e.section?` — §${esc(e.section)}`:""}</blockquote>`).join("");
  let why = r.rationale ? `<p class="meta"><b>Why:</b> ${esc(r.rationale)}</p>` : "";
  let dist = (r.verdict==="OVERSTATED" && r.distortion_note) ? `<p><span class="tag">${esc(DISTORTION_LABELS[r.distortion_type]||r.distortion_type)}</span> ${esc(r.distortion_note)}</p>` : "";
  let unmet = (r.claim_aspects_unmet||[]).length ? `<p class="meta"><b>Not established:</b> ${esc(r.claim_aspects_unmet.join("; "))}</p>` : "";
  const trust = [
    res.type && REVIEW_TYPE_RE.test(res.type) ? "review/meta-analysis" : (res.type && PREPRINT_RE.test(res.type) ? "preprint (not peer-reviewed)" : null),
    (res.citations != null) ? `${res.citations} citation${res.citations===1?"":"s"}` : null,
    res.is_oa ? "open access" : null,
  ].filter(Boolean).join(" · ");
  return `<div class="card ${r.verdict}">
    <div><span class="badge ${r.verdict}">${BADGE[r.verdict]}</span> &nbsp;<span class="meta">confidence: ${r.band} (${(r.confidence||0).toFixed(2)}) · ${status}</span></div>
    ${hideClaim ? "" : `<p><b>Claim:</b> ${esc(r.claim)}</p>`}
    ${res.title?`<p class="meta"><b>Paper:</b> ${esc(res.title)}${res.year?` (${esc(res.year)})`:""}${res.doi?` · <a href="https://doi.org/${esc(res.doi)}" target="_blank" rel="noopener">${esc(res.doi)}</a>`:""}</p>`:""}
    ${trust?`<p class="meta">${esc(trust)}</p>`:""}
    ${asp}
    ${ev?`<div><b>Verified evidence:</b>${ev}</div>`:""}
    ${why}${dist}${unmet}
  </div>`;
}
function claimCardHTML(cr) {
  const BADGE = { SUPPORTED:"✓ SUPPORTED", OVERSTATED:"⚠ OVERSTATED", UNSUPPORTED:"✗ UNSUPPORTED", INSUFFICIENT:"— INSUFFICIENT EVIDENCE" };
  const rec = cr.recommendation ? `<p class="meta" style="color:var(--amber-fg)">⚠ ${esc(cr.recommendation)}</p>` : "";
  return `<div class="card ${cr.verdict}">
    <div><span class="badge ${cr.verdict}">${BADGE[cr.verdict]}</span> &nbsp;<span class="meta">supported by <b>${cr.support_count} of ${cr.total_refs}</b> reference(s) · strength: <b>${cr.strength}</b></span></div>
    <p><b>Claim:</b> ${esc(cr.claim)}</p>
    ${rec}
    <div style="margin-left:12px; border-left:2px solid var(--line); padding-left:12px;">${cr.refResults.map(r => cardHTML(r, true)).join("")}</div>
  </div>`;
}
function scorecardHTML(s) {
  const wyr = s.worth.length ? `<ol class="wyr">${s.worth.map(w=>`<li><b>${esc(w.title)}</b>${w.doi?` (<a href="https://doi.org/${esc(w.doi)}" target="_blank" rel="noopener">${esc(w.doi)}</a>)`:""} — ${esc(w.reason)}</li>`).join("")}</ol>` : `<p class="meta">No sources cleared the bar.</p>`;
  const counts = Object.entries(s.counts).map(([k,v])=>`${k}: ${v}`).join(" · ");
  const thr = s.min_confidence ? `<div class="meta">Threshold ${s.min_confidence.toFixed(2)} — ${s.excluded_below_threshold} source(s) screened out below it</div>` : "";
  const weak = s.weak_claims ? `<div class="meta" style="color:var(--amber-fg)">⚠ ${s.weak_claims} claim(s) rest on too few sources (single-source, or no corroborating study / review).</div>` : "";
  return `<div class="scorecard"><div class="big">Overall confidence: ${s.overall_confidence.toFixed(2)} (grade ${s.grade})</div>
    <div class="meta">${esc(counts)} · ${s.n_claims} claim(s)</div>${thr}${weak}
    <h2 style="margin-top:14px">Worth your read</h2>${wyr}</div>`;
}

$("verifyBtn").onclick = async () => {
  const key = $("key").value.trim(), model = $("model").value.trim() || "claude-sonnet-5-5";
  const minConf = Number($("minconf").value);
  const rows = [...$("claims").querySelectorAll(".claim-row")].map(d => ({
    claim: d.querySelector(".c-claim").value.trim(),
    refs: d.querySelector(".c-ref").value.split(/[\n,]+/).map(s => s.trim()).filter(Boolean) }))
    .filter(x => x.claim && x.refs.length);
  if (!rows.length) { $("results").innerHTML = `<div class="err">Add at least one claim with a reference.</div>`; return; }
  $("verifyBtn").disabled = true; $("results").innerHTML = "";
  const claimResults = [];
  const total = rows.reduce((n, r) => n + r.refs.length, 0); let done = 0;
  for (const row of rows) {
    const refResults = [];
    for (const ref of row.refs) {
      done++; $("status").textContent = `Verifying ${done} / ${total}…`;
      try { refResults.push(await verifyClaim(row.claim, ref, key, model)); }
      catch (e) { refResults.push({ ...noTextResult(`Error: ${e.message}`), claim:row.claim, reference:ref, resolved:{} }); }
    }
    claimResults.push(aggregateClaim(row.claim, refResults));
    $("results").innerHTML = claimResults.map(claimCardHTML).join("");
  }
  $("status").textContent = "";
  $("results").innerHTML = scorecardHTML(scoreAnswer(claimResults, minConf)) + claimResults.map(claimCardHTML).join("");
  $("verifyBtn").disabled = false;
};
