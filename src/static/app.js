let fetchCount = 0;
let failCount = 0;
let evtSource = null;
let stockChart = null;
let stockChartResize = null;
let stockChartData = null;
let stockChartSymbol = null;
let stockChartName = null;
let stockChartInterval = 'intraday';
let stockChartSeries = null;
let stockChartPollTimer = null;
let stockChartPolling = false;
let stockChartModalOpenTs = 0;
let stockChartRequestToken = 0;
let holdingsSort = 'value';
const HOLDINGS_SORT_KEY = 'kis-holdings-sort';

/* ── 매수 정지 토글 ── */
async function toggleEntryPause() {
  const btn = document.getElementById('entry-pause-btn');
  const currentlyPaused = btn && btn.classList.contains('paused');
  const next = !currentlyPaused;
  if (next && !confirm('🛑 신규 매수를 정지하시겠습니까?\n\n기존 보유분 매도는 정상 작동합니다.')) return;
  if (!next && !confirm('▶ 매수를 재개하시겠습니까?')) return;
  try {
    const res = await fetch('/api/entry-pause', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({paused: next, reason: next ? '사용자 수동' : ''})
    });
    const data = await res.json();
    if (data.ok) {
      applyPauseUI(data.entry_paused);
      refreshNow();
    } else { alert('실패: ' + (data.error || 'unknown')); }
  } catch (e) { alert('네트워크 오류: ' + e.message); }
}

function applyPauseUI(paused) {
  const btn = document.getElementById('entry-pause-btn');
  const banner = document.getElementById('pause-banner');
  const icon = document.getElementById('entry-pause-icon');
  const label = document.getElementById('entry-pause-label');
  if (btn) btn.classList.toggle('paused', paused);
  if (banner) banner.style.display = paused ? '' : 'none';
  if (icon) icon.textContent = paused ? '▶' : '⏸';
  if (label) label.textContent = paused ? '자동매수 재개' : '자동매수 정지';
}

/* ── 모바일 탭 전환 — 정보/매매 2분할 ── */
function switchTab(name) {
  if (name !== 'info' && name !== 'trading') name = 'info';
  document.body.classList.remove('tab-info', 'tab-trading');
  document.body.classList.add('tab-' + name);
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  try { localStorage.setItem('activeTab', name); } catch(e) {}
}

/* 초기 탭 — 저장된 값(legacy 'analysis'는 'info'로 정규화) */
(function initTab() {
  let t = 'info';
  try { t = localStorage.getItem('activeTab') || 'info'; } catch(e) {}
  if (t !== 'info' && t !== 'trading') t = 'info';
  document.body.classList.remove('tab-info', 'tab-trading');
  document.body.classList.add('tab-' + t);
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.tab-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.tab === t);
    });
  });
})();

function getHoldingsSort() {
  try {
    const saved = localStorage.getItem(HOLDINGS_SORT_KEY);
    if (saved === 'value' || saved === 'pnl') return saved;
  } catch (e) {}
  return holdingsSort === 'pnl' ? 'pnl' : 'value';
}

function applyHoldingsSort() {
  const tbody = document.getElementById('positions-tbody');
  const sort = getHoldingsSort();
  holdingsSort = sort;
  document.querySelectorAll('[data-holdings-sort]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.holdingsSort === sort);
  });
  if (!tbody) return;
  const rows = Array.from(tbody.querySelectorAll('tr.row-compact'));
  if (!rows.length) return;
  const key = sort === 'pnl' ? 'pnlPct' : 'evalAmount';
  rows.sort((a, b) => Number(b.dataset[key] || 0) - Number(a.dataset[key] || 0));
  rows.forEach(row => tbody.appendChild(row));
}

function setHoldingsSort(sort) {
  if (sort !== 'value' && sort !== 'pnl') sort = 'value';
  holdingsSort = sort;
  try { localStorage.setItem(HOLDINGS_SORT_KEY, sort); } catch (e) {}
  applyHoldingsSort();
}
document.addEventListener('DOMContentLoaded', applyHoldingsSort);

