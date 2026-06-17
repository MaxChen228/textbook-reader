#!/usr/bin/env node
/*
 * render_check.js — ground-truth MathJax validator.
 *
 * Uses the SAME engine the reader ships (mathjax-full / tex-chtml-full) but
 * with the noerrors + noundefined packages REMOVED, so both reader failure
 * modes surface as real errors instead of being silently degraded:
 *   - noerrors absent  → a TeX parse error throws → merror node (yellow box in reader)
 *   - noundefined absent→ an undefined macro throws → merror node (red token in reader)
 * Shared macros (book_pipeline/math_macros.json) are injected identically to the
 * reader, so a formula we "revived" with a macro is reported as good here too.
 *
 * Protocol:
 *   stdin : one JSON array  [{i:<id>, s:<tex source>, d:<display bool>}, ...]
 *   stdout: JSON Lines, one per input  {i, ok:true} | {i, ok:false, err:"<message>"}
 *
 * Memory: the lite DOM accumulates nodes; we rebuild the document every
 * REBUILD_EVERY conversions to keep heap bounded over ~500k formulas.
 * Run with --max-old-space-size if validating the whole corpus at once.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const { mathjax } = require('mathjax-full/js/mathjax.js');
const { TeX } = require('mathjax-full/js/input/tex.js');
const { SVG } = require('mathjax-full/js/output/svg.js');
const { liteAdaptor } = require('mathjax-full/js/adaptors/liteAdaptor.js');
const { RegisterHTMLHandler } = require('mathjax-full/js/handlers/html.js');
const { AllPackages } = require('mathjax-full/js/input/tex/AllPackages.js');

// bussproofs needs a real output jax with getBBox() (proof-tree layout); the
// lite adaptor has none and textbooks never use proof trees → drop it.
// noerrors/noundefined are the whole point to remove (let failures surface).
const SKIP = new Set(['noerrors', 'noundefined', 'bussproofs']);
const PACKAGES = AllPackages.filter((p) => !SKIP.has(p));

function loadMacros() {
  const p = path.join(__dirname, 'math_macros.json');
  try {
    const raw = JSON.parse(fs.readFileSync(p, 'utf8'));
    // math_macros.json = { macros: {...}, ... }  (ignore metadata keys)
    return raw && typeof raw === 'object' && raw.macros ? raw.macros : (raw || {});
  } catch (e) {
    return {};
  }
}

// headless mathjax-full 的 tex.macros option 在「filtered packages」下不生效（實測
// configmacros handler 拿不到定義），但 \def 前綴 100% 可靠。故把 math_macros.json
// 編成 \def preamble 前綴到每條公式 —— 定義與 reader 的 tex.macros 等價、渲染一致。
function buildPreamble(macros) {
  const out = [];
  for (const cs of Object.keys(macros)) {
    const v = macros[cs];
    const name = cs.startsWith('\\') ? cs : '\\' + cs;
    if (typeof v === 'string') {
      out.push(`\\def${name}{${v}}`);
    } else if (Array.isArray(v)) {
      const body = v[0];
      const nargs = Number(v[1] || 0);
      const params = Array.from({ length: nargs }, (_, k) => `#${k + 1}`).join('');
      out.push(`\\def${name}${params}{${body}}`);
    }
  }
  return out.join('');
}

const MACROS = loadMacros();
const PREAMBLE = buildPreamble(MACROS);
const adaptor = liteAdaptor();
RegisterHTMLHandler(adaptor);

function buildDoc() {
  const tex = new TeX({ packages: PACKAGES });
  const svg = new SVG();
  return mathjax.document('', { InputJax: tex, OutputJax: svg });
}

let doc = buildDoc();
const REBUILD_EVERY = 2000;

// data-mjx-error="<message>" carries the human-readable failure reason.
const ERR_ATTR = /data-mjx-error="([^"]*)"/;

function check(src, display) {
  try {
    const node = doc.convert(PREAMBLE + src, { display: !!display });
    const html = adaptor.outerHTML(node);
    const m = html.match(ERR_ATTR);
    if (m) return { ok: false, err: decodeEntities(m[1]) };
    if (html.includes('mjx-merror')) return { ok: false, err: 'merror' };
    return { ok: true };
  } catch (e) {
    return { ok: false, err: String((e && e.message) || e) };
  }
}

// preamble 自身若壞會毒害每條公式 → 啟動先驗（前綴 + 平凡式）。
function assertPreambleClean() {
  const r = check('1', false);
  if (!r.ok) {
    process.stderr.write('render_check: math_macros.json preamble is broken: ' + r.err + '\n');
    process.exit(3);
  }
}

function decodeEntities(s) {
  return String(s)
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');
}

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on('data', (c) => chunks.push(c));
    process.stdin.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    process.stdin.on('error', reject);
  });
}

(async () => {
  assertPreambleClean();
  const text = await readStdin();
  let items;
  try {
    items = JSON.parse(text);
  } catch (e) {
    process.stderr.write('render_check: bad JSON on stdin\n');
    process.exit(2);
  }
  const out = [];
  for (let n = 0; n < items.length; n++) {
    const it = items[n];
    const r = check(it.s, it.d);
    out.push(JSON.stringify({ i: it.i, ok: r.ok, err: r.err }));
    if (out.length >= 256) {
      process.stdout.write(out.join('\n') + '\n');
      out.length = 0;
    }
    if ((n + 1) % REBUILD_EVERY === 0) doc = buildDoc();
  }
  if (out.length) process.stdout.write(out.join('\n') + '\n');
})();
