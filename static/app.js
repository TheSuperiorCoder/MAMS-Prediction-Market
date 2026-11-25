// MAMS Prediction Market — front-end helpers (no dependencies).

const SVG_NS = 'http://www.w3.org/2000/svg';
const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
// Timestamps from the server are school-time wall-clock strings ("2025-03-04 15:30:00"). Parsing them as UTC and
// formatting with timeZone "UTC" keeps that wall clock exactly, whatever the viewer's own time zone is.
const parseTime = s => Date.parse(s.replace(' ', 'T') + 'Z');
function wallNow(tz) {
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', second: 'numeric' }).formatToParts(new Date()).map(x => [x.type, x.value]));
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
}
const fmt = n => n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const escapeHtml = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function svg(tag, attrs, parent) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}

// Animate a number shown in `el` from `from` to `to`, formatting each frame with `format`.
function tween(el, from, to, ms, format) {
  if (el._tween) cancelAnimationFrame(el._tween);
  if (reducedMotion() || from === to || !isFinite(from)) { el.textContent = format(to); return; }
  const start = performance.now();
  const step = now => {
    const k = Math.min(1, (now - start) / ms);
    const e = 1 - Math.pow(1 - k, 3);
    el.textContent = format(from + (to - from) * e);
    if (k < 1) el._tween = requestAnimationFrame(step);
  };
  el._tween = requestAnimationFrame(step);
}