function setText(id, text) {
  const el = document.getElementById(id);
  if (el && el.textContent !== text) el.textContent = text;
}
function setHTML(id, html) {
  const el = document.getElementById(id);
  if (el && el.innerHTML !== html) el.innerHTML = html;
}
function setColor(id, color) {
  const el = document.getElementById(id);
  if (el) el.style.color = color;
}
function fmtSigned(n) {
  return (n >= 0 ? '+' : '') + n.toLocaleString('ko-KR') + '원';
}
function fmtInt(n) { return n.toLocaleString('ko-KR') + '원'; }
function fmtPrice(n) { return Number(n || 0).toLocaleString('ko-KR'); }

function fmtKstTime(timestamp) {
  const d = new Date(Number(timestamp) * 1000);
  return d.toLocaleTimeString('ko-KR', {
    timeZone: 'Asia/Seoul',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function fmtKstDate(timestamp) {
  const d = new Date(Number(timestamp) * 1000);
  return d.toLocaleDateString('ko-KR', {
    timeZone: 'Asia/Seoul',
    month: '2-digit',
    day: '2-digit',
  });
}

function fmtBusinessDayDate(day) {
  if (!day) return '';
  if (typeof day === 'string') {
    const parts = day.split('-');
    if (parts.length === 3) return `${parts[1]}/${parts[2]}`;
    return day || '';
  }
  return `${String(day.month).padStart(2, '0')}/${String(day.day).padStart(2, '0')}`;
}

function chartTickFormatter(time, tickMarkType, locale) {
  if (typeof time === 'number') return fmtKstTime(time);
  return fmtBusinessDayDate(time);
}

function chartTimeFormatter(time) {
  if (typeof time === 'number') return fmtKstTime(time);
  return fmtBusinessDayDate(time);
}

function chartLocalTtl(interval) {
  return interval === 'intraday' ? 10 * 60 * 1000 : 24 * 60 * 60 * 1000;
}

function chartLocalKey(symbol, interval) {
  return 'kis-chart:' + symbol + ':' + interval;
}

function getLocalChart(symbol, interval) {
  try {
    const raw = localStorage.getItem(chartLocalKey(symbol, interval));
    if (!raw) return null;
    const payload = JSON.parse(raw);
    if (!payload || !payload.data || !payload.savedAt) return null;
    if (Date.now() - payload.savedAt > chartLocalTtl(interval)) return null;
    return payload.data;
  } catch (e) {
    return null;
  }
}

function saveLocalChart(symbol, interval, data) {
  try {
    localStorage.setItem(chartLocalKey(symbol, interval), JSON.stringify({
      savedAt: Date.now(),
      data,
    }));
  } catch (e) {
    // 브라우저 저장공간이 꽉 찬 경우에도 차트 표시 자체는 계속한다.
  }
}

function chartTimeKey(t) {
  if (typeof t === 'number') return t;
  if (t && typeof t === 'object') return `${t.year}-${t.month}-${t.day}`;
  return String(t || '');
}

function cssVar(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim();
}

function addSeriesCompat(chart, seriesType, options, fallbackName) {
  if (chart.addSeries && seriesType) return chart.addSeries(seriesType, options);
  return chart[fallbackName](options);
}

function applyDefaultChartRange(chart, data) {
  const candles = data && data.candles ? data.candles : [];
  const timeScale = chart && chart.timeScale ? chart.timeScale() : null;
  if (!timeScale) return;
  if (data.interval === 'intraday' && candles.length > 55 && timeScale.setVisibleLogicalRange) {
    const visible = Math.min(55, candles.length);
    timeScale.setVisibleLogicalRange({
      from: candles.length - visible,
      to: candles.length + 3,
    });
    return;
  }
  timeScale.fitContent();
}

function setChartIntervalUI(interval) {
  document.querySelectorAll('[data-chart-interval]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.chartInterval === interval);
  });
}

function openStockChartModal() {
  const modal = document.getElementById('stock-chart-modal');
  if (!modal) return;
  stockChartModalOpenTs = Date.now();
  modal.style.display = 'flex';
  document.body.classList.add('modal-open');
}

function closeStockChartModal() {
  stockChartRequestToken++;
  const modal = document.getElementById('stock-chart-modal');
  if (modal) modal.style.display = 'none';
  document.body.classList.remove('modal-open');
  stopStockChartPolling();
  if (stockChartResize) {
    stockChartResize.disconnect();
    stockChartResize = null;
  }
  if (stockChart) {
    stockChart.remove();
    stockChart = null;
  }
  const canvas = document.getElementById('stock-chart-canvas');
  if (canvas) {
    canvas.innerHTML = '';
    canvas.style.display = 'none';
  }
  stockChartSeries = null;
  stockChartData = null;
  stockChartSymbol = null;
}

async function showStockChart(symbol, name, interval) {
  const requestToken = ++stockChartRequestToken;
  stopStockChartPolling();
  stockChartSymbol = symbol;
  stockChartName = name || symbol;
  stockChartInterval = interval || stockChartInterval || 'intraday';
  setChartIntervalUI(stockChartInterval);
  const title = document.getElementById('stock-chart-title');
  const note = document.getElementById('stock-chart-note');
  const empty = document.getElementById('stock-chart-empty');
  const err = document.getElementById('stock-chart-error');
  const toolbar = document.getElementById('stock-chart-toolbar');
  const meta = document.getElementById('stock-chart-meta');
  const canvas = document.getElementById('stock-chart-canvas');
  if (!canvas) return;
  openStockChartModal();
  if (title) title.textContent = (stockChartName || symbol) + ' 차트';
  if (note) note.textContent = symbol;
  if (empty) empty.style.display = 'none';
  if (err) err.style.display = 'none';
  if (toolbar) toolbar.style.display = '';
  if (meta) meta.textContent = '불러오는 중...';
  canvas.style.display = 'block';
  const localCached = getLocalChart(symbol, stockChartInterval);
  if (localCached) {
    stockChartData = localCached;
    renderStockChart(localCached, {localCached: true, refreshing: true});
  } else {
    canvas.innerHTML = '';
  }
  try {
    const params = new URLSearchParams({interval: stockChartInterval});
    const res = await fetch('/api/chart/' + encodeURIComponent(symbol) + '?' + params, {cache: 'no-store'});
    const data = await res.json();
    if (requestToken !== stockChartRequestToken) return;
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    stockChartData = data;
    saveLocalChart(symbol, stockChartInterval, data);
    renderStockChart(data);
    startStockChartPolling();
  } catch (e) {
    if (requestToken !== stockChartRequestToken) return;
    if (localCached) {
      if (meta) meta.textContent = (meta.textContent || '') + ' · 갱신 실패';
      return;
    }
    if (stockChart) {
      stockChart.remove();
      stockChart = null;
    }
    canvas.style.display = 'none';
    if (meta) meta.textContent = '';
    if (err) {
      err.textContent = '차트 로드 실패: ' + e.message;
      err.style.display = 'block';
    }
  }
}

function renderStockChart(data, opts = {}) {
  const L = window.LightweightCharts;
  const canvas = document.getElementById('stock-chart-canvas');
  const meta = document.getElementById('stock-chart-meta');
  if (!canvas) return;
  if (!L || !L.createChart) {
    throw new Error('차트 라이브러리를 불러오지 못했습니다.');
  }
  if (stockChartResize) {
    stockChartResize.disconnect();
    stockChartResize = null;
  }
  if (stockChart) {
    stockChart.remove();
    stockChart = null;
  }
  canvas.innerHTML = '';
  const text = cssVar('--text') || '#edf1f7';
  const muted = cssVar('--muted') || '#99a4b5';
  const bg = cssVar('--surface') || '#141922';
  const line = cssVar('--line') || '#283142';
  stockChart = L.createChart(canvas, {
    width: canvas.clientWidth,
    height: canvas.clientHeight,
    layout: {
      background: {type: 'solid', color: bg},
      textColor: text,
      fontFamily: 'Sweet, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    },
    grid: {
      vertLines: {color: line},
      horzLines: {color: line},
    },
    rightPriceScale: {
      borderColor: line,
      scaleMargins: {top: 0.08, bottom: 0.26},
    },
    timeScale: {
      borderColor: line,
      timeVisible: data.interval === 'intraday',
      secondsVisible: false,
      tickMarkFormatter: chartTickFormatter,
    },
    localization: {
      priceFormatter: price => fmtPrice(price),
      timeFormatter: chartTimeFormatter,
    },
    crosshair: {
      mode: L.CrosshairMode ? L.CrosshairMode.Normal : 0,
    },
  });

  const candleSeries = addSeriesCompat(stockChart, L.CandlestickSeries, {
    upColor: '#ef4444',
    downColor: '#3b82f6',
    borderVisible: false,
    wickUpColor: '#ef4444',
    wickDownColor: '#3b82f6',
    title: '',
  }, 'addCandlestickSeries');
  candleSeries.setData(data.candles || []);

  const volumeSeries = addSeriesCompat(stockChart, L.HistogramSeries, {
    priceFormat: {type: 'volume'},
    priceScaleId: '',
    color: 'rgba(153, 164, 181, 0.35)',
    title: '',
    lastValueVisible: false,
    priceLineVisible: false,
  }, 'addHistogramSeries');
  if (volumeSeries.priceScale) {
    volumeSeries.priceScale().applyOptions({scaleMargins: {top: 0.78, bottom: 0}});
  } else if (stockChart.priceScale) {
    stockChart.priceScale('').applyOptions({scaleMargins: {top: 0.78, bottom: 0}});
  }
  volumeSeries.setData(data.volume || []);
  stockChartSeries = {candle: candleSeries, volume: volumeSeries, ma: {}};

  const lineSeries = [
    ['ma5', '#f59e0b', 'MA5'],
    ['ma20', '#22c55e', 'MA20'],
    ['ma60', '#a78bfa', 'MA60'],
  ];
  lineSeries.forEach(([key, color, title]) => {
    const points = data.ma && data.ma[key] ? data.ma[key] : [];
    if (!points.length) return;
    const s = addSeriesCompat(stockChart, L.LineSeries, {
      color,
      lineWidth: key === 'ma20' ? 2 : 1,
      priceLineVisible: false,
      lastValueVisible: false,
      title: '',
    }, 'addLineSeries');
    s.setData(points);
    stockChartSeries.ma[key] = s;
  });

  (data.levels || []).forEach(level => {
    candleSeries.createPriceLine({
      price: Number(level.price),
      color: level.color || muted,
      lineWidth: 1,
      lineStyle: L.LineStyle ? L.LineStyle.Dashed : 2,
      axisLabelVisible: true,
      title: String(level.title || ''),
    });
  });

  applyDefaultChartRange(stockChart, data);
  stockChartResize = new ResizeObserver(() => {
    if (stockChart && canvas.clientWidth > 0) {
      stockChart.applyOptions({width: canvas.clientWidth, height: canvas.clientHeight});
    }
  });
  stockChartResize.observe(canvas);

  if (meta) {
    if (opts.localCached) {
      updateChartMeta(data, opts.refreshing ? ' · 로컬 캐시 갱신 중' : ' · 로컬 캐시');
    } else {
      updateChartMeta(data);
    }
  }
}

function mergeSeriesByTime(oldItems, newItems) {
  const merged = new Map();
  (oldItems || []).forEach(item => merged.set(chartTimeKey(item.time), item));
  (newItems || []).forEach(item => merged.set(chartTimeKey(item.time), item));
  return Array.from(merged.values()).sort((a, b) => {
    const ka = chartTimeKey(a.time);
    const kb = chartTimeKey(b.time);
    return ka < kb ? -1 : (ka > kb ? 1 : 0);
  });
}

function applyChartIncrementalUpdate(newData) {
  if (!stockChartData || !stockChartSeries || !stockChartSeries.candle || !stockChartSeries.volume) {
    renderStockChart(newData);
    return;
  }
  if (newData.symbol !== stockChartData.symbol || newData.interval !== stockChartData.interval) {
    renderStockChart(newData);
    return;
  }

  const oldLast = stockChartData.candles && stockChartData.candles[stockChartData.candles.length - 1];
  const newLast = newData.candles && newData.candles[newData.candles.length - 1];
  if (!oldLast || !newLast) {
    renderStockChart(newData);
    return;
  }

  const oldLastKey = chartTimeKey(oldLast.time);
  const updateCandles = (newData.candles || []).filter(c => chartTimeKey(c.time) >= oldLastKey);
  const updateVolumes = (newData.volume || []).filter(v => chartTimeKey(v.time) >= oldLastKey);
  updateCandles.forEach(c => stockChartSeries.candle.update(c));
  updateVolumes.forEach(v => stockChartSeries.volume.update(v));

  stockChartData = {
    ...newData,
    candles: mergeSeriesByTime(stockChartData.candles, updateCandles),
    volume: mergeSeriesByTime(stockChartData.volume, updateVolumes),
  };

  saveLocalChart(stockChartData.symbol, stockChartData.interval, stockChartData);
  updateChartMeta(stockChartData);
}

function updateChartMeta(data, suffix = '') {
  const meta = document.getElementById('stock-chart-meta');
  if (!meta) return;
  const last = data.last || {};
  const ind = data.indicators || {};
  const rsi = ind.rsi14 == null ? '-' : Number(ind.rsi14).toFixed(1);
  const rsiText = data.interval === 'intraday' ? '' : ` · RSI ${rsi}`;
  let cacheText = '';
  if (data.cache && data.cache.stale) cacheText = ` · 서버 캐시 ${data.cache.age_sec || 0}s`;
  meta.textContent = `${data.symbol} · ${data.interval_label || ''} · 종가 ${fmtPrice(last.close)} · 거래량 ${fmtPrice(last.volume)}${rsiText}${cacheText}${suffix}`;
}

async function pollStockChart() {
  if (!stockChartSymbol || stockChartPolling) return;
  stockChartPolling = true;
  try {
    const params = new URLSearchParams({interval: stockChartInterval, refresh: '1'});
    const res = await fetch('/api/chart/' + encodeURIComponent(stockChartSymbol) + '?' + params, {cache: 'no-store'});
    const data = await res.json();
    if (res.ok) applyChartIncrementalUpdate(data);
  } catch (e) {
    if (stockChartData) updateChartMeta(stockChartData, ' · 갱신 지연');
  } finally {
    stockChartPolling = false;
  }
}

function startStockChartPolling() {
  stopStockChartPolling();
  const intervalMs = stockChartInterval === 'intraday' ? 30000 : 180000;
  stockChartPollTimer = setInterval(pollStockChart, intervalMs);
}

function stopStockChartPolling() {
  if (stockChartPollTimer) {
    clearInterval(stockChartPollTimer);
    stockChartPollTimer = null;
  }
  stockChartPolling = false;
}

function applySnapshot(snap) {
  const s = snap.summary;
  setText('updated-at', snap.updated_at);
  // KPI Bar
  setText('daily-pnl', fmtSigned(s.daily_pnl));    setColor('daily-pnl', s.pnl_color);
  setText('daily-pnl-meta', s.pnl_source_label || '');
  setText('unrealized-pnl', fmtSigned(s.unrealized_pnl)); setColor('unrealized-pnl', s.unr_color);
  setText('unrealized-pnl-meta', s.unrealized_source_label || '');
  setText('total-eval', fmtInt(s.total_eval));
  setText('order-cash', fmtInt(s.order_cash));
  setHTML('win-rate', s.win_rate.toFixed(0) + '% <small class="text-muted">(' + s.wins + '/' + s.total_trades + ')</small>');
  // Mini Stats (매매 영역)
  setText('account-cash', fmtInt(s.account_cash));
  setText('eval-amt', fmtInt(s.eval_amt));
  setText('closed-count', s.closed_count + '건');
  setText('cand-count', s.cand_count + '종목');
  setText('total-realized', fmtSigned(s.total_realized)); setColor('total-realized', s.total_realized_color);
  // 정보 영역
  setHTML('market-overview', snap.market_overview_html || '');
  setHTML('position-analysis', snap.position_analysis_html || '');
  setHTML('chart-panel', snap.chart_html);
  setHTML('account-ledger', snap.account_ledger_html || '');
  setHTML('selection-pipeline', snap.selection_pipeline_html || '');
  setHTML('selection-briefing', snap.selection_briefing_html || '');
  setHTML('candidate-radar-tbody', snap.candidate_radar_html || '');
  // 매매 영역
  setHTML('strategy-cards', snap.strategy_html);
  setHTML('positions-tbody', snap.positions_html);
  applyHoldingsSort();
  setHTML('candidates-tbody', snap.candidates_html);
  setHTML('watchlist-tbody', snap.watchlist_html || '');
  setHTML('sell-blacklist-tbody', snap.sell_blacklist_html || '');
  setHTML('closed-tbody', snap.closed_html);
  setHTML('events-panel', snap.events_html);
  setHTML('bot-panel', snap.bot_html);
  applyPauseUI(!!snap.entry_paused);
  fetchCount++;
  setText('fetch-count', fetchCount);
}

/* SSE 연결 */
function connectSSE() {
  if (evtSource) evtSource.close();
  evtSource = new EventSource('/api/stream');
  evtSource.onmessage = function(event) {
    try {
      const snap = JSON.parse(event.data);
      applySnapshot(snap);
      setHTML('live-status', '● LIVE');
      failCount = 0;
    } catch (e) { /* 파싱 오류 무시 */ }
  };
  evtSource.onerror = function() {
    failCount++;
    setHTML('live-status', '● OFFLINE (' + failCount + ')');
    evtSource.close();
    evtSource = null;
    setTimeout(connectSSE, 3000);
  };
}

/* 폴링 fallback (SSE 미지원 환경) */
async function refreshNow() {
  try {
    const res = await fetch('/api/snapshot', {cache: 'no-store'});
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const snap = await res.json();
    applySnapshot(snap);
    setHTML('live-status', '● LIVE');
    failCount = 0;
  } catch (e) {
    failCount++;
    setHTML('live-status', '● OFFLINE (' + failCount + ')');
  }
}

/* SSE 시작 */
connectSSE();

/* 이벤트 위임 — 동적 HTML의 data-action 버튼 처리 */
function _handleAction(e) {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  const action = btn.dataset.action;
  if (action === 'show-analysis') {
    showAgentModal(btn.dataset.symbol, btn.dataset.name);
  } else if (action === 'show-chart') {
    showStockChart(btn.dataset.symbol, btn.dataset.name);
  } else if (action === 'close-modal') {
    _hideModal();
  } else if (action === 'close-chart-modal') {
    closeStockChartModal();
  }
}
document.addEventListener('touchend', function(e) {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  e.preventDefault(); // 후속 click 이벤트 방지 (ghost click)
  _handleAction(e);
}, {passive: false});
document.addEventListener('click', _handleAction);
document.addEventListener('click', e => {
  const btn = e.target.closest('[data-holdings-sort]');
  if (!btn) return;
  e.preventDefault();
  setHoldingsSort(btn.dataset.holdingsSort);
});
document.addEventListener('click', e => {
  const btn = e.target.closest('[data-chart-interval]');
  if (!btn) return;
  e.preventDefault();
  if (!stockChartSymbol) return;
  showStockChart(stockChartSymbol, stockChartName, btn.dataset.chartInterval);
});

/* 수동 매도 (시장가) */
async function sellPosition(symbol, name, qty) {
  if (!confirm(name + ' ' + qty + '주를 시장가 매도하시겠습니까?')) return;
  try {
    const res = await fetch('/api/sell', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({symbol})
    });
    const data = await res.json();
    if (data.ok) {
      alert(name + ' 매도 완료\n체결가: ' + data.close_price.toLocaleString() + '원\nPnL: ' + data.pnl.toLocaleString() + '원');
      refreshNow();
    } else {
      alert('매도 실패: ' + (data.error || 'unknown'));
    }
  } catch (e) { alert('네트워크 오류: ' + e.message); }
}

/* 매도 금지 종목 — 추가 */
async function addSellBlacklist() {
  const input = document.getElementById('blacklist-symbol-input');
  const symbol = (input.value || '').trim();
  if (!symbol || !/^\d{6}$/.test(symbol)) {
    alert('6자리 종목코드를 입력하세요.');
    return;
  }
  try {
    const res = await fetch('/api/sell-blacklist/add', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({symbol}),
    });
    const data = await res.json();
    if (data.ok) {
      input.value = '';
      refreshNow();
    } else {
      alert('실패: ' + (data.error || 'unknown'));
    }
  } catch (e) { alert('네트워크 오류: ' + e.message); }
}

/* 매도 금지 종목 — 해제 */
async function removeSellBlacklist(symbol, name) {
  if (!confirm(`${name}(${symbol}) 매도 금지를 해제하시겠습니까?\n해제 후에는 봇이 자동 매도할 수 있습니다.`)) return;
  try {
    const res = await fetch('/api/sell-blacklist/remove', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({symbol}),
    });
    const data = await res.json();
    if (data.ok) {
      refreshNow();
    } else {
      alert('실패: ' + (data.error || 'unknown'));
    }
  } catch (e) { alert('네트워크 오류: ' + e.message); }
}

