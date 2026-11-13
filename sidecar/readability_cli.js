// Reads HTML on stdin, page URL in argv[2]; prints {"title","text"} using @mozilla/readability + jsdom.
// jsdom is created WITHOUT runScripts, so page scripts are never executed.
const fs = require("fs");
const { JSDOM, VirtualConsole } = require("jsdom");
const { Readability } = require("@mozilla/readability");
const html = fs.readFileSync(0, "utf8");
try {
  const dom = new JSDOM(html, { url: process.argv[2] || "https://example.invalid/", virtualConsole: new VirtualConsole() });
  const a = new Readability(dom.window.document).parse();
  const text = a && a.textContent ? a.textContent.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim() : "";
  process.stdout.write(JSON.stringify({ title: (a && a.title) || "", text }));
} catch (e) {
  process.stdout.write(JSON.stringify({ title: "", text: "" }));
}
