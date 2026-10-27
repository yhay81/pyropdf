// Print pdf.js's reading of each PDF file named on the command line, one JSON
// line per file: each page's size in points and its text. tools/oracles.py
// runs this as a development-only oracle.
import { readFile } from "node:fs/promises";
import { getDocument } from "pdfjs-dist/legacy/build/pdf.mjs";

for (const path of process.argv.slice(2)) {
  const data = new Uint8Array(await readFile(path));
  const task = getDocument({ data, verbosity: 0, isEvalSupported: false });
  const document = await task.promise;
  const pages = [];
  for (let number = 1; number <= document.numPages; number += 1) {
    const page = await document.getPage(number);
    const { width, height } = page.getViewport({ scale: 1 });
    const content = await page.getTextContent();
    const text = content.items.map((item) => item.str ?? "").join("");
    pages.push([width, height, text]);
  }
  await task.destroy();
  process.stdout.write(`${JSON.stringify(pages)}\n`);
}
