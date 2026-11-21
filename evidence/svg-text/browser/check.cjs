/** Check SVG search, selection and painted pixels in three real browser engines. */
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { chromium, firefox, webkit } = require("playwright-core");
const { PNG } = require("pngjs");

const directory = process.argv[2];
if (!directory) throw new Error("Pass the directory containing the paired SVG files.");

const queries = {
  positions: ["Searchable", "Rotated", "Slanted", "Invisible"],
  "upstream-ocr": ["Scanned page", "with OCR text"],
  "upstream-layer": ["Fish & <Chips>", "fi", "outlined", "up", "縦書", "a"],
  "nonembedded-latin": ["Hello big world"],
  "nonembedded-cjk": ["計画地"],
  "hebrew-niqqud": ["שָׁלוֹם עולם", "םלוע םֹולָׁש"],
};

async function openSvg(page, fixture, mode) {
  const file = path.resolve(directory, fixture + "-0-" + mode + ".svg");
  await page.goto(pathToFileURL(file).href);
  await page.mouse.move(895, 795);
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  return PNG.sync.read(await page.screenshot({ animations: "disabled", caret: "hide" }));
}

function changedPixels(a, b) {
  assert.equal(a.width, b.width);
  assert.equal(a.height, b.height);
  let changed = 0;
  for (let i = 0; i < a.data.length; i += 4) {
    if (a.data.readUInt32LE(i) !== b.data.readUInt32LE(i)) changed++;
  }
  return changed;
}

// Evaluated by the browser in the SVG document.
function searchAndMeasure(word) {
  const selection = window.getSelection();
  selection.removeAllRanges();
  const found = window.find(word, true, false, true, false, false, false);
  const selected = selection.toString();
  selection.removeAllRanges();
  const node = Array.from(document.querySelectorAll("text")).find(el => el.textContent === word);
  if (!node) return { found, selected, width: null, height: null, point: null };

  const range = document.createRange();
  range.selectNodeContents(node);
  const { width, height } = range.getBoundingClientRect();
  const ch = node.getExtentOfChar(Math.min(1, node.getNumberOfChars() - 1));
  const middle = new DOMPoint(ch.x + ch.width / 2, ch.y + ch.height / 2);
  const point = middle.matrixTransform(node.getScreenCTM());
  return { found, selected, width, height, point: [point.x, point.y] };
}

async function checkBrowser(engine, browserType) {
  const browser = await browserType.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 900, height: 800 }, deviceScaleFactor: 1 });
    for (const [fixture, words] of Object.entries(queries)) {
      const original = await openSvg(page, fixture, "paths");
      assert.equal(await page.locator("text").count(), 0);
      assert.equal((await page.evaluate(searchAndMeasure, words[0])).found, false);
      const layer = await openSvg(page, fixture, "layer");
      const changed = changedPixels(original, layer);
      for (const word of words) {
        const result = await page.evaluate(searchAndMeasure, word);
        let clicked = "";
        if (fixture === "positions") {
          assert.ok(result.point);
          await page.mouse.dblclick(...result.point);
          clicked = await page.evaluate(() => String(getSelection()));
          assert.equal(clicked, word);
        }
        if (fixture !== "hebrew-niqqud") {
          assert.equal(result.found, true, fixture + ": " + word);
          assert.equal(result.selected, word);
        }
        console.log([engine, browser.version(), fixture, word, result.found, result.selected, clicked,
          result.width?.toFixed(2) ?? "", result.height?.toFixed(2) ?? "", changed].join("\t"));
      }
      assert.equal(changed, 0, engine + ": " + fixture);
    }
  } finally {
    await browser.close();
  }
}

async function main() {
  console.log("browser\tversion\tfixture\tquery\tfound\tselected\tdouble_click\twidth_css_px\theight_css_px\tchanged_pixels");
  for (const [engine, browserType] of Object.entries({ chromium, firefox, webkit })) {
    await checkBrowser(engine, browserType);
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