// Price-over-time chart: smoothed diagonal lines per outcome with dynamic Y-axis scaling and 1D / 1W / All range buttons.
// `lines` limits how many outcomes are drawn (a live Yes/No market only needs its Yes line).
// `colors` overrides the outcome palette (Yes/No markets use the Yes and No colours).
function drawChart(el, history, allLabels, endTime, lines, tz, colors) {
  if (!el || !history.length) return;
  const labels = allLabels.slice(0, lines || allLabels.length);
  const color = i => (colors && colors[i]) || `var(--c${i})`;
  const L = 38, R = 10, T = 12, B = 26;
  let W = 640, H = 230;  // set from the element's width on each render so text stays 11px on phones
  const all = history.map(h => ({ t: parseTime(h.t), p: h.p }));
  const end = endTime ? parseTime(endTime) : wallNow(tz || 'America/New_York');
  all.push({ t: Math.max(end, all[all.length - 1].t), p: all[all.length - 1].p });
  const stepPriceAt = (pts, t) => {
    let cur = pts[0];
    for (const pt of pts) { if (pt.t <= t) cur = pt; else break; }
    return cur.p;
  };
  // Time-weighted average over [a, b] on a step-function price series, smoothing rapid bursts into gradual diagonals.
  const bucketSeries = (pts, t0, t1, bins) => {
    if (pts.length <= 2 || t1 <= t0) return pts;
    const dt = (t1 - t0) / bins;
    const out = [{ t: t0, p: pts[0].p.slice() }];
    let idx = 0;
    for (let k = 1; k < bins; k++) {
      const a = t0 + (k - 1) * dt;
      const b = t0 + k * dt;
      while (idx + 1 < pts.length && pts[idx + 1].t <= a) idx++;
      const acc = new Array(labels.length).fill(0);
      let curT = a;
      let j = idx;
      while (j + 1 < pts.length && pts[j + 1].t < b) {
        const seg = pts[j + 1].t - curT;
        for (let i = 0; i < labels.length; i++) acc[i] += pts[j].p[i] * seg;
        curT = pts[j + 1].t;
        j++;
      }
      const rem = b - curT;
      for (let i = 0; i < labels.length; i++) acc[i] = (acc[i] + pts[j].p[i] * rem) / dt;
      out.push({ t: b, p: acc });
    }
    out.push({ t: t1, p: pts[pts.length - 1].p.slice() });
    return out;
  };
  // Linear interpolation along the rendered diagonal series so hover dots track the drawn line exactly.
  const interpPriceAt = (series, t) => {
    if (t <= series[0].t) return series[0].p;
    const last = series[series.length - 1];
    if (t >= last.t) return last.p;
    let lo = 0, hi = series.length - 1;
    while (lo + 1 < hi) {
      const mid = (lo + hi) >> 1;
      if (series[mid].t <= t) lo = mid; else hi = mid;
    }
    const a = series[lo], b = series[hi];
    const f = b.t > a.t ? (t - a.t) / (b.t - a.t) : 0;
    return a.p.map((v, i) => v + (b.p[i] - v) * f);
  };
  const computeYAxis = series => {
    let maxP = 0;
    for (const pt of series) {
      for (let i = 0; i < labels.length; i++) {
        if (pt.p[i] > maxP) maxP = pt.p[i];
      }
    }
    const rawMax = Math.min(1, Math.max(0.1, maxP * 1.2));
    if (rawMax >= 0.85) return { yMax: 1, ticks: [0, 0.25, 0.5, 0.75, 1] };
    const step = rawMax <= 0.20 ? 0.05 : rawMax <= 0.50 ? 0.10 : 0.20;
    const yMax = Math.min(1, Math.ceil(rawMax / step - 1e-9) * step);
    const ticks = [];
    for (let v = 0; v <= yMax + 1e-9; v += step) ticks.push(Math.min(1, Math.round(v * 100) / 100));
    return { yMax, ticks };
  };
  const span = all[all.length - 1].t - all[0].t;
  const buttons = [...(el.parentElement.querySelectorAll('[data-range]') || [])];
  // a range at least as long as the whole history would look just like "All"
  buttons.forEach(b => { const r = +b.dataset.range; if (r && r >= span) b.hidden = true; });
  if (buttons.filter(b => !b.hidden).length < 2 && buttons[0]) buttons[0].parentElement.hidden = true;
  let firstDraw = true;

  let current = 0;
  function render(range) {
    current = range;
    W = Math.max(300, Math.round(el.clientWidth || 640));
    H = W < 500 ? 200 : 240;
    el.replaceChildren();
    let pts = all;
    if (range && end - range > all[0].t) {
      const start = end - range;
      pts = [{ t: start, p: stepPriceAt(all, start) }, ...all.filter(x => x.t > start)];
    }
    const t0 = pts[0].t, t1 = Math.max(pts[pts.length - 1].t, t0 + 1);
    const bins = Math.min(48, Math.max(16, Math.round((W - L - R) / 14)));
    const series = bucketSeries(pts, t0, t1, bins);
    const { yMax, ticks } = computeYAxis(series);
    const x = t => L + (t - t0) / (t1 - t0) * (W - L - R);
    const y = p => T + (1 - Math.min(yMax, Math.max(0, p)) / yMax) * (H - T - B);

    const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Price history chart' });
    for (const g of ticks) {
      svg('line', { x1: L, x2: W - R, y1: y(g), y2: y(g), class: 'gridline' }, root);
      svg('text', { x: L - 8, y: y(g) + 4, class: 'axis', 'text-anchor': 'end' }, root).textContent = `${Math.round(g * 100)}%`;
    }
    // Time axis: clock times when the chart spans under a day, dates otherwise; the last tick says "now" while live.
    const shortSpan = t1 - t0 < 86400000;
    const tickFmt = new Intl.DateTimeFormat('en-US', shortSpan
      ? { hour: 'numeric', minute: '2-digit', timeZone: 'UTC' } : { month: 'short', day: 'numeric', timeZone: 'UTC' });
    const TICKS = 4;
    let prevLabel = null;
    for (let i = 0; i < TICKS; i++) {
      const t = t0 + (t1 - t0) * i / (TICKS - 1);
      const last = i === TICKS - 1;
      const label = last && !endTime ? 'now' : tickFmt.format(t);
      if (!last && label === prevLabel) continue;  // a short span would repeat the same label
      if (last && label === prevLabel) { root.lastChild.remove(); }  // keep the end label, drop the duplicate before it
      prevLabel = label;
      svg('text', { x: x(t), y: H - 6, class: 'axis', 'text-anchor': i === 0 ? 'start' : last ? 'end' : 'middle' }, root).textContent = label;
    }

    const animate = firstDraw && !reducedMotion();
    const last = series[series.length - 1];
    labels.forEach((_, i) => {
      let d = '';
      series.forEach((pt, k) => {
        const px = x(pt.t).toFixed(1), py = y(pt.p[i]).toFixed(1);
        d += k === 0 ? `M${px},${py}` : `L${px},${py}`;
      });
      if (labels.length === 1) {  // a single line gets a soft gradient under it
        const id = `cg${Math.random().toString(36).slice(2, 8)}`;
        const defs = svg('defs', {}, root);
        const grad = svg('linearGradient', { id, x1: 0, x2: 0, y1: 0, y2: 1 }, defs);
        svg('stop', { offset: '0%', style: `stop-color: ${color(i)}; stop-opacity: .22` }, grad);
        svg('stop', { offset: '100%', style: `stop-color: ${color(i)}; stop-opacity: 0` }, grad);
        svg('path', { d: `${d}L${x(last.t).toFixed(1)},${y(0).toFixed(1)}L${x(series[0].t).toFixed(1)},${y(0).toFixed(1)}Z`, fill: `url(#${id})`, class: animate ? 'area' : '' }, root);
      }
      svg('path', { d, class: `line${animate ? ' draw' : ''}`, pathLength: 1, style: `stroke: ${color(i)}` }, root);
      if (!endTime) svg('circle', { cx: x(last.t), cy: y(last.p[i]), r: 4, class: 'end-pulse', style: `fill: ${color(i)}` }, root);
      svg('circle', { cx: x(last.t), cy: y(last.p[i]), r: 4, class: 'end-dot', style: `fill: ${color(i)}` }, root);
    });
    firstDraw = false;
    el.appendChild(root);

    // Hover/touch readout: a guide line, a dot on each line, and each outcome's price at that moment.
    const guide = svg('line', { y1: T, y2: H - B, class: 'guide', visibility: 'hidden' }, root);
    const dots = labels.map((_, i) => svg('circle', { r: 4.5, class: 'hover-dot', visibility: 'hidden', style: `fill: ${color(i)}` }, root));
    const tip = document.createElement('div');
    tip.className = 'chart-tip';
    tip.hidden = true;
    el.appendChild(tip);
    const hide = () => { guide.setAttribute('visibility', 'hidden'); dots.forEach(d => d.setAttribute('visibility', 'hidden')); tip.hidden = true; };
    root.addEventListener('pointermove', e => {
      const box = root.getBoundingClientRect();
      const vx = (e.clientX - box.left) / box.width * W;
      if (vx < L || vx > W - R) return hide();
      const t = t0 + (vx - L) / (W - L - R) * (t1 - t0);
      const p = interpPriceAt(series, t);
      guide.setAttribute('x1', vx); guide.setAttribute('x2', vx); guide.setAttribute('visibility', 'visible');
      dots.forEach((d, i) => { d.setAttribute('cx', vx); d.setAttribute('cy', y(p[i])); d.setAttribute('visibility', 'visible'); });
      tip.hidden = false;
      tip.innerHTML = `<div class="muted small">${new Date(t).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'UTC' })}</div>` +
        labels.map((name, i) => `<div><i style="background: ${color(i)}"></i>${escapeHtml(name)} <b>${(p[i] * 100).toFixed(1)}%</b></div>`).join('');
      const left = vx / W * box.width;
      const flip = left + 16 + tip.offsetWidth > box.width;
      tip.style.left = `${Math.max(0, flip ? left - 16 - tip.offsetWidth : left + 16)}px`;
    });
    root.addEventListener('pointerleave', hide);
  }

  buttons.forEach(b => b.addEventListener('click', () => {
    buttons.forEach(o => o.classList.toggle('active', o === b));
    render(+b.dataset.range);
  }));
  render(0);
  let lastW = el.clientWidth, resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (Math.abs(el.clientWidth - lastW) > 8) { lastW = el.clientWidth; render(current); } }, 150);
  });
}

