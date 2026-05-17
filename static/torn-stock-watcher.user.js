// ==UserScript==
// @name         Torn Stock Watcher - Fries91 Starter
// @namespace    Fries91.Torn.StockWatcher
// @version      0.2.1
// @description  Torn stock watcher overlay with predicted return simulator.
// @author       Fries91
// @match        https://www.torn.com/*
// @match        https://*.torn.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @updateURL    https://torn-smart-stock-watcher.onrender.com/static/torn-stock-watcher.user.js
// @downloadURL  https://torn-smart-stock-watcher.onrender.com/static/torn-stock-watcher.user.js
// @grant        GM_setValue
// @connect      *
// ==/UserScript==

(function () {
  'use strict';

  const APP = 'tsw_';
  const K_API = APP + 'api_key';
  const K_BACKEND = APP + 'backend_url';

  const DEFAULT_BACKEND = 'https://torn-smart-stock-watcher.onrender.com';

  const css = `
    #tswBtn{position:fixed;left:14px;bottom:72px;z-index:999999;width:44px;height:44px;border-radius:14px;
      border:1px solid rgba(255,255,255,.25);background:#121827;color:#fff;font-size:22px;box-shadow:0 8px 24px rgba(0,0,0,.35)}
    #tswPanel{position:fixed;left:12px;right:12px;top:70px;max-width:760px;margin:auto;z-index:1000000;
      background:#0e1422;color:#eaf0ff;border:1px solid rgba(255,255,255,.14);border-radius:18px;
      box-shadow:0 20px 60px rgba(0,0,0,.55);font-family:Arial,sans-serif;overflow:hidden}
    #tswPanel *{box-sizing:border-box}
    .tswHead{display:flex;gap:10px;align-items:center;justify-content:space-between;padding:12px 14px;background:#151d31}
    .tswTitle{font-weight:800;font-size:16px}
    .tswClose{background:#2b344f;color:#fff;border:0;border-radius:10px;padding:8px 10px}
    .tswBody{padding:12px;max-height:74vh;overflow:auto}
    .tswTabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
    .tswTab{border:1px solid rgba(255,255,255,.14);background:#192238;color:#fff;border-radius:12px;padding:8px 10px}
    .tswTab.active{background:#2f6bff}
    .tswCard{background:#111a2d;border:1px solid rgba(255,255,255,.12);border-radius:16px;padding:12px;margin:10px 0}
    .tswGrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
    @media(max-width:560px){.tswGrid{grid-template-columns:1fr}#tswPanel{top:54px}}
    .tswInput{width:100%;background:#0a1020;color:#fff;border:1px solid rgba(255,255,255,.18);border-radius:12px;padding:10px;margin:5px 0}
    .tswBtn2{background:#2f6bff;color:#fff;border:0;border-radius:12px;padding:10px 12px;font-weight:700;margin:5px 5px 5px 0}
    .tswBtnBad{background:#7b2434}
    .tswMuted{color:#aab6d3;font-size:12px}
    .tswGood{color:#6df5a8}.tswBad{color:#ff7b90}.tswWarn{color:#ffd36d}
    .tswRow{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid rgba(255,255,255,.08);padding:7px 0}
  `;

  function addStyle() {
    if (document.getElementById('tswStyle')) return;
    const s = document.createElement('style');
    s.id = 'tswStyle';
    s.textContent = css;
    document.head.appendChild(s);
  }

  function getBackend() {
    return (GM_getValue(K_BACKEND, DEFAULT_BACKEND) || DEFAULT_BACKEND).replace(/\/+$/, '');
  }

  function api(path, opts = {}) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: opts.method || 'GET',
        url: getBackend() + path,
        headers: {'Content-Type': 'application/json'},
        data: opts.body ? JSON.stringify(opts.body) : undefined,
        onload: r => {
          try { resolve(JSON.parse(r.responseText)); }
          catch { resolve({ok:false,error:r.responseText}); }
        },
        onerror: reject
      });
    });
  }

  function money(n) {
    if (n === undefined || n === null || isNaN(n)) return '-';
    return '$' + Number(n).toLocaleString(undefined, {maximumFractionDigits: 2});
  }

  function pct(n) {
    if (n === undefined || n === null || isNaN(n)) return '-';
    const cls = Number(n) >= 0 ? 'tswGood' : 'tswBad';
    return `<span class="${cls}">${Number(n).toFixed(2)}%</span>`;
  }

  function showPanel() {
    addStyle();
    let p = document.getElementById('tswPanel');
    if (p) { p.remove(); return; }

    p = document.createElement('div');
    p.id = 'tswPanel';
    p.innerHTML = `
      <div class="tswHead">
        <div class="tswTitle">📈 Torn Stock Watcher</div>
        <button class="tswClose" id="tswClose">Close</button>
      </div>
      <div class="tswBody">
        <div class="tswTabs">
          <button class="tswTab active" data-tab="pick">Today’s Pick</button>
          <button class="tswTab" data-tab="stocks">All Stocks</button>
          <button class="tswTab" data-tab="settings">Settings</button>
          <button class="tswTab" data-tab="tos">ToS / API</button>
        </div>
        <div id="tswContent">Loading...</div>
      </div>
    `;
    document.body.appendChild(p);

    p.querySelector('#tswClose').onclick = () => p.remove();
    p.querySelectorAll('.tswTab').forEach(b => b.onclick = () => {
      p.querySelectorAll('.tswTab').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      renderTab(b.dataset.tab);
    });

    renderTab('pick');
  }

  async function renderTab(tab) {
    const c = document.getElementById('tswContent');
    if (!c) return;

    if (tab === 'settings') {
      c.innerHTML = `
        <div class="tswCard">
          <b>Settings</b>
          <p class="tswMuted">Put your Render backend URL and Torn API key here.</p>
          <label>Backend URL</label>
          <input class="tswInput" id="tswBackend" value="${getBackend()}">
          <label>Torn API Key</label>
          <input class="tswInput" id="tswApiKey" type="password" value="${GM_getValue(K_API, '')}">
          <button class="tswBtn2" id="tswSave">Save</button>
          <button class="tswBtn2" id="tswSnap">Record Snapshot Now</button>
          <button class="tswBtn2" id="tswAutoStatus">Check Auto Watcher</button>
          <div id="tswSetMsg" class="tswMuted"></div>
          <div id="tswAutoBox" class="tswCard" style="display:none"></div>
        </div>
      `;
      document.getElementById('tswSave').onclick = () => {
        GM_setValue(K_BACKEND, document.getElementById('tswBackend').value.trim());
        GM_setValue(K_API, document.getElementById('tswApiKey').value.trim());
        document.getElementById('tswSetMsg').textContent = 'Saved.';
      };
      document.getElementById('tswSnap').onclick = async () => {
        const msg = document.getElementById('tswSetMsg');
        msg.textContent = 'Recording...';
        GM_setValue(K_BACKEND, document.getElementById('tswBackend').value.trim());
        GM_setValue(K_API, document.getElementById('tswApiKey').value.trim());
        const res = await api('/api/snapshot', {method:'POST', body:{api_key: GM_getValue(K_API, '')}});
        msg.textContent = res.ok ? `Saved ${res.saved} stock prices.` : `Error: ${res.error}`;
      };

      document.getElementById('tswAutoStatus').onclick = async () => {
        const box = document.getElementById('tswAutoBox');
        box.style.display = 'block';
        box.innerHTML = 'Checking auto watcher...';
        const res = await api('/api/auto_status');
        if (!res.ok) {
          box.innerHTML = `<span class="tswBad">Error checking auto watcher.</span>`;
          return;
        }
        box.innerHTML = `
          <b>Auto Watcher</b>
          <div class="tswRow"><span>Enabled</span><b>${res.auto_enabled ? 'Yes' : 'No'}</b></div>
          <div class="tswRow"><span>Render API key set</span><b>${res.has_render_api_key ? 'Yes' : 'No'}</b></div>
          <div class="tswRow"><span>Interval</span><b>${Math.round(res.interval_seconds / 60)} min</b></div>
          <div class="tswRow"><span>Last check</span><b>${res.state.last_check_ts ? new Date(res.state.last_check_ts * 1000).toLocaleString() : '-'}</b></div>
          <div class="tswRow"><span>Last saved</span><b>${res.state.last_saved_ts ? new Date(res.state.last_saved_ts * 1000).toLocaleString() : '-'}</b></div>
          <p class="tswMuted">${res.state.last_message || ''}</p>
          ${res.state.last_error ? `<p class="tswBad">${res.state.last_error}</p>` : ''}
        `;
      };
      return;
    }

    if (tab === 'tos') {
      c.innerHTML = `
        <div class="tswCard">
          <b>API Key Use</b>
          <p>This tool uses your Torn API key only to request stock price data and save price snapshots to your own backend.</p>
          <p>No Torn password is ever requested.</p>
          <p>Data stored: stock prices and timestamps. Your API key is stored locally in this userscript settings, not shown in the overlay after save.</p>
          <p>Purpose: trend tracking, personal stock simulation, and prediction scoring.</p>
          <p class="tswMuted">Predictions are estimates and can be wrong. Use this as a helper, not a guaranteed profit tool.</p>
        </div>
      `;
      return;
    }

    if (tab === 'stocks') {
      c.innerHTML = `<div class="tswCard">Loading stocks...</div>`;
      const res = await api('/api/stocks');
      if (!res.ok) {
        c.innerHTML = `<div class="tswCard tswBad">Error: ${res.error || 'Could not load stocks.'}</div>`;
        return;
      }
      c.innerHTML = `
        <div class="tswCard"><b>All Stocks</b><div class="tswMuted">Ranked by momentum score.</div></div>
        ${res.items.map(x => `
          <div class="tswCard">
            <div class="tswRow"><b>${x.acronym}</b><span>${money(x.current_price)}</span></div>
            <div class="tswRow"><span>24h</span><span>${pct(x.change_24h)}</span></div>
            <div class="tswRow"><span>Score</span><span>${x.score}</span></div>
            <div class="tswRow"><span>Target</span><span>${money(x.target_price)} / ${pct(x.target_pct)}</span></div>
            <div class="tswRow"><span>Risk</span><span>${x.risk}</span></div>
            <button class="tswBtn2" onclick="window.tswSimStock('${x.acronym}')">Simulate</button>
          </div>
        `).join('')}
      `;
      return;
    }

    c.innerHTML = `<div class="tswCard">Loading today’s pick...</div>`;
    const res = await api('/api/pick');
    if (!res.ok || !res.pick) {
      c.innerHTML = `
        <div class="tswCard">
          <b>No prediction yet</b>
          <p class="tswMuted">Go to Settings, save your API key, then click “Record Snapshot Now”. You need at least 2 snapshots before predictions work.</p>
        </div>
      `;
      return;
    }

    const x = res.pick;
    c.innerHTML = `
      <div class="tswCard">
        <b>📈 Today’s Best Pick: ${x.acronym}</b>
        <div class="tswMuted">${x.name}</div>
        <div class="tswGrid">
          <div class="tswCard"><div>Current</div><b>${money(x.current_price)}</b></div>
          <div class="tswCard"><div>Target</div><b>${money(x.target_price)}</b><div>${pct(x.target_pct)}</div></div>
          <div class="tswCard"><div>Stop-loss</div><b>${money(x.stop_loss_price)}</b><div class="tswBad">-${x.stop_loss_pct}%</div></div>
          <div class="tswCard"><div>Risk</div><b>${x.risk}</b><div>Score: ${x.score}</div></div>
        </div>
      </div>
      <div class="tswCard">
        <b>💰 Investment Simulator</b>
        <input class="tswInput" id="tswAmount" inputmode="numeric" placeholder="Amount to invest, ex: 100000000">
        <button class="tswBtn2" id="tswSim">Calculate Predicted Return</button>
        <div id="tswSimOut"></div>
      </div>
    `;
    document.getElementById('tswSim').onclick = () => simulate(x.acronym);
  }

  async function simulate(stock) {
    const amount = Number((document.getElementById('tswAmount') || {}).value || 0);
    const out = document.getElementById('tswSimOut');
    if (!amount || amount <= 0) {
      out.innerHTML = `<p class="tswBad">Enter an amount first.</p>`;
      return;
    }
    out.innerHTML = `<p class="tswMuted">Calculating...</p>`;
    const res = await api(`/api/simulate?amount=${encodeURIComponent(amount)}&stock=${encodeURIComponent(stock || '')}`);
    if (!res.ok) {
      out.innerHTML = `<p class="tswBad">Error: ${res.error}</p>`;
      return;
    }
    out.innerHTML = `
      <div class="tswCard">
        <div class="tswRow"><span>Shares</span><b>${res.shares.toLocaleString()}</b></div>
        <div class="tswRow"><span>Estimated spent</span><b>${money(res.estimated_spent)}</b></div>
        <div class="tswRow"><span>Predicted total</span><b>${money(res.predicted_total)}</b></div>
        <div class="tswRow"><span>Predicted profit</span><b class="${res.predicted_profit >= 0 ? 'tswGood' : 'tswBad'}">${money(res.predicted_profit)}</b></div>
        <div class="tswRow"><span>Predicted ROI</span><b>${pct(res.predicted_roi_pct)}</b></div>
        <div class="tswRow"><span>If wrong / stop-loss</span><b class="tswBad">${money(res.possible_loss)}</b></div>
        <p class="tswMuted">${res.warning}</p>
      </div>
    `;
  }

  window.tswSimStock = async function(stock) {
    await renderTab('pick');
    setTimeout(() => {
      const amt = document.getElementById('tswAmount');
      if (amt) amt.focus();
    }, 100);
  };

  function mountButton() {
    addStyle();
    if (document.getElementById('tswBtn')) return;
    const b = document.createElement('button');
    b.id = 'tswBtn';
    b.textContent = '📈';
    b.title = 'Torn Stock Watcher';
    b.onclick = showPanel;
    document.body.appendChild(b);
  }

  mountButton();
  setInterval(mountButton, 3000);
})();
