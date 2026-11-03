# Worth Your Read for Zotero

A Zotero 7–10 plugin that answers questions about your papers and checks your
citations, grounded in the papers' own text: every answer comes with verbatim
quotes that are machine-checked against the paper, a confidence score, and a
link to the page. All reading and judging happens on your machine in the
[Worth Your Read sidecar](../README.md#local-sidecar-for-the-zotero-plugin)
with a local Ollama model; the plugin only talks to `127.0.0.1`.

## What it does

**Read** — a *Worth Your Read* section in the item pane (library and PDF reader):

- **Ask** a question about the paper → short claims, each with the passages it
  rests on and their page. Claims whose passage isn't found in the paper are
  marked, not hidden.
- **Verify claim** → SUPPORTED / OVERSTATED / UNSUPPORTED / INSUFFICIENT, with the
  quote and page behind each part of the claim. An overstated claim gets a
  **Reword to match the evidence** button.
- **Summarize → note** saves a grounded summary as a child note.
- Select text in a PDF → **Verify with Worth Your Read** in the selection popup.
- Click any `p. N` link to open the PDF at that page.
- **Highlight in PDF** turns the verified passages into ordinary Zotero
  highlights, with the claim in the comment: green = supports the claim,
  yellow = supports only a weaker version (the claim is overstated), red =
  contradicts it, blue = a passage behind an answer or summary. They're
  **temporary**: removed at your next action, when you close that PDF, or at the
  next Zotero start — click **Keep highlights** to make them permanent (tagged
  `worth-your-read`). Only highlights this plugin made are ever removed. Set
  `extensions.wyr.temporaryHighlights` to `false` to keep every highlight.

**Ask across papers** — select two or more papers and right-click → *Ask across
selected papers…*, or right-click a collection → *Ask across this collection…*.
One question, one answer drawn from all of them (the 12 most relevant if there
are more). Each paper is asked on its own — its passages checked against its
own text — and one more step merges those findings, keeping agreement and
disagreement between papers visible. Each answer claim names the papers it
rests on with their verbatim passages; the merge can only point at checked
findings, never write quotes, and any finding it leaves out is still listed.
About 20 seconds per paper on gemma4:26b. **Save as note with
citations** stores it as a note with live Zotero citations (page locators
included), which Zotero's Word/LibreOffice plugin can insert with *Add Note*.

**Research** — *Tools → Research a question with Worth Your Read…*, or
**Find similar papers** (item pane button / item right-click). It finds seed
papers for the question, then snowballs through the citation graph: each
round follows the references of, and the papers citing, every paper accepted in
the last round, screens them for relevance (keyword coverage, then the local
model scoring 0–3 against the question's outcome), and accepts the relevant
ones — until a round finds nothing new ("exhausted") or a cap is hit. Weaker
matches are followed only one citation hop from a direct hit, so the search
can't wander from "metformin and lifespan" into "metformin and melanoma"; the
summary says when that limit applied. Several records of the same paper
(reprints, corrections) are shown once, and a paper already in your library
under another DOI is recognized by its title. Results are grouped (directly on the
question / supporting evidence / cited core), show how each paper was found,
and mark papers already in your library. **Import selected into a new
collection** adds them with their open-access PDFs. If OpenAlex's request limit
is reached the search stops and says so — it never reports a cut-short search
as exhausted. Only the question's keywords and OpenAlex paper ids leave your
machine.

**Write (manuscript mode)** — start one with *Tools → New manuscript with Worth
Your Read* (or right-click a collection → *New manuscript…*, which files it
there), or right-click an existing note → **Check citations with Worth Your
Read**. The note opens in its own tab in Zotero's normal note editor, with a
panel beside it. Each sentence that carries a Zotero citation is checked against
the paper it cites (the local PDF when you have it). Keep writing: the panel
follows your edits — sentences you haven't changed keep their verdict, new or
edited cited sentences are marked *edited — not checked yet*, and **Check N
edited claim(s)** re-checks just those (nothing runs the model while you type).
For a sentence that isn't supported you can:

- **Reword to match the evidence**, then **Apply wording to the note** (the
  citation stays; the new wording is re-checked);
- **Find a supporting source**, then **Add to Zotero and cite** — it imports the
  paper by DOI with its open-access PDF and adds the citation after the existing one;
- **Highlight evidence in PDF** marks the passages behind the verdict in the cited paper.

## Install

1. Install the sidecar once, from the project folder:
   ```bash
   pipx install './worth_your_read[sidecar]'
   ```
   (or `pip install -e './worth_your_read[sidecar]'` into a venv, then point
   `extensions.wyr.sidecarCommand` at that venv's `bin/wyr-sidecar`).
2. In Zotero: *Tools → Plugins → ⚙ → Install Plugin From File…* and choose
   `build/worth-your-read-0.1.0.xpi`.

You don't start the sidecar yourself: the first time the plugin needs it, it
launches `wyr-sidecar` (found in `~/.local/bin`, `/opt/homebrew/bin` or
`/usr/local/bin`, or from `extensions.wyr.sidecarCommand`) with Ollama as the
backend, and stops it when Zotero quits. Starting it loads no model — Ollama
is only called when you run an action. If a sidecar is already running (e.g.
you started `wyr-sidecar` by hand), the plugin uses that one and leaves it
alone. Its output goes to `sidecar.log` in the data directory. (macOS/Linux;
on Windows start `wyr-sidecar` yourself.)

Build the `.xpi` from this folder:
```bash
zip -r build/worth-your-read-0.1.0.xpi manifest.json bootstrap.js prefs.js locale content -x "content/selftest.js"
```

## Settings (Config Editor)

| Pref | Default | |
|---|---|---|
| `extensions.wyr.port` | `23120` | sidecar port |
| `extensions.wyr.dataDir` | `~/Library/Application Support/WorthYourRead` (macOS) | where the sidecar's token lives |
| `extensions.wyr.temporaryHighlights` | `true` | remove highlights after each action unless kept |
| `extensions.wyr.autoStart` | `true` | start the sidecar when needed, stop it on quit |
| `extensions.wyr.sidecarCommand` | *(auto-detect)* | shell command that runs the sidecar |
| `extensions.wyr.model` | `gemma4:26b` | Ollama model the auto-started sidecar uses |

## Development

Plain JavaScript, no build step. To load the source directly, put a text file
named `worth-your-read@local` containing the absolute path of this folder
(with a trailing `/`) in your Zotero profile's `extensions/` folder, then
restart Zotero.

`content/selftest.js` (not shipped) drives the real buttons against a live
sidecar: set `extensions.wyr.selftest` to an output path and start Zotero with
the paper DOI `10.1371/journal.pmed.0020124` in the library; it writes a JSON
report of every step.

## Known limits

- Environment variables from your shell (e.g. `WYR_OPENALEX_API_KEY`) don't reach a
  sidecar Zotero starts from the Dock; start `wyr-sidecar` by hand to use them.
- Applying a rewording rewrites the text before that citation as plain text
  (inline formatting in that sentence is dropped); the citation itself is kept.
- Rewording and finding sources fetch papers online by DOI (cached on disk);
  only search terms and DOIs leave the machine, never your notes or PDFs.
- Word/LibreOffice documents aren't checked directly — draft in a Zotero note
  (manuscript mode), then insert it into Word with Zotero's *Add Note*, which
  keeps the citations live.
- Page locators in saved answers are the PDF's page position, not its printed
  page label.