// Buy/sell panel with a live quote from /api/quote.
function setupTradeForm(form) {
  if (!form) return;
  const actionInput = form.querySelector('input[name=action]');
  const orderTypeInput = form.querySelector('input[name=order_type]');
  const limitField = form.querySelector('#limit-pct-field');
  const limitInput = form.querySelector('#limit-pct');
  const limitHint = form.querySelector('#limit-hint');
  const valueInput = form.querySelector('#value');
  const valueLabel = form.querySelector('#value-label');
  const prefix = form.querySelector('#value-prefix');
  const preview = form.querySelector('#preview');
  const submit = form.querySelector('#submit-btn');
  const maxBtn = form.querySelector('#max-btn');
  const quick = form.querySelector('#quick');
  const balance = parseFloat(maxBtn.dataset.balance);
  const entryFee = parseFloat(form.dataset.entryFee) || 0;  // charged on your first bet in this market
  const lateT = parseFloat(form.dataset.lateT) || 0;        // late-trading multiplier T (0 = no fee)
  const snipeRate = parseFloat(form.dataset.snipeRate) || 0; // buys pay at least this x T x shares x P^2
  // always shown in the summary: Ⓜ0.00 until the last 2 hours before betting closes
  const lateRow = (r, late, sign = '') => late
    ? row(`Late fee (×${r.late_multiplier}${r.late_fee_to ? `, to ${escapeHtml(r.late_fee_to)}` : ''})`, `${sign}${money(late)}`, 'down')
    : row('Late fee', money(0));
  const minOut = form.querySelector('#min-out');
  let timer = null, ctrl = null, quoted = null;  // quoted = { key } of the quote the preview shows
  const shown = {};  // headline numbers currently on screen, so the next quote can count from them

  const selected = () => form.querySelector('input[name=outcome_id]:checked');
  const outcomeName = () => selected().dataset.label;
  const currentPrice = () => parseFloat(selected().dataset.price) || 0;
  const held = () => parseFloat(selected().dataset.held) || 0;
  const paid = () => parseFloat(selected().dataset.paid) || 0;  // what you spent on the shares you still hold
  const row = (label, value, cls = '') => `<div class="${cls}"><span>${label}</span><b>${value}</b></div>`;
  // a headline figure that counts up from the previous quote's value
  const big = (key, n, sub = '') => `<span class="win-val"><b data-tween="${key}" data-v="${n}">${money(n)}</b>${sub}</span>`;
  const money = n => `Ⓜ${fmt(n)}`;
  const signed = n => `${n >= 0 ? '+' : '−'}${money(Math.abs(n))}`;
  const isBuy = () => actionInput.value === 'buy';
  const isLimit = () => orderTypeInput && orderTypeInput.value === 'limit';
  const defaultButton = () => isLimit()
    ? `Place limit ${isBuy() ? 'buy' : 'sell'} for “${outcomeName()}”`
    : `${isBuy() ? 'Buy' : 'Sell'} “${outcomeName()}”`;

  function updateLimitHint() {
    if (!limitHint || !isLimit()) return;
    const curPct = Math.round(currentPrice() * 100);
    const pct = parseFloat(limitInput.value);
    const targetTxt = (pct >= 1 && pct <= 99) ? `${pct}%` : 'your target %';
    limitHint.textContent = isBuy()
      ? `Activates when “${outcomeName()}” is at or below ${targetTxt} (now ${curPct}%).`
      : `Activates when “${outcomeName()}” is at or above ${targetTxt} (now ${curPct}%).`;
  }

  function renderLimitPreview() {
    clearTimeout(timer);
    if (ctrl) ctrl.abort();
    minOut.value = '';
    preview.classList.remove('stale');
    quoted = null; clearShown();
    updateLimitHint();
    const v = parseFloat(valueInput.value);
    const pct = parseFloat(limitInput.value);
    if (!(pct >= 1 && pct <= 99)) {
      setPreview('<span class="muted">Enter a target probability between 1% and 99%.</span>', false);
      return;
    }
    if (!(v > 0)) {
      setPreview(`<span class="muted">${isBuy() ? 'Enter an amount to spend.' : 'Enter shares to sell.'}</span>`, false);
      return;
    }
    if (isBuy() && v < 1) {
      setPreview('<span class="down">Minimum bet is Ⓜ1.</span>', false);
      return;
    }
    const pTarget = pct / 100;
    const curP = currentPrice();
    const immediate = isBuy() ? curP <= pTarget + 1e-9 : curP >= pTarget - 1e-9;
    const name = outcomeName();
    const pctStr = `${Math.round(pct * 10) / 10}%`;
    if (isBuy()) {
      const minShares = v / pTarget;
      const warn = v + entryFee > balance
        ? `<div class="down"><span>Balance note</span><b>Current balance (${money(balance)}) is lower; will only fill what you can afford.</b></div>`
        : '';
      setPreview(
        row('Order type', immediate ? 'Limit buy (fills now up to limit)' : 'Limit buy (open until triggered)') +
        row('Trigger price', `≤ ${pctStr} (now ${Math.round(curP * 100)}%)`) +
        row('Max spend', money(v)) +
        (entryFee ? row('Entry fee (on first fill)', money(entryFee)) : '') +
        row('Min shares at limit', `${fmt(minShares)} shares`) +
        row('Spendable balance', 'Unchanged until triggered') +
        warn,
        true, `Limit buy “${name}” ≤ ${pctStr}`
      );
    } else {
      const minProceeds = v * pTarget;
      const warn = v > held()
        ? `<div class="down"><span>Holdings note</span><b>You own ${fmt(held())} shares; will only sell shares you hold when triggered.</b></div>`
        : '';
      setPreview(
        row('Order type', immediate ? 'Limit sell (fills now up to limit)' : 'Limit sell (open until triggered)') +
        row('Trigger price', `≥ ${pctStr} (now ${Math.round(curP * 100)}%)`) +
        row('Shares to sell', `${fmt(v)} shares`) +
        row('Min proceeds at limit', money(minProceeds)) +
        warn,
        true, `Limit sell “${name}” ≥ ${pctStr}`
      );
    }
  }

  function refresh() {
    valueLabel.textContent = isBuy() ? 'Amount to spend' : `Shares to sell (you own ${fmt(held())})`;
    prefix.hidden = !isBuy();
    valueInput.placeholder = isBuy() ? '0' : '0 shares';
    quick.hidden = !isBuy();
    if (limitField) {
      limitField.hidden = !isLimit();
      limitInput.required = isLimit();
      if (isLimit() && !limitInput.value) {
        limitInput.value = Math.min(99, Math.max(1, Math.round(currentPrice() * 100)));
      }
    }
    submit.textContent = defaultButton();
    // the button takes the colour of what you're doing: buying Yes (or any outcome) is green, buying No or selling is crimson
    const buyingNo = isBuy() && form.querySelector('.outcome-pick.binary') && selected() === form.querySelectorAll('input[name=outcome_id]')[1];
    submit.classList.toggle('btn-buy', isBuy() && !buyingNo);
    submit.classList.toggle('btn-sell', !isBuy() || !!buyingNo);
    if (isLimit()) renderLimitPreview();
    else requestQuote();
  }

  function setPreview(html, canSubmit, buttonText) {
    preview.innerHTML = html;
    submit.disabled = !canSubmit;
    submit.textContent = buttonText || defaultButton();
    preview.querySelectorAll('[data-tween]').forEach(el => {
      const key = el.dataset.tween, to = parseFloat(el.dataset.v);
      if (key in shown && shown[key] !== to) {
        tween(el, shown[key], to, 320, money);
        el.classList.remove('flash-num'); void el.offsetWidth; el.classList.add('flash-num');
      }
      shown[key] = to;
    });
  }
  const clearShown = () => { for (const k in shown) delete shown[k]; };

  // A quote is only valid for the exact outcome|action|amount it was fetched for.
  const keyFor = v => `${selected().value}|${actionInput.value}|${v}`;
  const floor6 = n => Math.floor(n * 1e6) / 1e6;
  const floor2 = n => Math.floor(n * 100) / 100;
  const round2 = n => Math.round(n * 100) / 100;
  // Biggest bet you can afford. The late fee per coin bet is the larger of 0.07 x T x (1 - P) and snipeRate x T x P.
  // Buying pushes P up from the current price, so the first part is largest at the current price and the second
  // is at most snipeRate x T (P < 1). Using both bounds leaves enough room for the fee.
  const maxBuy = () => {
    const p = parseFloat(selected().dataset.price) || 0;
    const rate = Math.max(0.07 * lateT * (1 - p), snipeRate * lateT);
    return Math.max(0, floor2((balance - entryFee) / (1 + rate)));
  };

  function requestQuote() {
    if (isLimit()) { renderLimitPreview(); return; }
    clearTimeout(timer);
    if (ctrl) ctrl.abort();  // drop any quote still in flight
    minOut.value = '';
    preview.classList.remove('stale');
    const v = parseFloat(valueInput.value);
    if (!isBuy() && held() <= 0) {
      quoted = null; clearShown();
      setPreview(`<span class="muted">You don't own any “${escapeHtml(outcomeName())}” shares to sell.</span>`, false);
      return;
    }
    if (!(v > 0)) {
      quoted = null; clearShown();
      setPreview(`<span class="muted">${isBuy() ? 'Enter an amount to see your payout.' : 'Enter shares to see what you get back.'}</span>`, false);
      return;
    }
    const key = keyFor(v);
    // Same outcome and action, new amount: keep the old numbers on screen but dim them until the new quote lands.
    const sameKind = quoted && quoted.key.split('|').slice(0, 2).join('|') === key.split('|').slice(0, 2).join('|');
    if (!sameKind) { preview.innerHTML = '<span class="muted">Getting a quote…</span>'; quoted = null; clearShown(); }
    preview.classList.toggle('stale', !!sameKind);
    submit.disabled = true;
    timer = setTimeout(async () => {
      ctrl = new AbortController();
      const params = new URLSearchParams({ market: form.dataset.market, outcome: selected().value, action: actionInput.value, value: v });
      try {
        const res = await (await fetch(`${form.dataset.quote}?${params}`, { signal: ctrl.signal })).json();
        if (isLimit() || keyFor(parseFloat(valueInput.value)) !== key) return;  // the input moved on while we waited
        preview.classList.remove('stale');
        if (!res.ok) { quoted = null; clearShown(); setPreview(`<span class="down">${escapeHtml(res.error || 'Can\'t quote that trade.')}</span>`, false); return; }
        quoted = { key };
        minOut.value = floor6((isBuy() ? res.shares : res.coins) * 0.98);  // refuse if the price moves >2% against you
        const newPct = `${(res.new_price * 100).toFixed(1)}%`;
        const name = outcomeName();
        if (isBuy()) {
          const fee = res.entry_fee || 0, late = res.late_fee || 0;
          const total = res.coins + fee + late;
          setPreview(
            `<div class="win"><span>To win</span>${big('win', res.shares, `<small class="${res.shares >= total ? 'up' : 'down'}">${signed(res.shares - total)} profit</small>`)}</div>` +
            `<div class="total"><span>You pay</span>${big('total', total)}</div>` +
            row('Bet', money(res.coins)) +
            (fee ? row(`Entry fee (first bet${res.creator ? `, to ${escapeHtml(res.creator)}` : ''})`, money(fee)) : '') +
            lateRow(res, late) +
            row('You get', `${fmt(res.shares)} shares`) +
            row('Avg price per share', money(res.avg_price)) +
            row('If it wins', `you get ${money(res.shares)} (${signed(res.shares - total)})`, 'up') +
            (res.est_bonus ? row('<span title="Your share of the money that would be left in the pot if this outcome won right now. It changes as people trade.">+ est. pot bonus</span>',
                                 `~${money(res.est_bonus)}`, 'up') : '') +
            row('If it loses', `you lose ${money(total)}`, 'down') +
            row('Balance after', money(balance - total)) +
            row('New price', newPct),
            true, `Pay ${money(total)} for “${name}”`);
        } else {
          const costOfThese = held() > 0 ? paid() * Math.min(res.shares / held(), 1) : 0;
          const late = res.late_fee || 0, got = res.coins - late;
          setPreview(
            `<div class="win"><span>You receive</span>${big('got', got, `<small class="${got >= costOfThese ? 'up' : 'down'}">${signed(got - costOfThese)} vs. what you paid</small>`)}</div>` +
            row('Sale value', money(res.coins)) +
            lateRow(res, late, '−') +
            row('You sell', `${fmt(res.shares)} of your ${fmt(held())} shares`) +
            row('Avg price per share', money(res.avg_price)) +
            row('You paid for these', money(costOfThese)) +
            row('Profit / loss', signed(got - costOfThese), got >= costOfThese ? 'up' : 'down') +
            row('Balance after', money(balance + got)) +
            row('New price', newPct),
            true, `Sell for ${money(got)}`);
        }
      } catch (err) {
        if (err.name === 'AbortError') return;
        quoted = null; clearShown();
        preview.classList.remove('stale');
        setPreview('<span class="muted">Quote unavailable. Try again.</span>', false);
      }
    }, 150);
  }

  form.querySelectorAll('[data-seg="action"] button').forEach(btn => btn.addEventListener('click', () => {
    if (btn.dataset.action === actionInput.value) return;
    form.querySelectorAll('[data-seg="action"] button').forEach(b => {
      b.classList.toggle('active', b === btn);
      b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
    });
    actionInput.value = btn.dataset.action;
    valueInput.value = '';
    valueInput.step = isBuy() ? '0.01' : 'any';
    refresh();
  }));
  form.querySelectorAll('[data-seg="order-type"] button').forEach(btn => btn.addEventListener('click', () => {
    if (!orderTypeInput || btn.dataset.orderType === orderTypeInput.value) return;
    form.querySelectorAll('[data-seg="order-type"] button').forEach(b => {
      b.classList.toggle('active', b === btn);
      b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
    });
    orderTypeInput.value = btn.dataset.orderType;
    refresh();
  }));
  form.querySelectorAll('input[name=outcome_id]').forEach(r => r.addEventListener('change', () => {
    if (limitInput && isLimit()) {
      limitInput.value = Math.min(99, Math.max(1, Math.round(currentPrice() * 100)));
    }
    refresh();
  }));
  if (limitInput) limitInput.addEventListener('input', renderLimitPreview);
  valueInput.addEventListener('input', requestQuote);
  maxBtn.addEventListener('click', () => {
    valueInput.value = isBuy() ? maxBuy() : floor6(held());
    requestQuote();
  });
  quick.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
    const cap = isLimit() ? Infinity : maxBuy();
    valueInput.value = round2(Math.min((parseFloat(valueInput.value) || 0) + parseFloat(b.dataset.amt), cap));
    requestQuote();
  }));
  refresh();
}

