/* Worth Your Read — Zotero plugin.
 *
 * A thin UI over the local Worth Your Read sidecar (127.0.0.1, see
 * worth_your_read/sidecar.py). The sidecar does all the reading, judging and
 * quote verification; this file only:
 *   - Read:  an item-pane section to ask the open paper a question, verify a
 *            claim against it, or save a grounded summary as a child note;
 *            a "Verify" button on PDF text selections; quotes jump to their page.
 *   - Write: check every cited sentence in a draft note against the paper it
 *            cites, then reword it, or find and cite a source that supports it.
 * No paper text leaves the machine: the plugin only talks to the sidecar.
 */

const XHTML = "http://www.w3.org/1999/xhtml";
const VERDICT_COLOR = {
  SUPPORTED: "#1b7f3b", OVERSTATED: "#b8860b",
  UNSUPPORTED: "#b3261e", INSUFFICIENT: "#6b6b6b",
};

// Highlight colors from Zotero's own palette, and the tag every highlight
// this plugin makes carries (so they can be filtered or removed together).
const HIGHLIGHT = { supports: "#5fb236", partly: "#ffd400", contradicts: "#ff6666", passage: "#2ea8e5" };
const HL_TAG = "worth-your-read";
// Highlights are temporary by default: they also carry this tag and are removed
// at the start of your next action, when that PDF's tab closes, or at the next
// Zotero start — unless you click "Keep highlights", which drops the tag.
// Only annotations carrying this tag are ever deleted; your own never are.
const TEMP_TAG = "worth-your-read:temporary";

// --- the sidecar process ----------------------------------------------------
// The plugin starts the sidecar itself the first time it's needed (unless one
// is already listening) and stops it when Zotero quits — only a sidecar it
// started. Starting it loads no model: Ollama is only called by a job you run.

const Sidecar = {
  proc: null,
  starting: null,

  /** A shell command line: the `sidecarCommand` pref, else wyr-sidecar where pip/pipx put it. */
  async command() {
    const custom = Zotero.Prefs.get("extensions.wyr.sidecarCommand", true);
    if (custom) return custom;
    const home = Services.dirsvc.get("Home", Ci.nsIFile).path;
    for (const dir of [PathUtils.join(home, ".local", "bin"), "/opt/homebrew/bin", "/usr/local/bin"]) {
      const path = PathUtils.join(dir, "wyr-sidecar");
      if (await IOUtils.exists(path)) return `'${path}'`;
    }
    return null;
  },

  logPath() {
    return PathUtils.join(Client.dataDir(), "sidecar.log");
  },

  /** Start the sidecar and wait until it answers; shared by concurrent callers. */
  ensure() {
    this.starting ||= this.start().finally(() => { this.starting = null; });
    return this.starting;
  },

  async start() {
    if (!Zotero.Prefs.get("extensions.wyr.autoStart", true)) {
      throw new Error("Can't reach the Worth Your Read sidecar. Start it with `wyr-sidecar` "
        + "(or turn on extensions.wyr.autoStart).");
    }
    if (Zotero.isWin) throw new Error("Can't reach the sidecar. Start it with `wyr-sidecar`.");
    if (await Client.reachable()) return;    // already up (e.g. started by hand)
    const cmd = await this.command();
    if (!cmd) {
      throw new Error("The Worth Your Read sidecar isn't installed where the plugin looks. "
        + "Install it from the project folder (`pipx install './worth_your_read[sidecar]'`) "
        + "or set extensions.wyr.sidecarCommand to its path.");
    }
    await IOUtils.makeDirectory(Client.dataDir(), { ignoreExisting: true });
    const { Subprocess } = ChromeUtils.importESModule("resource://gre/modules/Subprocess.sys.mjs");
    // Through sh so its output goes straight to a log file (an undrained pipe
    // would block it) and the command pref may carry arguments.
    this.proc = await Subprocess.call({
      command: "/bin/sh",
      arguments: ["-c", `exec ${cmd} >>"$1" 2>&1`, "wyr-sidecar", this.logPath()],
      environment: {
        WYR_USE_OLLAMA: "1",
        WYR_USE_BEDROCK: "0",
        WYR_OLLAMA_MODEL: Zotero.Prefs.get("extensions.wyr.model", true) || "gemma4:26b",
        WYR_SIDECAR_PORT: String(Zotero.Prefs.get("extensions.wyr.port", true) || 23120),
        WYR_DATA_DIR: Client.dataDir(),
      },
      environmentAppend: true,
    });
    const proc = this.proc;
    let exited = false;
    proc.wait().then(() => { exited = true; if (this.proc === proc) this.proc = null; });
    for (let i = 0; i < 60; i++) {           // up to 30 s
      await Zotero.Promise.delay(500);
      if (exited) break;
      if (await Client.reachable()) return;
    }
    if (!exited) proc.kill();
    let tail = "";
    try {
      tail = (await IOUtils.readUTF8(this.logPath())).trim().split("\n").slice(-3).join(" ");
    }
    catch (e) {}
    throw new Error("The Worth Your Read sidecar didn't start" + (tail ? `: ${tail}` : ".")
      + ` (log: ${this.logPath()})`);
  },

  stop() {
    this.proc?.kill();
    this.proc = null;
  },
};

// --- the sidecar client -----------------------------------------------------

