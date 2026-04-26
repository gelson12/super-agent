// main.js — Entry point. Loads world + bots + sprites, wires UI, runs the loop.

import { loadFloors } from './world.js';
import { loadBots } from './agents.js';
import { Scheduler, loadSchedule } from './meetings.js';
import { SpriteCache } from './sprites.js';
import { Renderer, HUD } from './renderer.js';
import { LiveFeed } from './live.js';
import { openSpriteEditor } from './sprite_editor.js';

const SIM_SPEED = 60;     // 60x real time so a meeting fires every minute or so

(async function boot() {
  const canvas = document.getElementById('world');
  const loadingHint = document.getElementById('loading-hint');

  loadingHint.textContent = 'Loading floors…';
  const floors = await loadFloors();

  loadingHint.textContent = 'Loading bots & schedule…';
  const bots = await loadBots();
  const schedule = await loadSchedule();

  loadingHint.textContent = 'Loading sprites…';
  const sprites = await new SpriteCache().load(bots);

  const scheduler = new Scheduler(floors, bots, schedule);
  scheduler.setSpeed(SIM_SPEED);

  const renderer = new Renderer(canvas, floors, sprites, scheduler, bots);
  await renderer.init();
  renderer.setActiveFloor(2);

  const hud = new HUD(bots, scheduler, renderer);
  hud.onSpriteEdit = (bot) => openSpriteEditor(bot, sprites, hud);
  const live = new LiveFeed(bots, hud);

  // Wire UI: floor pills (manual change disables camera follow).
  document.querySelectorAll('.floor-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      renderer.setActiveFloor(+btn.dataset.floor);
      renderer.followFocused = false;
    });
  });

  // Wire UI: mode toggle.
  document.querySelectorAll('.mode-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      live.setMode(btn.dataset.mode);
    });
  });
  live.onConnChange = (ok) => {
    document.getElementById('status-conn').textContent = `conn: ${ok ? 'live' : 'offline'}`;
  };
  live.start();

  // Click on canvas → focus nearest bot on the active floor.
  canvas.addEventListener('click', (e) => {
    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left, py = e.clientY - rect.top;
    const tile = renderer.pxToTile(px, py);
    let best = null, bestDist = Infinity;
    for (const bot of bots) {
      if (bot.floor !== renderer.activeFloor) continue;
      const d = Math.hypot(bot.x - tile.x, bot.y - tile.y);
      if (d < bestDist) { bestDist = d; best = bot; }
    }
    if (best && bestDist < 4) {
      renderer.setFocusedBot(best.id);
      renderer.followFocused = true;
      document.querySelectorAll('.bot-row').forEach(el => {
        el.classList.toggle('focused', el.dataset.botId === best.id);
      });
    }
  });

  // HUD collapse toggle (button + 'h' shortcut). Collapsing the right
  // panel widens the canvas so the office floor reads bigger.
  const stageEl = document.getElementById('stage');
  function setHudCollapsed(collapsed) {
    stageEl.classList.toggle('hud-collapsed', collapsed);
    document.body.classList.toggle('hud-collapsed', collapsed);
    // Trigger a canvas resize so the renderer rebuilds its DPR-scaled buffer.
    setTimeout(() => window.dispatchEvent(new Event('resize')), 320);
  }
  document.getElementById('hud-toggle').addEventListener('click', () => {
    setHudCollapsed(!stageEl.classList.contains('hud-collapsed'));
  });

  // Keyboard shortcuts:
  //   g  toggle tile-grid debug overlay
  //   1/2/3  switch floor
  //   +/-  adjust sim speed (10× → 600×)
  //   h  toggle HUD panel
  //   esc  clear focus / camera follow
  const SPEED_TABLE = [10, 30, 60, 120, 300, 600];
  let speedIdx = SPEED_TABLE.indexOf(SIM_SPEED);
  if (speedIdx < 0) speedIdx = 2;
  function changeSpeed(delta) {
    speedIdx = Math.max(0, Math.min(SPEED_TABLE.length - 1, speedIdx + delta));
    scheduler.setSpeed(SPEED_TABLE[speedIdx]);
    hud.pushActivity(`sim speed: ${SPEED_TABLE[speedIdx]}× real time`);
  }
  window.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.key === 'g' || e.key === 'G') renderer.toggleDebugGrid();
    else if (e.key === '1' || e.key === '2' || e.key === '3') {
      renderer.setActiveFloor(+e.key);
      renderer.followFocused = false;
    } else if (e.key === '+' || e.key === '=') changeSpeed(+1);
    else if (e.key === '-' || e.key === '_') changeSpeed(-1);
    else if (e.key === 'h' || e.key === 'H') {
      setHudCollapsed(!stageEl.classList.contains('hud-collapsed'));
    } else if (e.key === 'Escape') {
      renderer.setFocusedBot(null);
      renderer.followFocused = false;
      document.querySelectorAll('.bot-row').forEach(el => el.classList.remove('focused'));
    }
  });

  // Speech bubbles + activity log entries when meetings start/end.
  scheduler.on('meetingStart', (m) => {
    for (const bot of m.participants) {
      renderer.flashBubble(bot.id, m.ev.label || m.ev.id, 4500);
    }
    hud.pushActivity(`📅 ${m.ev.label || m.ev.id} → ${m.participants.map(b => b.name).join(', ')}`);
  });
  scheduler.on('meetingEnd', (m) => {
    hud.pushActivity(`✓ ${m.ev.label || m.ev.id} ended`);
  });

  // Initial dispersion so the office has motion the second the page loads.
  scheduler.disperseInitial();

  loadingHint.classList.add('hidden');

  let lastFrame = performance.now();
  let fps = 60, fpsAccum = 0, fpsTicks = 0;
  function frame(now) {
    const dt = now - lastFrame;
    lastFrame = now;
    fpsAccum += dt; fpsTicks++;
    if (fpsAccum > 500) {
      fps = Math.round(1000 * fpsTicks / fpsAccum);
      fpsAccum = 0; fpsTicks = 0;
    }

    scheduler.tick(now);
    for (const bot of bots) bot.update(now);
    renderer.draw(now);
    hud.updateBotList();
    hud.updateClock(scheduler.now());
    if (now % 1500 < 16) hud.updateActivity();
    if (now % 5000 < 16) hud.updateSchedule();
    hud.updateStatus(live.mode, live.connected ? 'live' : 'offline', fps, bots.length);

    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  // Convenience: expose for console debugging.
  window.OFFICE = { floors, bots, scheduler, renderer, hud, live };
})().catch(err => {
  console.error('[main] boot failed:', err);
  document.getElementById('loading-hint').textContent = 'Boot error — see console.';
});
