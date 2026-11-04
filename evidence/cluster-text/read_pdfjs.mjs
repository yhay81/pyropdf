// Print what pdf.js extracts from a PDF's first page.
// Usage: node read_pdfjs.mjs PDFJS_DIR FILE, where PDFJS_DIR holds node_modules/pdfjs-dist.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const [directory, file] = process.argv.slice(2);
const module = join(directory, "node_modules/pdfjs-dist/legacy/build/pdf.mjs");
const { getDocument } = await import(pathToFileURL(module).href);

const pdf = await getDocument({ data: new Uint8Array(readFileSync(file)), verbosity: 0 }).promise;
const page = await pdf.getPage(1);
const content = await page.getTextContent();
process.stdout.write(content.items.map((item) => item.str + (item.hasEOL ? "\n" : "")).join(""));
await pdf.destroy();