/* 현재가 셀 클릭 → 지정가 매도 */
async function sellAtPrice(symbol, name, qty, price) {
  if (!price || price <= 0) return;
  if (!confirm(`${name} ${qty}주를 ${price.toLocaleString()}원에 지정가 매도하시겠습니까?`)) return;
  try {
    const res = await fetch('/api/sell', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({symbol, price})
    });
    const data = await res.json();
    if (data.ok) {
      alert(`${name} 지정가 매도 주문 전송\n가격: ${data.close_price.toLocaleString()}원\nPnL(예상): ${data.pnl.toLocaleString()}원`);
      refreshNow();
    } else {
      alert('매도 실패: ' + (data.error || 'unknown'));
    }
  } catch (e) { alert('네트워크 오류: ' + e.message); }
}

/* 인라인 가격 편집 */
function editPrice(td, symbol, field, currentVal) {
  if (td.querySelector('input')) return;
  const orig = td.textContent;
  const input = document.createElement('input');
  input.className = 'edit-input';
  input.type = 'number';
  input.value = currentVal;
  input.onkeydown = (e) => {
    if (e.key === 'Enter') input.blur();
    if (e.key === 'Escape') { td.textContent = orig; }
  };
  input.onblur = async () => {
    const val = parseFloat(input.value);
    if (!val || val <= 0 || val === currentVal) { td.textContent = orig; return; }
    try {
      const body = {symbol};
      body[field] = val;
      const res = await fetch('/api/update-position', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      });
      const data = await res.json();
      if (data.ok) {
        td.textContent = val.toLocaleString('ko-KR');
        refreshNow();
      } else { td.textContent = orig; alert('수정 실패: ' + data.error); }
    } catch (e) { td.textContent = orig; }
  };
  td.textContent = '';
  td.appendChild(input);
  input.focus();
  input.select();
}