// Outcome buttons on the market page (Yes/No, "Buy 42¢", the phone bet bar): pick that outcome in the order
// ticket, switch it to Buy and bring the ticket into view.
function setupPickers() {
  const form = document.getElementById('trade-form'), ticket = document.getElementById('trade');
  document.querySelectorAll('[data-pick]').forEach(btn => btn.addEventListener('click', e => {
    e.preventDefault();
    if (form) {
      const buy = form.querySelector('.seg [data-action=buy]');
      if (form.querySelector('input[name=action]').value !== 'buy') buy.click();
      const radio = form.querySelector(`input[name=outcome_id][value="${btn.dataset.pick}"]`);
      if (radio && !radio.checked) { radio.checked = true; radio.dispatchEvent(new Event('change', { bubbles: true })); }
    }
    if (!ticket) return;
    const r = ticket.getBoundingClientRect();
    if (r.top < 60 || r.top > window.innerHeight * 0.6) {
      ticket.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'start' });
    }
    ticket.classList.remove('flash-target'); void ticket.offsetWidth; ticket.classList.add('flash-target');
    const value = document.getElementById('value');
    if (value && window.matchMedia('(hover: hover)').matches) setTimeout(() => value.focus({ preventScroll: true }), 300);
  }));
}

// Market form (propose / review / admin create): Yes/No vs multiple-choice toggle and a live preview card.
function setupMarketForm(form) {
  const kindInputs = form.querySelectorAll('input[name=kind]');
  const multiOnly = form.querySelector('[data-multi-only]');
  const ouOnly = form.querySelectorAll('[data-ou-only]');
  const ouInputs = form.querySelectorAll('input[name=ou_line], input[name=ou_unit]');
  const outcomes = form.querySelector('textarea[name=outcomes]');
  const rulesLabel = form.querySelector('[data-rules-label]');
  const stakePick = form.querySelector('[data-stake-pick]');
  const rules = form.querySelector('textarea[name=description]');
  const preview = document.querySelector('#market-preview .preview-body');
  const field = name => (form.querySelector(`[name=${name}]`) || {}).value || '';
  const kind = () => (form.querySelector('input[name=kind]:checked') || {}).value || 'multi';

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function ouText() {
    const line = field('ou_line').trim() || '?';
    const unit = field('ou_unit').trim();
    return line + (unit && /^[A-Za-z0-9]/.test(unit) ? ' ' + unit : unit);
  }

  function render() {
    const k = kind();
    const yesno = k === 'yesno', ou = k === 'overunder', multi = k === 'multi';
    if (multiOnly) multiOnly.hidden = !multi;
    ouOnly.forEach(e => { e.hidden = !ou; });
    ouInputs.forEach(i => { i.disabled = !ou; });
    const lineInput = form.querySelector('input[name=ou_line]');
    if (lineInput) lineInput.required = ou;
    if (outcomes) {
      outcomes.disabled = !multi;
      outcomes.required = multi;
      if (multi && /^\s*yes\s*\n\s*no\s*$/i.test(outcomes.value)) outcomes.value = 'Option A\nOption B';
    }
    if (stakePick) {
      // the creator's opening bet can be on any of the market's outcomes
      const names = yesno ? ['Yes', 'No']
        : ou ? [`Over ${ouText()}`, `Under ${ouText()}`]
        : (outcomes ? outcomes.value.split('\n').map(x => x.trim()).filter(Boolean).slice(0, 8) : []);
      const keep = stakePick.options.length ? stakePick.value : stakePick.dataset.selected;
      const current = Array.from(stakePick.options).map(o => o.textContent);
      if (current.join('\n') !== names.join('\n')) {
        stakePick.replaceChildren(...names.map((n, i) => { const o = el('option', '', n); o.value = i; return o; }));
        stakePick.value = Number(keep) < names.length ? keep : 0;
      }
    }
    const heading = { yesno: 'Resolves YES if…', overunder: 'What number is measured, and how', multi: 'How the winner is decided' }[k];
    if (rulesLabel) rulesLabel.textContent = heading;
    if (rules) rules.placeholder = rules.dataset[k + 'Placeholder'] || '';
    if (!preview) return;

    preview.replaceChildren();
    const card = el('div', 'preview-market');
    const meta = el('div', 'card-meta');
    meta.append(el('span', 'tag', field('category') || 'Category'), el('span', 'closes', 'Preview'));
    card.append(meta, el('h3', '', field('question').trim() || 'Your question will show up here'));
    if (yesno || ou) {
      const row = el('div', 'chance-row');
      const chance = el('div', 'chance');
      chance.append(el('span', 'chance-pct', '50%'), document.createTextNode(' '), el('span', 'muted', 'chance'));
      const yn = el('div', 'yn');
      yn.append(el('span', 'yn-yes', `${ou ? 'Over' : 'Yes'} 50¢`), el('span', 'yn-no', `${ou ? 'Under' : 'No'} 50¢`));
      row.append(chance, yn);
      if (ou) card.append(el('div', 'ou-line', `Over / Under ${ouText()}`));
      card.append(row);
    } else {
      const labels = field('outcomes').split('\n').map(x => x.trim()).filter(Boolean).slice(0, 8);
      const list = el('ul', 'odds');
      labels.forEach((name, i) => {
        const li = el('li');
        const bar = el('span', 'bar'), fill = el('span');
        fill.style.width = `${100 / Math.max(labels.length, 1)}%`;
        fill.style.background = `var(--c${i})`;
        fill.style.animation = 'none';
        bar.append(fill);
        li.append(el('span', 'odds-label', name), bar, el('span', 'odds-pct', `${Math.round(100 / labels.length)}%`));
        list.append(li);
      });
      card.append(list);
    }
    const box = el('div', 'rules');
    box.append(el('h4', '', heading.replace('…', '')),
               el('p', '', field('description').trim() || '…'));
    if (field('rules_source').trim()) box.append(el('h4', '', 'Source'), el('p', '', field('rules_source').trim()));
    if (field('rules_otherwise').trim()) box.append(el('h4', '', 'Otherwise'), el('p', '', field('rules_otherwise').trim()));
    const closes = field('closes_at');
    if (closes && !isNaN(new Date(closes))) {
      box.append(el('h4', '', 'Betting closes'), el('p', '', new Date(closes).toLocaleString(undefined,
        { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' })));
    }
    card.append(box);
    preview.append(card);
  }

  form.addEventListener('input', render);
  form.addEventListener('change', render);
  render();
}

document.querySelectorAll('form[data-market-form]').forEach(setupMarketForm);


// Loan offer form: show what the borrower would pay back.
document.querySelectorAll('[data-loan-form]').forEach(form => {
  const out = form.querySelector('[data-loan-summary]');
  const base = out.textContent;
  const update = () => {
    const amt = parseFloat(form.amount.value), rate = parseFloat(form.rate.value), days = parseInt(form.days.value, 10);
    if (!(amt > 0) || !(rate >= 0) || !(days > 0)) { out.textContent = base; return; }
    const back = Math.round(amt * (1 + rate / 100) * 100) / 100;
    const who = form.borrower.value.trim() || 'The borrower';
    out.textContent = `${who} would pay back Ⓜ${back.toFixed(2)} (Ⓜ${(back - amt).toFixed(2)} interest) within ${days} day${days === 1 ? '' : 's'}. ` + base;
  };
  form.addEventListener('input', update);
  update();
});

// Confirmation dialogs: any form with data-confirm="message" (and optionally data-confirm-title /
// data-confirm-label), or a submit button with its own data-confirm, asks first.
(function setupConfirm() {
  const dlg = document.getElementById('confirm-dialog');
  let pending = null;
  document.addEventListener('submit', e => {
    const form = e.target;
    const src = e.submitter && e.submitter.dataset.confirm ? e.submitter : form;
    const message = src.dataset.confirm;
    if (!message || form.dataset.confirmed) return;
    e.preventDefault();
    if (!dlg || !dlg.showModal) {
      if (window.confirm(message)) { form.dataset.confirmed = '1'; form.requestSubmit(e.submitter); }
      return;
    }
    pending = { form, submitter: e.submitter };
    dlg.querySelector('[data-title]').textContent = src.dataset.confirmTitle || 'Are you sure?';
    dlg.querySelector('[data-message]').textContent = message;
    const ok = dlg.querySelector('[data-ok]');
    ok.textContent = src.dataset.confirmLabel || 'Confirm';
    // destructive actions get a crimson confirm button
    ok.classList.toggle('btn-brand', /delete|cancel|block|ban|reject|reset/i.test(ok.textContent));
    dlg.showModal();
  });
  if (dlg) dlg.addEventListener('close', () => {
    const p = pending;
    pending = null;
    if (p && dlg.returnValue === 'ok') { p.form.dataset.confirmed = '1'; p.form.requestSubmit(p.submitter); }
    dlg.returnValue = '';
  });
})();


// Bottom "Bet" bar on phones: shows the current chance and jumps to the ticket; hidden while the ticket is on screen.
function setupBetBar() {
  const bar = document.getElementById('bet-bar'), panel = document.getElementById('trade');
  if (!bar || !panel || !('IntersectionObserver' in window)) return;
  new IntersectionObserver(([entry]) => { bar.hidden = entry.isIntersecting; }, { threshold: 0.15 }).observe(panel);
}

// Phone "More" sheet.
(function setupMenu() {
  const btn = document.getElementById('menu-btn'), sheet = document.getElementById('menu-sheet');
  if (!btn || !sheet || !sheet.showModal) return;
  btn.addEventListener('click', () => sheet.showModal());
  sheet.addEventListener('click', e => {
    if (e.target === sheet || e.target.closest('[data-close-sheet]') || e.target.closest('a')) sheet.close();
  });
})();

// Desktop account menu: close on outside click or Escape.
(function setupUserMenu() {
  const menu = document.querySelector('.user-menu');
  if (!menu) return;
  document.addEventListener('click', e => { if (menu.open && !menu.contains(e.target)) menu.open = false; });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && menu.open) { menu.open = false; menu.querySelector('summary').focus(); } });
})();


// Home page filters: the server renders every market; tabs and topic chips just hide cards and update the URL
// (still shareable as ?status=&cat=) without a request. Without JS the links reload the page as before.
(function setupMarketFilters() {
  const bar = document.getElementById('market-filters'), grid = document.getElementById('market-grid');
  if (!bar || !grid) return;
  const cards = [...grid.querySelectorAll('.market-card')];
  const empty = document.getElementById('empty-state');
  const extras = document.querySelectorAll('[data-default-only]');  // spotlight and movers: default view only
  const tabs = [...bar.querySelectorAll('[data-status]')], chips = [...bar.querySelectorAll('[data-cat]')];
  const params = new URLSearchParams(location.search);
  let status = tabs.some(t => t.dataset.status === params.get('status')) ? params.get('status') : 'open';
  let cat = chips.some(c => c.dataset.cat === params.get('cat')) ? params.get('cat') : '';
  const matches = (state, s) => s === 'all' || state === s || (s === 'closed' && ['closed', 'proposed', 'disputed'].includes(state));

  function apply(animate) {
    let shown = 0;
    for (const card of cards) {
      const ok = matches(card.dataset.state, status) && (!cat || card.dataset.cat === cat);
      card.hidden = !ok;
      if (ok) {
        if (animate) { card.style.setProperty('--i', shown); card.classList.remove('rise'); void card.offsetWidth; card.classList.add('rise'); }
        shown++;
      }
    }
    tabs.forEach(t => { const on = t.dataset.status === status; t.classList.toggle('active', on); t.setAttribute('aria-pressed', on); });
    chips.forEach(c => { const on = c.dataset.cat === cat; c.classList.toggle('active', on); c.setAttribute('aria-pressed', on); });
    const filtered = status !== 'open' || !!cat;
    extras.forEach(x => { x.hidden = filtered; });
    empty.hidden = shown > 0;
    document.getElementById('empty-title').innerHTML = `<strong>${filtered ? 'No markets match these filters.' : 'No open markets right now.'}</strong>`;
    document.getElementById('empty-hint').textContent = filtered ? 'Try another topic or status.' : 'Check back soon, or propose one yourself.';
    document.getElementById('clear-filters').hidden = !filtered;
    const q = new URLSearchParams();
    if (status !== 'open') q.set('status', status);
    if (cat) q.set('cat', cat);
    history.replaceState(null, '', location.pathname + (q.toString() ? `?${q}` : ''));
    // keep each link pointing at the right page for "open in new tab" and no-JS fallbacks
    tabs.forEach(t => { const p = new URLSearchParams(); p.set('status', t.dataset.status); if (cat) p.set('cat', cat); t.href = `/?${p}`; });
    chips.forEach(c => { const p = new URLSearchParams(); p.set('status', status); if (c.dataset.cat) p.set('cat', c.dataset.cat); c.href = `/?${p}`; });
  }
  bar.addEventListener('click', e => {
    const tab = e.target.closest('[data-status]'), chip = e.target.closest('[data-cat]');
    if (!tab && !chip || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    if (tab) status = tab.dataset.status; else cat = chip.dataset.cat;
    if (chip) chip.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'nearest', inline: 'center' });
    apply(true);
  });
  document.getElementById('clear-filters').addEventListener('click', e => { e.preventDefault(); status = 'open'; cat = ''; apply(true); });
})();


