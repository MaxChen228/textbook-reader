(function () {
  function bindHistoryBackLinks(root = document) {
    root.querySelectorAll('[data-history-back]').forEach((link) => {
      if (link.dataset.historyBackBound === 'true') return;
      link.dataset.historyBackBound = 'true';
      link.addEventListener('click', (event) => {
        if (window.history.length > 1) {
          event.preventDefault();
          window.history.back();
        }
      });
    });
  }

  function countUp(el, target, duration) {
    if (!el) return;
    const start = performance.now();
    el.classList.add('counting');

    function tick(now) {
      const t = Math.min((now - start) / duration, 1);
      const ease = t === 1 ? 1 : 1 - Math.pow(2, -10 * t);
      el.textContent = Math.round(ease * target);
      if (t < 1) {
        requestAnimationFrame(tick);
      } else {
        el.textContent = target;
        el.classList.remove('counting');
      }
    }

    requestAnimationFrame(tick);
  }

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function safeHtml(text) {
    const segments = String(text ?? '')
      .replace(/\r\n/g, '\n')
      .replace(/\r/g, '\n')
      .split(/((?:\$\$[\s\S]*?\$\$))/g);

    return segments.map((segment, index) => {
      if (index % 2 === 1) {
        return escapeHtml(segment.trim());
      }

      return escapeHtml(segment)
        .replace(/\n{3,}/g, '\n\n')
        .replace(/\n/g, '<br>')
        .replace(/^(<br>)+|(<br>)+$/g, '');
    }).join('');
  }

  // MathJax CDN 用 async 載入，呼叫端可能在 CDN executed 前先呼叫到。
  // _mathjax_head.html 把 window.MathJax 設成 config 物件（truthy 但無 typesetPromise），
  // 所以只測 `!window.MathJax` 不夠 — 必須等 typesetPromise 函式真的出現。
  function renderMath(targets) {
    if (!window.MathJax) return Promise.resolve();

    function callTypeset() {
      if (targets == null) return MathJax.typesetPromise();
      const nodes = Array.isArray(targets) ? targets.filter(Boolean) : [targets].filter(Boolean);
      if (!nodes.length) return Promise.resolve();
      return MathJax.typesetPromise(nodes);
    }

    if (typeof MathJax.typesetPromise === 'function') return callTypeset();

    // CDN 尚未 ready：輪詢 startup.promise / typesetPromise（最多 ~3s）。
    return new Promise(resolve => {
      let tries = 0;
      const tick = () => {
        if (window.MathJax && typeof MathJax.typesetPromise === 'function') {
          (window.MathJax.startup && window.MathJax.startup.promise
            ? MathJax.startup.promise
            : Promise.resolve()
          ).then(() => callTypeset()).then(resolve, resolve);
          return;
        }
        if (++tries > 60) {
          console.warn('[renderMath] MathJax not ready after 3s — math left unrendered');
          return resolve();
        }
        setTimeout(tick, 50);
      };
      tick();
    });
  }

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    const contentType = response.headers.get('content-type') || '';
    let payload = null;

    if (contentType.includes('application/json')) {
      payload = await response.json();
    } else {
      const text = await response.text();
      payload = text ? { message: text } : null;
    }

    if (!response.ok) {
      const message = payload && typeof payload === 'object'
        ? payload.error || payload.message || payload.detail
        : null;
      const error = new Error(message || `HTTP ${response.status}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }

    return payload;
  }

  function errorMessage(error, fallback) {
    return (error && error.message) || fallback;
  }

  function createChip({
    text = '',
    html = null,
    count = null,
    className = '',
    active = false,
    tag = 'button',
    type = 'button',
    value = null,
    dataset = {},
    onClick = null,
  } = {}) {
    const chip = document.createElement(tag);
    const extraClasses = String(className || '').trim();
    chip.className = `chip qbk-chip${extraClasses ? ` ${extraClasses}` : ''}${active ? ' active' : ''}`;
    if (tag === 'button' && type) chip.type = type;
    if (value != null) chip.dataset.val = value;
    Object.entries(dataset || {}).forEach(([key, val]) => {
      if (val != null) chip.dataset[key] = val;
    });
    if (html != null) chip.innerHTML = html;
    else chip.textContent = text;
    if (count != null) {
      const n = document.createElement('span');
      n.className = 'n';
      n.textContent = count;
      chip.appendChild(n);
    }
    if (onClick) chip.addEventListener('click', onClick);
    return chip;
  }

  function bindSidebarDrawer({
    sidebarId = 'sidebar',
    overlayId = 'sidebar-overlay',
    buttonId = 'btn-sidebar',
    openClass = 'open',
    closeOnEscape = false,
  } = {}) {
    const sidebar = document.getElementById(sidebarId);
    const overlay = document.getElementById(overlayId);
    const button = document.getElementById(buttonId);
    if (!sidebar || !overlay || !button) return null;

    const open = () => {
      sidebar.classList.add(openClass);
      overlay.classList.add(openClass);
    };
    const close = () => {
      sidebar.classList.remove(openClass);
      overlay.classList.remove(openClass);
    };
    const toggle = () => {
      sidebar.classList.toggle(openClass);
      overlay.classList.toggle(openClass);
    };

    if (button.dataset.drawerBound !== 'true') {
      button.dataset.drawerBound = 'true';
      button.addEventListener('click', toggle);
    }
    if (overlay.dataset.drawerBound !== 'true') {
      overlay.dataset.drawerBound = 'true';
      overlay.addEventListener('click', close);
    }
    if (closeOnEscape && document.body && document.body.dataset.drawerEscapeBound !== 'true') {
      document.body.dataset.drawerEscapeBound = 'true';
      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') close();
      });
    }

    return { open, close, toggle };
  }

  function renderMarkdown(md) {
    if (typeof marked === 'undefined') return safeHtml(md);
    if (!renderMarkdown._htmlSafe) {
      // 一次性：中和 OCR 來源夾帶的原生 HTML（公開站自動爬任意 PDF → 儲存型 XSS）。只動 raw-HTML
      // token，正常 markdown/標題/清單照常；math 已 stash 出去不受影響。失敗則退回原行為、不破渲染。
      try {
        var escHtml = function (h) {
          var s = (h && typeof h === 'object' && h.text != null) ? h.text : h;
          return String(s == null ? '' : s).replace(/</g, '&lt;').replace(/>/g, '&gt;');
        };
        marked.use({ renderer: { html: escHtml } });
      } catch (e) { /* renderer API 變動 → 維持原行為 */ }
      renderMarkdown._htmlSafe = true;
    }
    var stash = [];
    var save = function (m) { stash.push(m); return '\x00M' + (stash.length - 1) + '\x00'; };
    var safe = md.replace(/\$\$[\s\S]*?\$\$/g, save).replace(/\$[^$\n]+?\$/g, save);
    var html = marked.parse(safe);
    html = html.replace(/\x00M(\d+)\x00/g, function (_, i) { return stash[i]; });
    return html;
  }

  // MathJax 設定的單一 source。各頁的 <script>MathJax={...}</script>
  // 應替換為 QBankShared.mathJaxConfig() 後注入。
  const mathJaxConfig = {
    tex: {
      inlineMath: [['$', '$']],
      displayMath: [['$$', '$$']],
      tags: 'none',
      packages: { '[+]': ['ams', 'boldsymbol', 'noerrors', 'noundefined', 'unicode', 'enclose'] },
        /* MACROS:BEGIN — generated from book_pipeline/math_macros.json by build/gen_macros.py; do not edit by hand */
        macros: {
          "\\AA": "{\\unicode{xC5}}",
          "\\Breve": ["\\breve{#1}", 1],
          "\\Ddot": ["\\ddot{#1}", 1],
          "\\P": "{\\unicode{xB6}}",
          "\\Vec": ["\\vec{#1}", 1],
          "\\aa": "{\\unicode{xE5}}",
          "\\allowbreak": "{}",
          "\\astrosun": "{\\unicode{x2609}}",
          "\\circled": ["\\enclose{circle}{#1}", 1],
          "\\dag": "{\\dagger}",
          "\\ddag": "{\\ddagger}",
          "\\displaylimits": "{}",
          "\\dprime": "{\\unicode{x2033}}",
          "\\em": "{}",
          "\\harpoonleft": "{\\leftharpoonup}",
          "\\hdots": "{\\cdots}",
          "\\joinrel": "\\mathrel{\\mkern-3mu}",
          "\\llangle": "{\\unicode{x27EA}}",
          "\\llbracket": "{\\unicode{x27E6}}",
          "\\normalfont": "{}",
          "\\nsimeq": "{\\unicode{x2244}}",
          "\\oiiint": "{\\unicode{x2230}}",
          "\\oiint": "{\\unicode{x222F}}",
          "\\overbar": ["\\overline{#1}", 1],
          "\\overrightharpoon": ["\\overrightarrow{#1}", 1],
          "\\pounds": "{\\unicode{xA3}}",
          "\\rrangle": "{\\unicode{x27EB}}",
          "\\rrbracket": "{\\unicode{x27E7}}",
          "\\sb": "_",
          "\\sc": "{}",
          "\\sp": "^",
          "\\textcircled": ["\\enclose{circle}{#1}", 1],
          "\\textmd": ["\\text{#1}", 1],
          "\\textsc": ["\\text{#1}", 1],
          "\\textsuperscript": ["{}^{\\text{#1}}", 1],
          "\\thickspace": "{\\;}",
          "\\widecheck": ["\\check{#1}", 1],
        },
        /* MACROS:END */
    },
    options: { skipHtmlTags: ['script', 'noscript', 'style', 'textarea'] },
    loader: { load: ['[tex]/ams', '[tex]/boldsymbol', '[tex]/noerrors', '[tex]/noundefined', '[tex]/unicode', '[tex]/enclose'] },
    startup: { typeset: false },
  };

  function printTypographyCss() {
    return `
@import url('https://cdn.jsdelivr.net/npm/computer-modern@0.1.3/cmu-serif.css');
@import url('https://fonts.googleapis.com/css2?family=Noto+Serif+TC:wght@200;300;400;600&display=swap');
:root {
  --latin-serif: 'CMU Serif', 'Computer Modern Serif', 'Computer Modern', 'Latin Modern Roman', 'Times New Roman';
  --cjk-serif: 'Noto Serif TC', 'Source Han Serif TC', '思源宋體 TC', 'Songti TC', 'STSong', serif;
  --serif: var(--latin-serif), var(--cjk-serif), serif;
  --mono: 'JetBrains Mono', 'SF Mono', 'Fira Code', monospace;
}`;
  }

  // 開新視窗、寫 body、注入 MathJax、等 fonts/images 就緒後呼叫 window.print()。
  // bodyHtml 是純內容（不含 <html><head>），title 為視窗標題，extraStyles 為頁面 <style>。
  function openPrintWindow({ title, bodyHtml, extraStyles }) {
    const html = `<!DOCTYPE html><html lang="zh-TW"><head>
<meta charset="UTF-8"><title>${escapeHtml(title || '')}</title>
<script>MathJax=${JSON.stringify(mathJaxConfig)};</` + `script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml-full.js"></` + `script>
<style>${extraStyles || ''}</style></head><body>
${bodyHtml}
<script>
function waitImages(){var imgs=Array.from(document.images||[]);if(!imgs.length)return Promise.resolve();return Promise.all(imgs.map(function(img){if(img.complete)return Promise.resolve();return new Promise(function(r){img.addEventListener("load",r,{once:true});img.addEventListener("error",r,{once:true});});}));}
function nextPaint(){return new Promise(function(r){requestAnimationFrame(function(){requestAnimationFrame(r);});});}
async function printWhenReady(){try{await window.MathJax.startup.promise;await MathJax.typesetPromise();if(document.fonts&&document.fonts.ready)await document.fonts.ready;await waitImages();await nextPaint();setTimeout(function(){window.print();},80);}catch(e){console.error(e);window.print();}}
window.addEventListener("load",printWhenReady,{once:true});
</` + `script></body></html>`;
    const w = window.open('', '_blank');
    if (!w) return null;
    w.document.write(html);
    w.document.close();
    return w;
  }

  function relTime(dt) {
    // 已含時區（Z 或 ±hh:mm 偏移）就別再補 Z，否則 "…+00:00Z" 非法 → NaN
    var d = new Date(/(Z|[+-]\d\d:?\d\d)$/.test(dt) ? dt : dt + 'Z');
    var diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return '剛剛';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 時前';
    return Math.floor(diff / 86400) + ' 天前';
  }

  // 主題 + 換皮統一管理器。三頁共用單一真相：
  //   localStorage 'textbook.settings.v1' 的 .theme(auto/light/dark) 與 .skin(paper/claude…)
  //   兩軸都寫到 <body data-theme data-skin>；token 定義全在 design/tokens.css。
  // 新增一張皮：tokens.css 加區塊 + 下方 SKINS 加名字，UI(data-skin-set 按鈕)自動長出。
  const theme = (function () {
    const KEY = 'textbook.settings.v1';
    const MODES = ['auto', 'light', 'dark'];
    const SKINS = ['paper', 'claude'];
    const mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

    function read() { try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch { return {}; } }
    function patch(p) { const s = read(); Object.assign(s, p); try { localStorage.setItem(KEY, JSON.stringify(s)); } catch {} }
    function mode() { const m = read().theme; return MODES.includes(m) ? m : 'auto'; }
    function skin() { const k = read().skin; return SKINS.includes(k) ? k : 'paper'; }
    function resolvedMode() { const m = mode(); return (m === 'dark' || (m === 'auto' && mq && mq.matches)) ? 'dark' : 'light'; }

    // 把目前狀態寫到 body，並同步任何 [data-theme-set]/[data-skin-set] 控制鈕的 active 態。
    function apply() {
      const b = document.body;
      if (b) { b.dataset.theme = resolvedMode(); b.dataset.skin = skin(); }
      document.querySelectorAll('[data-theme-set]').forEach((el) =>
        el.classList.toggle('active', el.dataset.themeSet === mode()));
      document.querySelectorAll('[data-skin-set]').forEach((el) =>
        el.classList.toggle('active', el.dataset.skinSet === skin()));
    }
    function setMode(m) { if (MODES.includes(m)) { patch({ theme: m }); apply(); } }
    function setSkin(k) { if (SKINS.includes(k)) { patch({ skin: k }); apply(); } }

    // 一次裝好：套用現態 + 綁 [data-theme-set]/[data-skin-set] 委派點擊 + 跟系統明暗。
    function init(opts = {}) {
      apply();
      document.addEventListener('click', (e) => {
        const t = e.target.closest('[data-theme-set]');
        if (t) { setMode(t.dataset.themeSet); opts.onChange && opts.onChange(); return; }
        const s = e.target.closest('[data-skin-set]');
        if (s) { setSkin(s.dataset.skinSet); opts.onChange && opts.onChange(); }
      });
      if (mq) {
        const cb = () => { if (mode() === 'auto') { apply(); opts.onChange && opts.onChange(); } };
        if (mq.addEventListener) mq.addEventListener('change', cb);
        else if (mq.addListener) mq.addListener(cb);
      }
      return api;
    }
    const api = { KEY, MODES, SKINS, read, mode, skin, resolvedMode, apply, setMode, setSkin, init };
    return api;
  })();

  window.QBankShared = {
    bindHistoryBackLinks,
    bindSidebarDrawer,
    countUp,
    createChip,
    escapeHtml,
    fetchJson,
    errorMessage,
    mathJaxConfig,
    openPrintWindow,
    printTypographyCss,
    relTime,
    renderMarkdown,
    renderMath,
    safeHtml,
    theme,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => bindHistoryBackLinks(), { once: true });
  } else {
    bindHistoryBackLinks();
  }
})();
