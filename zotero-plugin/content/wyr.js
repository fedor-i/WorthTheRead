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

/** Reject if `promise` hasn't settled within `ms`. */
function withTimeout(promise, ms) {
  const win = Zotero.getMainWindow();     // the plugin scope has no timers of its own
  let timer;
  return Promise.race([promise, new Promise((_, reject) => {
    timer = win.setTimeout(() => reject(new Error("timed out")), ms);
  })]).finally(() => win.clearTimeout(timer));
}

/** "Ioannidis 2005 — Why most published research…" for lists and prompts. */
function shortLabel(item) {
  const year = Zotero.Date.strToDate(item.getField("date") || "").year;
  // firstCreator wraps names in invisible bidi isolates (U+2068/U+2069).
  const who = [item.firstCreator.replace(/[\u2066-\u2069]/g, ""), year].filter(Boolean).join(" ");
  const title = item.getDisplayTitle();
  return (who ? `${who} — ` : "") + (title.length > 70 ? title.slice(0, 67) + "…" : title);
}

const READ_FIRST = 5;     // papers highlighted "Read first" in an answer note's references

const LEFT_OUT = { limit: "beyond the number of papers you chose", no_text: "no readable text",
  load_failed: "couldn't be loaded" };

/** "Martin 1990 — Glacial-interglacial… (no readable text)" per paper an answer didn't read. */
function leftOutLines(r) {
  return (r.left_out || (r.skipped || []).map(label => ({ label })))
    .map(p => p.reason ? `${p.label} (${LEFT_OUT[p.reason] || p.reason})` : p.label);
}

/** A claim as HTML with each paper's citation placed right after each part of
 * the sentence it backs (`source.part`; a paper backing two parts is cited at
 * both), or at the end when it backs the whole sentence; `cite(n)` gives a
 * paper's citation HTML. */
function citedClaimHTML(claim, sources, cite) {
  const esc = Zotero.Utilities.htmlSpecialChars;
  const end = claim.replace(/[\s.]+$/, "").length;      // before the final full stop
  const at = new Map();                                // offset -> papers cited there
  const add = (pos, n) => { if (!(at.get(pos) || []).includes(n)) at.set(pos, [...(at.get(pos) || []), n]); };
  for (const n of new Set(sources.map(s => s.paper))) {
    const placed = sources.filter(s => s.paper === n && s.part && claim.includes(s.part));
    for (const s of placed) add(Math.min(claim.indexOf(s.part) + s.part.length, end), n);
    if (!placed.length) add(end, n);
  }
  let html = "", last = 0;
  for (const pos of [...at.keys()].sort((a, b) => a - b)) {
    html += esc(claim.slice(last, pos)) + " " + at.get(pos).map(cite).join(" ");
    last = pos;
  }
  return html + esc(claim.slice(last));
}

/** The zotero:// path of an item's library: "library", or "groups/<id>". */
function libraryPath(item) {
  return item.library.libraryType === "group"
    ? `groups/${Zotero.Groups.getGroupIDFromLibraryID(item.libraryID)}` : "library";
}