const Client = {
  base() {
    return `http://127.0.0.1:${Zotero.Prefs.get("extensions.wyr.port", true) || 23120}`;
  },

  dataDir() {
    const custom = Zotero.Prefs.get("extensions.wyr.dataDir", true);
    if (custom) return custom;
    const home = Services.dirsvc.get("Home", Ci.nsIFile).path;
    return Zotero.isMac
      ? PathUtils.join(home, "Library", "Application Support", "WorthYourRead")
      : PathUtils.join(home, ".worth_your_read");
  },

  async token() {
    return (await IOUtils.readUTF8(PathUtils.join(this.dataDir(), "sidecar.token"))).trim();
  },

  /** Is anything answering on the sidecar port? */
  async reachable() {
    try {
      // With successCodes: false a refused connection resolves with status 0.
      const xhr = await Zotero.HTTP.request("GET", this.base() + "/health",
        { successCodes: false, timeout: 2000 });
      return xhr.status > 0;
    }
    catch (e) {
      return false;
    }
  },

  async request(method, path, body) {
    const send = async () => Zotero.HTTP.request(method, this.base() + path, {
      headers: { "X-WYR-Token": await this.token(), "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
      responseType: "json",
      successCodes: false,
      timeout: 15000,
    });
    let xhr;
    try {
      xhr = await send();
    }
    catch (e) {}               // no token yet: it has never run
    if (!xhr?.status) {
      // Not running: start it and try once more.
      await Sidecar.ensure();
      xhr = await send();
      if (!xhr.status) throw new Error("Can't reach the Worth Your Read sidecar.");
    }
    if (xhr.status === 401) throw new Error("The sidecar rejected this plugin's token.");
    if (xhr.status >= 400) throw new Error(xhr.response?.detail || `Sidecar error ${xhr.status}`);
    return xhr.response;
  },

  health() {
    return this.request("GET", "/health");
  },

  /** Run one job on the sidecar and resolve with its result. */
  async run(kind, args, onProgress) {
    const { job_id } = await this.request("POST", "/jobs", { kind, args });
    for (;;) {
      await Zotero.Promise.delay(1000);
      const job = await this.request("GET", `/jobs/${job_id}`);
      if (job.progress && onProgress) onProgress(job.progress);
      if (job.status === "done") return job.result;
      if (job.status === "error") throw new Error(job.error);
      if (job.status === "cancelled") throw new Error("Cancelled.");
    }
  },
};

// --- small helpers ------------------------------------------------------------

function titleKey(title) {
  return (title || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

function h(doc, tag, props = {}, ...children) {
  const el = doc.createElementNS(XHTML, tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "style") el.setAttribute("style", v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(typeof c === "string" ? doc.createTextNode(c) : c);
  }
  return el;
}

function badge(doc, label, color) {
  return h(doc, "span", {
    style: `display:inline-block;padding:0 6px;border-radius:3px;font-weight:600;`
      + `font-size:11px;color:#fff;background:${color}`,
  }, label);
}

const DOI_RE = /10\.\d{4,9}\/[-._;()/:A-Za-z0-9]+/;

/** Which paper an item stands for: its PDF (read offline) and its DOI/URL. */
async function paperFor(item) {
  let parent = item, att = null;
  if (item.isAttachment()) {
    att = item;
    parent = item.parentItem || item;
  }
  else if (item.isRegularItem()) {
    att = await item.getBestAttachment();
  }
  const paper = { title: parent.getDisplayTitle() };
  if (att && att.attachmentContentType === "application/pdf") paper.attachment_key = att.key;
  if (parent.isRegularItem()) {
    const doi = parent.getField("DOI") || (parent.getField("extra").match(DOI_RE) || [])[0];
    const url = parent.getField("url");
    if (doi) paper.reference = doi;
    else if (url) paper.reference = url;
  }
  return { paper, parent, att };
}

/** Highlight entries for a verify result: every verified aspect/evidence quote.
 * Supporting passages are green only when the whole claim is SUPPORTED, yellow
 * when the claim overstates them; contradicting passages are red. */
function verdictHighlights(r, claim) {
  const out = [], seen = new Set();
  const full = r.verdict === "SUPPORTED";
  const add = (quote, supports, what) => {
    if (!quote || seen.has(quote)) return;
    seen.add(quote);
    const color = !supports ? HIGHLIGHT.contradicts : full ? HIGHLIGHT.supports : HIGHLIGHT.partly;
    const label = !supports ? "Contradicts" : full ? "Supports" : "Partly supports";
    out.push({ quote, color, comment: `${label}${what ? ` (${what})` : ""}: “${claim}” `
      + `— Worth Your Read: ${r.verdict}` });
  };
  for (const a of r.aspects || []) add(a.quote, a.status === "ENTAILED", `${a.type}: ${a.text}`);
  for (const e of r.evidence || []) add(e.quote, e.stance !== "CONTRADICTS", "");
  return out;
}

/** Highlight entries for an ask/summary result: each grounded claim's passages. */
function answerHighlights(r) {
  return (r.claims || []).filter(c => c.status === "GROUNDED").flatMap(c =>
    c.quotes.filter(q => q.verified).map(q => ({ quote: q.text, color: HIGHLIGHT.passage,
      comment: c.claim + (r.question ? `\n— Q: ${r.question}` : "") })));
}

async function openAtPage(att, page) {
  if (!att) return;
  await Zotero.Reader.open(att.id, page ? { pageIndex: page - 1 } : undefined);
}

// --- the plugin -------------------------------------------------------------

Zotero.WorthYourRead = {
  client: Client,
  sidecar: Sidecar,
  id: null,
  rootURI: null,
  sectionID: null,
  menuID: null,
  notifierID: null,
  readerHandler: null,
  state: new Map(),       // item id -> { busy, view, result, error, claim }
  bodies: new Map(),      // item id -> last rendered section body
  drafts: new Map(),      // note id -> draft-check state (rendered in a tab)
  researches: [],         // research-tab states
  researchMenuIDs: [],

  async startup({ id, rootURI }) {
    this.id = id;
    this.rootURI = rootURI;
    this.sectionID = Zotero.ItemPaneManager.registerSection({
      paneID: "worth-your-read",
      pluginID: id,
      header: { l10nID: "wyr-section-head", icon: rootURI + "content/icon.svg" },
      sidenav: { l10nID: "wyr-section-sidenav", icon: rootURI + "content/icon.svg" },
      onRender: ({ body, item }) => this.renderSection(body, item),
    });
    // Notes show the note editor, not item-pane sections, so the draft check
    // lives on the item context menu (shown for a single selected note).
    this.menuID = Zotero.MenuManager?.registerMenu({
      menuID: "wyr-check-draft",
      pluginID: id,
      target: "main/library/item",
      menus: [{
        menuType: "menuitem",
        l10nID: "wyr-menu-check-draft",
        icon: rootURI + "content/icon.svg",
        onShowing: (event, ctx) => ctx.setVisible(ctx.items?.length === 1 && ctx.items[0].isNote()),
        onCommand: (event, ctx) => this.openDraftCheck(ctx.items[0]),
      }],
    });
    this.researchMenuIDs = [
      Zotero.MenuManager?.registerMenu({
        menuID: "wyr-research", pluginID: id, target: "main/menubar/tools",
        menus: [{ menuType: "menuitem", l10nID: "wyr-menu-research",
          onCommand: () => this.openResearch({}) }],
      }),
      Zotero.MenuManager?.registerMenu({
        menuID: "wyr-similar", pluginID: id, target: "main/library/item",
        menus: [{ menuType: "menuitem", l10nID: "wyr-menu-similar", icon: rootURI + "content/icon.svg",
          onShowing: (event, ctx) => ctx.setVisible(ctx.items?.length === 1 && ctx.items[0].isRegularItem()),
          onCommand: (event, ctx) => this.findSimilar(ctx.items[0]) }],
      }),
    ].filter(Boolean);
    this.readerHandler = (event) => this.onSelectionPopup(event);
    Zotero.Reader.registerEventListener("renderTextSelectionPopup", this.readerHandler, id);
    // Clear temporary highlights when the PDF they're on is closed, and any
    // left over from the last session.
    this.notifierID = Zotero.Notifier.registerObserver({
      notify: (event) => { if (event === "close") this.clearTemporaryHighlights({ closedOnly: true }); },
    }, ["tab"], "worth-your-read");
    Zotero.uiReadyPromise.then(() => this.clearTemporaryHighlights());
    for (const win of Zotero.getMainWindows()) this.addToWindow(win);

    const selftest = Zotero.Prefs.get("extensions.wyr.selftest", true);
    if (selftest) {
      // Developer self-test (see content/selftest.js): drives the real UI
      // against a live sidecar and writes a report to the given path.
      try {
        Services.scriptloader.loadSubScript(rootURI + "content/selftest.js");
        Zotero.WorthYourReadSelfTest.run(selftest);
      }
      catch (e) {
        Zotero.debug("Worth Your Read: self-test unavailable in this build");
      }
    }
  },

  addToWindow(win) {
    win.MozXULElement.insertFTLIfNeeded("wyr.ftl");
  },

  removeFromWindow(win) {
    win.document.querySelector('[href="wyr.ftl"]')?.remove();
  },

  shutdown() {
    Sidecar.stop();
    if (this.sectionID) Zotero.ItemPaneManager.unregisterSection(this.sectionID);
    if (this.menuID) Zotero.MenuManager.unregisterMenu(this.menuID);
    for (const m of this.researchMenuIDs || []) Zotero.MenuManager.unregisterMenu(m);
    if (this.readerHandler) {
      Zotero.Reader.unregisterEventListener("renderTextSelectionPopup", this.readerHandler);
    }
    if (this.notifierID) Zotero.Notifier.unregisterObserver(this.notifierID);
    for (const win of Zotero.getMainWindows()) this.removeFromWindow(win);
  },

  // --- Read: the item-pane section ------------------------------------------

  setState(itemID, patch) {
    this.state.set(itemID, { ...(this.state.get(itemID) || {}), ...patch });
    const body = this.bodies.get(itemID);
    if (body?.isConnected && body.dataset.wyrItem == itemID) {
      this.renderSection(body, Zotero.Items.get(itemID));
    }
  },

  renderSection(body, item) {
    body.replaceChildren();
    if (!item) return;
    body.dataset.wyrItem = item.id;
    this.bodies.set(item.id, body);
    const doc = body.ownerDocument;
    if (!item.isRegularItem() && !item.isAttachment()) return;

    const st = this.state.get(item.id) || {};
    const input = h(doc, "textarea", {
      rows: "2", placeholder: "Ask this paper a question, or type a claim to verify…",
      style: "width:100%;box-sizing:border-box;resize:vertical",
    });
    if (st.claim) input.value = st.claim;
    const go = (kind) => () => {
      const text = input.value.trim();
      if (text) this.runRead(item, kind, text);
    };
    const busy = !!st.busy;
    body.append(h(doc, "div", { style: "display:flex;flex-direction:column;gap:6px" },
      input,
      h(doc, "div", { style: "display:flex;gap:6px;flex-wrap:wrap" },
        h(doc, "button", { dataset: { action: "ask" }, onclick: go("ask"), ...(busy ? { disabled: "true" } : {}) }, "Ask"),
        h(doc, "button", { dataset: { action: "verify" }, onclick: go("verify"), ...(busy ? { disabled: "true" } : {}) }, "Verify claim"),
        h(doc, "button", { dataset: { action: "summarize" }, onclick: () => this.runRead(item, "summarize"),
          ...(busy ? { disabled: "true" } : {}) }, "Summarize → note"),
        item.isRegularItem() ? h(doc, "button", { dataset: { action: "similar" },
          onclick: () => this.findSimilar(item) }, "Find similar papers") : null),
      st.busy && h(doc, "div", { style: "color:var(--fill-secondary)" }, st.busy),
      st.error && h(doc, "div", { style: "color:#b3261e" }, st.error),
      st.result && this.renderResult(doc, item, st.view, st.result)));
  },

  async runRead(item, kind, text = "") {
    this.clearTemporaryHighlights();
    const label = { ask: "Reading the paper…", verify: "Checking the claim against the paper…",
      summarize: "Writing a grounded summary…", revise: "Rewording to match the evidence…" }[kind];
    this.setState(item.id, { busy: label + " (runs locally; may take ~30 s)", error: null, hl: null,
      claim: kind === "summarize" ? this.state.get(item.id)?.claim : text });
    try {
      const { paper, parent } = await paperFor(item);
      if (!paper.attachment_key && !paper.reference) {
        throw new Error("This item has no PDF and no DOI or URL to read.");
      }
      let result;
      if (kind === "ask") result = await Client.run("ask", { paper, question: text });
      else if (kind === "verify") result = await Client.run("verify", { paper, claim: text });
      else if (kind === "revise") {
        result = await Client.run("revise", { claim: text, reference: paper.reference,
          title: paper.title, broaden: false });
      }
      else {
        result = await Client.run("summarize", { paper });
        const note = new Zotero.Item("note");
        note.libraryID = parent.libraryID;
        note.parentID = parent.id;
        note.setNote(result.note_html);
        await note.saveTx();
        result.note_id = note.id;
      }
      this.setState(item.id, { busy: null, view: kind, result });
      return result;
    }
    catch (e) {
      Zotero.logError(e);
      this.setState(item.id, { busy: null, error: e.message });
      return null;
    }
  },

  renderResult(doc, item, view, r) {
    const box = h(doc, "div", { dataset: { wyrResult: view },
      style: "border-top:1px solid var(--fill-quinary);padding-top:6px;display:flex;flex-direction:column;gap:6px" });
    const quoteLink = (q, page) => h(doc, "a", {
      href: "#", style: "cursor:pointer",
      onclick: async (e) => { e.preventDefault(); const { att } = await paperFor(item); openAtPage(att, page); },
    }, page ? `p. ${page}` : "open");
    if (view === "verify" || view === "revise") {
      const v = r.verdict;
      if (view === "revise") {
        box.append(h(doc, "div", {}, h(doc, "b", {}, r.revised ? "Reworded: " : "No rewording needed: "),
          r.claim || ""));
      }
      box.append(h(doc, "div", {}, badge(doc, v || "?", VERDICT_COLOR[v] || "#666"),
        ` (${(r.confidence ?? 0).toFixed(2)})`));
      if (r.summary) box.append(h(doc, "div", {}, r.summary));
      for (const a of r.aspects || []) {
        box.append(h(doc, "div", { style: "font-size:12px" },
          `${a.status === "ENTAILED" ? "✓" : a.status === "CONTRADICTED" ? "✗" : "—"} ${a.type}: ${a.text} `,
          a.quote ? h(doc, "div", { style: "margin-left:12px;color:var(--fill-secondary)" },
            `“${a.quote}” `, quoteLink(a.quote, a.page)) : null));
      }
      const buttons = h(doc, "div", { style: "display:flex;gap:6px;flex-wrap:wrap" });
      if (view === "verify" && verdictHighlights(r, r.claim).length) {
        buttons.append(h(doc, "button", { dataset: { action: "highlight" },
          onclick: () => this.highlightRead(item, view, r) }, "Highlight in PDF"));
      }
      if (view === "verify" && ["OVERSTATED", "UNSUPPORTED"].includes(r.verdict)) {
        buttons.append(h(doc, "button", { dataset: { action: "reword" },
          onclick: () => this.runRead(item, "revise", r.claim) }, "Reword to match the evidence"));
      }
      box.append(buttons, this.hlNote(doc, item));
      return box;
    }
    // ask / summarize: grounded claims
    box.append(h(doc, "div", {},
      h(doc, "b", {}, `Confidence ${r.band} (${r.confidence.toFixed(2)})`),
      ` · ${r.grounded} grounded, ${r.ungrounded} not found in the text`,
      r.abstract_only ? " · abstract only" : ""));
    if (view === "summarize" && r.note_id) {
      box.append(h(doc, "div", { style: "color:#1b7f3b" }, "Saved as a child note."));
    }
    for (const c of r.claims || []) {
      const ok = c.status === "GROUNDED";
      box.append(h(doc, "div", { style: `font-size:12px;${ok ? "" : "color:#b3261e"}` },
        `${ok ? "✓" : "⚠"} ${c.claim}`,
        ...c.quotes.filter(q => q.verified).map(q => h(doc, "div",
          { style: "margin-left:12px;color:var(--fill-secondary)" }, `“${q.text}” `, quoteLink(q.text, q.page)))));
    }
    if (r.not_addressed?.length) {
      box.append(h(doc, "div", { style: "font-size:12px;color:var(--fill-secondary)" },
        "Not addressed by the paper: " + r.not_addressed.join("; ")));
    }
    if (answerHighlights(r).length) {
      box.append(h(doc, "div", {}, h(doc, "button", { dataset: { action: "highlight" },
        onclick: () => this.highlightRead(item, view, r) }, "Highlight in PDF")), this.hlNote(doc, item));
    }
    return box;
  },

  hlNote(doc, item) {
    const st = this.state.get(item.id) || {};
    if (!st.hl) return null;
    return h(doc, "div", { dataset: { wyrHl: "" }, style: "font-size:12px;color:var(--fill-secondary)" }, st.hl, " ",
      st.hlTemp ? h(doc, "button", { dataset: { action: "keep-highlights" }, onclick: async () => {
        const n = await this.keepHighlights();
        this.setState(item.id, { hl: `Kept ${n} highlight(s) permanently (tagged “${HL_TAG}”).`, hlTemp: false });
      } }, "Keep highlights") : null);
  },

  // --- highlighting quotes in the PDF -------------------------------------------

  /** Turn verified quotes into Zotero highlight annotations on the PDF (the
   * sidecar finds their rectangles), then open the reader at the first one.
   * Quotes already highlighted by this plugin aren't duplicated. */
  async highlight(att, entries) {
    const temporary = Zotero.Prefs.get("extensions.wyr.temporaryHighlights", true) !== false;
    if (!att || att.attachmentContentType !== "application/pdf") {
      throw new Error("There's no PDF attached to highlight in.");
    }
    if (!entries.length) throw new Error("No verified passages to highlight.");
    const { quotes } = await Client.run("locate", {
      paper: { attachment_key: att.key }, quotes: entries.map(e => e.quote) });
    const existing = new Map(att.getAnnotations().filter(a => a.hasTag(HL_TAG))
      .map(a => [a.annotationText, a.key]));
    const pad = (n, w) => String(Math.max(0, Math.round(n || 0))).padStart(w, "0");
    let first = null, made = 0, missing = 0, already = 0;
    for (const [i, loc] of quotes.entries()) {
      if (!loc.found) { missing++; continue; }
      if (existing.has(loc.text)) {
        already++;
        first = first || existing.get(loc.text);
        continue;
      }
      const ann = await Zotero.Annotations.saveFromJSON(att, {
        key: Zotero.DataObjectUtilities.generateKey(),
        type: "highlight",
        text: loc.text,
        comment: entries[i].comment,
        color: entries[i].color,
        pageLabel: loc.page_label,
        sortIndex: `${pad(loc.page - 1, 5)}|${pad(0, 6)}|${pad(loc.sort_top, 5)}`,
        position: { pageIndex: loc.page - 1, rects: loc.rects },
        tags: temporary ? [{ name: HL_TAG }, { name: TEMP_TAG }] : [{ name: HL_TAG }],
      });
      existing.set(loc.text, ann.key);
      first = first || ann.key;
      made++;
    }
    if (first) await Zotero.Reader.open(att.id, { annotationID: first });
    const parts = [`Highlighted ${made} passage(s) in the PDF`];
    if (already) parts.push(`${already} already highlighted`);
    if (missing) parts.push(`${missing} couldn't be placed on the page`);
    return { made, already, missing, temporary, message: parts.join(" · ") + "." };
  },

  /** Temporary highlights this plugin made, as annotation items. */
  async temporaryHighlights() {
    const out = [];
    const tagID = Zotero.Tags.getID(TEMP_TAG);
    if (!tagID) return out;
    for (const lib of Zotero.Libraries.getAll()) {
      const ids = await Zotero.Tags.getTagItems(lib.libraryID, tagID);
      out.push(...Zotero.Items.get(ids).filter(i => i.isAnnotation() && i.hasTag(TEMP_TAG)));
    }
    return out;
  },

  /** Delete temporary highlights — all of them, or (closedOnly) only those on
   * PDFs that no longer have an open reader tab. */
  async clearTemporaryHighlights({ closedOnly = false } = {}) {
    try {
      let anns = await this.temporaryHighlights();
      if (closedOnly) {
        const open = new Set((Zotero.Reader._readers || []).map(r => r.itemID));
        anns = anns.filter(a => !open.has(a.parentItemID));
      }
      if (anns.length) await Zotero.Items.erase(anns.map(a => a.id));
      return anns.length;
    }
    catch (e) {
      Zotero.logError(e);
      return 0;
    }
  },

  /** "Keep highlights": make the current temporary highlights permanent. */
  async keepHighlights() {
    const anns = await this.temporaryHighlights();
    for (const a of anns) {
      a.removeTag(TEMP_TAG);
      await a.saveTx();
    }
    return anns.length;
  },

  async highlightRead(item, view, r) {
    this.setState(item.id, { hl: "Finding the passages on the page…", hlTemp: false });
    try {
      await this.clearTemporaryHighlights();      // the previous turn's highlights go
      const { att } = await paperFor(item);
      const entries = view === "verify" ? verdictHighlights(r, r.claim) : answerHighlights(r);
      const res = await this.highlight(att, entries);
      this.setState(item.id, { hl: res.message + (res.temporary && res.made
        ? " They'll be removed at your next action or when you close the PDF." : ""),
        hlTemp: res.temporary && res.made > 0 });
      return res;
    }
    catch (e) {
      this.setState(item.id, { hl: e.message });
      return null;
    }
  },

  // --- Research: snowball the citation graph from a question -------------------

  /** "Find similar": research seeded by this paper (its DOI), with its title +
   * abstract as the question. */
  findSimilar(item, { maxPapers } = {}) {
    const doi = item.getField("DOI") || (item.getField("extra").match(DOI_RE) || [])[0];
    if (!doi) {
      Zotero.alert(null, "Worth Your Read", "Finding similar papers needs the item's DOI.");
      return null;
    }
    const abstract = item.getField("abstractNote") || "";
    return this.openResearch({
      question: `${item.getField("title")}. ${abstract}`.slice(0, 1200),
      seedDois: [doi], title: `Similar: ${item.getDisplayTitle()}`, autostart: true, maxPapers,
    });
  },

  openResearch({ question = "", seedDois = [], title = "Research", autostart = false, maxPapers = 120 }) {
    const win = Zotero.getMainWindow();
    const { id: tabID, container } = win.Zotero_Tabs.add({
      type: "wyr-research", title: `WYR: ${title}`.slice(0, 60), select: true, data: {} });
    const rs = { tabID, container, question, seedDois, title, maxPapers, running: false,
      progress: null, result: null, error: null, selected: new Set(), importMsg: null };
    this.researches.push(rs);
    this.renderResearch(rs);
    if (autostart) this.runResearch(rs);
    return rs;
  },

  async runResearch(rs) {
    if (!rs.question.trim() && !rs.seedDois.length) return;
    Object.assign(rs, { running: true, error: null, result: null, progress: { stage: "starting" },
      selected: new Set(), importMsg: null });
    this.renderResearch(rs);
    try {
      rs.result = await Client.run("research", { question: rs.question, seed_dois: rs.seedDois,
        max_papers: rs.maxPapers }, (p) => { rs.progress = p; this.renderResearch(rs); });
      const have = await this.libraryDOIs();
      for (const p of rs.result.papers) {
        p.in_library = !!this.inLibrary(have, p);
        if (p.relevance === 3 && p.doi && !p.in_library) rs.selected.add(p.id);
      }
    }
    catch (e) { rs.error = e.message; }
    rs.running = false;
    this.renderResearch(rs);
    return rs;
  },

  /** DOIs (lowercase) of regular items already in the user's library. */
  /** Library items keyed by DOI and by normalized title ("t:…"): the same
   * paper often has several DOIs (journal version, reprint, preprint). */
  async libraryDOIs() {
    const items = await Zotero.Items.getAll(Zotero.Libraries.userLibraryID, true);
    const have = new Map();
    for (const i of items.filter(i => i.isRegularItem())) {
      if (i.getField("DOI")) have.set(i.getField("DOI").toLowerCase(), i);
      const t = titleKey(i.getField("title"));
      if (t) have.set("t:" + t, i);
    }
    return have;
  },

  inLibrary(have, p) {
    return (p.doi && have.get(p.doi.toLowerCase())) || have.get("t:" + titleKey(p.title)) || null;
  },

  renderResearch(rs) {
    const doc = rs.container.ownerDocument;
    const r = rs.result;
    const q = h(doc, "textarea", { rows: "2", placeholder: "Your research question…",
      style: "width:100%;box-sizing:border-box;font-size:13px" });
    q.value = rs.question;
    q.addEventListener("input", () => { rs.question = q.value; });
    const cap = h(doc, "input", { type: "number", min: "10", max: "500", value: String(rs.maxPapers),
      style: "width:70px" });
    cap.addEventListener("change", () => { rs.maxPapers = parseInt(cap.value) || 120; });
    const stageText = (p) => !p ? "" : p.stage === "seeds" ? "Finding seed papers…"
      : p.stage === "expanding" ? `Round ${p.round}: following references and citations of ${p.frontier} paper(s)… (${p.accepted} relevant so far)`
      : p.stage === "screening" ? `Round ${p.round}: screening ${p.candidates} candidates… (${p.accepted} relevant so far)`
      : "Starting…";
    const root = h(doc, "div", { dataset: { wyrResearch: "" },
      style: "overflow:auto;height:100%;padding:16px 24px;box-sizing:border-box;font-size:13px;"
        + "background:var(--material-background);color:var(--fill-primary)" },
      h(doc, "h2", { style: "margin:0 0 8px" }, "Worth Your Read — research"),
      h(doc, "div", { style: "color:var(--fill-secondary);margin-bottom:6px" },
        "Finds seed papers for your question, then follows their references and the papers citing them, "
        + "screening each round for relevance, until a round finds nothing new."),
      rs.seedDois.length ? h(doc, "div", { style: "margin-bottom:6px" }, `Seeded by: ${rs.seedDois.join(", ")}`) : null,
      q,
      h(doc, "div", { style: "display:flex;gap:8px;align-items:center;margin:6px 0 10px" },
        h(doc, "button", { dataset: { action: "research-start" }, onclick: () => this.runResearch(rs),
          ...(rs.running ? { disabled: "true" } : {}) }, r ? "Search again" : "Start"),
        h(doc, "span", {}, "up to"), cap, h(doc, "span", {}, "papers"),
        h(doc, "span", { style: "color:var(--fill-secondary)" },
          "· only search terms and paper ids leave your machine")),
      rs.running ? h(doc, "div", { dataset: { wyrProgress: "" } }, stageText(rs.progress)) : null,
      rs.error ? h(doc, "div", { style: "color:#b3261e" }, rs.error) : null);
    if (r) root.append(this.renderResearchResult(doc, rs));
    rs.container.replaceChildren(root);
  },

  renderResearchResult(doc, rs) {
    const r = rs.result;
    const byId = new Map(r.papers.map(p => [p.id, p]));
    const box = h(doc, "div", {},
      h(doc, "div", { style: "margin-bottom:6px" }, h(doc, "b", {}, r.summary)),
      h(doc, "div", { style: "color:var(--fill-secondary);margin-bottom:8px" },
        r.rounds.map(x => `round ${x.round}: ${x.accepted}/${x.candidates}`).join(" · ")));
    const sel = (pred) => () => { rs.selected = new Set(r.papers.filter(pred).filter(p => p.doi).map(p => p.id));
      this.renderResearch(rs); };
    box.append(h(doc, "div", { style: "display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px" },
      h(doc, "button", { onclick: sel(p => p.relevance === 3) }, "Select direct"),
      h(doc, "button", { onclick: sel(p => p.relevance >= 2) }, "Select direct + supporting"),
      h(doc, "button", { onclick: sel(() => true) }, "Select all"),
      h(doc, "button", { onclick: sel(() => false) }, "Clear"),
      h(doc, "button", { dataset: { action: "research-import" }, onclick: () => this.importResearch(rs),
        ...(rs.selected.size ? {} : { disabled: "true" }) },
        `Import ${rs.selected.size} selected into a new collection`)));
    if (rs.importMsg) box.append(h(doc, "div", { style: "margin-bottom:8px;color:#1b7f3b" }, rs.importMsg));
    const groups = [
      ["Directly on the question", p => p.relevance === 3 && !p.cited_core],
      ["Supporting evidence (other populations, models, mechanisms)", p => p.relevance === 2 && !p.cited_core],
      ["Cited core — referenced by many of the papers above", p => p.cited_core],
      ["Your seed papers", p => p.relevance === undefined],
    ];
    for (const [label, pred] of groups) {
      const ps = r.papers.filter(pred);
      if (!ps.length) continue;
      box.append(h(doc, "h3", { style: "margin:12px 0 4px" }, `${label} (${ps.length})`));
      for (const p of ps) {
        const via = (p.via || []).slice(0, 2).map(v => `${v.rel} “${(byId.get(v.from)?.title || v.from).slice(0, 60)}”`).join("; ");
        const cb = h(doc, "input", { type: "checkbox", ...(rs.selected.has(p.id) ? { checked: "true" } : {}),
          ...(p.doi ? {} : { disabled: "true" }) });
        cb.addEventListener("change", () => { cb.checked ? rs.selected.add(p.id) : rs.selected.delete(p.id);
          this.renderResearch(rs); });
        box.append(h(doc, "div", { dataset: { paper: p.id }, style: "display:flex;gap:8px;margin:3px 0" }, cb,
          h(doc, "div", {},
            h(doc, "span", {}, `${p.title} (${p.year || "n.d."})`),
            p.in_library ? h(doc, "span", { style: "color:#1b7f3b" }, " · in library") : null,
            p.is_oa ? h(doc, "span", { style: "color:var(--fill-secondary)" }, " · open access") : null,
            p.round ? h(doc, "div", { style: "font-size:11px;color:var(--fill-secondary)" },
              `round ${p.round}${p.support ? ` · pointed to by ${p.support} accepted paper(s)` : ""}${via ? ` · ${via}` : ""}`) : null)));
      }
    }
    return box;
  },

  /** Import the selected papers (by DOI, with open-access PDFs) into a new
   * collection named after the question; papers already in the library are
   * added to the collection, not duplicated. */
  async importResearch(rs) {
    const r = rs.result;
    const picked = r.papers.filter(p => rs.selected.has(p.id) && p.doi);
    const lib = Zotero.Libraries.userLibraryID;
    const have = await this.libraryDOIs();
    const col = new Zotero.Collection();
    col.libraryID = lib;
    col.name = `WYR: ${(rs.title !== "Research" ? rs.title : rs.question).slice(0, 70)}`;
    await col.saveTx();
    let added = 0, existing = 0, failed = 0, pdfs = 0;
    for (const [i, p] of picked.entries()) {
      rs.importMsg = `Importing ${i + 1} of ${picked.length}…`;
      this.renderResearch(rs);
      try {
        let item = this.inLibrary(have, p);
        if (item) existing++;
        else {
          const tr = new Zotero.Translate.Search();
          tr.setIdentifier({ DOI: p.doi });
          tr.setTranslator(await tr.getTranslators());
          [item] = await tr.translate({ libraryID: lib, collections: [col.id] });
          added++;
          if (await Zotero.Attachments.addAvailableFile(item).catch(() => null)) pdfs++;
        }
        if (!item.inCollection(col.id)) {
          item.addToCollection(col.id);
          await item.saveTx();
        }
        p.in_library = true;
      }
      catch (e) { failed++; }
    }
    rs.collectionID = col.id;
    rs.importMsg = `Collection “${col.name}”: ${added} imported (${pdfs} with PDFs), `
      + `${existing} already in your library${failed ? `, ${failed} failed` : ""}.`;
    this.renderResearch(rs);
    return rs;
  },

  // --- Read: verify a PDF selection -------------------------------------------

  onSelectionPopup({ reader, doc, params, append }) {
    const text = params?.annotation?.text;
    if (!text) return;
    append(h(doc, "button", {
      style: "margin-top:4px",
      onclick: () => this.verifySelection(reader.itemID, text),
    }, "Verify with Worth Your Read"));
  },

  async verifySelection(attachmentID, text) {
    const att = Zotero.Items.get(attachmentID);
    const pw = new Zotero.ProgressWindow({ closeOnClick: true });
    pw.changeHeadline("Worth Your Read");
    pw.addDescription("Checking the selected claim against this paper…");
    pw.show();
    const r = await this.runRead(att, "verify", text);
    if (att.parentItem) this.setState(att.parentItem.id, { view: "verify", result: r, claim: text });
    const done = new Zotero.ProgressWindow({ closeOnClick: true });
    done.changeHeadline(r ? `Worth Your Read: ${r.verdict}` : "Worth Your Read");
    done.addDescription(r ? r.summary : (this.state.get(att.id)?.error || "Failed."));
    pw.close();
    done.show();
    done.startCloseTimer(8000);
    return r;
  },

  // --- Write: check a draft note's citations -----------------------------------

  /** (sentence, cited items) pairs from a note's Zotero citations. */
  async extractCitations(note) {
    const win = Zotero.getMainWindow();
    const doc = new win.DOMParser().parseFromString(note.getNote(), "text/html");
    const pairs = [];
    const spans = [...doc.querySelectorAll("span.citation[data-citation]")];
    for (const [index, span] of spans.entries()) {
      let citation;
      try { citation = JSON.parse(decodeURIComponent(span.dataset.citation)); }
      catch (e) { continue; }
      const sentence = sentenceBefore(span);
      const cited = [];
      for (const ci of citation.citationItems || []) {
        const uri = (ci.uris || ci.uri || [])[0];
        const item = uri ? await Zotero.URI.getURIItem(uri) : null;
        cited.push({ item, doi: item ? null : ci.itemData?.DOI || null,
          title: item ? item.getDisplayTitle() : ci.itemData?.title || uri || "?" });
      }
      if (sentence && cited.length) pairs.push({ index, sentence, cited });
    }
    return pairs;
  },

  async openDraftCheck(note) {
    const win = Zotero.getMainWindow();
    const { id: tabID, container } = win.Zotero_Tabs.add({
      type: "wyr-draft", title: `Check: ${note.getNoteTitle() || "draft"}`, select: true, data: {},
    });
    const draft = { note, tabID, container, rows: [], status: "Reading citations…" };
    this.drafts.set(note.id, draft);
    this.renderDraft(draft);
    const pairs = await this.extractCitations(note);
    draft.rows = pairs.flatMap(p => p.cited.map(c => ({ ...p, cited: c, state: "queued" })));
    draft.status = draft.rows.length
      ? `Checking ${draft.rows.length} cited claim(s) against their sources…`
      : "No Zotero citations found in this note.";
    this.renderDraft(draft);
    for (const row of draft.rows) {
      row.state = "running";
      this.renderDraft(draft);
      try {
        const paper = row.cited.item ? (await paperFor(row.cited.item)).paper
          : { reference: row.cited.doi || "", title: row.cited.title };
        row.paper = paper;
        row.result = await Client.run("verify", { paper, claim: row.sentence });
        row.state = "done";
      }
      catch (e) {
        row.state = "error";
        row.error = e.message;
      }
      this.renderDraft(draft);
    }
    this.updateDraftStatus(draft);
    this.renderDraft(draft);
    return draft;
  },

  updateDraftStatus(draft) {
    const ok = draft.rows.filter(r => r.result?.verdict === "SUPPORTED").length;
    draft.status = `${ok} of ${draft.rows.length} cited claim(s) supported by their source.`;
  },

  renderDraft(draft) {
    const { container } = draft;
    const doc = container.ownerDocument;
    container.replaceChildren(h(doc, "div",
      { dataset: { wyrDraft: draft.note.id },
        style: "overflow:auto;height:100%;padding:16px 24px;box-sizing:border-box;font-size:13px;"
          + "background:var(--material-background);color:var(--fill-primary)" },
      h(doc, "h2", { style: "margin:0 0 4px" }, "Worth Your Read — citation check"),
      h(doc, "div", { style: "color:var(--fill-secondary);margin-bottom:12px" }, draft.status),
      ...draft.rows.map((row, i) => this.renderDraftRow(doc, draft, row, i))));
  },

  renderDraftRow(doc, draft, row, i) {
    const r = row.result;
    const v = r?.verdict;
    const card = h(doc, "div", { dataset: { row: i },
      style: "border:1px solid var(--fill-quinary);border-radius:6px;padding:10px;margin-bottom:10px" },
      h(doc, "div", {}, h(doc, "b", {}, row.sentence)),
      h(doc, "div", { style: "color:var(--fill-secondary);margin:2px 0 6px" }, `cites: ${row.cited.title}`));
    if (row.state !== "done") {
      card.append(h(doc, "div", { style: row.state === "error" ? "color:#b3261e" : "color:var(--fill-secondary)" },
        row.state === "error" ? row.error : row.state === "running" ? "Checking…" : "Waiting…"));
      return card;
    }
    card.append(h(doc, "div", {}, badge(doc, v, VERDICT_COLOR[v] || "#666"),
      ` ${r.band} (${r.confidence.toFixed(2)}) — ${r.summary || ""}`));
    if (row.revision) {
      card.append(h(doc, "div", { style: "margin-top:6px" }, h(doc, "b", {}, "Suggested wording: "),
        row.revision.text, " ", badge(doc, row.revision.verdict || "?", VERDICT_COLOR[row.revision.verdict] || "#666")));
    }
    for (const s of row.suggestions || []) {
      card.append(h(doc, "div", { style: "margin-top:4px" },
        badge(doc, s.verdict, VERDICT_COLOR[s.verdict] || "#666"),
        ` ${s.title} (${s.year || "n.d."}) `,
        s.added ? h(doc, "span", { style: "color:#1b7f3b" }, "added and cited")
          : h(doc, "button", { dataset: { action: "add-cite" }, onclick: () => this.addAndCite(draft, row, s) },
            s.verdict === "SUPPORTED" ? "Add to Zotero and cite"
              : "Add and cite anyway (only partly supports this)")));
    }
    if (row.busy) card.append(h(doc, "div", { style: "color:var(--fill-secondary)" }, row.busy));
    if (row.error) card.append(h(doc, "div", { style: "color:#b3261e" }, row.error));
    const actions = h(doc, "div", { style: "display:flex;gap:6px;margin-top:8px" });
    if (v !== "SUPPORTED" && row.paper?.reference && !row.revision) {
      actions.append(h(doc, "button", { dataset: { action: "reword" },
        onclick: () => this.draftReword(draft, row) }, "Reword to match the evidence"));
    }
    if (row.revision && !row.revision.applied) {
      actions.append(h(doc, "button", { dataset: { action: "apply" },
        onclick: () => this.applyRevision(draft, row) }, "Apply wording to the note"));
    }
    if (row.revision?.applied) actions.append(h(doc, "span", { style: "color:#1b7f3b" }, "Applied to the note."));
    if (v !== "SUPPORTED" && !row.suggestions) {
      actions.append(h(doc, "button", { dataset: { action: "find-source" },
        onclick: () => this.draftSuggest(draft, row) }, "Find a supporting source"));
    }
    if (row.cited.item && verdictHighlights(r, row.sentence).length) {
      actions.append(h(doc, "button", { dataset: { action: "highlight" },
        onclick: () => this.draftHighlight(draft, row) }, "Highlight evidence in PDF"));
    }
    card.append(actions);
    if (row.hl) {
      card.append(h(doc, "div", { style: "font-size:12px;color:var(--fill-secondary);margin-top:4px" }, row.hl, " ",
        row.hlTemp ? h(doc, "button", { dataset: { action: "keep-highlights" }, onclick: async () => {
          const n = await this.keepHighlights();
          row.hl = `Kept ${n} highlight(s) permanently (tagged “${HL_TAG}”).`;
          row.hlTemp = false;
          this.renderDraft(draft);
        } }, "Keep highlights") : null));
    }
    return card;
  },

  async draftHighlight(draft, row) {
    row.hl = "Finding the passages on the page…";
    this.renderDraft(draft);
    try {
      const { att } = await paperFor(row.cited.item);
      await this.clearTemporaryHighlights();
      const res = await this.highlight(att, verdictHighlights(row.result, row.sentence));
      row.hl = res.message + (res.temporary && res.made
        ? " They'll be removed at your next action or when you close the PDF." : "");
      row.hlTemp = res.temporary && res.made > 0;
    }
    catch (e) { row.hl = e.message; }
    this.renderDraft(draft);
  },

  async draftReword(draft, row) {
    row.busy = "Rewording to match the evidence…";
    this.renderDraft(draft);
    try {
      const r = await Client.run("revise", { claim: row.sentence, reference: row.paper.reference,
        title: row.paper.title, broaden: false });
      if (!r.revised) throw new Error("The model found no better wording supported by this source.");
      row.revision = { text: r.claim, verdict: r.verdict, applied: false };
    }
    catch (e) { row.error = e.message; }
    row.busy = null;
    this.renderDraft(draft);
  },

  async draftSuggest(draft, row) {
    row.busy = "Searching for papers that support this claim (about a minute per candidate)…";
    this.renderDraft(draft);
    try {
      const r = await Client.run("suggest", { claim: row.revision?.text || row.sentence,
        exclude: row.paper?.reference || "", n: 2, breadth: 4 });
      row.suggestions = r.suggested || [];
      if (!row.suggestions.length) row.error = r.summary;
    }
    catch (e) { row.error = e.message; }
    row.busy = null;
    this.renderDraft(draft);
  },

  /** Replace the cited sentence's wording in the note, keeping its citation. */
  async applyRevision(draft, row) {
    const note = draft.note;
    const win = Zotero.getMainWindow();
    const doc = new win.DOMParser().parseFromString(note.getNote(), "text/html");
    const span = doc.querySelectorAll("span.citation[data-citation]")[row.index];
    if (!span || !replaceSentenceBefore(span, row.sentence, row.revision.text)) {
      row.error = "Couldn't find that sentence in the note any more — it may have been edited.";
    }
    else {
      note.setNote(doc.body.innerHTML);
      await note.saveTx();
      row.revision.applied = true;
      row.sentence = row.revision.text.replace(/[.\s]+$/, "");
      // Re-check the new wording against the same source.
      row.busy = "Re-checking the new wording…";
      this.renderDraft(draft);
      try {
        row.result = await Client.run("verify", { paper: row.paper, claim: row.sentence });
      }
      catch (e) { row.error = e.message; }
      row.busy = null;
      this.updateDraftStatus(draft);
    }
    this.renderDraft(draft);
  },

  /** Import a suggested paper by DOI (with its open-access PDF) and cite it
   * right after the sentence's existing citation. */
  async addAndCite(draft, row, s) {
    row.busy = `Adding ${s.doi || s.title} to Zotero…`;
    this.renderDraft(draft);
    try {
      const note = draft.note;
      const tr = new Zotero.Translate.Search();
      tr.setIdentifier({ DOI: s.doi });
      tr.setTranslator(await tr.getTranslators());
      const [item] = await tr.translate({ libraryID: note.libraryID });
      await Zotero.Attachments.addAvailableFile(item).catch(() => null);
      const win = Zotero.getMainWindow();
      const doc = new win.DOMParser().parseFromString(note.getNote(), "text/html");
      const span = doc.querySelectorAll("span.citation[data-citation]")[row.index];
      // Build the citation the way Zotero's note editor does: item URI plus
      // CSL item data (without it the editor renders an empty "()").
      const citationItem = { uris: [Zotero.URI.getItemURI(item)],
        itemData: Zotero.Utilities.Item.itemToCSLJSON(item) };
      const citation = { citationItems: [citationItem], properties: {} };
      const cite = doc.createElement("span");
      cite.className = "citation";
      cite.dataset.citation = encodeURIComponent(JSON.stringify(citation));
      cite.innerHTML = Zotero.EditorInstanceUtilities.formatCitation(citation);
      span.after(doc.createTextNode(" "), cite);
      const container = doc.querySelector("body > div[data-schema-version]");
      if (container) {
        let stored = [];
        try { stored = JSON.parse(decodeURIComponent(container.dataset.citationItems || "%5B%5D")); }
        catch (e) {}
        stored.push(citationItem);
        container.dataset.citationItems = encodeURIComponent(JSON.stringify(stored));
      }
      note.setNote(doc.body.innerHTML);
      await note.saveTx();
      s.added = true;
      // The new citation sits right after this one, so later rows' citations
      // each move one position along.
      for (const r of draft.rows) if (r.index > row.index) r.index++;
    }
    catch (e) { row.error = `Couldn't add it: ${e.message}`; }
    row.busy = null;
    this.renderDraft(draft);
  },
};

// --- sentence handling in note HTML --------------------------------------------

const BLOCK = "p,li,div,h1,h2,h3,h4,h5,h6,blockquote,td";

/** Text nodes in the citation's block that come before it, skipping other
 * citations' rendered text ("(Smith 2010)"). */
function textNodesBefore(span) {
  const block = span.closest(BLOCK) || span.parentNode;
  const walker = block.ownerDocument.createTreeWalker(block, 4 /* SHOW_TEXT */);
  const nodes = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (span.compareDocumentPosition(n) & 4 /* FOLLOWING */) break;
    if (span.contains(n) || n.parentElement?.closest("span.citation")) continue;
    nodes.push(n);
  }
  return nodes;
}

/** The sentence a citation is attached to: the last sentence before it. */
function sentenceBefore(span) {
  const text = textNodesBefore(span).map(n => n.data).join("").replace(/\s+/g, " ").trim();
  const parts = text.split(/(?<=[.!?])\s+(?=[A-Z0-9(\[])/);
  return (parts[parts.length - 1] || "").replace(/[\s,;:(]+$/, "").trim();
}

function replaceSentenceBefore(span, sentence, replacement) {
  const nodes = textNodesBefore(span);
  const full = nodes.map(n => n.data).join("");
  const norm = full.replace(/\s+/g, " ");
  const idx = norm.lastIndexOf(sentence);
  if (!nodes.length || idx < 0) return false;
  // Rewrite the block's leading text in place. Inline formatting inside the
  // replaced run is not kept (the citation span itself is untouched).
  let text = norm.slice(0, idx) + replacement.replace(/[.\s]+$/, "") + norm.slice(idx + sentence.length);
  nodes[0].data = text;
  for (const n of nodes.slice(1)) n.data = "";
  return true;
}
