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