// Featured markets carousel: dots follow the scroll position, and it advances on its own until touched.
(function setupSpotlight() {
  const track = document.getElementById('spot-track'), dots = document.getElementById('spot-dots');
  if (!track || !dots) return;
  const buttons = [...dots.querySelectorAll('button')];
  const index = () => Math.round(track.scrollLeft / track.clientWidth);
  const go = i => track.scrollTo({ left: i * track.clientWidth, behavior: reducedMotion() ? 'auto' : 'smooth' });
  track.addEventListener('scroll', () => buttons.forEach((b, i) => b.classList.toggle('active', i === index())), { passive: true });
  buttons.forEach((b, i) => b.addEventListener('click', () => { stop(); go(i); }));
  let timer = reducedMotion() ? null : setInterval(() => {
    if (document.hidden || track.matches(':hover')) return;
    go((index() + 1) % buttons.length);
  }, 7000);
  const stop = () => { clearInterval(timer); timer = null; };
  track.addEventListener('pointerdown', stop, { once: true });
})();


// Sliding thumb for segmented controls (Buy/Sell, market type) and the sliding underline in the top nav.
(function setupSliders() {
  document.querySelectorAll('.seg').forEach(seg => {
    const thumb = document.createElement('span');
    thumb.className = 'seg-thumb';
    thumb.setAttribute('aria-hidden', 'true');
    seg.prepend(thumb);
    seg.classList.add('has-thumb');
    const active = () => seg.querySelector(':scope > button.active') ||
      (seg.querySelector('input:checked') || { closest: () => null }).closest('label');
    let first = true;
    const place = () => {
      const a = active();
      if (!a || !a.offsetWidth) return;
      if (first) thumb.style.transition = 'none';
      seg.style.setProperty('--seg-x', `${a.offsetLeft}px`);
      seg.style.setProperty('--seg-w', `${a.offsetWidth}px`);
      if (first) { void thumb.offsetWidth; thumb.style.transition = ''; first = false; }
    };
    seg.addEventListener('click', () => requestAnimationFrame(place));
    seg.addEventListener('change', () => requestAnimationFrame(place));
    if ('ResizeObserver' in window) new ResizeObserver(place).observe(seg);
    place();
  });

  const nav = document.querySelector('.nav');
  const ink = nav && nav.querySelector('.nav-ink');
  if (!ink) return;
  const links = [...nav.querySelectorAll('a')];
  const current = links.find(a => a.classList.contains('active'));
  const moveTo = a => {
    if (!a) { nav.style.setProperty('--ink-o', 0); return; }
    nav.style.setProperty('--ink-x', `${a.offsetLeft + 8}px`);
    nav.style.setProperty('--ink-w', `${a.offsetWidth - 16}px`);
    nav.style.setProperty('--ink-o', 1);
  };
  ink.style.transition = 'none';
  moveTo(current);
  void ink.offsetWidth;
  ink.style.transition = '';
  links.forEach(a => a.addEventListener('mouseenter', () => moveTo(a)));
  nav.addEventListener('mouseleave', () => moveTo(current));
  window.addEventListener('resize', () => moveTo(current));
})();