/* 후보 제거 */
async function removeCandidate(symbol, name) {
  if (!confirm(name + ' 후보를 제거하시겠습니까?')) return;
  try {
    const res = await fetch('/api/remove-candidate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({symbol})
    });
    const data = await res.json();
    if (data.ok) refreshNow();
    else alert('제거 실패: ' + (data.error || 'unknown'));
  } catch (e) { alert('네트워크 오류: ' + e.message); }
}

/* 에이전트 분석 모달 */
let _modalOpenTs = 0;

async function showAgentModal(symbol, name) {
  _modalOpenTs = Date.now();
  const modal = document.getElementById('agent-modal');
  const title = document.getElementById('modal-title');
  const body  = document.getElementById('modal-body');
  title.textContent = name + ' (' + symbol + ') — 에이전트 분석';
  body.innerHTML = '<div style="color:#666;font-size:12px">불러오는 중...</div>';
  modal.style.cssText = 'display:flex;position:fixed;top:0;left:0;right:0;bottom:0;z-index:9999;background:rgba(0,0,0,.65);align-items:center;justify-content:center;padding:16px';
  try {
    const res = await fetch('/api/candidate-detail/' + symbol);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const d = await res.json();
    let html = '';
    // 모더레이터 최종 판단
    const scoreStr = d.consensus_score != null ? ' · 신뢰도 ' + Math.round(d.consensus_score * 100) + '%' : '';
    html += '<div class="modal-rationale"><div class="modal-rationale-label">선정 근거' + scoreStr + '</div>' + escHtml(d.rationale || '-') + '</div>';
    // 에이전트 의견
    if (d.agent_opinions && d.agent_opinions.length > 0) {
      d.agent_opinions.forEach(op => {
        const pct = Math.round(op.conviction * 100);
        const barColor = op.role === 'risk' ? '#f9ca24' : '#00c9a7';
        const roleLabel = op.role === 'risk' ? '리스크 경고' : '매수 추천';
        const roleClass = op.role === 'risk' ? 'agent-role-risk' : 'agent-role-buy';
        html += '<div class="agent-card">'
          + '<div class="agent-card-header">'
          + '<span class="agent-label">' + escHtml(op.label) + '</span>'
          + '<span class="' + roleClass + '">' + roleLabel + '</span>'
          + '</div>'
          + '<div class="conviction-row">'
          + '<div class="conviction-bar-bg"><div class="conviction-bar" style="width:' + pct + '%;background:' + barColor + '"></div></div>'
          + '<span class="conviction-val">' + pct + '%</span>'
          + '</div>'
          + '<div class="agent-rationale">' + escHtml(op.rationale || '-') + '</div>'
          + '</div>';
      });
    } else {
      html += '<div style="color:#555;font-size:12px">에이전트별 상세 의견 없음<br><small>(다음 토론부터 기록됩니다)</small></div>';
    }
    body.innerHTML = html;
  } catch (e) {
    body.innerHTML = '<div style="color:#ff6b6b;font-size:12px">불러오기 실패: ' + e.message + '</div>';
  }
}

