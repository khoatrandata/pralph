from __future__ import annotations

import json
import threading
import webbrowser
from functools import partial
from http import HTTPStatus
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from urllib.parse import unquote

from pralph.models import StoryStatus
from pralph.state import StateManager

VIEWER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Planned Ralph — Story Viewer</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --purple: #bc8cff; --cyan: #39d2c0;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5; }

  header { background: var(--surface); border-bottom: 1px solid var(--border);
    padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 18px; font-weight: 600; }
  header .stats { color: var(--muted); font-size: 13px; margin-left: auto; }

  .layout { display: flex; height: calc(100vh - 49px); }
  .sidebar { width: 380px; min-width: 300px; border-right: 1px solid var(--border);
    display: flex; flex-direction: column; background: var(--surface); }
  .filters { padding: 12px; border-bottom: 1px solid var(--border);
    display: flex; flex-wrap: wrap; gap: 8px; }
  .filters input, .filters select { background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px;
    font-size: 13px; outline: none; }
  .filters input:focus, .filters select:focus { border-color: var(--accent); }
  .filters input { flex: 1; min-width: 120px; }

  .story-list { flex: 1; overflow-y: auto; }
  .story-item { padding: 10px 14px; border-bottom: 1px solid var(--border);
    cursor: pointer; transition: background 0.1s; }
  .story-item:hover { background: rgba(88,166,255,0.06); }
  .story-item.active { background: rgba(88,166,255,0.12);
    border-left: 3px solid var(--accent); padding-left: 11px; }
  .story-item .sid { font-size: 12px; font-weight: 600; color: var(--accent); }
  .story-item .stitle { font-size: 14px; margin-top: 2px; }
  .story-item .smeta { font-size: 11px; color: var(--muted); margin-top: 4px;
    display: flex; gap: 10px; flex-wrap: wrap; }

  .detail { flex: 1; overflow-y: auto; padding: 24px 32px; }
  .detail.empty { display: flex; align-items: center; justify-content: center;
    color: var(--muted); font-size: 15px; }

  .detail h2 { font-size: 22px; margin-bottom: 4px; }
  .detail .meta-row { display: flex; gap: 10px; flex-wrap: wrap;
    margin: 8px 0 20px; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-size: 12px; font-weight: 600; }
  .badge-status { background: var(--border); }
  .badge-status[data-v="pending"] { background: #1f2d40; color: var(--accent); }
  .badge-status[data-v="in_progress"] { background: #2d2300; color: var(--yellow); }
  .badge-status[data-v="implemented"] { background: #0d2818; color: var(--green); }
  .badge-status[data-v="skipped"],
  .badge-status[data-v="duplicate"] { background: #1c1c1c; color: var(--muted); }
  .badge-status[data-v="error"] { background: #2d0f0f; color: var(--red); }
  .badge-cat { background: #1f1633; color: var(--purple); }
  .badge-pri { background: #1a2332; color: var(--cyan); }
  .badge-cx { background: var(--border); color: var(--muted); }

  .section { margin-bottom: 20px; }
  .section h3 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px;
    color: var(--muted); margin-bottom: 8px; }
  .section .content { background: var(--bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px 16px; font-size: 14px; white-space: pre-wrap;
    word-break: break-word; }
  .section ul { list-style: none; }
  .section ul li { padding: 4px 0; font-size: 14px; }
  .section ul li::before { content: "✓ "; color: var(--green); font-weight: bold; }
  .dep-link { color: var(--accent); cursor: pointer; text-decoration: underline; }

  .edit-btn { background: var(--accent); color: #fff; border: none; border-radius: 6px;
    padding: 6px 16px; font-size: 13px; font-weight: 600; cursor: pointer; font-family: inherit; }
  .edit-btn:hover { opacity: 0.85; }

  .edit-form label { display: block; font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.5px; color: var(--muted); margin-bottom: 4px; margin-top: 14px; }
  .edit-form label:first-child { margin-top: 0; }
  .edit-form input, .edit-form textarea, .edit-form select {
    width: 100%; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
    font-size: 14px; font-family: inherit; outline: none; }
  .edit-form input:focus, .edit-form textarea:focus, .edit-form select:focus {
    border-color: var(--accent); }
  .edit-form textarea { min-height: 100px; resize: vertical; }
  .edit-form .btn-row { display: flex; gap: 10px; margin-top: 20px; }
  .edit-form .btn-save { background: var(--green); color: #fff; border: none;
    border-radius: 6px; padding: 8px 20px; font-size: 14px; font-weight: 600;
    cursor: pointer; font-family: inherit; }
  .edit-form .btn-save:hover { opacity: 0.85; }
  .edit-form .btn-cancel { background: var(--border); color: var(--text); border: none;
    border-radius: 6px; padding: 8px 20px; font-size: 14px; font-weight: 600;
    cursor: pointer; font-family: inherit; }
  .edit-form .btn-cancel:hover { opacity: 0.85; }

  @media (max-width: 768px) {
    .layout { flex-direction: column; }
    .sidebar { width: 100%; height: 40vh; min-width: 0; }
    .detail { height: 60vh; }
  }

  /* Tab system */
  .tab-bar { display: flex; gap: 0; margin-left: 24px; }
  .tab-btn { background: none; border: none; color: var(--muted); font-size: 14px;
    font-weight: 600; padding: 6px 16px; cursor: pointer; border-bottom: 2px solid transparent;
    transition: color 0.15s, border-color 0.15s; font-family: inherit; }
  .tab-btn:hover { color: var(--text); }
  .tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }

  /* Progress bar */
  .progress-wrap { flex: 1; display: flex; align-items: center; justify-content: center; gap: 8px; }
  .progress-bar { width: 220px; height: 16px; border: 1.5px solid var(--border);
    border-radius: 8px; overflow: hidden; background: var(--bg); }
  .progress-fill { height: 100%; background: var(--accent); border-radius: 6px;
    transition: width 0.4s ease, background 0.4s ease; min-width: 0; }
  .progress-pct { font-size: 12px; font-weight: 600; color: var(--muted); white-space: nowrap; }

  /* Timeline — flat left-to-right Gantt by dependency depth */
  .timeline-container { height: calc(100vh - 49px); overflow: hidden; }
  .timeline-scroll { height: 100%; overflow: auto; position: relative; }
  .timeline-lines { position: absolute; top: 0; left: 0; pointer-events: none; z-index: 10; }
  .timeline-lines path { fill: none; stroke: var(--border); stroke-width: 1.5;
    transition: stroke 0.15s, stroke-width 0.15s; }
  .timeline-lines path.highlighted { stroke: var(--accent); stroke-width: 2.5; }

  .tl-gantt { display: flex; gap: 40px; padding: 24px; align-items: flex-start; }
  .tl-col { display: flex; flex-direction: column; gap: 14px; }

  .tl-card { width: 192px; background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px 12px; cursor: pointer; transition: border-color 0.15s,
    box-shadow 0.15s; }
  .tl-card:hover { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
  .tl-card.highlighted { border-color: var(--accent); box-shadow: 0 0 8px rgba(88,166,255,0.25); }
  .tl-card[data-status="pending"] { border-left: 3px solid var(--accent); }
  .tl-card[data-status="in_progress"] { border-left: 3px solid var(--yellow); }
  .tl-card[data-status="implemented"] { border-left: 3px solid var(--green); }
  .tl-card[data-status="skipped"],
  .tl-card[data-status="duplicate"] { border-left: 3px solid var(--muted); }
  .tl-card[data-status="error"] { border-left: 3px solid var(--red); }
  .tl-card .card-head { display: flex; justify-content: space-between; align-items: center; }
  .tl-card .card-id { font-size: 11px; font-weight: 600; color: var(--accent); }
  .tl-card .card-pri { font-size: 10px; font-weight: 700; padding: 1px 6px;
    border-radius: 4px; background: var(--bg); }
  .card-pri[data-pri="1"] { color: var(--red); }
  .card-pri[data-pri="2"] { color: var(--yellow); }
  .card-pri[data-pri="3"] { color: var(--accent); }
  .card-pri[data-pri="4"] { color: var(--cyan); }
  .card-pri[data-pri="5"] { color: var(--muted); }
  .tl-card .card-title { font-size: 13px; margin-top: 4px; display: -webkit-box;
    -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .tl-card .card-status { margin-top: 6px; }
</style>
</head>
<body>
<header>
  <h1>Planned Ralph</h1>
  <div class="tab-bar">
    <button class="tab-btn active" data-tab="stories" onclick="switchTab('stories')">Stories</button>
    <button class="tab-btn" data-tab="timeline" onclick="switchTab('timeline')">Timeline</button>
  </div>
  <div class="progress-wrap">
    <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
    <span class="progress-pct" id="progressPct">0%</span>
  </div>
  <span class="stats" id="stats"></span>
</header>
<div class="layout">
  <div class="sidebar">
    <div class="filters">
      <input type="text" id="search" placeholder="Search stories...">
      <select id="fStatus"><option value="">All statuses</option></select>
      <select id="fCategory"><option value="">All categories</option></select>
      <select id="fPriority"><option value="">All priorities</option></select>
    </div>
    <div class="story-list" id="list"></div>
  </div>
  <div class="detail empty" id="detail">Select a story to view details</div>
</div>
<div class="timeline-container" id="timelineContainer" style="display:none">
  <div class="timeline-scroll" id="timelineScroll">
    <svg class="timeline-lines" id="timelineLines"></svg>
  </div>
</div>
<script>
let stories = [], selected = null;

async function load() {
  const r = await fetch('/api/stories');
  stories = await r.json();
  populateFilters();
  render();
  updateStats();
}

function populateFilters() {
  const statuses = [...new Set(stories.map(s => s.status))].sort();
  const cats = [...new Set(stories.map(s => s.category).filter(Boolean))].sort();
  const pris = [...new Set(stories.map(s => s.priority))].sort((a,b) => a-b);

  const fS = document.getElementById('fStatus');
  statuses.forEach(v => { const o = document.createElement('option'); o.value = v; o.textContent = v; fS.appendChild(o); });
  const fC = document.getElementById('fCategory');
  cats.forEach(v => { const o = document.createElement('option'); o.value = v; o.textContent = v; fC.appendChild(o); });
  const fP = document.getElementById('fPriority');
  pris.forEach(v => { const o = document.createElement('option'); o.value = String(v); o.textContent = 'P' + v; fP.appendChild(o); });
}

function getFiltered() {
  const q = document.getElementById('search').value.toLowerCase();
  const fs = document.getElementById('fStatus').value;
  const fc = document.getElementById('fCategory').value;
  const fp = document.getElementById('fPriority').value;
  return stories.filter(s => {
    if (fs && s.status !== fs) return false;
    if (fc && s.category !== fc) return false;
    if (fp && String(s.priority) !== fp) return false;
    if (q && !s.id.toLowerCase().includes(q) && !s.title.toLowerCase().includes(q)
        && !(s.content||'').toLowerCase().includes(q)) return false;
    return true;
  });
}

function render() {
  const filtered = getFiltered();
  const list = document.getElementById('list');
  list.innerHTML = filtered.map(s => `
    <div class="story-item ${selected === s.id ? 'active' : ''}" data-id="${s.id}">
      <div class="sid">${esc(s.id)}</div>
      <div class="stitle">${esc(s.title)}</div>
      <div class="smeta">
        <span class="badge badge-status" data-v="${s.status}">${s.status}</span>
        ${s.category ? '<span>' + esc(s.category) + '</span>' : ''}
        <span>P${s.priority}</span>
        <span>${s.complexity}</span>
      </div>
    </div>
  `).join('');
  list.querySelectorAll('.story-item').forEach(el => {
    el.addEventListener('click', () => selectStory(el.dataset.id));
  });
}

function selectStory(id) {
  selected = id;
  render();
  const s = stories.find(x => x.id === id);
  if (!s) return;
  const d = document.getElementById('detail');
  d.classList.remove('empty');

  const deps = (s.dependencies || []).map(dep =>
    `<span class="dep-link" data-dep="${esc(dep)}">${esc(dep)}</span>`
  ).join(', ') || '<span style="color:var(--muted)">None</span>';

  const ac = (s.acceptance_criteria || []).map(c =>
    `<li>${esc(c)}</li>`
  ).join('') || '<li style="color:var(--muted)">None specified</li>';

  d.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between">
      <h2>${esc(s.title)}</h2>
      <button class="edit-btn" onclick="editStory('${s.id}')">Edit</button>
    </div>
    <div class="meta-row">
      <span class="badge badge-status" data-v="${s.status}">${s.status}</span>
      ${s.category ? `<span class="badge badge-cat">${esc(s.category)}</span>` : ''}
      <span class="badge badge-pri">P${s.priority}</span>
      <span class="badge badge-cx">${s.complexity}</span>
      <span class="badge" style="background:var(--border);color:var(--muted)">${esc(s.id)}</span>
    </div>
    <div class="section">
      <h3>Content</h3>
      <div class="content">${esc(s.content || '(empty)')}</div>
    </div>
    <div class="section">
      <h3>Acceptance Criteria</h3>
      <ul>${ac}</ul>
    </div>
    <div class="section">
      <h3>Dependencies</h3>
      <div>${deps}</div>
    </div>
    ${s.metadata && Object.keys(s.metadata).length ? `
    <div class="section">
      <h3>Metadata</h3>
      <div class="content">${esc(JSON.stringify(s.metadata, null, 2))}</div>
    </div>` : ''}
  `;
  d.querySelectorAll('.dep-link').forEach(el => {
    el.addEventListener('click', () => selectStory(el.dataset.dep));
  });
}

function updateStats() {
  const total = stories.length;
  const byStatus = {};
  stories.forEach(s => { byStatus[s.status] = (byStatus[s.status]||0) + 1; });
  const parts = Object.entries(byStatus).sort().map(([k,v]) => `${v} ${k}`);
  document.getElementById('stats').textContent = `${total} stories — ${parts.join(' · ')}`;
  updateProgress();
}

function updateProgress() {
  const total = stories.length;
  const done = stories.filter(s =>
    ['implemented','skipped','duplicate','external'].includes(s.status)
  ).length;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  const fill = document.getElementById('progressFill');
  fill.style.width = pct + '%';
  fill.style.background = pct >= 100 ? 'var(--green)' : 'var(--accent)';
  document.getElementById('progressPct').textContent = pct + '%';
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

document.getElementById('search').addEventListener('input', render);
document.getElementById('fStatus').addEventListener('change', render);
document.getElementById('fCategory').addEventListener('change', render);
document.getElementById('fPriority').addEventListener('change', render);

/* --- Timeline view (Gantt grid: rows=priority, cols=global dependency depth) --- */
let timelineBuilt = false;
let currentTab = 'stories';

function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
  document.querySelector('.layout').style.display = tab === 'stories' ? 'flex' : 'none';
  document.getElementById('timelineContainer').style.display = tab === 'timeline' ? 'block' : 'none';
  if (tab === 'timeline') renderTimeline();
}

function computeGlobalColumns() {
  // Global topological sort across ALL stories — every dependency arrow points right
  const storyMap = {};
  stories.forEach(s => { storyMap[s.id] = s; });
  const inDeps = {}, outDeps = {};
  stories.forEach(s => { inDeps[s.id] = []; outDeps[s.id] = []; });
  stories.forEach(s => {
    (s.dependencies || []).forEach(dep => {
      if (storyMap[dep]) { inDeps[s.id].push(dep); outDeps[dep].push(s.id); }
    });
  });
  const col = {}, resolved = new Set();
  const queue = stories.filter(s => inDeps[s.id].length === 0).map(s => s.id);
  queue.forEach(id => { col[id] = 0; });
  let safety = 0;
  while (queue.length > 0 && safety < stories.length * 3) {
    const id = queue.shift();
    resolved.add(id);
    outDeps[id].forEach(next => {
      if (!resolved.has(next)) {
        const dc = inDeps[next].filter(d => col[d] !== undefined).map(d => col[d]);
        if (dc.length === inDeps[next].length) {
          col[next] = Math.max(...dc) + 1;
          queue.push(next);
        }
      }
    });
    safety++;
  }
  const maxC = Object.values(col).length > 0 ? Math.max(...Object.values(col)) : -1;
  stories.forEach(s => { if (col[s.id] === undefined) col[s.id] = maxC + 1; });

  // Identify leaf cards (nothing depends on them)
  const hasSuccessor = new Set();
  stories.forEach(s => {
    (s.dependencies || []).forEach(dep => { if (storyMap[dep]) hasSuccessor.add(dep); });
  });

  // Per priority, find max column among non-leaf cards
  const priMaxCol = {};
  stories.forEach(s => {
    if (hasSuccessor.has(s.id)) {
      priMaxCol[s.priority] = Math.max(priMaxCol[s.priority] || 0, col[s.id]);
    }
  });

  // Push leaf cards to the last column of their priority
  stories.forEach(s => {
    if (!hasSuccessor.has(s.id) && priMaxCol[s.priority] !== undefined) {
      col[s.id] = Math.max(col[s.id], priMaxCol[s.priority] + 1);
    }
  });

  return col;
}

function buildTimeline() {
  const globalCols = computeGlobalColumns();
  const maxCol = Math.max(0, ...Object.values(globalCols));
  const scroll = document.getElementById('timelineScroll');

  // Group stories into columns
  const columns = {};
  for (let c = 0; c <= maxCol; c++) columns[c] = [];
  stories.forEach(s => { columns[globalCols[s.id]].push(s); });

  // Sort column 0 (roots / no-dep cards) by priority then ID
  columns[0].sort((a, b) => a.priority - b.priority || a.id.localeCompare(b.id));

  // For later columns, sort by avg vertical position of dependencies to reduce line crossings
  const yIndex = {};
  columns[0].forEach((s, i) => { yIndex[s.id] = i; });
  for (let c = 1; c <= maxCol; c++) {
    columns[c].forEach(s => {
      const depYs = (s.dependencies || [])
        .filter(d => yIndex[d] !== undefined)
        .map(d => yIndex[d]);
      s._sort = depYs.length > 0 ? depYs.reduce((a, b) => a + b, 0) / depYs.length : 0;
    });
    columns[c].sort((a, b) => a._sort - b._sort || a.priority - b.priority);
    columns[c].forEach((s, i) => { yIndex[s.id] = i; });
  }

  // Render flat columns left-to-right
  let html = '<div class="tl-gantt">';
  for (let c = 0; c <= maxCol; c++) {
    if (columns[c].length === 0) continue;
    html += '<div class="tl-col">';
    columns[c].forEach(s => {
      html += `<div class="tl-card" data-id="${esc(s.id)}" data-status="${s.status}"
        onmouseenter="highlightDeps('${esc(s.id)}')" onmouseleave="clearHighlights()">
        <div class="card-head">
          <span class="card-id">${esc(s.id)}</span>
          <span class="card-pri" data-pri="${s.priority}">P${s.priority}</span>
        </div>
        <div class="card-title">${esc(s.title)}</div>
        <div class="card-status">
          <span class="badge badge-status" data-v="${s.status}" style="font-size:11px;padding:1px 8px;">${s.status}</span>
        </div>
      </div>`;
    });
    html += '</div>';
  }
  html += '</div>';
  scroll.innerHTML = html + '<svg class="timeline-lines" id="timelineLines"></svg>';

  scroll.querySelectorAll('.tl-card').forEach(card => {
    card.addEventListener('click', () => { selectStory(card.dataset.id); switchTab('stories'); });
  });
}

function drawLines() {
  const svg = document.getElementById('timelineLines');
  if (!svg) return;
  const scroll = document.getElementById('timelineScroll');
  const scrollRect = scroll.getBoundingClientRect();
  const w = scroll.scrollWidth, h = scroll.scrollHeight;
  svg.setAttribute('width', w); svg.setAttribute('height', h);
  svg.style.width = w + 'px'; svg.style.height = h + 'px';

  let defs = `<defs>
    <marker id="ah" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6Z" fill="#30363d"/></marker>
    <marker id="ah-hl" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6Z" fill="#58a6ff"/></marker></defs>`;

  let paths = '';
  const cardEls = {};
  scroll.querySelectorAll('.tl-card').forEach(el => { cardEls[el.dataset.id] = el; });

  stories.forEach(s => {
    (s.dependencies || []).forEach(dep => {
      const from = cardEls[dep], to = cardEls[s.id];
      if (!from || !to) return;
      const fr = from.getBoundingClientRect(), tr = to.getBoundingClientRect();
      const x1 = fr.right - scrollRect.left + scroll.scrollLeft;
      const y1 = fr.top + fr.height/2 - scrollRect.top + scroll.scrollTop;
      const x2 = tr.left - scrollRect.left + scroll.scrollLeft;
      const y2 = tr.top + tr.height/2 - scrollRect.top + scroll.scrollTop;
      const cx = Math.max(24, Math.abs(x2 - x1) * 0.35);
      paths += `<path data-from="${esc(dep)}" data-to="${esc(s.id)}"
        d="M${x1},${y1} C${x1+cx},${y1} ${x2-cx},${y2} ${x2},${y2}"
        marker-end="url(#ah)"/>`;
    });
  });
  svg.innerHTML = defs + paths;
}

function highlightDeps(storyId) {
  const s = stories.find(x => x.id === storyId);
  if (!s) return;
  const deps = new Set(s.dependencies || []);
  const dependents = new Set();
  stories.forEach(x => { if ((x.dependencies||[]).includes(storyId)) dependents.add(x.id); });
  document.querySelectorAll('.tl-card').forEach(card => {
    if (deps.has(card.dataset.id) || dependents.has(card.dataset.id))
      card.classList.add('highlighted');
  });
  document.querySelectorAll('#timelineLines path').forEach(p => {
    if (p.dataset.from === storyId || p.dataset.to === storyId) {
      p.classList.add('highlighted');
      p.setAttribute('marker-end', 'url(#ah-hl)');
    }
  });
}

function clearHighlights() {
  document.querySelectorAll('.tl-card.highlighted').forEach(c => c.classList.remove('highlighted'));
  document.querySelectorAll('#timelineLines path.highlighted').forEach(p => {
    p.classList.remove('highlighted');
    p.setAttribute('marker-end', 'url(#ah)');
  });
}

function renderTimeline() {
  buildTimeline();
  requestAnimationFrame(() => { requestAnimationFrame(drawLines); });
  timelineBuilt = true;
}

window.addEventListener('resize', () => {
  if (currentTab === 'timeline' && timelineBuilt) renderTimeline();
});

const STORY_STATUSES = ['pending','in_progress','implemented','rework','skipped','duplicate','external','error'];

function editStory(id) {
  const s = stories.find(x => x.id === id);
  if (!s) return;
  const d = document.getElementById('detail');

  const statusOpts = STORY_STATUSES.map(v =>
    `<option value="${v}" ${s.status===v?'selected':''}>${v}</option>`
  ).join('');
  const priOpts = [1,2,3,4,5].map(v =>
    `<option value="${v}" ${s.priority===v?'selected':''}>${v}</option>`
  ).join('');
  const cxOpts = ['easy','medium','hard'].map(v =>
    `<option value="${v}" ${s.complexity===v?'selected':''}>${v}</option>`
  ).join('');

  d.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
      <h2>Editing: ${esc(s.id)}</h2>
    </div>
    <div class="edit-form">
      <label>Title</label>
      <input type="text" id="ef-title">
      <label>Status</label>
      <select id="ef-status">${statusOpts}</select>
      <label>Category</label>
      <input type="text" id="ef-category">
      <label>Priority</label>
      <select id="ef-priority">${priOpts}</select>
      <label>Complexity</label>
      <select id="ef-complexity">${cxOpts}</select>
      <label>Content</label>
      <textarea id="ef-content"></textarea>
      <label>Acceptance Criteria (one per line)</label>
      <textarea id="ef-ac"></textarea>
      <label>Dependencies (comma-separated IDs)</label>
      <input type="text" id="ef-deps">
      <div class="btn-row">
        <button class="btn-save" onclick="saveStory('${s.id}')">Save</button>
        <button class="btn-cancel" onclick="selectStory('${s.id}')">Cancel</button>
      </div>
    </div>
  `;
  document.getElementById('ef-title').value = s.title;
  document.getElementById('ef-category').value = s.category || '';
  document.getElementById('ef-content').value = s.content || '';
  document.getElementById('ef-ac').value = (s.acceptance_criteria || []).join('\n');
  document.getElementById('ef-deps').value = (s.dependencies || []).join(', ');
}

async function saveStory(id) {
  const data = {
    title: document.getElementById('ef-title').value,
    status: document.getElementById('ef-status').value,
    category: document.getElementById('ef-category').value,
    priority: parseInt(document.getElementById('ef-priority').value, 10),
    complexity: document.getElementById('ef-complexity').value,
    content: document.getElementById('ef-content').value,
    acceptance_criteria: document.getElementById('ef-ac').value.split('\n').filter(l => l.trim()),
    dependencies: document.getElementById('ef-deps').value.split(',').map(s => s.trim()).filter(Boolean),
  };
  const r = await fetch('/api/stories/' + encodeURIComponent(id), {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data),
  });
  if (r.ok) {
    const updated = await r.json();
    const idx = stories.findIndex(s => s.id === id);
    if (idx !== -1) stories[idx] = updated;
    render();
    updateStats();
    selectStory(id);
  } else {
    alert('Failed to save: ' + r.status);
  }
}

load();
</script>
</body>
</html>"""


class ViewerHandler(BaseHTTPRequestHandler):
    state: StateManager

    def do_GET(self):
        if self.path == '/api/stories':
            self._serve_stories()
        elif self.path == '/api/status':
            self._serve_status()
        else:
            self._serve_html()

    def _serve_html(self):
        body = VIEWER_HTML.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stories(self):
        stories = self.state.load_stories()
        data = [s.to_dict() for s in stories]
        body = json.dumps(data).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_status(self):
        entries = []
        if self.state.status_path.exists():
            for line in self.state.status_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        body = json.dumps(entries).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        if not self.path.startswith('/api/stories/'):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        story_id = unquote(self.path[len('/api/stories/'):])

        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return

        stories = self.state.load_stories()
        story = next((s for s in stories if s.id == story_id), None)
        if not story:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        editable = ('title', 'content', 'acceptance_criteria', 'priority',
                     'category', 'complexity', 'dependencies', 'status')
        for field in editable:
            if field in body:
                if field == 'status':
                    story.status = StoryStatus(body[field])
                else:
                    setattr(story, field, body[field])

        self.state._rewrite_stories(stories)

        resp = json.dumps(story.to_dict()).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, format, *args):
        # Suppress default request logging
        pass


def run_viewer(state: StateManager, *, port: int = 8411, open_browser: bool = True) -> None:
    """Start the viewer HTTP server."""
    handler = type('Handler', (ViewerHandler,), {'state': state})
    server = HTTPServer(('127.0.0.1', port), handler)
    url = f'http://127.0.0.1:{port}'

    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=[url]).start()

    print(f'pralph viewer running at {url}')
    print('Press Ctrl+C to stop')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopping viewer')
        server.shutdown()
