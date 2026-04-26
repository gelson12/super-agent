// sprite_editor.js — Modal that lets the user upload PNGs for a bot's
// 12 sprite slots (4 stand poses + 8 walk frames). On submit, posts a
// multipart form to the super-agent backend, which saves the files to
// disk and commits them to GitHub for durability.

const SLOTS = [
  { id: 'stand_down',   label: '↓ down',  section: 'stand', fileName: 'stand_down.png' },
  { id: 'stand_left',   label: '← left',  section: 'stand', fileName: 'stand_left.png' },
  { id: 'stand_right',  label: '→ right', section: 'stand', fileName: 'stand_right.png' },
  { id: 'stand_up',     label: '↑ up',    section: 'stand', fileName: 'stand_up.png' },

  { id: 'walk_down_1',  label: 'down 1',  section: 'walk-down', fileName: 'walk_down.png' },
  { id: 'walk_down_2',  label: 'down 2',  section: 'walk-down', fileName: 'walk_down_2.png' },

  { id: 'walk_left_1',  label: 'left 1',  section: 'walk-left', fileName: 'walk_left.png' },
  { id: 'walk_left_2',  label: 'left 2',  section: 'walk-left', fileName: 'walk_left_2.png' },

  { id: 'walk_right_1', label: 'right 1', section: 'walk-right', fileName: 'walk_right.png' },
  { id: 'walk_right_2', label: 'right 2', section: 'walk-right', fileName: 'walk_right_2.png' },

  { id: 'walk_up_1',    label: 'up 1',    section: 'walk-up', fileName: 'walk_up.png' },
  { id: 'walk_up_2',    label: 'up 2',    section: 'walk-up', fileName: 'walk_up_2.png' },
];

// Folder aliases the backend / sprites loader accepts (mirrors sprites.js
// FOLDER_ALIASES — kept short here; the backend probes the same set).
const FOLDER_PROBE = {
  ceo:            ['ceo', 'ceo_alpha'],
  cto:            ['cto', 'cto_alpha'],
  coo:            ['coo', 'coo_alpha'],
  chief_of_staff: ['chief_of_staff', 'chief_of_staff_alpha'],
  cso:            ['cso', 'cso_alpha', 'chief_security_officer'],
  researcher:     ['researcher', 'researcher_alpha'],
  pm:             ['pm', 'pm_alpha', 'project_manager'],
  marketing:      ['marketing', 'marketing_alpha'],
  finance:        ['finance', 'finance_alpha', 'accountant'],
  website:        ['website', 'website_alpha', 'website_designer'],
  cleaner:        ['cleaner', 'cleaner_alpha'],
  crypto:         ['crypto', 'crypto_alpha'],
  scholar:        ['scholar', 'scholar_alpha'],
  nova:           ['nova', 'nova_alpha'],
  writer:         ['writer', 'writer_alpha'],
};

let _modalEl, _formEl, _statusEl, _saveBtn;
let _currentBot = null;
let _currentSprites = null;
let _currentHud = null;

export function openSpriteEditor(bot, sprites, hud) {
  _currentBot = bot;
  _currentSprites = sprites;
  _currentHud = hud;
  _ensureBound();

  document.getElementById('sprite-modal-bot').textContent = bot.name;
  _statusEl.textContent = '';
  _statusEl.className = 'modal-status';
  _saveBtn.disabled = false;
  _formEl.reset();

  // Build the slot grid. Try each candidate folder URL until one yields
  // an existing sprite for the slot — used as the "current" thumbnail.
  for (const section of ['stand', 'walk-down', 'walk-left', 'walk-right', 'walk-up']) {
    const grid = document.getElementById(`slot-grid-${section}`);
    grid.innerHTML = '';
    for (const slot of SLOTS.filter(s => s.section === section)) {
      grid.appendChild(_buildSlotCell(slot, bot.id));
    }
  }

  _modalEl.hidden = false;
  document.body.style.overflow = 'hidden';
}