function _hideModal() {
  document.getElementById('agent-modal').style.cssText = 'display:none;position:fixed;top:0;left:0;right:0;bottom:0;z-index:9999';
}
function closeAgentModal(e) {
  if (Date.now() - _modalOpenTs < 400) return;
  if (e && e.target !== document.getElementById('agent-modal')) return;
  _hideModal();
}
document.addEventListener('click', e => {
  if (e.target === document.getElementById('stock-chart-modal') && Date.now() - stockChartModalOpenTs > 250) {
    closeStockChartModal();
  }
  if (e.target === document.getElementById('agent-modal')) {
    closeAgentModal(e);
  }
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const chartModal = document.getElementById('stock-chart-modal');
  if (chartModal && chartModal.style.display !== 'none') {
    closeStockChartModal();
    return;
  }
  _hideModal();
});
// 모달 닫기 버튼용 전역 함수 (onclick 속성에서 호출)
function closeModalBtn() { _hideModal(); }

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* 테마 토글 */
function toggleTheme() {
  document.body.classList.toggle('light');
  localStorage.setItem('theme', document.body.classList.contains('light') ? 'light' : 'dark');
  if (stockChartData) renderStockChart(stockChartData);
}
if (localStorage.getItem('theme') === 'light') document.body.classList.add('light');

/* 재토론 */
async function triggerRescreen() {
  const btn = document.getElementById('rescreen-btn');
  const msg = document.getElementById('rescreen-msg');
  if (!confirm('지금 재토론을 실행하시겠어요? LLM 비용이 발생합니다.')) return;
  btn.disabled = true; btn.innerHTML = '⏳ 실행 중...';
  msg.textContent = '';
  try {
    const res = await fetch('/api/rescreen', {method: 'POST'});
    const data = await res.json();
    if (res.ok && data.ok) {
      msg.textContent = '✓ 재토론 시작 (오늘 ' + data.count_today + '회째, pid=' + data.pid + ')';
      msg.style.color = '#00c9a7';
    } else {
      msg.textContent = '✗ 실행 불가: ' + (data.reason || data.error || 'unknown');
      msg.style.color = '#ff6b6b';
    }
  } catch (e) {
    msg.textContent = '✗ 네트워크 오류: ' + e.message;
    msg.style.color = '#ff6b6b';
  } finally {
    setTimeout(() => {
      btn.disabled = false;
      btn.innerHTML = '재토론';
    }, 3000);
  }
}
