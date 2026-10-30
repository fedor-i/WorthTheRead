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
// Preprint <-> published-journal linkage: a cited preprint that later cleared
// peer review is more trustworthy; one that never did is a mild caution.
async function biorxivPublished(doi) {
  for (const server of ["biorxiv", "medrxiv"]) {
    try {
      const j = await getJSON(`https://api.biorxiv.org/details/${server}/${doi}`);  // literal slashes in the DOI path
      const coll = j.collection || [];
      const pub = ((coll.length ? coll[coll.length - 1].published : "") || "");
      if (pub && pub.toUpperCase() !== "NA") return pub;
    } catch (e) { /* non-fatal */ }
  }
  return null;
}
function relationDoi(msg, key) {
  for (const r of ((msg.relation && msg.relation[key]) || []))
    if ((r["id-type"] || "").toLowerCase() === "doi" && r.id) return r.id;
  return null;
}
async function journalMeta(doi) {
  try {
    const m = (await getJSON(`https://api.crossref.org/works/${encodeURIComponent(doi)}`)).message;
    let year = null;
    for (const k of ["published-print","published-online","issued"]) {
      const dp = m[k] && m[k]["date-parts"]; if (dp && dp[0] && dp[0][0]) { year = dp[0][0]; break; }
    }
    return { doi, journal:(m["container-title"] && m["container-title"][0]) || null, year };
  } catch (e) { return { doi, journal:null, year:null }; }
}
async function preprintLinks(out, msg) {
  const doi = out.doi || "";
  out.is_preprint = (msg.type || "").toLowerCase().includes("posted-content") || doi.startsWith("10.1101/");
  if (out.is_preprint) {
    let pub = doi.startsWith("10.1101/") ? await biorxivPublished(doi) : null;
    pub = pub || relationDoi(msg, "is-preprint-of");
    if (pub) out.published_as = await journalMeta(pub);
  } else {
    const pre = relationDoi(msg, "has-preprint");
    if (pre) out.has_preprint = { doi: pre };
  }
}
async function resolve(ref) {
  const ids = parseReference(ref);
  const out = { ids, resolved:false, citation_ok:false, title:null, authors:null,
                year:null, journal:null, doi:ids.doi, pmcid:ids.pmcid, pmid:ids.pmid,
                retracted:false, concern:false, citations:null, type:null, is_oa:false,
                is_preprint:false, published_as:null, has_preprint:null, error:null };
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
    await preprintLinks(out, msg);
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
  let res = await resolve(reference);
  // Prefer the published version of record: if the cited reference is a
  // preprint that later cleared peer review, verify against the published paper.
  let verifiedFromPreprint = null;
  const pub = res.is_preprint ? res.published_as : null;
  if (pub && pub.doi) {
    const pubRes = await resolve(pub.doi);
    if (pubRes.resolved) { verifiedFromPreprint = res.doi; res = pubRes; }
  }
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
                citations:res.citations, type:res.type, is_oa:res.is_oa,
                is_preprint:res.is_preprint, published_as:res.published_as, has_preprint:res.has_preprint,
                verified_from_preprint: verifiedFromPreprint },
    verified_from_preprint: verifiedFromPreprint,
    has_results: fetched.has_results, sections_available: fetched.sections_available, open_access: fetched.open_access,
  });
  return result;
}

// ---------- the verify → revise loop (claims self-improve) ----------
// Given a claim and how its cited paper(s) actually verified, rewrite the claim
// so every part is supported by the quoted evidence — dropping/correcting any
// unsupported magnitude, population, direction, or causal scope. Returns the
// tightened claim text, or null if no key / no change.
async function reviseClaim(claim, refResults, key, model) {
  if (!key) return null;
  const findings = refResults.map(r => {
    const res = r.resolved || {};
    const quotes = (r.aspects || []).filter(a => a.quote)
      .map(a => `• [${a.status} ${a.type}] “${a.quote}”`)
      .concat((r.evidence || []).map(e => `• “${e.quote}”`)).slice(0, 6).join("\n");
    const unmet = (r.claim_aspects_unmet || []).join("; ");
    const dist = r.distortion_note ? ` Distortion: ${r.distortion_note}` : "";
    return `PAPER: ${res.title || r.reference} — verdict ${r.verdict}.${dist}\n`
      + `Verified from the text:\n${quotes || "(none)"}\nNot established by the text: ${unmet || "(none)"}`;
  }).join("\n\n");
  const sys = `You tighten a scientific CLAIM so that every part of it is directly supported by the cited paper(s). Using ONLY the verified findings provided, rewrite the claim to remove or correct anything the papers do not establish — an unsupported magnitude, the wrong or over-broad population, an unsupported direction, causal language the evidence doesn't justify, or any over-generalized scope. Stay specific and faithful; do not introduce new assertions and do not weaken it beyond what the evidence requires. Return ONLY the rewritten claim as one plain sentence — no preamble, no quotation marks.`;
  const user = `CLAIM:\n${claim}\n\nVERIFIED FINDINGS FROM THE CITED PAPER(S):\n${findings}\n\nRewrite the claim so it is fully supported by these findings.`;
  try {
    const r = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: { "content-type":"application/json", "x-api-key":key, "anthropic-version":"2023-06-01", "anthropic-dangerous-direct-browser-access":"true" },
      body: JSON.stringify({ model, max_tokens: 400, system: sys, messages: [{ role:"user", content: user }] }),
    });
    if (!r.ok) return null;
    const j = await r.json();
    let text = (j.content || []).filter(b => b.type === "text").map(b => b.text).join(" ").trim();
    text = text.replace(/^```[a-zA-Z]*\n?/, "").replace(/\n?```$/, "");
    text = text.replace(/^["'“”]+|["'“”]+$/g, "").replace(/\s+/g, " ").trim();
    return text || null;
  } catch { return null; }
}