// Big percentages count up from zero when the page loads.
(function setupCountUps() {
  if (reducedMotion()) return;
  document.querySelectorAll('[data-count]').forEach((el, k) => {
    const m = /^(\d+)%$/.exec(el.textContent.trim());
    if (!m) return;
    const to = +m[1];
    el.textContent = '0%';
    setTimeout(() => tween(el, 0, to, 900, n => `${Math.round(n)}%`), 120 + Math.min(k, 10) * 40);
  });
})();


// Toasts: short plain confirmations go away on their own; errors, receipts and anything with numbers stay until closed.
(function setupToasts() {
  const box = document.getElementById('toasts');
  if (!box) return;
  const dismiss = t => {
    if (t.classList.contains('leaving')) return;
    t.classList.add('leaving');
    const done = () => { t.remove(); if (!box.querySelector('.toast')) box.remove(); };
    t.addEventListener('animationend', done, { once: true });
    setTimeout(done, 600);
  };
  box.querySelectorAll('.toast').forEach((t, i) => {
    const x = t.querySelector('.toast-x');
    x.hidden = false;
    x.addEventListener('click', () => dismiss(t));
    t.style.animationDelay = `${i * 90}ms`;
    const text = t.querySelector('p').textContent.trim();
    if (t.dataset.cat === 'success' && text.length < 80 && !/[0-9Ⓜ]/.test(text)) {
      const bar = document.createElement('span');
      bar.className = 'toast-timer';
      t.append(bar);
      bar.addEventListener('animationend', () => dismiss(t));
    }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { const t = box.querySelector('.toast:not(.leaving)'); if (t && !document.querySelector('dialog[open]')) dismiss(t); }
  });
})();


