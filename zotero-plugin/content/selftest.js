/* Developer self-test: with extensions.wyr.selftest set to an output path,
 * drive the plugin's real buttons against a live sidecar on the paper with
 * DOI 10.1371/journal.pmed.0020124 and write a JSON report. Not shipped. */
Zotero.WorthYourReadSelfTest = {
  async run(outPath) {
    const WYR = Zotero.WorthYourRead;
    const R = { steps: {} };
    const t0 = Date.now();
    const log = (k, v) => { R.steps[k] = v; Zotero.debug("WYR-SELFTEST " + k); };
    const save = () => IOUtils.writeUTF8(outPath, JSON.stringify(R, null, 2));
    const delay = ms => Zotero.Promise.delay(ms);
    const until = async (fn, ms = 600000) => {
      const end = Date.now() + ms;
      while (Date.now() < end) { if (fn()) return true; await delay(500); }
      return false;
    };
    const body = id => WYR.bodies.get(id);
    const click = (root, action) => {
      const b = root.querySelector(`[data-action="${action}"]`);
      if (!b) throw new Error(`no button ${action}`);
      b.click();
    };
    const brief = r => r && ({ verdict: r.verdict, confidence: r.confidence, grounded: r.grounded,
      ungrounded: r.ungrounded, claims: (r.claims || []).map(c => [c.status, c.claim.slice(0, 80),
        c.quotes.filter(q => q.verified).map(q => q.page)]),
      aspects: (r.aspects || []).map(a => [a.type, a.status, a.page]), summary: r.summary });
    try {
      await Zotero.uiReadyPromise;
      await delay(2000);
      const reachableBefore = await WYR.client.reachable();
      log("health", await WYR.client.health());
      if ((Zotero.Prefs.get("extensions.wyr.selftestOnly", true) || "") === "sidecar") {
        // Auto-start only: no job runs, so no model is loaded.
        log("sidecar", { reachableBefore, startedByPlugin: !!WYR.sidecar.proc,
          pid: WYR.sidecar.proc?.pid ?? null, log: WYR.sidecar.logPath() });
        R.seconds = (Date.now() - t0) / 1000;
        await save();
        return;
      }
      const lib = Zotero.Libraries.userLibraryID;
      const all = await Zotero.Items.getAll(lib, true);
      const paper = all.find(i => i.isRegularItem() && i.getField("DOI") === "10.1371/journal.pmed.0020124");
      if (!paper) throw new Error("test paper not in library");
      const pane = Zotero.getActiveZoteroPane();
      await pane.selectItem(paper.id);
      await until(() => body(paper.id)?.isConnected, 10000);
      log("sectionRendered", !!body(paper.id)?.isConnected);

      const only = Zotero.Prefs.get("extensions.wyr.selftestOnly", true) || "";
      if (only === "notes") {
        // The ask-across note's links and References, from a made-up answer
        // over the first papers of the iron collection. No model job runs.
        const win = Zotero.getMainWindow();
        const col = Zotero.Collections.getByLibrary(lib).find(c => c.name.startsWith("WYR: Is primary production"));
        const st = WYR.openAskMany({ collection: col });
        const items = st.items;
        const att0 = await items[0].getBestAttachment();
        log("tab", { limitInput: !!st.container.querySelector("[data-wyr-limit]"), maxPapers: st.maxPapers,
          text: st.container.textContent.slice(0, 400) });
        const p = (n, abstract_only = false) => ({ n, label: items[n - 1].getDisplayTitle().slice(0, 40),
          key: items[n - 1].key, cited: 1, used: true, abstract_only });
        const src = (paper, page, part = null) => ({ paper, quote: `SELFTEST quote ${paper}`, verified: true, page, part });
        st.question = "SELFTEST notes: iron or nitrogen?";
        Object.assign(st, { running: true, progress: { stage: "merge", papers: 5, claims: 2,
          draft: { answer: "SELFTEST draft answer [P1].", passages: 12, papers: 4 },
          findings: [{ paper: 1, label: "Paper one", claim: "SELFTEST live finding." }] } });
        WYR.renderAskMany(st);
        const waiting = st.container.textContent;
        st.running = false;
        WYR.renderAskMany(st);
        log("whileWaiting", { draft: waiting.includes("Quick draft, not checked yet") && waiting.includes("SELFTEST draft answer [P1]."),
          findings: waiting.includes("Checked findings so far (1)") && waiting.includes("SELFTEST live finding."),
          goneAfter: !st.container.textContent.includes("SELFTEST draft answer") });
        st.result = { question: st.question, band: "Medium", confidence: 0.6, grounded: 3, ungrounded: 0,
          merged: true, not_addressed: [],
          papers: [p(1), p(2), p(3), p(4, true), { ...p(5), used: false, cited: 0 }],
          overview: [{ claim: "SELFTEST short answer: iron in HNLC areas.", status: "GROUNDED", sources: [src(1, 3), src(2, null)] },
            { claim: "SELFTEST unbacked sentence.", status: "UNGROUNDED", sources: [] }],
          claims: [
            { claim: "Iron limits one third of the ocean, specifically HNLC areas.", status: "GROUNDED", extra: false,
              sources: [src(1, 3, "Iron limits one third of the ocean"), src(2, null, "specifically HNLC areas"),
                src(1, 4, "specifically HNLC areas")] },
            { claim: "Main claim from paper 4 (abstract), up 2ly%.", status: "GROUNDED", extra: false, sources: [src(4, null), src(1, 5)],
              unsourced_numbers: ["2"],
              problems: [{ words: "another study found", why: "it reads as 2 studies, but its findings come from 1 paper" },
                { words: "", why: "the finding says lesser, not greater" }] },
            { claim: "Further finding from paper 3.", status: "GROUNDED", extra: true, sources: [src(3, 2)] }],
          skipped: [], left_out: [
            { label: items[5].getDisplayTitle(), key: items[5].key, reason: "limit" },
            { label: items[6].getDisplayTitle(), key: items[6].key, reason: "no_text" },
            { label: "Unknown paper", key: "", reason: "load_failed" }] };
        WYR.renderAskMany(st);
        log("resultText", st.container.textContent.slice(-600));
        const tabText = st.container.textContent;
        log("tabWarnings", { numbers: tabText.includes("Check this: 2 is not in any of its sources"),
          problemWords: tabText.includes("Check this: “another study found” — it reads as 2 studies"),
          problemNoWords: tabText.includes("Check this: the finding says lesser, not greater"),
          shortAnswer: tabText.includes("Short answer") && tabText.includes("SELFTEST short answer: iron in HNLC areas. [1, 2]")
            && tabText.includes("Check this: none of its findings backs this sentence.") });
        const note = await WYR.saveAskManyNote(st);
        const html = note.getNote();
        log("noteHTML", { firstPaperHasPDF: !!att0?.attachmentReaderType,
          select: (html.match(/zotero:\/\/select\//g) || []).length,
          openPdf: (html.match(/zotero:\/\/open-pdf\//g) || []).length,
          highlights: (html.match(/background-color/g) || []).length,
          // the first paper's citation sits before ", specifically", the second's after it
          partCitation: html.indexOf('class="citation"', html.indexOf("<p>Iron limits")) < html.indexOf(", specifically HNLC")
            && html.indexOf('class="citation"', html.indexOf(", specifically HNLC")) < html.indexOf("</p>", html.indexOf("<p>Iron limits")),
          noDraft: !html.includes("SELFTEST draft answer") && !tabText.includes("SELFTEST draft answer"),
          shortAnswer: html.indexOf("<h2>Short answer</h2>") < html.indexOf("SELFTEST short answer")
            && html.indexOf("SELFTEST short answer") < html.indexOf("<h2>Evidence</h2>"),
          numbersFlag: html.includes("Check: 2 is not in its sources"),
          problemFlags: html.includes("Check: “another study found” — it reads as 2 studies")
            && html.includes("Check: the finding says lesser, not greater"),
          // paper 1 backs both parts: cited twice in the sentence
          firstPaperCitedTwice: (html.match(/<p>Iron limits[\s\S]*?<\/p>/)?.[0].match(/class="citation"/g) || []).length === 3,
          firstClaim: html.match(/<p>Iron limits[\s\S]*?<\/p>/)?.[0].replace(/<span class="citation"[^>]*>([\s\S]*?)<\/span>/g, "[$1]"),
          online: (html.match(/>Online</g) || []).length, onlineQuotes: (html.match(/>online</g) || []).length,
          onlineSample: html.match(/href="(https?:[^"]+)">Online/)?.[1],
          html: html.slice(html.indexOf("<h2>References")) });
        win.Zotero_Tabs.close(st.tabID);
        // Does Zotero's own note editor keep the links and highlights?
        const { id: tabID, container } = win.Zotero_Tabs.add({ type: "wyr-selftest", title: "selftest", select: true, data: {} });
        const editor = win.document.createXULElement("note-editor");
        editor.style.cssText = "display:flex;height:100%";
        container.append(editor);
        editor.mode = "view";
        editor.viewMode = "tab";
        editor.item = note;
        await delay(4000);
        const idoc = editor.querySelector("iframe")?.contentDocument;
        const pm = idoc?.querySelector(".ProseMirror");
        log("editor", { found: !!pm,
          links: pm ? [...pm.querySelectorAll('a[href^="zotero://"]')].map(a => a.getAttribute("href")).slice(0, 6) : null,
          linkCount: pm?.querySelectorAll('a[href^="zotero://"]').length,
          highlights: pm ? [...pm.querySelectorAll("span[style*=background]")].map(x => x.getAttribute("style")) : null,
          text: pm?.textContent.slice(-900) });
        win.Zotero_Tabs.close(tabID);
        note.deleted = true;
        await note.saveTx();
        R.seconds = (Date.now() - t0) / 1000;
        await save();
        return;
      }
      if (only === "topicAsk") {
        // Continue a topic run from its imported collection: drop duplicate
        // collections of the same name (collections only — items stay), then
        // ask across the fullest one and save the note there.
        const win = Zotero.getMainWindow();
        const question = Zotero.Prefs.get("extensions.wyr.selftestQuestion", true);
        const name = `WYR: ${question.slice(0, 70)}`;
        const cols = Zotero.Collections.getByLibrary(lib).filter(c => c.name === name)
          .sort((a, b) => b.getChildItems().length - a.getChildItems().length);
        const col = cols[0];
        for (const dup of cols.slice(1)) await dup.eraseTx();
        log("collection", { name: col.name, items: col.getChildItems().filter(i => i.isRegularItem()).length,
          removedDuplicates: cols.length - 1 });
        let t = Date.now();
        const st = WYR.openAskMany({ collection: col });
        st.question = question;
        await WYR.runAskMany(st);
        const r = st.result;
        log("askMany", { s: (Date.now() - t) / 1000, error: st.error, band: r?.band, confidence: r?.confidence,
          grounded: r?.grounded, ungrounded: r?.ungrounded, merged: r?.merged, skipped: r?.skipped,
          left_out: r?.left_out, truncated: r?.truncated,
          not_addressed: r?.not_addressed, papers: r?.papers.map(p => [p.n, p.cited, p.abstract_only, p.label]),
          claims: (r?.claims || []).map(c => [c.status, c.extra ? "extra" : "main", c.claim,
            c.sources.map(x => [x.paper, x.page, x.quote.slice(0, 100)])]) });
        if (r?.claims?.some(c => c.status === "GROUNDED")) {
          const note = await WYR.saveAskManyNote(st);
          const html = note.getNote();
          log("note", { id: note.id, inCollection: note.inCollection(col.id),
            citations: (html.match(/class="citation"/g) || []).length,
            select: (html.match(/zotero:\/\/select\//g) || []).length,
            pageLinks: (html.match(/>open p\. /g) || []).length,
            online: (html.match(/>[Oo]nline</g) || []).length,
            references: html.slice(html.indexOf("<h2>References")).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ") });
        }
        win.Zotero_Tabs.close(st.tabID);
        R.seconds = (Date.now() - t0) / 1000;
        await save();
        return;
      }
      if (only === "topic") {
        // A whole topic, end to end: research → import → ask across the collection → note.
        const win = Zotero.getMainWindow();
        const question = Zotero.Prefs.get("extensions.wyr.selftestQuestion", true);
        let t = Date.now();
        const rs = WYR.openResearch({ question, title: question, autostart: true, maxPapers: 60 });
        await until(() => !rs.running && (rs.result || rs.error), 1800000);
        const res = rs.result;
        log("research", { s: (Date.now() - t) / 1000, error: rs.error, summary: res?.summary,
          rounds: res?.rounds, stopped: res?.stopped,
          papers: (res?.papers || []).map(p => [p.relevance ?? "seed", p.round, p.cited_core ? "core" : "",
            p.year, (p.title || "").slice(0, 90), p.doi || ""]) });
        await save();
        if (!res) throw new Error("research failed: " + rs.error);
        // Import the strongest: every relevance-3 paper, then relevance 2 by support, up to 25.
        const ranked = res.papers.filter(p => p.doi && !p.cited_core)
          .sort((a, b) => (b.relevance ?? 3) - (a.relevance ?? 3) || (b.support || 0) - (a.support || 0));
        rs.selected = new Set(ranked.filter(p => (p.relevance ?? 3) >= 2).slice(0, 25).map(p => p.id));
        t = Date.now();
        await WYR.importResearch(rs);
        const col = Zotero.Collections.get(rs.collectionID);
        const items = col.getChildItems().filter(i => i.isRegularItem());
        const withPDF = [];
        for (const i of items) if (await i.getBestAttachment()) withPDF.push(i.id);
        log("import", { s: (Date.now() - t) / 1000, msg: rs.importMsg, collection: col.name,
          items: items.length, withPDF: withPDF.length });
        await save();
        win.Zotero_Tabs.close(rs.tabID);

        t = Date.now();
        const st = WYR.openAskMany({ collection: col });
        st.question = question;
        await WYR.runAskMany(st);
        const r = st.result;
        log("askMany", { s: (Date.now() - t) / 1000, error: st.error, band: r?.band, confidence: r?.confidence,
          grounded: r?.grounded, ungrounded: r?.ungrounded, merged: r?.merged, skipped: r?.skipped,
          left_out: r?.left_out, truncated: r?.truncated,
          not_addressed: r?.not_addressed, papers: r?.papers.map(p => [p.n, p.cited, p.abstract_only, p.label]),
          claims: (r?.claims || []).map(c => [c.status, c.extra ? "extra" : "main", c.claim,
            c.sources.map(x => [x.paper, x.page, x.quote.slice(0, 100)])]) });
        if (r?.claims?.some(c => c.status === "GROUNDED")) {
          const note = await WYR.saveAskManyNote(st);
          const html = note.getNote();
          log("note", { id: note.id, inCollection: note.inCollection(col.id),
            citations: (html.match(/class="citation"/g) || []).length,
            select: (html.match(/zotero:\/\/select\//g) || []).length,
            pageLinks: (html.match(/>open p\. /g) || []).length,
            online: (html.match(/>[Oo]nline</g) || []).length,
            references: html.slice(html.indexOf("<h2>References")).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ") });
        }
        win.Zotero_Tabs.close(st.tabID);
        R.seconds = (Date.now() - t0) / 1000;
        await save();
        return;
      }
      if (only === "live2") {
        // Approved live run: one cross-paper question, one manuscript re-check.
        const win = Zotero.getMainWindow();
        const keys = ["CEUPQHKJ", "YU69WJ6N", "J93PJPZL", "EAUCCQ6M"];
        const papers = keys.map(k => Zotero.Items.getByLibraryAndKey(lib, k)).filter(Boolean);
        const question = "How likely is a published research finding to be false, and what affects that probability?";
        // Earlier empty answers saved as notes go to the trash.
        const stale = all.filter(i => i.isNote() && !i.deleted && i.getNoteTitle() === question);
        for (const n of stale) { n.deleted = true; await n.saveTx(); }
        log("trashedStaleNotes", stale.length);
        const st = WYR.openAskMany({ items: papers });
        st.question = question;
        let t = Date.now();
        await WYR.runAskMany(st);
        const r = st.result;
        log("askMany", { s: (Date.now() - t) / 1000, error: st.error, band: r?.band, confidence: r?.confidence,
          grounded: r?.grounded, ungrounded: r?.ungrounded, skipped: r?.skipped, not_addressed: r?.not_addressed,
          papers: r?.papers, claims: (r?.claims || []).map(c => [c.status, c.claim,
            c.sources.map(x => [x.paper, x.verified, x.reattributed, x.page, x.quote.slice(0, 90)])]) });
        if (r) {
          const note = await WYR.saveAskManyNote(st);
          const html = note.getNote();
          const d = new win.DOMParser().parseFromString(html, "text/html");
          log("askManyNote", { citations: d.querySelectorAll("span.citation").length,
            citeTexts: [...d.querySelectorAll("span.citation")].slice(0, 6).map(x => x.textContent),
            storedItems: JSON.parse(decodeURIComponent(d.querySelector("[data-citation-items]")
              ?.dataset.citationItems || "%5B%5D")).length,
            text: d.body.textContent.slice(0, 700) });
        }
        log("askManyTabText", st.container.textContent.slice(0, 1500));
        win.Zotero_Tabs.close(st.tabID);
        if (Zotero.Prefs.get("extensions.wyr.selftestAskOnly", true)) {
          R.seconds = (Date.now() - t0) / 1000;
          await save();
          return;
        }

        const ms = await WYR.newManuscript(null);
        const cite = encodeURIComponent(JSON.stringify({
          citationItems: [{ uris: [Zotero.URI.getItemURI(paper)] }], properties: {} }));
        ms.note.setNote(`<div data-schema-version="9"><h1>Manuscript live test</h1><p>Most published research `
          + `findings are false, regardless of study design or field `
          + `<span class="citation" data-citation="${cite}">(Ioannidis 2005)</span>.</p></div>`);
        await ms.note.saveTx();
        await until(() => ms.rows.length > 0 && ms.panel.querySelector('[data-action="check-edited"]'), 10000);
        t = Date.now();
        ms.panel.querySelector('[data-action="check-edited"]').click();
        await until(() => ms.rows.every(r => ["done", "error"].includes(r.state)), 300000);
        log("manuscriptRecheck", { s: (Date.now() - t) / 1000, status: ms.status,
          rows: ms.rows.map(r => [r.sentence, r.state, r.result?.verdict, r.result?.confidence, r.error || null]),
          panel: ms.panel.textContent.slice(0, 500) });
        win.Zotero_Tabs.close(ms.tabID);
        ms.note.deleted = true;
        await ms.note.saveTx();
        R.seconds = (Date.now() - t0) / 1000;
        await save();
        return;
      }
      if (only === "ui") {
        // New UI without running any model job: menus, the ask-across tab,
        // and manuscript mode tracking an edit.
        const win = Zotero.getMainWindow();
        const papers = all.filter(i => i.isRegularItem()).slice(0, 3);
        await pane.selectItems(papers.map(i => i.id));
        const popup = win.document.getElementById("zotero-itemmenu");
        await pane.buildItemContextMenu();
        const shown = new Promise(r => popup.addEventListener("popupshown", r, { once: true }));
        popup.openPopup(win.document.getElementById("zotero-items-tree"), "overlap", 50, 50);
        await Promise.race([shown, delay(5000)]);
        await delay(500);
        const askEntry = popup.querySelector('menuitem[data-l10n-id="wyr-menu-ask-many"]');
        log("askManyMenu", { selected: papers.length, found: !!askEntry, hidden: askEntry?.hidden });
        popup.hidePopup();
        const coll = Zotero.Collections.getByLibrary(lib)[0];
        if (coll) {
          await pane.collectionsView.selectByID("C" + coll.id);
          const cpop = win.document.getElementById("zotero-collectionmenu");
          await pane.buildCollectionContextMenu();
          const cshown = new Promise(r => cpop.addEventListener("popupshown", r, { once: true }));
          cpop.openPopup(win.document.getElementById("zotero-collections-tree"), "overlap", 20, 20);
          await Promise.race([cshown, delay(5000)]);
          await delay(500);
          const ce = cpop.querySelector('menuitem[data-l10n-id="wyr-menu-ask-collection"]');
          log("askCollectionMenu", { collection: coll.name, found: !!ce, hidden: ce?.hidden });
          cpop.hidePopup();
        }
        const st = WYR.openAskMany({ items: papers });
        log("askManyTab", { items: st?.items.length, text: st?.container.textContent.slice(0, 300),
          hasAskButton: !!st?.container.querySelector('[data-action="ask-many"]') });
        win.Zotero_Tabs.close(st.tabID);

        // Tools → New manuscript: a fresh note opened in manuscript mode.
        const ms = await WYR.newManuscript(coll || null);
        log("newManuscript", { noteTitle: ms.note.getNoteTitle(), inCollection: coll ? ms.note.inCollection(coll.id) : null,
          editor: ms.editor?.localName, editorItem: ms.editor?.item?.id === ms.note.id, rows: ms.rows.length,
          tabSelected: win.Zotero_Tabs.selectedID === ms.tabID,
          toolsMenu: [...win.document.querySelectorAll('menuitem[data-l10n-id="wyr-menu-new-manuscript"]')].length });
        win.Zotero_Tabs.close(ms.tabID);
        ms.note.deleted = true;
        await ms.note.saveTx();

        // Manuscript mode on a note with no citations: opening runs no check.
        const note = new Zotero.Item("note");
        note.libraryID = lib;
        note.setNote(`<div data-schema-version="9"><h1>Manuscript UI test</h1><p>No citations yet.</p></div>`);
        await note.saveTx();
        const draft = await WYR.openDraftCheck(note);
        await delay(3000);
        log("manuscriptOpen", { status: draft.status, rows: draft.rows.length,
          editor: draft.editor?.localName, editorItem: draft.editor?.item?.id === note.id,
          panelText: draft.panel.textContent.slice(0, 200) });
        // An edit adds a cited sentence: it should appear as edited, unchecked.
        const cite = encodeURIComponent(JSON.stringify({
          citationItems: [{ uris: [Zotero.URI.getItemURI(paper)] }], properties: {} }));
        note.setNote(`<div data-schema-version="9"><h1>Manuscript UI test</h1><p>Most published research `
          + `findings are false <span class="citation" data-citation="${cite}">(Ioannidis 2005)</span>.</p></div>`);
        await note.saveTx();
        await until(() => draft.rows.length > 0, 10000);
        log("manuscriptEdit", { status: draft.status, rows: draft.rows.map(r => [r.sentence, r.state]),
          checkButton: !!draft.panel.querySelector('[data-action="check-edited"]') });
        win.Zotero_Tabs.close(draft.tabID);
        await delay(500);
        log("manuscriptClosed", { stillTracked: WYR.drafts.has(note.id) });
        note.deleted = true;            // to the Zotero trash, not erased
        await note.saveTx();
        R.seconds = (Date.now() - t0) / 1000;
        await save();
        return;
      }
      if (only === "research") {
        // Clear earlier runs: their collection goes, and the items they imported
        // (anything in it except the test paper) go to the Zotero trash.
        for (const c of Zotero.Collections.getByLibrary(lib).filter(c => c.name.startsWith("WYR: Similar:"))) {
          for (const it of c.getChildItems().filter(i => i.id !== paper.id && i.dateAdded > "2026-11-02")) {
            it.deleted = true;
            await it.saveTx();
          }
          await c.eraseTx();
        }
        // Find similar on the test paper, small cap so it finishes quickly.
        const progressSeen = [];
        const rs = WYR.findSimilar(paper, { maxPapers: 15 });
        await until(() => !rs.running && (rs.result || rs.error), 900000);
        log("research", { error: rs.error, summary: rs.result?.summary, rounds: rs.result?.rounds,
          papers: (rs.result?.papers || []).map(p => [p.relevance ?? "seed", p.round, p.cited_core ? "core" : "",
            (p.title || "").slice(0, 60), p.in_library ? "lib" : ""]),
          preselected: rs.selected.size,
          tabText: rs.container.textContent.slice(0, 600) });
        if (rs.result) {
          const pick = rs.result.papers.filter(p => p.doi && !p.in_library).slice(0, 2);
          rs.selected = new Set(pick.map(p => p.id));
          WYR.renderResearch(rs);
          const btn = rs.container.querySelector('[data-action="research-import"]');
          btn.click();
          await until(() => /Collection/.test(rs.importMsg || ""), 300000);
          const col = Zotero.Collections.get(rs.collectionID);
          log("researchImport", { message: rs.importMsg, collection: col?.name,
            items: col ? col.getChildItems().map(i => i.getDisplayTitle().slice(0, 60)) : [],
            imported: pick.map(p => p.title.slice(0, 60)) });
        }
        R.seconds = (Date.now() - t0) / 1000;
        await save();
        return;
      }
      if (only === "highlight") {
        const att0 = await paper.getBestAttachment();
        const wyrAnns = () => att0.getAnnotations().filter(a => a.hasTag("worth-your-read"));
        const tempAnns = () => wyrAnns().filter(a => a.hasTag("worth-your-read:temporary"));
        // Start clean: remove highlights earlier test runs left on this test PDF.
        if (wyrAnns().length) await Zotero.Items.erase(wyrAnns().map(a => a.id));
        const hlDone = () => /Highlighted|couldn't|No |There's/.test(WYR.state.get(paper.id)?.hl || "");
        const reselect = async () => {
          Zotero.getMainWindow().Zotero_Tabs.select("zotero-pane");
          await pane.selectItem(paper.id);
          await until(() => body(paper.id)?.isConnected, 10000);
        };
        const runIn = async (kind, text) => {
          const b = body(paper.id);
          b.querySelector("textarea").value = text;
          click(b, kind);
          await until(() => !WYR.state.get(paper.id)?.busy && WYR.state.get(paper.id)?.view === kind
            || WYR.state.get(paper.id)?.error);
        };
        // 1) verify -> highlight: temporary highlights, reader opens
        await runIn("verify", "Most published research findings are false, regardless of study design or field.");
        click(body(paper.id), "highlight");
        await until(hlDone, 120000);
        await delay(2000);
        log("highlightVerify", { message: WYR.state.get(paper.id).hl, wyr: wyrAnns().length, temporary: tempAnns().length,
          tabType: Zotero.getMainWindow().Zotero_Tabs.selectedType,
          annotations: wyrAnns().map(a => ({ color: a.annotationColor, page: a.annotationPageLabel,
            text: a.annotationText.slice(0, 60), comment: a.annotationComment.slice(0, 90) })) });
        // 2) the next action (a new question) removes them
        await reselect();
        await runIn("ask", "Which factors make a research finding less likely to be true?");
        await delay(1500);
        log("nextActionClears", { wyr: wyrAnns().length, temporary: tempAnns().length });
        // 3) highlight the answer, then Keep -> permanent
        click(body(paper.id), "highlight");
        await until(hlDone, 120000);
        const made = tempAnns().length;
        await reselect();
        click(body(paper.id), "keep-highlights");
        await until(() => /Kept/.test(WYR.state.get(paper.id)?.hl || ""), 10000);
        log("keep", { madeTemporary: made, message: WYR.state.get(paper.id).hl,
          wyr: wyrAnns().length, temporary: tempAnns().length });
        // 4) a temporary highlight goes when its PDF tab closes
        await runIn("verify", "The smaller the studies conducted in a scientific field, the less likely the research findings are to be true.");
        click(body(paper.id), "highlight");
        await until(hlDone, 120000);
        await delay(2000);
        const tempBefore = tempAnns().length;
        const win = Zotero.getMainWindow();
        const readerTab = win.Zotero_Tabs._tabs.find(t => t.type === "reader");
        if (readerTab) win.Zotero_Tabs.close(readerTab.id);
        await until(() => tempAnns().length === 0, 10000);
        log("closeTabClears", { tempBefore, readerTabClosed: !!readerTab, temporaryAfter: tempAnns().length,
          keptStillThere: wyrAnns().length });
        R.seconds = (Date.now() - t0) / 1000;
        await save();
        return;
      }

      // Ask (through the section's own textarea + button)
      let b = body(paper.id);
      b.querySelector("textarea").value = "Which factors make a research finding less likely to be true?";
      let t = Date.now();
      click(b, "ask");
      await until(() => !WYR.state.get(paper.id)?.busy && WYR.state.get(paper.id)?.view === "ask"
        || WYR.state.get(paper.id)?.error);
      log("ask", { s: (Date.now() - t) / 1000, error: WYR.state.get(paper.id).error,
        ...brief(WYR.state.get(paper.id).result) });
      log("askSectionText", body(paper.id).textContent.slice(0, 600));

      // Verify a claim (button) and Reword (button that appears on OVERSTATED)
      b = body(paper.id);
      b.querySelector("textarea").value = "Most published research findings are false, regardless of study design or field.";
      t = Date.now();
      click(b, "verify");
      await until(() => !WYR.state.get(paper.id)?.busy && WYR.state.get(paper.id)?.view === "verify"
        || WYR.state.get(paper.id)?.error);
      log("verify", { s: (Date.now() - t) / 1000, error: WYR.state.get(paper.id).error,
        ...brief(WYR.state.get(paper.id).result) });
      const rewordBtn = body(paper.id).querySelector('[data-action="reword"]');
      log("rewordButtonShown", !!rewordBtn);
      if (rewordBtn) {
        t = Date.now();
        rewordBtn.click();
        await until(() => !WYR.state.get(paper.id)?.busy && WYR.state.get(paper.id)?.view === "revise"
          || WYR.state.get(paper.id)?.error);
        const r = WYR.state.get(paper.id).result || {};
        log("reword", { s: (Date.now() - t) / 1000, revised: r.revised, claim: r.claim, verdict: r.verdict,
          error: WYR.state.get(paper.id).error });
      }

      // Summarize -> child note
      const notesBefore = paper.getNotes().length;
      t = Date.now();
      click(body(paper.id), "summarize");
      await until(() => !WYR.state.get(paper.id)?.busy && WYR.state.get(paper.id)?.view === "summarize"
        || WYR.state.get(paper.id)?.error);
      const sr = WYR.state.get(paper.id).result;
      log("summarize", { s: (Date.now() - t) / 1000, error: WYR.state.get(paper.id).error,
        notesBefore, notesAfter: paper.getNotes().length,
        noteStart: sr?.note_id ? Zotero.Items.get(sr.note_id).getNote().slice(0, 300) : null, ...brief(sr) });

      // Jump to a quote's page (click the first "p. N" link)
      const link = [...body(paper.id).querySelectorAll("a")].find(a => /^p\. \d+$/.test(a.textContent));
      if (link) {
        link.click();
        await until(() => Zotero.getMainWindow().Zotero_Tabs.selectedType === "reader", 15000);
        await delay(3000);
        const reader = Zotero.Reader.getByTabID(Zotero.getMainWindow().Zotero_Tabs.selectedID);
        log("jumpToPage", { link: link.textContent, tabType: Zotero.getMainWindow().Zotero_Tabs.selectedType,
          readerItem: reader?.itemID });
        Zotero.getMainWindow().Zotero_Tabs.select("zotero-pane");
      }

      // Verify a PDF selection (the popup button's handler)
      const att = await paper.getBestAttachment();
      t = Date.now();
      const sel = await WYR.verifySelection(att.id,
        "The smaller the studies conducted in a scientific field, the less likely the research findings are to be true.");
      log("verifySelection", { s: (Date.now() - t) / 1000, ...brief(sel) });

      // Write mode: a draft note citing the paper
      const cite = encodeURIComponent(JSON.stringify({
        citationItems: [{ uris: [Zotero.URI.getItemURI(paper)] }], properties: {} }));
      const note = new Zotero.Item("note");
      note.libraryID = lib;
      note.setNote(`<div data-schema-version="9"><h1>Draft intro</h1><p>Research findings are often unreliable. `
        + `Most published research findings are false, regardless of study design or field `
        + `<span class="citation" data-citation="${cite}">(Ioannidis 2005)</span>. `
        + `Smaller studies make a research finding less likely to be true `
        + `<span class="citation" data-citation="${cite}">(Ioannidis 2005)</span>.</p></div>`);
      await note.saveTx();
      log("extractCitations", (await WYR.extractCitations(note)).map(p => [p.index, p.sentence, p.cited.map(c => c.title)]));
      await pane.selectItem(note.id);
      log("menuRegistered", !!WYR.menuID);
      // Open the item context menu and click our entry, as a user would.
      const win = Zotero.getMainWindow();
      const popup = win.document.getElementById("zotero-itemmenu");
      await pane.buildItemContextMenu();   // what a right-click runs first
      const shown = new Promise(r => popup.addEventListener("popupshown", r, { once: true }));
      popup.openPopup(win.document.getElementById("zotero-items-tree"), "overlap", 50, 50);
      await Promise.race([shown, delay(5000)]);
      await delay(500);
      const entry = popup.querySelector('menuitem[data-l10n-id="wyr-menu-check-draft"]');
      log("menuEntry", { found: !!entry, hidden: entry?.hidden, label: entry?.label,
        custom: [...popup.querySelectorAll("menuitem")].map(m => m.getAttribute("data-l10n-id")).filter(Boolean).slice(-8) });
      t = Date.now();
      if (entry) entry.doCommand(); else WYR.openDraftCheck(note);
      popup.hidePopup();
      await until(() => WYR.drafts.get(note.id)?.rows?.length
        && WYR.drafts.get(note.id).rows.every(r => ["done", "error"].includes(r.state)));
      const draft = WYR.drafts.get(note.id);
      log("draftCheck", { s: (Date.now() - t) / 1000, status: draft.status,
        rows: draft.rows.map(r => [r.sentence, r.result?.verdict, r.error || null]) });
      const tabRoot = () => draft.container.querySelector("[data-wyr-draft]");
      const bad = draft.rows.findIndex(r => r.result && r.result.verdict !== "SUPPORTED");
      if (bad >= 0) {
        const row = draft.rows[bad];
        const card = () => tabRoot().querySelector(`[data-row="${bad}"]`);
        row.error = null;
        t = Date.now();
        click(card(), "find-source");
        await until(() => !row.busy && (row.suggestions || row.error), 900000);
        log("draftSuggest", { s: (Date.now() - t) / 1000, error: row.error || null,
          suggestions: (row.suggestions || []).map(s => [s.verdict, s.doi, (s.title || "").slice(0, 70)]) });
        if (row.suggestions?.length) {
          const before = (note.getNote().match(/class="citation"/g) || []).length;
          t = Date.now();
          click(card(), "add-cite");
          await until(() => !row.busy, 120000);
          log("draftAddCite", { s: (Date.now() - t) / 1000, error: row.error || null, citationsBefore: before,
            citationsAfter: (note.getNote().match(/class="citation"/g) || []).length,
            citeTexts: [...new (Zotero.getMainWindow().DOMParser)().parseFromString(note.getNote(), "text/html")
              .querySelectorAll("span.citation")].map(x => x.textContent) });
        }
        t = Date.now();
        click(card(), "reword");
        await until(() => !row.busy && (row.revision || row.error));
        log("draftReword", { s: (Date.now() - t) / 1000, revision: row.revision, error: row.error || null });
        if (row.revision) {
          click(card(), "apply");
          await until(() => row.revision.applied && !row.busy || row.error, 120000);
          log("draftApply", { applied: row.revision.applied, error: row.error || null,
            reverified: row.result?.verdict,
            noteText: note.getNote().replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").slice(0, 400) });
        }
      }
      log("draftTabText", tabRoot().textContent.slice(0, 1500));
    }
    catch (e) {
      R.fatal = String(e) + "\n" + e.stack;
    }
    R.seconds = (Date.now() - t0) / 1000;
    await save();
  },
};
