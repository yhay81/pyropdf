/** Check SVG search, selection and painted pixels in three real browser engines. */
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { chromium, firefox, webkit } = require("playwright-core");
const { PNG } = require("pngjs");

const directory = process.argv[2];
if (!directory) throw new Error("Pass the directory containing the paired SVG files.");

const logicalHebrew = "שָׁלוֹם עולם";
// Points in PDF page coordinates, inside the second glyph of each 18 pt Helvetica word.
const pointerPositions = {
  Searchable: [47, 150],
  Rotated: [100, 53],
  Slanted: [160, 78],
  Invisible: [30, 19],
};

const queries = {
  positions: ["Searchable", "Rotated", "Slanted", "Invisible"],
  "upstream-ocr": ["Scanned page", "with OCR text"],
  "upstream-layer": ["Fish & <Chips>", "fi", "outlined", "up", "縦書", "a"],
  "nonembedded-latin": ["Hello big world"],
  "nonembedded-cjk": ["計画地"],
  "hebrew-niqqud": [logicalHebrew, "םלוע םֹולָׁש"],
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
  if (!node) return { found, selected, width: null, height: null };

  const range = document.createRange();
  range.selectNodeContents(node);
  const { width, height } = range.getBoundingClientRect();
  return { found, selected, width, height };
}

async function clickPdfPoint(page, point, word) {
  const [x, y, hitsGlyph] = await page.evaluate(({ point: [pdfX, pdfY], word }) => {
    window.getSelection().removeAllRanges();
    const svg = document.documentElement;
    const viewportPoint = new DOMPoint(pdfX, 180 - pdfY).matrixTransform(svg.getScreenCTM());
    const node = Array.from(svg.querySelectorAll("text")).find(el => el.textContent === word);
    const hitsGlyph = !!node && node.getCharNumAtPosition(
      viewportPoint.matrixTransform(node.getScreenCTM().inverse())
    ) >= 0;
    return [viewportPoint.x, viewportPoint.y, hitsGlyph];
  }, { point, word });
  await page.mouse.dblclick(x, y);
  return { hitsGlyph, text: await page.evaluate(() => String(window.getSelection())) };
}

// A misplaced but still searchable transparent layer must fail the same pointer test.
async function checkDisplacement(page) {
  const layer = page.locator("#text-layer");
  assert.equal(await layer.getAttribute("transform"), null);
  await layer.evaluate(node => node.setAttribute("transform", "translate(200 65)"));
  const matched = {};
  for (const [word, point] of Object.entries(pointerPositions)) {
    const click = await clickPdfPoint(page, point, word);
    assert.equal(click.hitsGlyph, false, "displaced layer: " + word);
    matched[word] = click.text === word;
  }
  await layer.evaluate(node => node.removeAttribute("transform"));
  assert.ok(Object.values(matched).includes(false), "displacement must change selection");
  return matched;
}

async function checkBrowser(engine, browserType) {
  const browser = await browserType.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 900, height: 800 }, deviceScaleFactor: 1 });
    for (const [fixture, words] of Object.entries(queries)) {
      const original = await openSvg(page, fixture, "paths");
      assert.equal(await page.locator("text").count(), 0);
      assert.equal((await page.evaluate(searchAndMeasure, words[0])).found, false);
      if (fixture === "positions") {
        for (const [word, point] of Object.entries(pointerPositions)) {
          assert.notEqual((await clickPdfPoint(page, point, word)).text, word);
        }
      }
      const layer = await openSvg(page, fixture, "layer");
      const changed = changedPixels(original, layer);
      const displaced = fixture === "positions" ? await checkDisplacement(page) : {};
      for (const word of words) {
        const result = await page.evaluate(searchAndMeasure, word);
        const expected = word !== logicalHebrew;
        assert.equal(result.found, expected, fixture + ": " + word);
        assert.equal(result.selected, expected ? word : "");
        const point = fixture === "positions" ? pointerPositions[word] : null;
        let clicked = "";
        if (point) {
          const click = await clickPdfPoint(page, point, word);
          assert.equal(click.hitsGlyph, true, "aligned layer: " + word);
          assert.equal(click.text, word);
          clicked = click.text;
        }
        console.log([engine, browser.version(), fixture, word, result.found, result.selected, clicked,
          result.width?.toFixed(2) ?? "", result.height?.toFixed(2) ?? "",
          point?.[0] ?? "", point?.[1] ?? "", displaced[word] ?? "", changed].join("\t"));
      }
      assert.equal(changed, 0, engine + ": " + fixture);
    }
  } finally {
    await browser.close();
  }
}

async function main() {
  console.log("browser\tversion\tfixture\tquery\tfound\tselected\tdouble_click\twidth_css_px\theight_css_px\tclick_pdf_x\tclick_pdf_y\tdisplaced_click_matches\tchanged_pixels");
  for (const [engine, browserType] of Object.entries({ chromium, firefox, webkit })) {
    await checkBrowser(engine, browserType);
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
