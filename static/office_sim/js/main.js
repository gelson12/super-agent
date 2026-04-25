// main.js — Entry point. Loads world + bots + sprites, wires UI, runs the loop.

import { loadFloors } from './world.js';
import { loadBots } from './agents.js';
import { Scheduler, loadSchedule } from './meetings.js';
import { SpriteCache } from './sprites.js';
import { Renderer, HUD } from './renderer.js';
import { LiveFeed } from './live.js';

const SIM_SPEED = 60;     // 60x real time so a meeting fires every minute or so

(async function boot() {
  const canvas = document.getElementById('world');
  const loadingHint = document.getElementById('loading-hint');

  loadingHint.textContent = 'Loading floors…';
  const floors = await loadFloors();

  loadingHint.textContent = 'Loading sprites…';
  const sprites = await new SpriteCache().load();

  loadingHint.textContent = 'Loading bots & schedule…';
  const bots = await loadBots();
  const schedule = await loadSchedule();

  const scheduler = new Scheduler(floors, bots, schedule);
  scheduler.setSpeed(SIM_SPEED);

  const renderer = new Renderer(canvas, floors, sprites, scheduler, bots);
  await renderer.init();
  renderer.setActiveFloor(2);

  const hud = new HUD(bots, scheduler, renderer);
  const live = new LiveFeed(bots, hud);

  // Wire UI: floor pills.
  document.querySelectorAll('.floor-pill').forEach(btn => {
    btn.addEventListener('click', () => renderer.setActiveFloor(+btn.dataset.floor));
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
      document.querySelectorAll('.bot-row').forEach(el => {
        el.classList.toggle('focused', el.dataset.botId === best.id);
      });
    }
  });

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