// After a trade or the daily bonus the server leaves a note on <body>: tick the balance over from the old amount
// and play a short burst — confetti for a buy, coins flying into the balance for a sale or the bonus.
(function celebrate() {
  const raw = document.body.dataset.celebrate;
  if (!raw) return;
  let c;
  try { c = JSON.parse(raw); } catch (e) { return; }
  const bal = document.querySelector('.balance');
  const valueEl = bal && bal.querySelector('[data-balance-value]');
  const to = bal ? parseFloat(bal.dataset.balance) : NaN;
  const tick = () => {
    if (!valueEl || !isFinite(c.from) || !isFinite(to) || c.from === to) return;
    tween(valueEl, c.from, to, 900, fmt);
    bal.classList.remove('pulse-up', 'pulse-down'); void bal.offsetWidth;
    bal.classList.add(to > c.from ? 'pulse-up' : 'pulse-down');
  };
  if (valueEl && isFinite(c.from)) valueEl.textContent = fmt(c.from);
  if (reducedMotion()) { tick(); return; }

  const css = getComputedStyle(document.documentElement);
  const v = name => css.getPropertyValue(name).trim();
  const balRect = () => (bal && bal.offsetParent ? bal.getBoundingClientRect() : null);

  if (c.kind === 'buy') {
    const btn = document.getElementById('submit-btn');
    const r = btn && btn.getBoundingClientRect();
    const origin = r && r.top > 0 && r.bottom < window.innerHeight
      ? { x: r.left + r.width / 2, y: r.top } : { x: window.innerWidth / 2, y: window.innerHeight * 0.35 };
    confetti(origin, [v(c.color || '--yes') || v('--yes'), v('--brand'), v('--wpi-gray'), v('--gold'), v('--ink')]);
    setTimeout(tick, 300);
  } else {
    const target = balRect();
    if (!target) { tick(); return; }
    let from;
    const toast = document.querySelector('.toast');
    if (c.kind === 'bonus') from = { x: target.left + target.width / 2 - 40, y: target.bottom + 150, spread: 140 };
    else if (toast) { const t = toast.getBoundingClientRect(); from = { x: t.left + 30, y: t.top + t.height / 2, spread: 60 }; }
    else from = { x: window.innerWidth / 2, y: window.innerHeight / 2, spread: 80 };
    coins(from, target, c.kind === 'bonus' ? 12 : 8, tick);
  }

  function confetti(origin, colors) {
    const canvas = document.createElement('canvas');
    canvas.className = 'fx-canvas';
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = innerWidth * dpr; canvas.height = innerHeight * dpr;
    document.body.append(canvas);
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    const parts = Array.from({ length: 110 }, () => {
      const a = -Math.PI / 2 + (Math.random() - 0.5) * 1.9, s = 7 + Math.random() * 9;
      return { x: origin.x, y: origin.y, vx: Math.cos(a) * s, vy: Math.sin(a) * s, r: Math.random() * Math.PI,
               vr: (Math.random() - 0.5) * 0.35, w: 5 + Math.random() * 6, h: 3 + Math.random() * 5,
               c: colors[Math.floor(Math.random() * colors.length)] || '#ac2b37', round: Math.random() < 0.25 };
    });
    const start = performance.now();
    (function frame(now) {
      const t = now - start;
      ctx.clearRect(0, 0, innerWidth, innerHeight);
      for (const p of parts) {
        p.vy += 0.32; p.vx *= 0.985; p.vy *= 0.985; p.x += p.vx; p.y += p.vy; p.r += p.vr;
        ctx.save();
        ctx.globalAlpha = Math.max(0, 1 - Math.max(0, t - 1100) / 700);
        ctx.translate(p.x, p.y); ctx.rotate(p.r); ctx.fillStyle = p.c;
        if (p.round) { ctx.beginPath(); ctx.arc(0, 0, p.h / 1.4, 0, Math.PI * 2); ctx.fill(); }
        else ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h * Math.abs(Math.cos(p.r * 2)) + 1);
        ctx.restore();
      }
      if (t < 1800) requestAnimationFrame(frame); else canvas.remove();
    })(start);
  }

  function coins(from, target, n, done) {
    const tx = target.left + target.width / 2 - 11, ty = target.top + target.height / 2 - 11;
    let left = n;
    for (let i = 0; i < n; i++) {
      const coin = document.createElement('span');
      coin.className = 'fx-coin';
      coin.textContent = 'Ⓜ';
      document.body.append(coin);
      const sx = from.x + (Math.random() - 0.5) * from.spread - 11, sy = from.y + (Math.random() - 0.5) * 30 - 11;
      // arc above the straight path, but never off the top of the screen
      const mx = (sx + tx) / 2 + (Math.random() - 0.5) * 120, my = Math.max(14, (sy + ty) / 2 - 50 - Math.random() * 50);
      coin.animate([
        { transform: `translate(${sx}px, ${sy}px) scale(.6)`, opacity: 0 },
        { transform: `translate(${mx}px, ${my}px) scale(1.1)`, opacity: 1, offset: 0.45 },
        { transform: `translate(${tx}px, ${ty}px) scale(.5)`, opacity: 0.9 },
      ], { duration: 750 + Math.random() * 250, delay: i * 60, easing: 'cubic-bezier(.5, 0, .4, 1)', fill: 'both' })
        .finished.then(() => { coin.remove(); if (--left === 0) done(); });
    }
    setTimeout(() => { if (bal) { bal.style.transform = 'scale(1.06)'; setTimeout(() => { bal.style.transform = ''; }, 200); } }, 600);
  }
})();


