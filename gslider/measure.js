/* gslider measure script.
 *
 * Injected at the end of <body> in a temp copy of the deck where only one
 * `.slide-wrap.gslider-render` is visible at 1920x1080. It waits for the page
 * and its fonts, measures every text block of that slide, writes the result as
 * JSON into a JSON script element with a random per-run id, and then
 * tags the measured blocks (data-gslider-hide) and gradient-text elements
 * (data-gslider-gradient) so the plate CSS hides their text for the screenshot.
 *
 * Chrome runs with --dump-dom and --virtual-time-budget: requestAnimationFrame
 * does not fire in that mode, so every frame wait races a short timer.
 */
(function () {
  'use strict';
  var out = document.createElement('script');
  out.type = 'application/json';
  out.id = '__GSLIDER_OUT_ID__';  // replaced by a random id per run (measure.py)
  out.textContent = '{"status":"pending"}';
  (document.body || document.documentElement).appendChild(out);

  function write(obj) {
    out.textContent = JSON.stringify(obj).replace(/</g, '\\u003c');
  }

  var GENERICS = ['serif', 'sans-serif', 'monospace', 'cursive', 'fantasy', 'system-ui',
    'ui-serif', 'ui-sans-serif', 'ui-monospace', 'ui-rounded', 'emoji', 'math', 'fangsong'];
  var SKIP_TAGS = ['SCRIPT', 'STYLE', 'TEMPLATE', 'NOSCRIPT', 'IMG', 'VIDEO', 'AUDIO', 'CANVAS',
    'INPUT', 'TEXTAREA', 'SELECT', 'IFRAME', 'OBJECT', 'EMBED', 'PICTURE'];
  var PRE = ['pre', 'pre-wrap', 'pre-line', 'break-spaces'];

  function css(el, pseudo) { return getComputedStyle(el, pseudo || null); }
  function r1(v) { return Math.round(v * 10) / 10; }

  function frame() {
    return new Promise(function (resolve) {
      requestAnimationFrame(function () { resolve(); });
      setTimeout(resolve, 50);
    });
  }

  function families(value) {
    var list = [], cur = '', quote = null;
    for (var i = 0; i < value.length; i++) {
      var ch = value[i];
      if (quote) { if (ch === quote) quote = null; else cur += ch; }
      else if (ch === '"' || ch === "'") quote = ch;
      else if (ch === ',') { list.push(cur.trim()); cur = ''; }
      else cur += ch;
    }
    if (cur.trim()) list.push(cur.trim());
    return list;
  }

  function isGeneric(f) { return GENERICS.indexOf(f.toLowerCase()) >= 0; }

  function generic(list) {
    for (var i = 0; i < list.length; i++) if (isGeneric(list[i])) return list[i].toLowerCase();
    return 'sans-serif';
  }

  function parseColor(s) {
    var m = /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)/.exec(s || '');
    if (!m) return null;
    var a = m[4] === undefined ? 1 : (m[4].slice(-1) === '%' ? parseFloat(m[4]) / 100 : parseFloat(m[4]));
    return { r: +m[1], g: +m[2], b: +m[3], a: a };
  }

  function hex(c) {
    function h(v) { var s = Math.round(v).toString(16); return s.length < 2 ? '0' + s : s; }
    return '#' + h(c.r) + h(c.g) + h(c.b);
  }

  function gradientClip(cs) {
    return cs.backgroundClip === 'text' || cs.webkitBackgroundClip === 'text';
  }

  function firstStop(image) {
    var m = /rgba?\([^)]*\)/.exec(image || '');
    return m ? parseColor(m[0]) : null;
  }

  function visibleRects(node) {
    var range = document.createRange();
    range.selectNodeContents(node);
    return Array.prototype.filter.call(range.getClientRects(), function (r) {
      return r.width > 0 && r.height > 0;
    });
  }

  // --- block detection --------------------------------------------------------

  function hidden(el) {
    var cs = css(el);
    return cs.display === 'none' || (el.getClientRects().length === 0 && cs.display !== 'contents');
  }

  function inlineLevel(el) {
    if (hidden(el)) return true;  // contributes nothing
    if (el.tagName === 'BR') return true;
    var d = css(el).display;
    return d.indexOf('inline') === 0 || d === 'contents';
  }

  function allInline(el) {
    for (var c = el.firstElementChild; c; c = c.nextElementSibling) {
      if (c.namespaceURI === 'http://www.w3.org/2000/svg') continue;  // inline SVG: stays in the plate
      if (!inlineLevel(c)) return false;
      if (!hidden(c) && !allInline(c)) return false;
    }
    return true;
  }

  function hasText(node) {
    var w = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    for (var t = w.nextNode(); t; t = w.nextNode()) {
      if (/\S/.test(t.data) && !t.parentElement.closest('svg') && visibleRects(t).length) return true;
    }
    return false;
  }

  function wrapOrphans(el) {
    // Consecutive inline children of a non-block element become one inline span.
    var groups = [], cur = [];
    Array.prototype.forEach.call(el.childNodes, function (c) {
      var inl = c.nodeType === 3 || (c.nodeType === 1 && c.namespaceURI !== 'http://www.w3.org/2000/svg'
        && SKIP_TAGS.indexOf(c.tagName) < 0 && inlineLevel(c) && allInline(c));
      if (inl) cur.push(c); else { if (cur.length) groups.push(cur); cur = []; }
    });
    if (cur.length) groups.push(cur);
    var spans = [];
    groups.forEach(function (g) {
      var text = g.map(function (n) { return n.textContent; }).join('');
      if (!/\S/.test(text)) return;
      var span = document.createElement('span');
      span.setAttribute('data-gslider-anon', '');
      el.insertBefore(span, g[0]);
      g.forEach(function (n) { span.appendChild(n); });
      if (hasText(span)) spans.push(span);
    });
    return spans;
  }

  // --- per block ----------------------------------------------------------------

  var probeCache = {};
  function normalLineHeight(cs) {
    var key = [cs.fontFamily, cs.fontSize, cs.fontWeight, cs.fontStyle].join('|');
    if (probeCache[key] === undefined) {
      var d = document.createElement('div');
      d.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap;line-height:normal;'
        + 'padding:0;border:0;margin:0;';
      d.style.fontFamily = cs.fontFamily; d.style.fontSize = cs.fontSize;
      d.style.fontWeight = cs.fontWeight; d.style.fontStyle = cs.fontStyle;
      d.textContent = 'Hxg';
      document.body.appendChild(d);
      probeCache[key] = d.getBoundingClientRect().height;
      d.remove();
    }
    return probeCache[key];
  }

  function align(cs) {
    var a = cs.textAlign;
    var rtl = cs.direction === 'rtl';
    if (a === 'start') return rtl ? 'right' : 'left';
    if (a === 'end') return rtl ? 'left' : 'right';
    if (a.indexOf('center') >= 0) return 'center';
    if (a.indexOf('right') >= 0) return 'right';
    if (a.indexOf('left') >= 0) return 'left';
    return a;  // justify
  }

  function applyTransform(text, t) {
    if (t === 'uppercase') return text.toUpperCase();
    if (t === 'lowercase') return text.toLowerCase();
    if (t === 'capitalize') return text.replace(/(^|\s)(\S)/g, function (m, a, b) { return a + b.toUpperCase(); });
    return text;
  }

  function translationOnly(t) {
    if (t === 'none') return true;
    var m = /^matrix\(([^)]*)\)$/.exec(t);
    if (!m) return false;
    var v = m[1].split(',').map(parseFloat);
    return Math.abs(v[0] - 1) < 1e-6 && Math.abs(v[1]) < 1e-6 && Math.abs(v[2]) < 1e-6 && Math.abs(v[3] - 1) < 1e-6;
  }

  function label(el) {
    var cls = (el.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean)[0];
    return '<' + el.tagName.toLowerCase() + (cls ? '.' + cls : '') + '>';
  }

  function whyLeftInPlate(el, wrap, textRect) {
    for (var a = el; a && a !== wrap.parentElement; a = a.parentElement) {
      var cs = css(a);
      if (!translationOnly(cs.transform)) return 'transform ' + cs.transform + ' on ' + label(a);
      if (cs.rotate !== 'none' || cs.scale !== 'none') return 'transform (rotate/scale) on ' + label(a);
      if (cs.zoom && cs.zoom !== '1' && a !== wrap) return 'zoom ' + cs.zoom + ' on ' + label(a);
      if (cs.writingMode && cs.writingMode.indexOf('horizontal') !== 0) return 'vertical writing mode on ' + label(a);
      if (a !== el || !/^inline/.test(cs.display)) {
        if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') {
          var r = a.getBoundingClientRect();
          var left = r.left + a.clientLeft, top = r.top + a.clientTop;
          var right = left + a.clientWidth, bottom = top + a.clientHeight;
          if (textRect.left < left - 1 || textRect.top < top - 1
              || textRect.right > right + 1 || textRect.bottom > bottom + 1) {
            return 'clipped by overflow on ' + label(a);
          }
        }
      }
    }
    return null;
  }

  function effectiveOpacity(el, wrap) {
    var o = 1;
    for (var a = el; a && a !== wrap.parentElement; a = a.parentElement) o *= parseFloat(css(a).opacity);
    return o;
  }

  function runColor(textParent, block) {
    // Gradient text: the first gradient stop; else the fill colour; else `color`.
    for (var a = textParent; a; a = a.parentElement) {
      var acs = css(a);
      if (gradientClip(acs) && acs.backgroundImage !== 'none') {
        var stop = firstStop(acs.backgroundImage);
        if (stop) return stop;
      }
      if (a === block) break;
    }
    var cs = css(textParent);
    var fill = parseColor(cs.webkitTextFillColor);
    var color = parseColor(cs.color);
    return fill && fill.a > 0 ? fill : color;
  }

  function collectRuns(block, blockTransform) {
    var runs = [];
    var w = document.createTreeWalker(block, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode: function (n) {
        if (n.nodeType === 1) {
          if (n.namespaceURI === 'http://www.w3.org/2000/svg' || SKIP_TAGS.indexOf(n.tagName) >= 0) {
            return NodeFilter.FILTER_REJECT;
          }
          return n.tagName === 'BR' ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    for (var n = w.nextNode(); n; n = w.nextNode()) {
      if (n.nodeType === 1) {  // <br>
        if (runs.length) { runs[runs.length - 1].text += '\n'; }
        continue;
      }
      var p = n.parentElement;
      var cs = css(p);
      if (cs.visibility !== 'visible' || !visibleRects(n).length) continue;
      var text = n.data;
      if (PRE.indexOf(cs.whiteSpace) < 0 && PRE.indexOf(cs.whiteSpaceCollapse) < 0) {
        text = text.replace(/[ \t\n\r\f]+/g, ' ');
        // Collapsed spaces stay in the text node they came from; a space-only node joins the run before.
        var core = text.trim();
        var prevSpace = !runs.length || /\s$/.test(runs[runs.length - 1].text);
        if (!core) {
          if (!prevSpace) runs[runs.length - 1].text += ' ';
          continue;
        }
        text = (text.charAt(0) === ' ' && !prevSpace ? ' ' : '') + core
          + (text.charAt(text.length - 1) === ' ' ? ' ' : '');
      }
      if (cs.textTransform !== blockTransform) text = applyTransform(text, cs.textTransform);
      var fams = families(cs.fontFamily);
      var col = runColor(p, block);
      if (!col || col.a === 0) continue;
      var run = {
        text: text,
        family: fams[0] || 'sans-serif',
        generic: generic(fams),
        size_px: r1(parseFloat(cs.fontSize)),
        weight: parseInt(cs.fontWeight, 10) || 400,
        bold: parseInt(cs.fontWeight, 10) >= 600,
        italic: cs.fontStyle !== 'normal',
        underline: cs.textDecorationLine.indexOf('underline') >= 0,
        color: hex(col)
      };
      var last = runs[runs.length - 1];
      if (last && ['family', 'generic', 'size_px', 'weight', 'bold', 'italic', 'underline', 'color']
          .every(function (k) { return last[k] === run[k]; })) {
        last.text += run.text;
      } else {
        runs.push(run);
      }
    }
    if (runs.length) runs[runs.length - 1].text = runs[runs.length - 1].text.replace(/\s+$/, '');
    return runs.filter(function (r) { return r.text.length; });
  }

  function lineCount(rects) {
    var sorted = rects.slice().sort(function (a, b) { return a.top - b.top; });
    // A rect starts a new line when its vertical centre is below the current line's
    // first rect (glyph boxes of consecutive lines overlap when line-height is tight).
    var lines = 0, bottom = -Infinity;
    sorted.forEach(function (r) {
      if ((r.top + r.bottom) / 2 > bottom) { lines += 1; bottom = r.bottom; }
    });
    return lines;
  }

  function contentBox(el) {
    var cs = css(el), r = el.getBoundingClientRect();
    var l = parseFloat(cs.borderLeftWidth) + parseFloat(cs.paddingLeft);
    var rt = parseFloat(cs.borderRightWidth) + parseFloat(cs.paddingRight);
    return { left: r.left + l, right: r.right - rt };
  }

  function measureBlock(el, wrap, origin, ctx) {
    var text = (el.textContent || '').replace(/\s+/g, ' ').trim();
    var range = document.createRange();
    range.selectNodeContents(el);
    // Line rects from text nodes only: an inline-block's own box would merge its lines into one.
    var rects = [];
    var tw = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (var t = tw.nextNode(); t; t = tw.nextNode()) {
      if (/\S/.test(t.data) && !t.parentElement.closest('svg')) rects = rects.concat(visibleRects(t));
    }
    if (!rects.length) return;
    var tr = range.getBoundingClientRect();
    var cs = css(el);
    if (cs.visibility !== 'visible' && !el.querySelector('*')) return;
    if (effectiveOpacity(el, wrap) < 0.05) return;
    var why = whyLeftInPlate(el, wrap, tr);
    if (why) { ctx.skipped.push({ text: text.slice(0, 200), reason: why }); return; }

    var transform = cs.textTransform === 'none' ? null : cs.textTransform;
    var runs = collectRuns(el, cs.textTransform);
    if (!runs.length) return;

    var box = el;
    while (box !== wrap && /^inline/.test(css(box).display)) box = box.parentElement;
    var cb = contentBox(box);
    var left = Math.min(cb.left, tr.left), right = Math.max(cb.right, tr.right);
    if (box !== el) { left = tr.left; right = tr.right; }  // inline block of a mixed parent

    var lh = cs.lineHeight === 'normal' ? normalLineHeight(cs) : parseFloat(cs.lineHeight);
    var half = Math.max(0, (lh - rects[0].height) / 2);
    var ls = cs.letterSpacing === 'normal' ? 0 : parseFloat(cs.letterSpacing);

    ctx.blocks.push({
      x: r1(left - origin.left), y: r1(tr.top - half - origin.top),
      w: r1(right - left), h: r1(tr.height + 2 * half),
      lines: lineCount(rects),
      align: align(cs),
      line_height_px: r1(lh),
      letter_spacing_px: r1(ls),
      runs: runs,
      transform: transform
    });
    ctx.hide.push(el);
  }

  function walk(el, wrap, origin, ctx) {
    if (el.namespaceURI === 'http://www.w3.org/2000/svg') {
      var svgText = Array.prototype.map.call(el.querySelectorAll('text'), function (t) {
        return t.textContent;
      }).join(' ').replace(/\s+/g, ' ').trim();
      if (svgText) ctx.skipped.push({ text: svgText.slice(0, 200), reason: 'text in SVG' });
      return;
    }
    if (SKIP_TAGS.indexOf(el.tagName) >= 0 || hidden(el)) return;
    if (el !== wrap && hasText(el) && allInline(el) && !/^inline/.test(css(el).display)) {
      measureBlock(el, wrap, origin, ctx);
      return;
    }
    wrapOrphans(el).forEach(function (span) { measureBlock(span, wrap, origin, ctx); });
    Array.prototype.slice.call(el.children).forEach(function (c) {
      if (c.hasAttribute('data-gslider-anon')) return;
      walk(c, wrap, origin, ctx);
    });
  }

  // --- fonts --------------------------------------------------------------------

  function localFontAvailable(family) {
    var c = document.createElement('canvas').getContext('2d');
    var sample = 'mmmmmmmmmmlli1WQ@#';
    return ['monospace', 'serif', 'sans-serif'].some(function (g) {
      c.font = '72px ' + g;
      var base = c.measureText(sample).width;
      c.font = '72px "' + family.replace(/"/g, '') + '", ' + g;
      return c.measureText(sample).width !== base;
    });
  }

  function unloadedFamilies(blocks) {
    var used = {};
    blocks.forEach(function (b) { b.runs.forEach(function (r) { used[r.family] = true; }); });
    var faces = Array.prototype.slice.call(document.fonts);
    return Object.keys(used).filter(function (fam) {
      if (isGeneric(fam)) return false;
      var mine = faces.filter(function (f) {
        return f.family.replace(/^["']|["']$/g, '').toLowerCase() === fam.toLowerCase();
      });
      if (mine.length) return !mine.some(function (f) { return f.status === 'loaded'; });
      return !localFontAvailable(fam);
    }).sort();
  }

  // --- plate tagging ------------------------------------------------------------

  var PSEUDO = { '::before': 'b', '::after': 'a', '::marker': 'm' };

  function keepPseudo(el) {
    Object.keys(PSEUDO).forEach(function (p) {
      var pcs = css(el, p);
      if (!pcs.content || pcs.content === 'none' || (pcs.content === 'normal' && p !== '::marker')) return;
      if (p === '::marker' && css(el).display !== 'list-item') return;
      var k = PSEUDO[p];
      el.style.setProperty('--gs-' + k + '-c', pcs.color);
      el.style.setProperty('--gs-' + k + '-f', pcs.webkitTextFillColor);
      el.style.setProperty('--gs-' + k + '-s', pcs.textShadow);
    });
  }

  function tagPlate(hide, wrap) {
    hide.forEach(function (el) {
      var all = [el].concat(Array.prototype.slice.call(el.querySelectorAll('*')));
      all.forEach(keepPseudo);
      for (var a = el; a && a !== wrap; a = a.parentElement) all.push(a);
      all.forEach(function (a) { if (gradientClip(css(a))) a.setAttribute('data-gslider-gradient', ''); });
    });
    hide.forEach(function (el) { el.setAttribute('data-gslider-hide', ''); });
  }

  // --- main ---------------------------------------------------------------------

  function slideBackground(wrap) {
    var slide = wrap.firstElementChild || wrap;
    var chain = [slide, wrap, document.body, document.documentElement];
    for (var i = 0; i < chain.length; i++) {
      var cs = css(chain[i]);
      if (cs.backgroundImage !== 'none' && i < 2) return null;  // gradient/image: read from the plate
      var c = parseColor(cs.backgroundColor);
      if (c && c.a > 0) return hex(c);
    }
    return '#ffffff';
  }

  async function main() {
    if (document.readyState !== 'complete') {
      await new Promise(function (r) { addEventListener('load', r, { once: true }); });
    }
    void document.body.offsetHeight;  // force layout so used fonts are requested
    await Promise.allSettled(Array.prototype.map.call(document.fonts, function (f) { return f.load(); }));
    await document.fonts.ready;
    await frame();
    await frame();

    var wrap = document.querySelector('.slide-wrap.gslider-render');
    if (!wrap) throw new Error('no .slide-wrap.gslider-render in the page');
    var origin = wrap.getBoundingClientRect();
    var ctx = { blocks: [], skipped: [], hide: [] };
    walk(wrap, wrap, origin, ctx);

    write({
      status: 'done',
      background: slideBackground(wrap),
      fonts_status: document.fonts.status,
      unloaded: unloadedFamilies(ctx.blocks),
      blocks: ctx.blocks,
      skipped: ctx.skipped
    });
    tagPlate(ctx.hide, wrap);
    await frame();
  }

  main().catch(function (e) { write({ status: 'error', error: String((e && e.stack) || e) }); });
})();