// Broaden step: discover papers the claim doesn't already cite, verify the
// claim against them, and keep the substantive results — aiming to add `want`
// NEW *supporting* sources. Uses multi-provider discovery (OpenAlex + Crossref
// + Europe PMC). Returns the newly verified ref-results (marked discovered).
async function strengthenClaim(claim, refResults, key, model, want, status) {
  const have = new Set();
  for (const r of refResults) {
    const res = r.resolved || {};
    if (res.doi) have.add(res.doi.toLowerCase());
    if (res.pmcid) have.add(res.pmcid.toLowerCase());
  }
  let cands = [];
  try { cands = await discoverSources(claim, want + have.size + 6); } catch { return []; }
  const fresh = [];
  for (const c of cands) {
    const k = (c.doi || c.pmcid || "").toLowerCase();
    if (!k || have.has(k)) continue;
    have.add(k); fresh.push(c.doi || c.pmcid);
  }
  const out = []; let supporting = 0;
  for (const ref of fresh) {
    if (supporting >= want || out.length >= want + 4) break;   // stop once we have enough / cap tries
    if (status) status();
    let r;
    try { r = { ...(await verifyClaim(claim, ref, key, model)), discovered: true }; }
    catch { continue; }
    if (r.verdict === "INSUFFICIENT") continue;                // no retrievable text -> skip as noise
    out.push(r);
    if (r.verdict === "SUPPORTED") supporting++;
  }
  return out;
}