/** The paper online: its DOI, else the item's URL. */
function onlineURL(item) {
  const doi = (item.getField("DOI") || "").match(DOI_RE)?.[0]
    || (item.getField("extra") || "").match(DOI_RE)?.[0];
  if (doi) return `https://doi.org/${doi}`;
  const url = item.getField("url");
  return /^https?:\/\//.test(url) ? url : null;
}

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
  askManys: [],           // ask-across-papers tab states
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
        menuID: "wyr-new-manuscript", pluginID: id, target: "main/menubar/tools",
        menus: [{ menuType: "menuitem", l10nID: "wyr-menu-new-manuscript",
          onCommand: () => this.newManuscript() }],
      }),
      Zotero.MenuManager?.registerMenu({
        menuID: "wyr-new-manuscript-collection", pluginID: id, target: "main/library/collection",
        menus: [{ menuType: "menuitem", l10nID: "wyr-menu-new-manuscript", icon: rootURI + "content/icon.svg",
          onShowing: (event, ctx) => ctx.setVisible(ctx.collectionTreeRows?.length === 1
            && ctx.collectionTreeRows[0].isCollection()),
          onCommand: (event, ctx) => this.newManuscript(ctx.collectionTreeRows[0].ref) }],
      }),
      Zotero.MenuManager?.registerMenu({
        menuID: "wyr-ask-many", pluginID: id, target: "main/library/item",
        menus: [{ menuType: "menuitem", l10nID: "wyr-menu-ask-many", icon: rootURI + "content/icon.svg",
          onShowing: (event, ctx) => ctx.setVisible((ctx.items || []).filter(i => i.isRegularItem()).length >= 2),
          onCommand: (event, ctx) => this.openAskMany({ items: ctx.items }) }],
      }),
      Zotero.MenuManager?.registerMenu({
        menuID: "wyr-ask-collection", pluginID: id, target: "main/library/collection",
        menus: [{ menuType: "menuitem", l10nID: "wyr-menu-ask-collection", icon: rootURI + "content/icon.svg",
          onShowing: (event, ctx) => ctx.setVisible(ctx.collectionTreeRows?.length === 1
            && ctx.collectionTreeRows[0].isCollection()),
          onCommand: (event, ctx) => this.openAskMany({ collection: ctx.collectionTreeRows[0].ref }) }],
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
      notify: (event, type, ids) => {
        if (type === "tab" && event === "close") {
          this.clearTemporaryHighlights({ closedOnly: true });
          for (const [noteID, d] of this.drafts) if (ids.includes(d.tabID)) this.drafts.delete(noteID);
        }
        // Manuscript mode: re-read the citations of an open draft after each edit.
        if (type === "item" && event === "modify") {
          for (const id of ids) if (this.drafts.has(id)) this.scheduleResync(this.drafts.get(id));
        }
      },
    }, ["tab", "item"], "worth-your-read");
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
        ...(rs.selected.size && !rs.importing ? {} : { disabled: "true" }) },
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
    if (rs.importing) return rs;          // one import at a time per search
    rs.importing = true;
    try {
      return await this.importResearchNow(rs);
    }
    finally {
      rs.importing = false;
      this.renderResearch(rs);
    }
  },

  async importResearchNow(rs) {
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
          [item] = await withTimeout(tr.translate({ libraryID: lib, collections: [col.id] }), 60000);
          added++;
          // A publisher site that never answers mustn't stall the rest: skip the PDF.
          if (await withTimeout(Zotero.Attachments.addAvailableFile(item), 90000).catch(() => null)) pdfs++;
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

  // --- Ask across papers: one question, several papers, one grounded answer ---

  openAskMany({ items = [], collection = null }) {
    const papers = (collection ? collection.getChildItems() : items).filter(i => i.isRegularItem());
    if (papers.length < 2) {
      Zotero.alert(null, "Worth Your Read", "Asking across papers needs at least two papers.");
      return null;
    }
    const title = collection ? collection.name : `${papers.length} papers`;
    const win = Zotero.getMainWindow();
    const { id: tabID, container } = win.Zotero_Tabs.add({
      type: "wyr-ask-many", title: `WYR: Ask ${title}`.slice(0, 60), select: true, data: {} });
    // 0 (the default) reads every paper; the user can lower it in the tab.
    const limit = Number(Zotero.Prefs.get("extensions.wyr.askManyMax", true)) || papers.length;
    const st = { tabID, container, items: papers, collection, title, question: "",
      maxPapers: Math.min(papers.length, limit), running: false, result: null, error: null, note: null };
    this.askManys.push(st);
    this.renderAskMany(st);
    return st;
  },

  async runAskMany(st) {
    if (!st.question.trim()) return;
    Object.assign(st, { running: true, result: null, error: null, note: null, progress: null });
    this.renderAskMany(st);
    try {
      const specs = [];
      for (const item of st.items) {
        const { paper } = await paperFor(item);
        specs.push({ ...paper, label: shortLabel(item), item_key: item.key });
      }
      st.result = await Client.run("ask_many", { question: st.question, papers: specs, max_papers: st.maxPapers },
        (p) => { st.progress = p; this.renderAskMany(st); });
    }
    catch (e) { st.error = e.message; }
    st.running = false;
    this.renderAskMany(st);
    return st;
  },

  renderAskMany(st) {
    const doc = st.container.ownerDocument;
    const q = h(doc, "textarea", { rows: "2", placeholder: "Your question for these papers…",
      style: "width:100%;box-sizing:border-box;font-size:13px" });
    q.value = st.question;
    q.addEventListener("input", () => { st.question = q.value; });
    const limit = h(doc, "input", { type: "number", min: "1", max: String(st.items.length),
      value: String(st.maxPapers), dataset: { wyrLimit: "" }, style: "width:4em" });
    const minutes = () => `about ${Math.max(1, Math.round((st.maxPapers * 20 + 180) / 60))} min`;
    const estimate = h(doc, "span", { style: "color:var(--fill-secondary)" }, minutes());
    limit.addEventListener("input", () => {
      const n = Math.round(Number(limit.value));
      if (n >= 1) st.maxPapers = Math.min(n, st.items.length);
      estimate.textContent = minutes();
    });
    const root = h(doc, "div", { dataset: { wyrAskMany: "" },
      style: "overflow:auto;height:100%;padding:16px 24px;box-sizing:border-box;font-size:13px;"
        + "background:var(--material-background);color:var(--fill-primary)" },
      h(doc, "h2", { style: "margin:0 0 8px" }, `Worth Your Read — ask ${st.title}`),
      h(doc, "div", { style: "color:var(--fill-secondary);margin-bottom:6px" },
        `Asks each of the ${st.items.length} papers on its own, then merges what they say into one answer; `
        + "every passage is checked against the paper it's attributed to. Runs on your local model "
        + "(about 20 s per paper) — paper text stays on this machine."),
      q,
      h(doc, "div", { style: "display:flex;gap:8px;align-items:center;margin:6px 0 10px" },
        h(doc, "button", { dataset: { action: "ask-many" }, onclick: () => this.runAskMany(st),
          ...(st.running ? { disabled: "true" } : {}) }, st.result ? "Ask again" : "Ask"),
        st.running ? null : h(doc, "label", {}, "reading the ", limit,
          ` most relevant of ${st.items.length} papers `),
        st.running ? null : estimate,
        st.running ? h(doc, "span", { dataset: { wyrProgress: "" } },
          st.progress?.stage === "paper" ? `Reading paper ${st.progress.paper} of ${st.progress.papers}: ${st.progress.label}…`
            : st.progress?.stage === "merge" ? `Merging ${st.progress.claims} findings from ${st.progress.papers} papers`
              + (st.progress.parts ? ` (part ${st.progress.part} of ${st.progress.parts})…` : "…")
              : st.progress?.stage === "check" ? `Checking which findings back each part of answer claim ${st.progress.claim} of ${st.progress.claims}…`
              : st.progress?.stage === "overview" ? `Writing a short answer from ${st.progress.claims} claims…`
              : st.progress?.stage === "wording" ? `Checking that answer claim ${st.progress.claim} of ${st.progress.claims} says only what its findings say…`
              : "Starting…") : null),
      st.error ? h(doc, "div", { style: "color:#b3261e" }, st.error) : null);
    if (st.result) root.append(this.renderAskManyResult(doc, st));
    st.container.replaceChildren(root);
  },

  renderAskManyResult(doc, st) {
    const r = st.result;
    const itemByKey = new Map(st.items.map(i => [i.key, i]));
    const paperN = new Map(r.papers.map(p => [p.n, p]));
    const sourceLine = (s) => {
      const p = paperN.get(s.paper);
      const item = p && itemByKey.get(p.key);
      const link = h(doc, "a", { href: "#", style: "font-weight:600",
        onclick: async (e) => {
          e.preventDefault();
          if (item) await openAtPage(await item.getBestAttachment(), s.page);
        } }, `[${s.paper}] ${p ? p.label : "?"}${s.page ? `, p. ${s.page}` : ""}`);
      return h(doc, "blockquote", { style: "margin:4px 0 4px 12px;color:var(--fill-secondary)" },
        link, s.reattributed ? " (the model cited a different paper; moved to the one that has it)" : "",
        s.part ? h(doc, "span", { style: "font-style:italic" }, ` — backs “${s.part}”`) : null,
        h(doc, "div", {}, `“${s.quote}”`));
    };
    const grounded = r.claims.filter(c => c.status === "GROUNDED");
    const main = grounded.filter(c => !c.extra), extra = grounded.filter(c => c.extra);
    const ungrounded = r.claims.filter(c => c.status !== "GROUNDED");
    const unused = r.papers.filter(p => !p.used);
    const numbersWarning = (c) => c.unsourced_numbers?.length ? h(doc, "div", { style: "color:#b3261e" },
      `Check this: ${c.unsourced_numbers.join(", ")} is not in any of its sources — the merge may have changed a figure.`) : null;
    const problemsWarning = (c) => (c.problems || []).map(p => h(doc, "div", { style: "color:#b3261e" },
      `Check this: ${p.words ? `“${p.words}” — ` : ""}${p.why}`));
    const claimList = (list) => h(doc, "ol", { style: "padding-left:20px" }, list.map(c =>
      h(doc, "li", { style: "margin-bottom:8px" }, c.claim, numbersWarning(c), ...problemsWarning(c),
        c.sources.filter(s => s.verified).map(sourceLine))));
    const overview = (r.overview || []).map(c => h(doc, "div", { style: "margin-bottom:4px" },
      `${c.claim} `, c.sources.length ? `[${[...new Set(c.sources.map(s => s.paper))].join(", ")}]` : "",
      c.sources.length ? null : h(doc, "div", { style: "color:#b3261e" },
        "Check this: none of its findings backs this sentence."),
      numbersWarning(c), ...problemsWarning(c)));
    if (!r.claims.length) {
      return h(doc, "div", { style: "color:#b3261e" },
        r.truncated
          ? "The model's answer was cut off before a single complete claim came through. "
            + "Try fewer papers or a narrower question."
          : "No usable answer came back from the model. Try rephrasing the question, or fewer papers.");
    }
    return h(doc, "div", {},
      r.truncated ? h(doc, "div", { style: "color:#9a6700;margin-bottom:6px" },
        `The model's answer was cut off; showing the ${r.claims.length} claim(s) that came through complete.`)
        : null,
      h(doc, "div", { style: "margin-bottom:8px" },
        badge(doc, `${r.band} · ${Number(r.confidence).toFixed(2)}`, "#555"), " ",
        `${r.grounded} grounded claim(s), ${r.ungrounded} not verified · `
        + `${r.papers.filter(p => p.used).length} of ${r.papers.length} papers cited`),
      overview.length ? h(doc, "h3", { style: "margin:10px 0 4px" }, "Short answer") : null,
      ...overview,
      overview.length ? h(doc, "h3", { style: "margin:10px 0 4px" }, "Evidence") : null,
      claimList(main),
      extra.length ? h(doc, "h3", { style: "margin:10px 0 4px" }, "Further findings from individual papers") : null,
      extra.length ? claimList(extra) : null,
      ungrounded.length ? h(doc, "div", {},
        h(doc, "b", {}, "Not verified against any paper (no passage matched):"),
        h(doc, "ul", {}, ungrounded.map(c => h(doc, "li", { style: "color:var(--fill-secondary)" }, c.claim))))
        : null,
      r.not_addressed?.length ? h(doc, "div", { style: "margin-top:6px" },
        h(doc, "b", {}, "Not addressed by these papers: "), r.not_addressed.join("; ")) : null,
      unused.length ? h(doc, "div", { style: "margin-top:6px;color:var(--fill-secondary)" },
        "Not cited in the answer: " + unused.map(p => `[${p.n}] ${p.label}`).join("; ")) : null,
      leftOutLines(r).length ? h(doc, "div", { style: "margin-top:6px;color:var(--fill-secondary)" },
        "Not read: " + leftOutLines(r).join("; ")) : null,
      grounded.length ? h(doc, "div", { style: "margin-top:12px" },
        st.note
          ? h(doc, "button", { dataset: { action: "ask-many-open-note" },
            onclick: () => Zotero.getActiveZoteroPane().selectItem(st.note.id) }, "Saved — show the note")
          : h(doc, "button", { dataset: { action: "ask-many-save" },
            onclick: () => this.saveAskManyNote(st) }, "Save as note with citations")) : null);
  },

  /** Save the answer as a standalone note whose citations are live Zotero
   * citations (with page locators), so it can go into Word via Zotero's
   * "Add Note" and keep them. */
  async saveAskManyNote(st) {
    const r = st.result;
    const esc = Zotero.Utilities.htmlSpecialChars;
    const itemByKey = new Map(st.items.map(i => [i.key, i]));
    const paperN = new Map(r.papers.map(p => [p.n, p]));
    const stored = new Map();
    const cite = (n, page) => {
      const item = itemByKey.get(paperN.get(n)?.key);
      if (!item) return "";
      const citationItem = { uris: [Zotero.URI.getItemURI(item)],
        itemData: Zotero.Utilities.Item.itemToCSLJSON(item) };
      stored.set(citationItem.uris[0], citationItem);
      const citation = { citationItems: [page ? { ...citationItem, locator: String(page), label: "page" }
        : citationItem], properties: {} };
      return `<span class="citation" data-citation="${encodeURIComponent(JSON.stringify(citation))}">`
        + `${Zotero.EditorInstanceUtilities.formatCitation(citation)}</span>`;
    };
    // Links into the library: the item itself, and its PDF at a page.
    const reader = new Map();
    for (const item of st.items) {
      const att = await item.getBestAttachment();
      if (att?.attachmentReaderType) reader.set(item.key, att);
    }
    const selectURL = (item) => `zotero://select/${libraryPath(item)}/items/${item.key}`;
    const pdfURL = (item, page) => {
      const att = reader.get(item.key);
      return att && `zotero://open-pdf/${libraryPath(att)}/items/${att.key}${page ? `?page=${page}` : ""}`;
    };
    const itemOf = (n) => itemByKey.get(paperN.get(n)?.key);
    const parts = [`<h1>${esc(st.question)}</h1>`,
      `<p><em>Worth Your Read · ${esc(r.band)} confidence (${Number(r.confidence).toFixed(2)}) · `
        + `read ${r.papers.length} of ${st.items.length} papers; every quote below was found in the paper it cites. `
        + `References and what to read are at the end.</em></p>`];
    const groundedClaims = r.claims.filter(c => c.status === "GROUNDED");
    const ordered = [...groundedClaims.filter(c => !c.extra), ...groundedClaims.filter(c => c.extra)];
    const warnings = (c) => (c.unsourced_numbers?.length
      ? ` <span style="background-color: #ff666680">Check: ${esc(c.unsourced_numbers.join(", "))} is not in its sources</span>` : "")
      + (c.problems || []).map(p => ` <span style="background-color: #ff666680">Check: `
        + `${p.words ? `“${esc(p.words)}” — ` : ""}${esc(p.why)}</span>`).join("");
    if (r.overview?.length) {
      parts.push("<h2>Short answer</h2>");
      for (const c of r.overview) {
        const unbacked = c.sources.length ? ""
          : ` <span style="background-color: #ff666680">Check: none of its findings backs this sentence</span>`;
        parts.push(`<p>${citedClaimHTML(c.claim, c.sources, n => cite(n))}${unbacked}${warnings(c)}</p>`);
      }
      parts.push("<h2>Evidence</h2>");
    }
    for (const c of ordered) {
      if (c.extra && c === ordered.find(x => x.extra)) parts.push("<h2>Further findings from individual papers</h2>");
      const ok = c.sources.filter(s => s.verified);
      parts.push(`<p>${citedClaimHTML(c.claim, ok, n => cite(n))}${warnings(c)}</p>`);
      for (const s of ok) {
        const item = itemOf(s.paper);
        const pdf = item && s.page && pdfURL(item, s.page), web = item && !pdf && onlineURL(item);
        parts.push(`<blockquote><p>“${esc(s.quote)}” ${cite(s.paper, s.page)}`
          + `${pdf ? ` <a href="${pdf}">open p. ${s.page}</a>` : web ? ` <a href="${esc(web)}">online</a>` : ""}`
          + `</p></blockquote>`);
      }
    }
    const ungrounded = r.claims.filter(c => c.status !== "GROUNDED");
    if (ungrounded.length) {
      parts.push("<h2>Not verified against the papers</h2><ul>"
        + ungrounded.map(c => `<li><p>${esc(c.claim)}</p></li>`).join("") + "</ul>");
    }
    if (r.not_addressed?.length) {
      parts.push(`<p><strong>Not addressed by these papers:</strong> ${esc(r.not_addressed.join("; "))}</p>`);
    }
    parts.push(...this.referencesHTML(st, { itemOf, selectURL, pdfURL }));
    const html = `<div data-schema-version="9" data-citation-items="`
      + `${encodeURIComponent(JSON.stringify([...stored.values()]))}">${parts.join("\n")}</div>`;
    const note = new Zotero.Item("note");
    note.libraryID = st.items[0].libraryID;
    note.setNote(html);
    const collection = st.collection || Zotero.getActiveZoteroPane().getSelectedCollection();
    if (collection) note.addToCollection(collection.id);
    await note.saveTx();
    st.note = note;
    this.renderAskMany(st);
    return note;
  },

  /** The note's closing "References" section: every paper asked, each linked
   * to the library (and its PDF), grouped by what you still need to read —
   * highlighted: the papers the answer rests on, and the ones it never read. */
  referencesHTML(st, { itemOf, selectURL, pdfURL }) {
    const r = st.result;
    const esc = Zotero.Utilities.htmlSpecialChars;
    const mark = (color, text) => `<span style="background-color: ${color}">${esc(text)}</span>`;
    const YELLOW = "#ffd40080", RED = "#ff666680";
    const counts = new Map();             // paper n -> [main claims, further findings]
    for (const c of r.claims.filter(c => c.status === "GROUNDED")) {
      for (const n of new Set(c.sources.filter(s => s.verified).map(s => s.paper))) {
        const k = counts.get(n) || [0, 0];
        k[c.extra ? 1 : 0]++;
        counts.set(n, k);
      }
    }
    const entry = (item, label, notes) => {
      if (!item) return `<li><p>${esc(label)}${notes.length ? ` — ${notes.join(" · ")}` : ""}</p></li>`;
      const year = Zotero.Date.strToDate(item.getField("date") || "").year;
      const who = item.firstCreator.replace(/[\u2066-\u2069]/g, "");
      const journal = item.getField("publicationTitle");
      const pdf = pdfURL(item), web = onlineURL(item);
      return `<li><p>${esc([who, year && `(${year})`].filter(Boolean).join(" "))}${who || year ? ". " : ""}`
        + `<a href="${selectURL(item)}">${richTitle(item)}</a>.`
        + `${journal ? ` <em>${esc(journal)}</em>.` : ""}`
        + `${pdf ? ` <a href="${pdf}">PDF</a>` : ""}${web ? ` <a href="${esc(web)}">Online</a>` : ""}${notes.length ? ` — ${notes.join(" · ")}` : ""}</p></li>`;
    };
    // Zotero titles may carry <sub>, <sup>, <i>, <b> (CO<sub>2</sub>): keep those, escape the rest.
    const richTitle = (item) => esc(item.getDisplayTitle())
      .replace(/&lt;(\/?)(sub|sup|i|b)&gt;/g, "<$1$2>");
    const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
    const core = [], detail = [], nothing = [];
    for (const p of r.papers) {
      const [main, extra] = counts.get(p.n) || [0, 0];
      const notes = [];
      if (main) notes.push(plural(main, "answer claim") + (extra ? `, ${plural(extra, "further finding")}` : ""));
      else if (extra) notes.push(`${plural(extra, "further finding")}`);
      else notes.push(p.error ? "the model failed on this paper" : "nothing on the question found");
      if (p.abstract_only && (main || extra)) notes.push(mark(RED, "Only the abstract was read") + " — get the full text");
      (main ? core : extra ? detail : nothing).push([main, entry(itemOf(p.n), p.label, notes), extra]);
    }
    // Read first: the READ_FIRST papers the answer leans on most (answer
    // claims, then further findings); the rest of the cited ones follow.
    core.sort((a, b) => b[0] - a[0] || b[2] - a[2]);
    const first = core.slice(0, READ_FIRST), alsoCited = core.slice(READ_FIRST);
    for (const x of first) x[1] = x[1].replace("<li><p>", `<li><p>${mark(YELLOW, "Read first")} `);
    const byKey = new Map(st.items.map(i => [i.key, i]));
    const unread = (r.left_out || (r.skipped || []).map(label => ({ label }))).map(p =>
      entry(byKey.get(p.key), p.label, [mark(RED, "Not read") + (p.reason ? ` — ${LEFT_OUT[p.reason] || p.reason}` : "")]));
    const section = (title, items) => items.length ? [`<h3>${title}</h3>`, `<ol>${items.join("")}</ol>`] : [];
    return ["<h2>References</h2>",
      `<p>${mark(YELLOW, "Read first")}: the answer leans on these most. `
        + `${mark(RED, "Not read")}: the assistant didn't read these, so the answer says nothing about them — read them yourself. `
        + "Titles open the paper in your library; “PDF” opens your copy, “Online” the publisher's page.</p>",
      ...section("Read first", first.map(x => x[1])),
      ...section("Not read by the assistant", unread),
      ...section("Also cited in the answer", alsoCited.map(x => x[1])),
      ...section("Supporting detail", detail.map(x => x[1])),
      ...section("Read, nothing on the question", nothing.map(x => x[1]))];
  },

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

  /** Manuscript mode: the note in Zotero's own editor, beside a panel that
   * checks each cited sentence against the paper it cites. Edits are tracked:
   * sentences you change are marked and re-checked when you ask, never
   * silently while you type. */
  async openDraftCheck(note) {
    const open = this.drafts.get(note.id);
    if (open) {
      Zotero.getMainWindow().Zotero_Tabs.select(open.tabID);
      return open;
    }
    const win = Zotero.getMainWindow();
    const doc = win.document;
    const { id: tabID, container } = win.Zotero_Tabs.add({
      type: "wyr-draft", title: `Manuscript: ${note.getNoteTitle() || "draft"}`, select: true, data: {},
    });
    const box = doc.createXULElement("hbox");
    box.setAttribute("flex", "1");
    box.style.cssText = "display:flex;height:100%;min-height:0";
    const editor = doc.createXULElement("note-editor");
    editor.style.cssText = "flex:1;min-width:0";
    const panel = h(doc, "div", { style: "width:460px;flex:none;overflow:auto;height:100%;padding:14px 18px;"
      + "box-sizing:border-box;font-size:13px;border-left:1px solid var(--fill-quinary);"
      + "background:var(--material-background);color:var(--fill-primary)" });
    box.append(editor, panel);
    container.replaceChildren(box);
    editor.mode = note.isEditable() ? "edit" : "view";
    editor.viewMode = "tab";
    editor.item = note;
    const draft = { note, tabID, container, panel, editor, rows: [], byKey: new Map(),
      checking: false, status: "Reading citations…" };
    this.drafts.set(note.id, draft);
    this.renderDraft(draft);
    await this.resyncDraft(draft);
    for (const row of draft.rows) row.state = "queued";
    await this.checkRows(draft, draft.rows);
    return draft;
  },

  /** Start a manuscript: a new note (in the given or currently selected
   * collection) opened straight in manuscript mode. */
  async newManuscript(collection = null) {
    const pane = Zotero.getActiveZoteroPane();
    collection ||= pane.getSelectedCollection();
    const note = new Zotero.Item("note");
    note.libraryID = collection?.libraryID ?? pane.getSelectedLibraryID() ?? Zotero.Libraries.userLibraryID;
    note.setNote(`<div data-schema-version="9"><h1>Untitled manuscript</h1><p></p></div>`);
    if (collection) note.addToCollection(collection.id);
    await note.saveTx();
    const draft = await this.openDraftCheck(note);
    draft.editor.focus();
    return draft;
  },

  rowKey(sentence, cited) {
    const text = sentence.replace(/\s+/g, " ").replace(/[\s.;:,]+$/, "").trim().toLowerCase();
    return `${text}|${cited.item?.key || cited.doi || cited.title}`;
  },

  scheduleResync(draft) {
    Zotero.getMainWindow().clearTimeout(draft.resyncTimer);
    draft.resyncTimer = Zotero.getMainWindow().setTimeout(() => this.resyncDraft(draft), 800);
  },

  /** Re-read the note's citations. A (sentence, cited paper) pair seen before
   * keeps its row — verdict, rewording, suggestions; anything new is a fresh
   * row marked as edited and not yet checked. */
  async resyncDraft(draft) {
    const pairs = await this.extractCitations(draft.note);
    const first = !draft.synced;          // rows found on opening get checked; later ones wait
    draft.synced = true;
    const rows = [];
    for (const p of pairs) {
      for (const cited of p.cited) {
        const key = this.rowKey(p.sentence, cited);
        let row = draft.byKey.get(key);
        if (!row) {
          row = { sentence: p.sentence, cited, state: first ? "queued" : "edited" };
          draft.byKey.set(key, row);
        }
        row.index = p.index;
        if (!rows.includes(row)) rows.push(row);
      }
    }
    draft.rows = rows;
    this.updateDraftStatus(draft);
    this.renderDraft(draft);
  },

  async checkRow(draft, row) {
    row.state = "running";
    row.error = null;
    this.renderDraft(draft);
    try {
      row.paper ||= row.cited.item ? (await paperFor(row.cited.item)).paper
        : { reference: row.cited.doi || "", title: row.cited.title };
      row.result = await Client.run("verify", { paper: row.paper, claim: row.sentence });
      row.state = "done";
    }
    catch (e) {
      row.state = "error";
      row.error = e.message;
    }
    this.updateDraftStatus(draft);
    this.renderDraft(draft);
  },

  async checkRows(draft, rows) {
    draft.checking = true;
    for (const row of rows) {
      if (draft.rows.includes(row)) await this.checkRow(draft, row);
    }
    draft.checking = false;
    this.updateDraftStatus(draft);
    this.renderDraft(draft);
  },

  updateDraftStatus(draft) {
    const rows = draft.rows;
    if (!rows.length) {
      draft.status = "No Zotero citations in this note yet — cite papers with the note editor's citation button.";
      return;
    }
    const ok = rows.filter(r => r.state === "done" && r.result?.verdict === "SUPPORTED").length;
    const edited = rows.filter(r => r.state === "edited").length;
    draft.status = `${ok} of ${rows.length} cited claim(s) supported by their source`
      + (edited ? ` · ${edited} edited, not checked yet` : "") + ".";
  },

  renderDraft(draft) {
    const { panel } = draft;
    const doc = panel.ownerDocument;
    const edited = draft.rows.filter(r => r.state === "edited" || r.state === "error");
    panel.replaceChildren(h(doc, "div", { dataset: { wyrDraft: draft.note.id } },
      h(doc, "h3", { style: "margin:0 0 4px" }, "Citation check"),
      h(doc, "div", { style: "color:var(--fill-secondary);margin-bottom:8px" }, draft.status),
      edited.length && !draft.checking
        ? h(doc, "button", { dataset: { action: "check-edited" }, style: "margin-bottom:10px",
          onclick: () => this.checkRows(draft, edited) }, `Check ${edited.length} edited claim(s)`)
        : null,
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
        row.state === "error" ? row.error : row.state === "running" ? "Checking…"
          : row.state === "edited" ? "Edited — not checked yet." : "Waiting…"));
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
      row.revision.applied = true;
      draft.byKey.delete(this.rowKey(row.sentence, row.cited));
      row.sentence = row.revision.text.replace(/[.\s]+$/, "");
      draft.byKey.set(this.rowKey(row.sentence, row.cited), row);
      note.setNote(doc.body.innerHTML);
      await note.saveTx();
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
      s.added = true;   // the edit resync picks up the new citation as an edited row
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
