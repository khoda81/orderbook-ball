(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    marketInput: $('marketInput'), loadButton: $('loadButton'), marketChooser: $('marketChooser'),
    marketSelect: $('marketSelect'), connectButton: $('connectButton'), disconnectButton: $('disconnectButton'),
    exportButton: $('exportButton'), clearButton: $('clearButton'), status: $('status'), message: $('message'),
    linearScale: $('linearScale'), logScale: $('logScale'), windowSelect: $('windowSelect'),
    ballValue: $('ballValue'), ballOdds: $('ballOdds'), probValue: $('probValue'), spreadValue: $('spreadValue'),
    sampleValue: $('sampleValue'), priceCanvas: $('priceCanvas'), heatmapCanvas: $('heatmapCanvas'),
    priceEmpty: $('priceEmpty'), heatmapEmpty: $('heatmapEmpty'), heatmapCaption: $('heatmapCaption'), colorLegend: $('colorLegend')
  };

  let markets = [];
  let socket = null;
  let liveConnected = false;
  let rows = [];
  let liveRows = [];
  let historyRows = [];
  let showingHistory = false;
  let historySerial = 0;
  let heatScale = 'linear';
  let rafPending = false;
  const MAX_ROWS = 20000;
  const GRID_N = 150;
  const LIVE_WARMUP_MS = 120000;
  const LIVE_WARMUP_ROWS = 1500;

  function setStatus(state, text) {
    els.status.className = `status ${state}`;
    els.status.innerHTML = '<span></span>' + text;
  }
  function setMessage(text, isError = false) {
    els.message.textContent = text || '';
    els.message.className = isError ? 'message error' : 'message';
  }
  function fmt(x, digits = 4) {
    return Number.isFinite(x) ? x.toFixed(digits) : '—';
  }
  function logistic(q) {
    if (q >= 0) return 1 / (1 + Math.exp(-q));
    const e = Math.exp(q); return e / (1 + e);
  }
  function liveReady() {
    if (liveRows.length < 2) return false;
    const first = liveRows[0].recv_ts_ms || liveRows[0].ts_ms;
    const last = liveRows[liveRows.length - 1].recv_ts_ms || liveRows[liveRows.length - 1].ts_ms;
    return liveRows.length >= LIVE_WARMUP_ROWS || last - first >= LIVE_WARMUP_MS;
  }
  function revealCharts() {
    els.exportButton.disabled = false;
    els.clearButton.disabled = false;
    els.priceEmpty.classList.add('hidden');
    els.heatmapEmpty.classList.add('hidden');
  }

  async function loadMarkets() {
    const value = els.marketInput.value.trim();
    if (!value) return setMessage('Search or paste a Polymarket URL / slug first.', true);
    disconnect();
    setMessage('Resolving market…');
    els.loadButton.disabled = true;
    try {
      const res = await fetch(`/api/markets?value=${encodeURIComponent(value)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Could not resolve market');
      markets = data.markets || [];
      if (!markets.length) throw new Error('No binary CLOB markets found.');
      els.marketSelect.replaceChildren(...markets.map((m, i) => {
        const o = document.createElement('option');
        o.value = String(i);
        o.textContent = m.question || `${m.a_label} / ${m.aprime_label}`;
        return o;
      }));
      els.marketChooser.classList.remove('hidden');
      if (markets.length === 1) {
        els.marketSelect.value = '0';
        setMessage(`${markets[0].a_label} / ${markets[0].aprime_label} — connecting automatically…`);
        queueMicrotask(() => connect());
      } else {
        setMessage(`${markets.length} binary submarkets found — choose one.`);
      }
    } catch (err) {
      markets = [];
      els.marketChooser.classList.add('hidden');
      setMessage(err.message, true);
    } finally {
      els.loadButton.disabled = false;
    }
  }

  async function loadHistory(market, serial) {
    try {
      const res = await fetch('/api/history', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...market, max_rows: 6000, archive_files: 6}),
      });
      const data = await res.json();
      if (serial !== historySerial) return;
      historyRows = Array.isArray(data.rows) ? data.rows : [];

      if (!historyRows.length) {
        if (!liveReady()) {
          const why = data.error ? ` (${data.error})` : '';
          setMessage(`No recent PMXT archive rows for this market${why}; collecting live data.`);
        }
        return;
      }

      if (!liveReady()) {
        rows = historyRows;
        showingHistory = true;
        revealCharts();
        updateMetrics(historyRows[historyRows.length - 1]);
        scheduleRender();
        const end = new Date(historyRows[historyRows.length - 1].ts_ms).toLocaleString();
        setStatus(liveConnected ? 'connected' : 'connecting', liveConnected ? 'Live · archive preview' : 'Archive preview');
        setMessage(`Showing ${historyRows.length.toLocaleString()} PMXT historical book updates through ${end}; live data is collecting in parallel.`);
      }
    } catch (err) {
      if (serial !== historySerial) return;
      setMessage(`History bootstrap unavailable (${err.message}); collecting live data.`);
    }
  }

  function switchToLive(market) {
    if (!liveRows.length) return;
    showingHistory = false;
    rows = liveRows;
    updateMetrics(liveRows[liveRows.length - 1]);
    revealCharts();
    setStatus(liveConnected ? 'connected' : 'idle', liveConnected ? 'Live' : 'Captured');
    setMessage(`${market.question} — live window warmed up; showing live paired top-of-book updates.`);
    scheduleRender();
  }

  function connect() {
    const market = markets[Number(els.marketSelect.value) || 0];
    if (!market) return;
    disconnect(false);
    clearData();
    const serial = ++historySerial;
    void loadHistory(market, serial);

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${location.host}/ws`);
    socket = ws;
    setStatus('connecting', 'Connecting');
    els.connectButton.disabled = true;
    els.disconnectButton.disabled = false;
    setMessage(`Opening ${market.a_label} / ${market.aprime_label} live books and loading PMXT history…`);

    ws.addEventListener('open', () => ws.send(JSON.stringify(market)));
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'status') {
        if (msg.state === 'connected') {
          liveConnected = true;
          setStatus('connected', showingHistory ? 'Live · archive preview' : 'Live');
          if (!showingHistory && !rows.length) {
            setMessage(`${market.question} — live stream connected; loading history / collecting updates.`);
          }
        }
        return;
      }
      if (msg.type === 'error') {
        setStatus('error', 'Error');
        setMessage(msg.message, true);
        return;
      }
      if (msg.type === 'tick') {
        liveRows.push(msg);
        if (liveRows.length > MAX_ROWS) liveRows.splice(0, liveRows.length - MAX_ROWS);

        if (showingHistory) {
          if (liveReady()) switchToLive(market);
          return;
        }

        rows = liveRows;
        updateMetrics(msg);
        revealCharts();
        scheduleRender();
      }
    });
    ws.addEventListener('close', () => {
      if (socket !== ws) return;
      liveConnected = false;
      setStatus('idle', 'Disconnected');
      setMessage(liveRows.length ? 'Stream stopped; captured live data remains available.' : 'Disconnected.');
      els.connectButton.disabled = false;
      els.disconnectButton.disabled = true;
      socket = null;
    });
    ws.addEventListener('error', () => {
      setStatus('error', 'Connection error');
      setMessage('WebSocket connection failed.', true);
    });
  }

  function disconnect(update = true) {
    if (socket) {
      const s = socket; socket = null; s.close();
    }
    liveConnected = false;
    if (update) historySerial += 1;
    els.connectButton.disabled = false;
    els.disconnectButton.disabled = true;
    if (update) setStatus('idle', rows.length ? 'Captured' : 'Idle');
  }

  function clearData() {
    rows = [];
    liveRows = [];
    historyRows = [];
    showingHistory = false;
    els.ballValue.textContent = '—'; els.ballOdds.textContent = 'log A/A′'; els.probValue.textContent = '—'; els.spreadValue.textContent = '—'; els.sampleValue.textContent = '0';
    els.exportButton.disabled = true; els.clearButton.disabled = true;
    els.priceEmpty.classList.remove('hidden'); els.heatmapEmpty.classList.remove('hidden');
    clearCanvas(els.priceCanvas); clearCanvas(els.heatmapCanvas);
  }

  function updateMetrics(r) {
    els.ballValue.textContent = fmt(r.q_ball, 4);
    els.ballOdds.textContent = `odds ${Math.exp(r.q_ball).toFixed(3)} : 1`;
    els.probValue.textContent = `${(100 * logistic(r.q_ball)).toFixed(2)}%`;
    els.spreadValue.textContent = fmt(r.q_ask - r.q_bid, 4);
    els.sampleValue.textContent = rows.length.toLocaleString();
  }

  function visibleRows() {
    if (!rows.length) return [];
    const seconds = Number(els.windowSelect.value);
    let out = rows;
    if (seconds > 0) {
      const cutoff = rows[rows.length - 1].ts_ms - seconds * 1000;
      let i = rows.length - 1;
      while (i > 0 && rows[i - 1].ts_ms >= cutoff) i--;
      out = rows.slice(i);
    }
    const cap = 3500;
    if (out.length > cap) {
      const stride = Math.ceil(out.length / cap);
      const sampled = [];
      for (let i = 0; i < out.length; i += stride) sampled.push(out[i]);
      if (sampled[sampled.length - 1] !== out[out.length - 1]) sampled.push(out[out.length - 1]);
      return sampled;
    }
    return out;
  }

  function scheduleRender() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      const data = visibleRows();
      drawPrice(data);
      drawHeatmap(data);
    });
  }

  function setupCanvas(canvas) {
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.max(1, Math.min(devicePixelRatio || 1, 2));
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height));
    if (canvas.width !== Math.floor(w * dpr) || canvas.height !== Math.floor(h * dpr)) {
      canvas.width = Math.floor(w * dpr); canvas.height = Math.floor(h * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return {ctx, w, h};
  }
  function clearCanvas(canvas) {
    const {ctx, w, h} = setupCanvas(canvas); ctx.clearRect(0,0,w,h);
  }
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function bounds(data) {
    let lo = Infinity, hi = -Infinity;
    for (const r of data) { lo = Math.min(lo, r.q_bid, r.q_ball); hi = Math.max(hi, r.q_ask, r.q_ball); }
    const span = Math.max(hi - lo, 0.02);
    return [lo - span * 0.08, hi + span * 0.08];
  }
  function tickFormat(v, span) { return span < .1 ? v.toFixed(3) : v.toFixed(2); }

  function drawAxes(ctx, w, h, pad, yLo, yHi, t0, t1) {
    ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';
    ctx.textBaseline = 'middle';
    const grid = css('--grid'), muted = css('--muted');
    ctx.lineWidth = 1; ctx.strokeStyle = grid; ctx.fillStyle = muted;
    const span = yHi - yLo;
    for (let i=0;i<=5;i++) {
      const y = pad.t + (h-pad.t-pad.b) * i/5;
      const v = yHi - span*i/5;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w-pad.r, y); ctx.stroke();
      ctx.fillText(tickFormat(v, span), 4, y);
    }
    ctx.textBaseline = 'alphabetic';
    const dt = Math.max(0, (t1-t0)/1000);
    ctx.fillText('0s', pad.l, h-6);
    ctx.textAlign = 'right'; ctx.fillText(`${dt.toFixed(dt < 10 ? 1 : 0)}s`, w-pad.r, h-6); ctx.textAlign = 'left';
  }

  function drawPrice(data) {
    const {ctx,w,h} = setupCanvas(els.priceCanvas); ctx.clearRect(0,0,w,h);
    if (data.length < 1) return;
    const pad = {l:58,r:12,t:16,b:28};
    const [yLo,yHi] = bounds(data); const t0=data[0].ts_ms, t1=Math.max(t0+1,data[data.length-1].ts_ms);
    const x = t => pad.l + (t-t0)/(t1-t0)*(w-pad.l-pad.r);
    const y = q => pad.t + (yHi-q)/(yHi-yLo)*(h-pad.t-pad.b);
    drawAxes(ctx,w,h,pad,yLo,yHi,t0,t1);

    ctx.fillStyle = 'rgba(219,255,101,.12)';
    ctx.beginPath();
    data.forEach((r,i) => { const X=x(r.ts_ms), Y=y(r.q_ask); i?ctx.lineTo(X,Y):ctx.moveTo(X,Y); });
    for (let i=data.length-1;i>=0;i--) ctx.lineTo(x(data[i].ts_ms), y(data[i].q_bid));
    ctx.closePath(); ctx.fill();

    line(data, 'q_mid', css('--cyan'), 1.1, .75);
    line(data, 'q_ratio_of_mids', css('--orange'), 1, .65, [5,4]);
    line(data, 'q_ball', css('--accent'), 2, 1);
    function line(arr,key,color,width,alpha,dash=[]) {
      ctx.beginPath(); ctx.strokeStyle=color; ctx.lineWidth=width; ctx.globalAlpha=alpha; ctx.setLineDash(dash);
      arr.forEach((r,i)=>{const X=x(r.ts_ms),Y=y(r[key]);i?ctx.lineTo(X,Y):ctx.moveTo(X,Y);}); ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha=1;
    }
  }

  function percentile(values, p) {
    if (!values.length) return 1;
    values.sort((a,b)=>a-b); const i=Math.min(values.length-1, Math.max(0, Math.floor((values.length-1)*p))); return values[i];
  }
  function mixColor(u) {
    u = Math.max(0, Math.min(1, u));
    const stops = [[21,24,32],[72,87,47],[219,255,101]];
    const z=u*2, i=Math.min(1,Math.floor(z)), f=z-i, a=stops[i], b=stops[i+1];
    return `rgb(${Math.round(a[0]+(b[0]-a[0])*f)},${Math.round(a[1]+(b[1]-a[1])*f)},${Math.round(a[2]+(b[2]-a[2])*f)})`;
  }

  function drawHeatmap(data) {
    const {ctx,w,h} = setupCanvas(els.heatmapCanvas); ctx.clearRect(0,0,w,h);
    if (data.length < 1) return;
    const pad={l:58,r:12,t:16,b:28}; const [yLo,yHi]=bounds(data); const t0=data[0].ts_ms,t1=Math.max(t0+1,data[data.length-1].ts_ms);
    drawAxes(ctx,w,h,pad,yLo,yHi,t0,t1);
    const plotW=w-pad.l-pad.r, plotH=h-pad.t-pad.b;
    const qGrid=Array.from({length:GRID_N},(_,j)=>yLo+(yHi-yLo)*j/(GRID_N-1));
    const lastOutside=new Array(GRID_N).fill(NaN);
    const cells=[]; const finite=[];
    for (let i=0;i<data.length;i++) {
      const r=data[i], ages=new Array(GRID_N).fill(NaN);
      for (let j=0;j<GRID_N;j++) {
        const q=qGrid[j];
        if (q < r.q_bid || q > r.q_ask) { lastOutside[j]=r.ts_ms; continue; }
        if (Number.isFinite(lastOutside[j])) {
          const age=(r.ts_ms-lastOutside[j])/1000;
          ages[j]=age; finite.push(age);
        }
      }
      cells.push(ages);
    }
    const rawMax=Math.max(percentile(finite, .97), 1e-6);
    const transformedMax = heatScale==='log' ? Math.log1p(rawMax) : rawMax;
    for (let i=0;i<data.length;i++) {
      const x0=pad.l + i/data.length*plotW;
      const x1=pad.l + (i+1)/data.length*plotW + .7;
      for (let j=0;j<GRID_N;j++) {
        const age=cells[i][j]; if (!Number.isFinite(age)) continue;
        const value=heatScale==='log' ? Math.log1p(age) : age;
        const u=Math.min(1,value/transformedMax);
        const yy=pad.t + plotH - (j+1)/GRID_N*plotH;
        ctx.fillStyle=mixColor(u); ctx.fillRect(x0,yy,Math.max(1,x1-x0),plotH/GRID_N+1);
      }
    }
    ctx.beginPath(); ctx.strokeStyle=css('--text'); ctx.lineWidth=1.15; ctx.globalAlpha=.9;
    data.forEach((r,i)=>{const X=pad.l+(r.ts_ms-t0)/(t1-t0)*plotW,Y=pad.t+(yHi-r.q_ball)/(yHi-yLo)*plotH;i?ctx.lineTo(X,Y):ctx.moveTo(X,Y);}); ctx.stroke();ctx.globalAlpha=1;
    els.colorLegend.lastElementChild.textContent = `${rawMax < 10 ? rawMax.toFixed(1) : rawMax.toFixed(0)} s (p97)`;
  }

  function setHeatScale(which) {
    heatScale=which;
    els.linearScale.classList.toggle('selected', which==='linear');
    els.logScale.classList.toggle('selected', which==='log');
    els.heatmapCaption.textContent = which==='linear'
      ? 'Color = Δt: seconds since each q level was last outside the spread (linear color scale).'
      : 'Color = log(1 + Δt), while the legend remains labeled in actual seconds.';
    scheduleRender();
  }

  function exportCsv() {
    const exportRows = liveRows.length ? liveRows : rows;
    if (!exportRows.length) return;
    const keys=['source','historical','ts_ms','recv_ts_ms','market','a_label','aprime_label','a_bid','a_ask','aprime_bid','aprime_ask','q_bid','q_ask','q_mid','q_ratio_of_mids','q_ball'];
    const esc=v=>{const s=String(v??'');return /[",\n]/.test(s)?`"${s.replaceAll('"','""')}"`:s;};
    const csv=[keys.join(','), ...exportRows.map(r=>keys.map(k=>esc(r[k])).join(','))].join('\n');
    const blob=new Blob([csv],{type:'text/csv'}); const url=URL.createObjectURL(blob); const a=document.createElement('a');
    a.href=url; a.download=`orderbook-ball-${exportRows[0].market || 'market'}.csv`; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  }

  els.loadButton.addEventListener('click', loadMarkets);
  els.marketInput.addEventListener('keydown', e=>{if(e.key==='Enter')loadMarkets();});
  els.connectButton.addEventListener('click', connect);
  els.disconnectButton.addEventListener('click', ()=>disconnect());
  els.exportButton.addEventListener('click', exportCsv);
  els.clearButton.addEventListener('click', clearData);
  els.linearScale.addEventListener('click', ()=>setHeatScale('linear'));
  els.logScale.addEventListener('click', ()=>setHeatScale('log'));
  els.windowSelect.addEventListener('change', scheduleRender);
  window.addEventListener('resize', scheduleRender);
})();
