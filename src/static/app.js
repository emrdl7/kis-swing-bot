let fetchCount = 0;
let failCount = 0;
let evtSource = null;

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

function applySnapshot(snap) {
  const s = snap.summary;
  setText('updated-at', snap.updated_at);
  setText('daily-pnl', fmtSigned(s.daily_pnl));    setColor('daily-pnl', s.pnl_color);
  setText('unrealized-pnl', fmtSigned(s.unrealized_pnl)); setColor('unrealized-pnl', s.unr_color);
  setText('active-count', s.active_count + '종목');
  setText('closed-count', s.closed_count + '건');
  setText('cand-count', s.cand_count + '종목');
  setText('account-cash', fmtInt(s.account_cash));
  setText('order-cash', fmtInt(s.order_cash));
  setText('eval-amt', fmtInt(s.eval_amt));
  setText('total-eval', fmtInt(s.total_eval));
  setHTML('win-rate', s.win_rate.toFixed(1) + '% <small class="text-muted">(' + s.wins + '/' + s.total_trades + ')</small>');
  setText('total-realized', fmtSigned(s.total_realized)); setColor('total-realized', s.total_realized_color);
  setHTML('positions-tbody', snap.positions_html);
  setHTML('candidates-tbody', snap.candidates_html);
  setHTML('closed-tbody', snap.closed_html);
  setHTML('chart-panel', snap.chart_html);
  setHTML('strategy-cards', snap.strategy_html);
  setHTML('bot-panel', snap.bot_html);
  setHTML('events-panel', snap.events_html);
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
      setHTML('live-status', '<span style="color:#00c9a7">● LIVE</span>');
      failCount = 0;
    } catch (e) { /* 파싱 오류 무시 */ }
  };
  evtSource.onerror = function() {
    failCount++;
    setHTML('live-status', '<span style="color:#ff6b6b">● OFFLINE (' + failCount + ')</span>');
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
    setHTML('live-status', '<span style="color:#00c9a7">● LIVE</span>');
    failCount = 0;
  } catch (e) {
    failCount++;
    setHTML('live-status', '<span style="color:#ff6b6b">● OFFLINE (' + failCount + ')</span>');
  }
}

/* SSE 시작 */
connectSSE();

/* 수동 매도 */
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
  modal.style.removeProperty('display');
  modal.style.setProperty('display', 'flex', 'important');
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
  document.getElementById('agent-modal').style.setProperty('display', 'none', 'important');
}
function closeAgentModal(e) {
  if (Date.now() - _modalOpenTs < 400) return;
  if (e && e.target !== document.getElementById('agent-modal')) return;
  _hideModal();
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') _hideModal(); });
// 모달 닫기 버튼용 전역 함수 (onclick 속성에서 호출)
function closeModalBtn() { _hideModal(); }

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* 테마 토글 */
function toggleTheme() {
  document.body.classList.toggle('light');
  localStorage.setItem('theme', document.body.classList.contains('light') ? 'light' : 'dark');
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
      btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><path d="M11 8v3l2 2"/></svg> 지금 재토론';
    }, 3000);
  }
}