// Installable app: register the service worker (offline page + cached static files, see templates/sw.js) and offer
// "Install app" where the browser supports it. Chrome/Edge/Android fire beforeinstallprompt; iPhone Safari never
// does, so there the button explains Share → Add to Home Screen instead. Hidden once installed.
(function setupInstall() {
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
    });
  }
  const standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  if (standalone) { document.documentElement.classList.add('standalone'); return; }
  const buttons = [...document.querySelectorAll('[data-install]')];
  if (!buttons.length) return;
  const show = on => buttons.forEach(b => { b.hidden = !on; });
  const ua = navigator.userAgent;
  const iosSafari = /iPhone|iPad|iPod/.test(ua) && /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS/.test(ua);
  let deferred = null;
  window.addEventListener('beforeinstallprompt', e => {
    e.preventDefault();  // keep Chrome's mini-infobar away; our own buttons show the prompt instead
    deferred = e;
    show(true);
  });
  window.addEventListener('appinstalled', () => { deferred = null; show(false); });
  if (iosSafari) show(true);
  buttons.forEach(b => b.addEventListener('click', async e => {
    e.preventDefault();
    const sheet = document.getElementById('menu-sheet');
    if (sheet && sheet.open) sheet.close();
    if (deferred) {
      deferred.prompt();
      const choice = await deferred.userChoice;
      deferred = null;
      if (choice.outcome === 'accepted') show(false);
    } else if (iosSafari) {
      const help = document.getElementById('install-help');
      if (help && help.showModal) help.showModal();
    }
  }));
})();


// Admin bulk actions: "tick all" and the ticked counter (works on any form with data-bulk-row checkboxes).
(function setupBulk() {
  document.querySelectorAll('form').forEach(form => {
    const all = form.querySelector('[data-bulk-all]');
    const rows = () => Array.from(form.querySelectorAll('[data-bulk-row]'));
    const count = form.querySelector('[data-bulk-count]');
    if (!all && !count) return;
    const update = () => {
      const ticked = rows().filter(r => r.checked).length;
      if (count) count.textContent = ticked;
      if (all) { all.checked = ticked > 0 && ticked === rows().length; all.indeterminate = ticked > 0 && ticked < rows().length; }
    };
    if (all) all.addEventListener('change', () => { rows().forEach(r => { r.checked = all.checked; }); update(); });
    form.addEventListener('change', e => { if (e.target.matches('[data-bulk-row]')) update(); });
    update();
  });
})();
