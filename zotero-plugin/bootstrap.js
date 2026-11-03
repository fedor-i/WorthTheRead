/* Worth Your Read — Zotero plugin bootstrap. All logic lives in content/wyr.js. */
var WorthYourRead;

function install() {}

async function startup({ id, version, rootURI }) {
  Services.scriptloader.loadSubScript(rootURI + "content/wyr.js");
  WorthYourRead = Zotero.WorthYourRead;
  await WorthYourRead.startup({ id, version, rootURI });
}

function onMainWindowLoad({ window }) {
  WorthYourRead?.addToWindow(window);
}

function onMainWindowUnload({ window }) {
  WorthYourRead?.removeFromWindow(window);
}

function shutdown() {
  WorthYourRead?.shutdown();
  WorthYourRead = undefined;
  delete Zotero.WorthYourRead;
}

function uninstall() {}