function _buildSlotCell(slot, botId) {
  const cell = document.createElement('label');
  cell.className = 'slot-cell';
  cell.innerHTML = `
    <span class="slot-label">${slot.label}</span>
    <span class="slot-thumb empty" data-slot-thumb></span>
    <input class="slot-input" type="file" accept="image/png" name="${slot.id}" data-slot-input>
  `;
  // Try to populate thumbnail from any existing live file
  const thumb = cell.querySelector('[data-slot-thumb]');
  const folders = FOLDER_PROBE[botId] || [botId];
  const candidatePaths = folders.flatMap(f => [
    `assets/sprites/bots/${f}/${slot.fileName}?v=${Date.now()}`,
  ]);
  _tryLoadFirst(candidatePaths).then(url => {
    if (url) {
      thumb.classList.remove('empty');
      thumb.innerHTML = `<img src="${url}" alt="">`;
    }
  });
  // Local-preview when the user picks a new file
  cell.querySelector('[data-slot-input]').addEventListener('change', (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const url = URL.createObjectURL(file);
    thumb.classList.remove('empty');
    thumb.innerHTML = `<img src="${url}" alt="">`;
  });
  return cell;
}

function _tryLoadFirst(urls) {
  return new Promise((resolve) => {
    if (!urls.length) return resolve(null);
    let i = 0;
    const tryNext = () => {
      if (i >= urls.length) return resolve(null);
      const img = new Image();
      img.onload = () => resolve(urls[i]);
      img.onerror = () => { i++; tryNext(); };
      img.src = urls[i];
    };
    tryNext();
  });
}

function _ensureBound() {
  if (_modalEl) return;
  _modalEl = document.getElementById('sprite-modal');
  _formEl = document.getElementById('sprite-modal-form');
  _statusEl = document.getElementById('sprite-modal-status');
  _saveBtn = document.getElementById('sprite-modal-save');

  document.getElementById('sprite-modal-close').addEventListener('click', closeSpriteEditor);
  document.getElementById('sprite-modal-cancel').addEventListener('click', closeSpriteEditor);
  _modalEl.addEventListener('click', (e) => {
    if (e.target === _modalEl) closeSpriteEditor();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !_modalEl.hidden) closeSpriteEditor();
  });
  _formEl.addEventListener('submit', _onSubmit);
}

export function closeSpriteEditor() {
  if (!_modalEl) return;
  _modalEl.hidden = true;
  document.body.style.overflow = '';
  _currentBot = null;
}

async function _onSubmit(e) {
  e.preventDefault();
  if (!_currentBot) return;

  // Gather files keyed by slot id (server normalises to filenames).
  const fd = new FormData();
  let count = 0;
  for (const slot of SLOTS) {
    const input = _formEl.querySelector(`input[name="${slot.id}"]`);
    if (input?.files?.[0]) {
      fd.append(slot.id, input.files[0]);
      count++;
    }
  }
  if (count === 0) {
    _statusEl.className = 'modal-status error';
    _statusEl.textContent = 'No files picked';
    return;
  }

  _saveBtn.disabled = true;
  _statusEl.className = 'modal-status';
  _statusEl.textContent = `Uploading ${count} file${count===1?'':'s'}…`;

  try {
    const r = await fetch(`/api/office_sim/sprites/${encodeURIComponent(_currentBot.id)}`, {
      method: 'POST',
      body: fd,
      // Same-origin → cookies/headers from the deploy; the FastAPI
      // route checks X-Token via the existing middleware so we add it
      // here from a meta tag if available.
      headers: _authHeaders(),
    });
    if (!r.ok) {
      const text = await r.text();
      throw new Error(`HTTP ${r.status}: ${text.slice(0, 200)}`);
    }
    const json = await r.json().catch(() => ({}));
    _statusEl.className = 'modal-status success';
    _statusEl.textContent = `Saved ${count} file${count===1?'':'s'}${json.committed ? ' (committed to GitHub)' : ''}. Reloading sprites…`;
    // Hot-reload the bot's sprites in-place
    await _currentSprites.reloadBot(_currentBot.id);
    if (_currentHud?.pushActivity) {
      _currentHud.pushActivity(`✦ ${_currentBot.name} sprites updated (${count} file${count===1?'':'s'})`);
    }
    setTimeout(closeSpriteEditor, 600);
  } catch (err) {
    _statusEl.className = 'modal-status error';
    _statusEl.textContent = `Upload failed: ${err.message}`;
    _saveBtn.disabled = false;
  }
}

function _authHeaders() {
  const headers = {};
  // Look for a meta tag the page can set with the X-Token; otherwise
  // fall back to a localStorage entry the user can paste in via console.
  const meta = document.querySelector('meta[name="super-agent-token"]');
  const token = meta?.content || localStorage.getItem('super_agent_token') || '';
  if (token) headers['X-Token'] = token;
  return headers;
}