// The self-improvement loop for one claim. For EVERY claim it (b) discovers
// `want` (2-3) corroborating papers and verifies the claim against them, and
// (a) rewrites an OVERSTATED/UNSUPPORTED claim to match the evidence — looping
// until the claim is supported by enough independent sources, or rounds run out.
async function verifyRowWithLoop(row, key, model, maxRounds, opts, status) {
  const { revise = true, broaden = true, want = 2 } = opts || {};
  let claim = row.claim;
  const trail = [];
  const addedRefs = [];
  let refResults = [];
  for (const ref of row.refs) {
    if (status) status(0, "verifying");
    try { refResults.push(await verifyClaim(claim, ref, key, model)); }
    catch (e) { refResults.push({ ...noTextResult(`Error: ${e.message}`), claim, reference:ref, resolved:{} }); }
  }
  const discoveredSupporting = () =>
    refResults.filter(r => r.discovered && r.verdict === "SUPPORTED").length;

  for (let round = 0; round <= maxRounds; round++) {
    const agg = aggregateClaim(claim, refResults);
    trail.push({ claim, verdict: agg.verdict, strength: agg.strength,
                 support_count: agg.support_count, total_refs: agg.total_refs });
    const needRevise = revise && (agg.verdict === "OVERSTATED" || agg.verdict === "UNSUPPORTED");
    const needBroaden = broaden && discoveredSupporting() < want;   // every claim seeks `want` corroborating sources
    if ((!needRevise && !needBroaden) || round >= maxRounds || !key) break;
    let changed = false;

    // (a) revise an overstated / unsupported claim, then re-verify current refs.
    if (needRevise) {
      if (status) status(round, "revising the claim");
      const revised = await reviseClaim(claim, refResults, key, model);
      if (revised && norm(revised) !== norm(claim)) {
        claim = revised;
        const re = [];
        for (const r of refResults) {
          try { re.push({ ...(await verifyClaim(claim, r.reference, key, model)), discovered: r.discovered }); }
          catch { re.push(r); }
        }
        refResults = re; changed = true;
      }
    }

    // (b) broaden: find corroborating papers (for every claim, until `want` support it).
    if (needBroaden) {
      if (status) status(round, "finding corroborating sources");
      const fresh = await strengthenClaim(claim, refResults, key, model,
                                          want - discoveredSupporting(), () => status && status(round, "finding corroborating sources"));
      if (fresh.length) {
        refResults = refResults.concat(fresh);
        for (const f of fresh) addedRefs.push(f.reference);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const agg = aggregateClaim(claim, refResults);
  agg.revisions = trail;
  agg.original_claim = row.claim;
  agg.revised = norm(claim) !== norm(row.claim);
  agg.added_refs = addedRefs;
  return agg;
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

// ---------- multi-provider, iterative source discovery ----------
// Round 1 fans out to OpenAlex + Crossref + Europe PMC in parallel — all keyless
// and CORS-open (Access-Control-Allow-Origin: *), so the browser calls them
// direct with no key, no proxy, no mailto/tracking. Round 2 follows the citation
// trail of the strongest hits (their referenced + related works) and folds in
// papers co-cited by several seeds, lifting recall the way an agent-style search
// does. Candidates are deduped by DOI, metadata merged, and ranked by a pure
// function of explicit trust signals (provider agreement, query-match rank,
// co-citation, citations, recency, OA, evidence tier) — so the same query always
// yields the same order (auditable). Retracted papers are dropped by default.
const OA_SELECT = "id,doi,display_name,title,publication_year,primary_location," +
  "cited_by_count,type,open_access,is_retracted,ids,referenced_works,related_works";
function cleanDoi(d) {
  if (!d) return null;
  return d.replace(/^https?:\/\/(dx\.)?doi\.org\//i, "").replace(/[).,;>"']+$/, "").toLowerCase();
}
function oaCandidate(w, provider, rank) {
  return {
    doi: cleanDoi(w.doi), pmcid: null,
    pmid: (w.ids && w.ids.pmid) ? w.ids.pmid.split("/").pop() : null,
    title: w.display_name || w.title || null, year: w.publication_year || null,
    venue: (w.primary_location && w.primary_location.source && w.primary_location.source.display_name) || null,
    citations: w.cited_by_count || 0, type: w.type || "", is_oa: !!(w.open_access && w.open_access.is_oa),
    retracted: !!w.is_retracted, provider, rank, cocite: 0,
    _oaid: (w.id || "").split("/").pop() || null,
    _refs: w.referenced_works || [], _related: w.related_works || [] };
}
async function openAlexSearch(query, n) {
  const j = await getJSON(`https://api.openalex.org/works?search=${encodeURIComponent(query)}&per_page=${n}&select=${OA_SELECT}`);
  return (j.results || []).map((w, i) => oaCandidate(w, "openalex", i));
}
// Batch-fetch OpenAlex works by id — the citation-trail candidates.
async function openAlexFetchIds(shortIds, cocite) {
  const out = [];
  for (let i = 0; i < shortIds.length; i += 50) {   // OR filter takes up to 50 ids
    const chunk = shortIds.slice(i, i + 50);
    let j;
    try { j = await getJSON(`https://api.openalex.org/works?filter=openalex_id:${encodeURIComponent(chunk.join("|"))}&per_page=50&select=${OA_SELECT}`); }
    catch (e) { continue; }
    for (const w of (j.results || [])) {
      const c = oaCandidate(w, "openalex-cite", 99);
      c.cocite = cocite.get((w.id || "").split("/").pop()) || 1;
      out.push(c);
    }
  }
  return out;
}
async function crossrefSearch(query, n) {
  const j = await getJSON(`https://api.crossref.org/works?query=${encodeURIComponent(query)}&rows=${n}&select=DOI,title,container-title,issued,is-referenced-by-count,type`);
  return ((j.message && j.message.items) || []).map((m, i) => ({
    doi: cleanDoi(m.DOI), pmcid: null, pmid: null,
    title: (m.title && m.title[0]) || null,
    year: (m.issued && m.issued["date-parts"] && m.issued["date-parts"][0] && m.issued["date-parts"][0][0]) || null,
    venue: (m["container-title"] && m["container-title"][0]) || null,
    citations: m["is-referenced-by-count"] || 0, type: m.type || "", is_oa: false,
    retracted: false, provider: "crossref", rank: i, cocite: 0 }));
}
async function epmcProviderSearch(query, n) {
  const j = await getJSON(`https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=${encodeURIComponent(query)}&format=json&resultType=lite&pageSize=${n}`);
  return (((j.resultList && j.resultList.result)) || []).map((r, i) => ({
    doi: cleanDoi(r.doi), pmcid: r.pmcid || null, pmid: r.pmid || null,
    title: r.title || null, year: r.pubYear ? Number(r.pubYear) : null, venue: r.journalTitle || null,
    citations: r.citedByCount || 0, type: r.pubType || "", is_oa: r.isOpenAccess === "Y",
    retracted: false, provider: "epmc", rank: i, cocite: 0 }));
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
    m.retracted = m.retracted || c.retracted;
    m.cocite = Math.max(m.cocite || 0, c.cocite || 0);
    for (const k of ["_oaid", "_refs", "_related"]) if (c[k] && !m[k]) m[k] = c[k];
  }
  return [...byKey.values()];
}
function trustScore(c) {
  let s = 0;
  const kw = (c.providers || []).filter(p => p !== "openalex-cite").length; // keyword agreement
  s += kw * 3;
  s += Math.max(0, 6 - (c.bestRank == null ? 9 : c.bestRank)); // query-match (top of a list)
  s += Math.min(c.cocite || 0, 4) * 2.5;                  // co-cited by strong seeds
  s += Math.log10((c.citations || 0) + 1) * 2;            // impact
  if (c.year) s += Math.max(0, c.year - 2000) * 0.05;     // mild recency tilt
  if (c.is_oa) s += 2;                                    // full text is verifiable
  if (REVIEW_TYPE_RE.test(c.type || "") || REVIEW_TYPE_RE.test(c.title || "")) s += 3; // evidence tier
  if (PREPRINT_RE.test(c.type || "")) s -= 2;             // not yet peer-reviewed
  return s;
}
// Retracted papers always sort last (even when a caller includes them), then by
// score, then a stable id tiebreak.
function sortCands(a, b) {
  return (a.retracted ? 1 : 0) - (b.retracted ? 1 : 0) ||
    b._score - a._score ||
    String(a.doi || a.pmcid).localeCompare(String(b.doi || b.pmcid));
}
// Collect citation-trail ids from the top seeds, counting how many point at each.
function citationTrail(ranked, seedK) {
  const cocite = new Map();
  for (const c of ranked.slice(0, seedK)) {
    if (!c._oaid) continue;
    const seen = new Set();
    for (const oid of [...(c._refs || []), ...(c._related || [])]) {
      const sid = (oid || "").split("/").pop();
      if (sid && !seen.has(sid)) { seen.add(sid); cocite.set(sid, (cocite.get(sid) || 0) + 1); }
    }
  }
  return cocite;
}
async function discoverSources(rawQuery, n = 3, opts = {}) {
  let query = (rawQuery || "").trim();
  let scope = opts.scope || null;
  const m = query.match(/(?:^|\s)(![a-z]+)\b/i);          // !bang provider scoping
  if (m && BANGS[m[1].toLowerCase()]) { scope = BANGS[m[1].toLowerCase()]; query = query.replace(m[1], "").replace(/\s+/g, " ").trim(); }
  if (!query) return [];
  const rounds = opts.rounds == null ? 2 : opts.rounds;
  const names = scope ? [scope] : ["openalex", "crossref", "epmc"];
  const per = Math.max(n + 3, 8);
  const lists = await Promise.all(names.map(name => PROVIDERS[name](query, per).catch(() => [])));
  let cands = mergeCandidates(lists);
  cands.forEach(c => c._score = trustScore(c));
  cands.sort(sortCands);
  // Round 2: follow the citation trail of the strongest OpenAlex hits.
  if (rounds >= 2 && !scope && names.includes("openalex")) {
    const cocite = citationTrail(cands, Math.min(4, cands.length));
    // Require co-citation by >=2 seeds (the citation analog of provider
    // agreement) — single-seed trail hits are the main source of topic drift.
    const ids = [...cocite.entries()].filter(([, v]) => v >= 2)
      .sort((a, b) => b[1] - a[1]).map(([k]) => k).slice(0, 50);
    if (ids.length) {
      const trail = await openAlexFetchIds(ids, cocite).catch(() => []);
      cands = mergeCandidates([...lists, trail]);
      cands.forEach(c => c._score = trustScore(c));
      cands.sort(sortCands);
    }
  }
  cands = cands.filter(c => c.doi || c.pmcid);
  if (!opts.includeRetracted) cands = cands.filter(c => !c.retracted);
  if (opts.peerReviewedOnly) cands = cands.filter(c => !PREPRINT_RE.test(c.type || ""));
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

// ---------- audit a paper's own citations ----------
// Pull a paper's introduction (Europe PMC JATS, CORS-open), and for each
// sentence that cites a reference, verify the claim against the paper it
// actually cites (inline <xref> links) — then highlight the intro by verdict.
const _ln = (el) => el.tagName.split(":").pop();
async function fetchJATS(pmcid) {
  pmcid = pmcid.toUpperCase().startsWith("PMC") ? pmcid.toUpperCase() : "PMC" + pmcid;
  const r = await fetch(`https://www.ebi.ac.uk/europepmc/webservices/rest/${pmcid}/fullTextXML`);
  if (!r.ok) return null;
  const doc = new DOMParser().parseFromString(await r.text(), "application/xml");
  return doc.querySelector("parsererror") ? null : doc;
}
function auditRefMap(doc) {
  const map = {};
  for (const ref of doc.getElementsByTagName("ref")) {
    const rid = ref.getAttribute("id"); if (!rid) continue;
    const at = ref.getElementsByTagName("article-title")[0];
    let doi = null;
    for (const pid of ref.getElementsByTagName("pub-id"))
      if (pid.getAttribute("pub-id-type") === "doi") doi = (pid.textContent || "").trim();
    map[rid] = { title: at ? at.textContent.replace(/\s+/g, " ").trim() : null, doi };
  }
  return map;
}
function flattenPara(el) {
  let s = "";
  for (const node of el.childNodes) {
    if (node.nodeType === 3) s += node.nodeValue;
    else if (node.nodeType === 1) {
      if (_ln(node) === "xref" && node.getAttribute("ref-type") === "bibr")
        s += ` [[${node.getAttribute("rid") || ""}]] `;
      else s += flattenPara(node);
    }
  }
  return s;
}
function findSectionEl(doc, kw) {
  kw = kw.toLowerCase(); let fallback = null;
  for (const sec of doc.getElementsByTagName("sec")) {
    let title = "";
    for (const ch of sec.children) if (_ln(ch) === "title") { title = ch.textContent.trim().toLowerCase(); break; }
    if (title.includes(kw)) return sec;
    if (!fallback && (title === "" || title === "background")) fallback = sec;
  }
  return fallback;
}
const SENT_RE = /(?<=[.!?])\s+(?=[A-Z0-9])/;
function segmentSection(sec, refMap, maxClaims) {
  const claims = [], paragraphs = [];
  for (const p of sec.getElementsByTagName("p")) {
    const para = [];
    for (const sent of flattenPara(p).split(SENT_RE)) {
      const rids = []; let m; const re = /\[\[([^\]]*)\]\]/g;
      while ((m = re.exec(sent))) for (const r of m[1].split(/\s+/)) if (r) rids.push(r);
      const text = sent.replace(/\[\[[^\]]*\]\]/g, "").replace(/\s+/g, " ").trim();
      if (!text) continue;
      const seg = { text, claim_idx: null };
      if (rids.length && text.length >= 45 && claims.length < maxClaims) {
        const cited = [...new Set(rids)].map(r => ({ rid: r, ...(refMap[r] || { title: null, doi: null }) }));
        seg.claim_idx = claims.length;
        claims.push({ claim: text, cited });
      }
      para.push(seg);
    }
    if (para.length) paragraphs.push(para);
  }
  return { claims, paragraphs };
}
async function doiFromTitle(title) {
  if (!title) return null;
  try {
    const j = await getJSON(`https://api.crossref.org/works?query.bibliographic=${encodeURIComponent(title)}&rows=1&select=DOI`);
    const items = (j.message && j.message.items) || [];
    return items.length ? cleanDoi(items[0].DOI) : null;
  } catch (e) { return null; }
}
const _AV = (v) => v === "SKIPPED" ? "INSUFFICIENT" : v;   // color class
function auditHTML(out) {
  const claims = out.claims, p = out.paper, counts = {};
  claims.forEach(c => counts[c.verdict] = (counts[c.verdict] || 0) + 1);
  const summ = Object.entries(counts).map(([k, v]) => `<span class="badge ${_AV(k)}">${esc(k)} ${v}</span>`).join(" ");
  const prose = out.paragraphs.map(para => "<p>" + para.map(seg => {
    const t = esc(seg.text);
    if (seg.claim_idx != null && seg.claim_idx < claims.length) {
      const v = claims[seg.claim_idx].verdict || "SKIPPED";
      return `<span class="aclaim ${_AV(v)}" title="${esc(v)} — cites ${esc((claims[seg.claim_idx].verified_against||{}).title || (claims[seg.claim_idx].verified_against||{}).doi || "?")}">${t}<sup>${seg.claim_idx + 1}</sup></span>`;
    }
    return t;
  }).join(" ") + "</p>").join("");
  const detail = claims.map((c, i) => {
    const v = c.verdict || "SKIPPED", va = c.verified_against || {};
    const src = va.doi ? `<a href="https://doi.org/${esc(va.doi)}" target="_blank" rel="noopener">${esc(va.title || va.doi)}</a>` : esc(va.title || c.note || "unresolved");
    const why = c.rationale ? `<p class="meta">${esc(c.rationale)}</p>` : "";
    const sg = c.suggestion;
    const sugline = sg ? `<p class="meta">&rarr; <b>suggested source</b> [${esc(sg.verdict)}]: ${esc(sg.title || sg.doi || "?")}${sg.doi ? ` · <a href="https://doi.org/${esc(sg.doi)}" target="_blank" rel="noopener">${esc(sg.doi)}</a>` : ""}</p>` : "";
    return `<div class="card ${_AV(v)}"><div><span class="badge ${_AV(v)}">${i + 1}. ${esc(v)}</span></div><p><b>Claim:</b> ${esc(c.claim)}</p><p class="meta">cites: ${src}</p>${why}${sugline}</div>`;
  }).join("");
  return `<div class="scorecard"><div class="big">${esc(p.title || p.doi || p.pmcid || "paper")}</div>
    <p class="meta">${p.doi ? `<a href="https://doi.org/${esc(p.doi)}" target="_blank" rel="noopener">${esc(p.doi)}</a> · ` : ""}introduction · ${summ}</p>
    <p class="meta">Highlights are claims that cite a reference, coloured by whether that reference supports them.</p></div>
    <div class="auditprose">${prose}</div>
    <h2>Claim by claim</h2>${detail}`;
}
async function auditPaperFlow() {
  const ref = $("auditRef").value.trim(), key = $("key").value.trim(), model = $("model").value.trim() || "claude-sonnet-5-5";
  const maxClaims = Math.max(1, Math.min(20, parseInt($("auditMax").value) || 8));
  const doFix = $("auditFix") && $("auditFix").checked;
  if (!ref) { $("auditOut").innerHTML = `<div class="err">Enter a paper DOI / PMID / PMCID.</div>`; return; }
  if (!key) { $("auditOut").innerHTML = `<div class="err">Add your Anthropic API key above.</div>`; return; }
  $("auditBtn").disabled = true; $("auditOut").innerHTML = ""; $("auditStatus").textContent = "Resolving paper…";
  try {
    const res = await resolve(ref);
    if (!res.pmcid) throw new Error("No open-access full text with citation links found for this paper (Europe PMC).");
    $("auditStatus").textContent = "Fetching full text…";
    const doc = await fetchJATS(res.pmcid);
    if (!doc) throw new Error("Full-text JATS could not be retrieved from Europe PMC.");
    const sec = findSectionEl(doc, "introduc");
    if (!sec) throw new Error("No introduction section found in this paper.");
    const { claims, paragraphs } = segmentSection(sec, auditRefMap(doc), maxClaims);
    if (!claims.length) throw new Error("No cited claims found in the introduction.");
    for (let i = 0; i < claims.length; i++) {
      $("auditStatus").textContent = `Verifying claim ${i + 1} / ${claims.length}…`;
      const c = claims[i];
      let primary = c.cited.find(x => x.doi);
      if (!primary) { const t = c.cited.find(x => x.title); if (t) { t.doi = await doiFromTitle(t.title); primary = t.doi ? t : null; } }
      if (!primary) { c.verdict = "SKIPPED"; c.note = "cited reference could not be resolved to a DOI"; continue; }
      const r = await verifyClaim(c.claim, primary.doi, key, model);
      c.verified_against = primary; c.verdict = r.verdict; c.confidence = r.confidence;
      c.rationale = r.rationale; c.has_results = r.has_results;
      if (doFix && c.verdict !== "SUPPORTED") {
        $("auditStatus").textContent = `Finding a better source for claim ${i + 1}…`;
        try {
          const sg = await suggestSources(c.claim, key, model, 1, primary.doi, 6);
          c.suggestion = sg.suggested.find(s => s.verdict === "SUPPORTED") || null;
        } catch (e) { /* non-fatal */ }
      }
    }
    $("auditStatus").textContent = "";
    $("auditOut").innerHTML = auditHTML({ paper: { title: res.title, doi: res.doi, pmcid: res.pmcid }, claims, paragraphs });
  } catch (e) {
    $("auditStatus").textContent = "";
    $("auditOut").innerHTML = `<div class="err">Audit failed: ${esc(e.message)}</div>`;
  }
  $("auditBtn").disabled = false;
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
  const status = [ r.discovered?"🔎 found by loop":"", r.citation_ok?"link ✓":"link ⚠", r.retracted?"<b>RETRACTED</b>":(r.concern?"concern":""), r.has_results?"full text ✓":"abstract only ⚠" ].filter(Boolean).join(" · ");
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
  const pub = res.published_as;
  const pre = res.verified_from_preprint;
  let preLine = "";
  if (pre) {
    preLine = `<p class="meta">✅ verified against the published version of record — cited reference was a preprint · <a href="https://doi.org/${esc(pre)}" target="_blank" rel="noopener">${esc(pre)}</a></p>`;
  } else if (pub && pub.journal) {
    const yr = pub.year ? ` (${esc(String(pub.year))})` : "";
    const link = pub.doi ? ` · <a href="https://doi.org/${esc(pub.doi)}" target="_blank" rel="noopener">${esc(pub.doi)}</a>` : "";
    preLine = `<p class="meta">📄 preprint — later published in <i>${esc(pub.journal)}</i>${yr}${link}</p>`;
  } else if (res.is_preprint) {
    preLine = `<p class="meta">📄 preprint — no peer-reviewed journal version found yet</p>`;
  } else if (res.has_preprint && res.has_preprint.doi) {
    preLine = `<p class="meta">📄 has a preprint · <a href="https://doi.org/${esc(res.has_preprint.doi)}" target="_blank" rel="noopener">${esc(res.has_preprint.doi)}</a></p>`;
  }
  return `<div class="card ${r.verdict}">
    <div><span class="badge ${r.verdict}">${BADGE[r.verdict]}</span> &nbsp;<span class="meta">confidence: ${r.band} (${(r.confidence||0).toFixed(2)}) · ${status}</span></div>
    ${hideClaim ? "" : `<p><b>Claim:</b> ${esc(r.claim)}</p>`}
    ${res.title?`<p class="meta"><b>Paper:</b> ${esc(res.title)}${res.year?` (${esc(res.year)})`:""}${res.doi?` · <a href="https://doi.org/${esc(res.doi)}" target="_blank" rel="noopener">${esc(res.doi)}</a>`:""}</p>`:""}
    ${trust?`<p class="meta">${esc(trust)}</p>`:""}
    ${preLine}
    ${asp}
    ${ev?`<div><b>Verified evidence:</b>${ev}</div>`:""}
    ${why}${dist}${unmet}
  </div>`;
}
function claimCardHTML(cr) {
  const BADGE = { SUPPORTED:"✓ SUPPORTED", OVERSTATED:"⚠ OVERSTATED", UNSUPPORTED:"✗ UNSUPPORTED", INSUFFICIENT:"— INSUFFICIENT EVIDENCE" };
  const rec = cr.recommendation ? `<p class="meta" style="color:var(--amber-fg)">⚠ ${esc(cr.recommendation)}</p>` : "";
  const nAdded = (cr.added_refs || []).length;
  const tags = [
    cr.revised ? `<span class="tag">auto-revised</span>` : "",
    nAdded ? `<span class="tag">+${nAdded} corroborating source${nAdded===1?"":"s"} found</span>` : "",
  ].filter(Boolean).join(" ");
  const tagStr = tags ? ` · ${tags}` : "";
  // Show the trail when the claim text changed OR support grew over the rounds.
  const rv = cr.revisions || [];
  const showTrail = rv.length > 1 && (cr.revised || rv.some(s => s.support_count !== rv[0].support_count || s.total_refs !== rv[0].total_refs));
  let trail = "";
  if (showTrail) {
    trail = `<div class="revtrail"><div class="meta"><b>Self-improvement loop:</b></div>`
      + rv.map((s, i) => `${i > 0 ? `<div class="revarrow">↓ ${cr.revised && norm(s.claim) !== norm(rv[i-1].claim) ? "revised claim" : "added sources"}</div>` : ""}`
          + `<div class="revstep ${i === rv.length - 1 ? "final" : ""}">`
          + `<span class="badge ${s.verdict}" style="font-size:.68rem">${BADGE[s.verdict]}</span> `
          + `<span class="meta">${s.support_count}/${s.total_refs} supporting · ${esc(s.strength)}</span><br>${esc(s.claim)}</div>`).join("")
      + `</div>`;
  }
  return `<div class="card ${cr.verdict}">
    <div><span class="badge ${cr.verdict}">${BADGE[cr.verdict]}</span> &nbsp;<span class="meta">supported by <b>${cr.support_count} of ${cr.total_refs}</b> reference(s) · strength: <b>${cr.strength}</b>${tagStr}</span></div>
    <p><b>Claim:</b> ${esc(cr.claim)}</p>
    ${trail}
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
  const autoRevise = $("autoRevise").checked;
  const autoBroaden = $("autoBroaden").checked;
  const loopOn = autoRevise || autoBroaden;
  const maxRounds = loopOn ? Math.max(1, Math.min(4, Number($("maxRounds").value) || 2)) : 0;
  const rows = [...$("claims").querySelectorAll(".claim-row")].map(d => ({
    el: d,
    claim: d.querySelector(".c-claim").value.trim(),
    refs: d.querySelector(".c-ref").value.split(/[\n,]+/).map(s => s.trim()).filter(Boolean) }))
    .filter(x => x.claim && x.refs.length);
  if (!rows.length) { $("results").innerHTML = `<div class="err">Add at least one claim with a reference.</div>`; return; }
  if (loopOn && !key) { $("results").innerHTML = `<div class="err">Auto-revise / find-sources needs your Anthropic key (it rewrites claims and judges newly found papers). Add it above, or uncheck both.</div>`; return; }
  $("verifyBtn").disabled = true; $("results").innerHTML = "";
  const opts = { revise: autoRevise, broaden: autoBroaden, want: 2 };
  const claimResults = [];
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    const cr = await verifyRowWithLoop(row, key, model, maxRounds, opts,
      (round, phase) => { $("status").textContent =
        `Claim ${i + 1} / ${rows.length} — ${phase || "verifying"}${round > 0 ? ` (round ${round})` : ""}…`; });
    // Write the final (possibly tightened) claim back into the editor so the
    // claims the user keeps are the improved ones.
    if (cr.revised && row.el) row.el.querySelector(".c-claim").value = cr.claim;
    claimResults.push(cr);
    $("results").innerHTML = claimResults.map(claimCardHTML).join("");
  }
  $("status").textContent = "";
  $("results").innerHTML = scorecardHTML(scoreAnswer(claimResults, minConf)) + claimResults.map(claimCardHTML).join("");
  $("verifyBtn").disabled = false;
};

$("auditBtn").onclick = auditPaperFlow;

// ---------- mode toggle ----------
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === t));
  const m = t.dataset.mode;
  document.querySelectorAll(".mode").forEach(sec => { sec.hidden = sec.id !== "mode-" + m; });
});

// ---------- fix a claim: find supporting sources ----------
const FIX_STOP = new Set(("a an the of in on for to and or but with without is are was were be been being this that these those by from as at into than then so not no any all can could may might also more most using used use via per between within across their its it they we our new recent here show shows showed suggest suggests found find results result study paper enables enable enabling provides provide providing protects protect protecting acts act acting plays play role roles function functions mechanism mechanisms process processes reaction centre center effect effects increase increases reduce reduces improve improves associated related secondary primary pathway pathways involved involves").split(" "));
function fixKeywords(text, k = 6) {
  const seen = new Set(), terms = [];
  for (const w of ((text || "").toLowerCase().match(/[a-z][a-z0-9-]{2,}/g) || [])) {
    if (FIX_STOP.has(w) || seen.has(w)) continue;
    seen.add(w); terms.push(w);
  }
  terms.sort((a, b) => {
    const da = (a.includes("-") || /\d/.test(a)) ? 1 : 0, db = (b.includes("-") || /\d/.test(b)) ? 1 : 0;
    return db - da || b.length - a.length;
  });
  return terms.slice(0, k).join(" ");
}
async function suggestSources(claim, key, model, n, exclude, breadth) {
  const skip = (exclude || "").toLowerCase().replace("doi:", "").trim();
  let cands = await discoverSources(claim, breadth);
  const seen = new Set(cands.map(c => (c.doi || c.pmcid || "").toLowerCase()));
  const kw = fixKeywords(claim);
  if (kw && kw !== claim.toLowerCase()) {
    for (const c of await discoverSources(kw, breadth)) {
      const k = (c.doi || c.pmcid || "").toLowerCase();
      if (k && !seen.has(k)) { seen.add(k); cands.push(c); }
    }
  }
  cands.sort((a, b) => (b._score || 0) - (a._score || 0));
  const found = []; let checked = 0;
  for (const c of cands) {
    const ref = c.doi || c.pmcid;
    if (!ref || ref.toLowerCase() === skip || checked >= breadth) continue;
    checked++;
    $("fixStatus").textContent = `Verifying candidate ${checked}…`;
    const r = await verifyClaim(claim, ref, key, model);
    if (["SUPPORTED", "OVERSTATED"].includes(r.verdict))
      found.push({ doi: c.doi, title: (r.resolved || {}).title || c.title, year: c.year,
                   verdict: r.verdict, confidence: r.confidence, has_results: r.has_results });
    if (found.filter(x => x.verdict === "SUPPORTED").length >= n) break;
  }
  found.sort((a, b) => ((a.verdict !== "SUPPORTED") - (b.verdict !== "SUPPORTED")) || (b.confidence || 0) - (a.confidence || 0));
  return { checked, suggested: found.slice(0, n + 2) };
}
$("fixBtn").onclick = async () => {
  const claim = $("fixClaim").value.trim(), key = $("key").value.trim(), model = $("model").value.trim() || "claude-sonnet-5-5";
  const exclude = $("fixExclude").value.trim();
  const breadth = Math.max(3, Math.min(16, parseInt($("fixBreadth").value) || 8));
  if (!claim) { $("fixOut").innerHTML = `<div class="err">Enter a claim.</div>`; return; }
  if (!key) { $("fixOut").innerHTML = `<div class="err">Add your Anthropic API key above.</div>`; return; }
  $("fixBtn").disabled = true; $("fixOut").innerHTML = ""; $("fixStatus").textContent = "Searching for candidate papers…";
  try {
    const r = await suggestSources(claim, key, model, 3, exclude, breadth);
    $("fixStatus").textContent = "";
    if (!r.suggested.length) {
      $("fixOut").innerHTML = `<div class="meta">Checked ${r.checked} papers — none supported the claim. Try rephrasing, or raise the breadth.</div>`;
    } else {
      $("fixOut").innerHTML = `<p class="meta">Checked ${r.checked} papers · ${r.suggested.length} suggestion(s), best first:</p>` +
        r.suggested.map(s => `<div class="card ${s.verdict}"><div><span class="badge ${s.verdict}">${s.verdict}</span> <span class="meta">confidence ${(s.confidence || 0).toFixed(2)}${s.has_results ? " · full text" : " · abstract"}</span></div>
        <p>${esc(s.title || "(untitled)")}${s.year ? ` (${esc(String(s.year))})` : ""}</p>
        ${s.doi ? `<p class="meta"><a href="https://doi.org/${esc(s.doi)}" target="_blank" rel="noopener">${esc(s.doi)}</a></p>` : ""}</div>`).join("");
    }
  } catch (e) { $("fixStatus").textContent = ""; $("fixOut").innerHTML = `<div class="err">Search failed: ${esc(e.message)}</div>`; }
  $("fixBtn").disabled = false;
};
