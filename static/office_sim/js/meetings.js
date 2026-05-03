// meetings.js — Daily-cadence scheduler + per-bot idle picker.
//
// Two responsibilities:
//   1. Drive scheduled meetings (daily/weekly cadences) — pre-gather 5 min
//      before the start time, walk participants to a chosen room, hold them
//      InMeeting for the duration, then disperse.
//   2. Pick idle activities for bots not currently in a meeting (coffee,
//      ping-pong, lounge, etc.) so the office never feels static.

const PRE_GATHER_MS = 5 * 60_000;       // 5 minutes before scheduled start
const DEFAULT_MEETING_MS = 8 * 60_000;  // 8 minutes
const IDLE_PICK_MS = 30_000;            // re-roll idle target every 30s

export class Scheduler {
  constructor(floors, bots, schedule) {
    this.floors = floors;
    this.bots = bots;
    this.schedule = schedule;
    this.activeMeetings = [];
    this.lastFiredKey = new Set();
    this.reservedRooms = new Map();
    this.lastIdleRoll = new Map();
    this.simSpeed = 1;
    // Default sim-time anchor: jump to 07:25 today UTC so the very first
    // scheduled cadence (07:30 Researcher) is visible within seconds of
    // page load. Override via setSimClock() if needed.
    this.simAnchor = (() => {
      const d = new Date();
      d.setUTCHours(7, 25, 0, 0);
      return d.getTime();
    })();
    this.simStart = performance.now();
    // Listeners (renderer/HUD subscribe for transient effects).
    this._listeners = { meetingStart: [], meetingEnd: [] };
  }

  on(eventName, fn) { (this._listeners[eventName] ||= []).push(fn); }
  _emit(eventName, payload) { for (const fn of (this._listeners[eventName] || [])) try { fn(payload); } catch {} }

  // Override the sim-time anchor (e.g., from a console call OFFICE.scheduler.setSimClock("13:30")).
  setSimClock(hhmm) {
    const [hh, mm] = String(hhmm).split(':').map(Number);
    const d = new Date();
    d.setUTCHours(hh || 0, mm || 0, 0, 0);
    this.simAnchor = d.getTime();
    this.simStart = performance.now();
    this.lastFiredKey.clear();
  }

  // Sim time (ms since epoch) — accelerated when simSpeed > 1.
  now() {
    const elapsed = performance.now() - this.simStart;
    return this.simAnchor + elapsed * this.simSpeed;
  }

  setSpeed(mult) {
    // Snap anchor so sim time stays continuous.
    this.simAnchor = this.now();
    this.simStart = performance.now();
    this.simSpeed = mult;
  }

  // Called every frame.
  tick(realNow) {
    const simTime = this.now();
    this._fireDailyTriggers(simTime);
    this._tickActiveMeetings(realNow, simTime);
    this._maintainIdle(realNow);
    // Periodic cleanup so lastFiredKey doesn't grow unbounded across days.
    if (this.lastFiredKey.size > 200) this._purgeOldKeys(simTime);
  }

  _purgeOldKeys(simTime) {
    const today = new Date(simTime).toISOString().slice(0, 10);
    for (const k of this.lastFiredKey) {
      if (!k.startsWith(today)) this.lastFiredKey.delete(k);
    }
  }

  _fireDailyTriggers(simTime) {
    const today = new Date(simTime);
    const yyyymmdd = today.toISOString().slice(0,10);
    for (const ev of this.schedule.daily || []) {
      const [hh, mm] = ev.time.split(':').map(Number);
      const startMs = new Date(today.getFullYear(), today.getMonth(), today.getDate(), hh, mm).getTime();
      const gatherAt = startMs - PRE_GATHER_MS;
      const k = `${yyyymmdd}-${ev.id}`;
      if (simTime >= gatherAt && simTime < startMs + (ev.durationMs ?? DEFAULT_MEETING_MS)
          && !this.lastFiredKey.has(k)) {
        this.lastFiredKey.add(k);
        this._startMeeting(ev, startMs);
      }
    }
  }

  _startMeeting(ev, startMs) {
    const participants = (ev.participants || []).map(id => this.bots.find(b => b.id === id)).filter(Boolean);
    if (!participants.length) return;
    // Pick a room.
    const room = this._pickRoom(participants.length, ev.preferredRoomType);
    if (!room) {
      console.warn(`[meetings] no available room for ${ev.id}; skipping`);
      return;
    }
    const meetingId = `${ev.id}-${startMs}`;
    this.reservedRooms.set(room.zoneId, meetingId);
    const anchors = this._anchorsForZone(room);
    const m = {
      id: meetingId,
      ev,
      startMs,
      endMs: startMs + (ev.durationMs ?? DEFAULT_MEETING_MS),
      room,
      participants,
      anchorAssignment: new Map(),
      arrived: new Set(),
    };
    participants.forEach((bot, i) => {
      const anchor = anchors[i % anchors.length];
      m.anchorAssignment.set(bot.id, anchor);
      bot.goTo(this.floors, room.floor, anchor.x, anchor.y, { label: ev.label || ev.id });
    });
    this.activeMeetings.push(m);
    this._emit('meetingStart', m);
  }

  _tickActiveMeetings(realNow, simTime) {
    for (const m of this.activeMeetings) {
      // Mark arrivals.
      for (const bot of m.participants) {
        if (m.arrived.has(bot.id)) continue;
        const a = m.anchorAssignment.get(bot.id);
        if (bot.floor === m.room.floor && Math.round(bot.x) === a.x && Math.round(bot.y) === a.y && bot.state !== 'walking') {
          m.arrived.add(bot.id);
          bot.state = 'inMeeting';
          // Face the table center.
          const cx = m.room.bounds[0] + Math.floor(m.room.bounds[2]/2);
          const cy = m.room.bounds[1] + Math.floor(m.room.bounds[3]/2);
          if (Math.abs(cx - a.x) >= Math.abs(cy - a.y)) bot.facing = cx >= a.x ? 'right' : 'left';
          else bot.facing = cy >= a.y ? 'down' : 'up';
        }
      }
      // End meeting.
      if (simTime >= m.endMs && !m._endedEmitted) {
        for (const bot of m.participants) {
          bot.state = 'idle';
          this._sendBotHome(bot);
        }
        this.reservedRooms.delete(m.room.zoneId);
        m._endedEmitted = true;
        this._emit('meetingEnd', m);
      }
    }
    this.activeMeetings = this.activeMeetings.filter(m => simTime < m.endMs);
  }

  _maintainIdle(realNow) {
    for (const bot of this.bots) {
      if (bot.state === 'walking' || bot.state === 'transit' || bot.state === 'inMeeting') continue;
      const last = this.lastIdleRoll.get(bot.id) ?? 0;
      if (realNow - last < IDLE_PICK_MS) continue;
      this.lastIdleRoll.set(bot.id, realNow);
      // 60% stay (work at desk), 40% wander to an affinity zone on their home floor.
      if (Math.random() < 0.6) continue;
      const zoneType = bot.affinity[Math.floor(Math.random() * bot.affinity.length)];
      const dst = this._pickRandomAnchorOfType(zoneType, bot.deskFloor);
      if (dst) bot.goTo(this.floors, dst.floor, dst.x, dst.y, { label: zoneType });
    }
  }

  // Run once after boot so the office has motion immediately.
  // 70% of bots head to an affinity zone, staggered so paths don't collide.
  disperseInitial() {
    const now = performance.now();
    let stagger = 0;
    for (const bot of this.bots) {
      this.lastIdleRoll.set(bot.id, now + stagger);   // forward-dated so the
      stagger += 800;                                  // next real roll is ~30s later
      if (Math.random() > 0.7) continue;
      const zoneType = bot.affinity[Math.floor(Math.random() * bot.affinity.length)];
      const dst = this._pickRandomAnchorOfType(zoneType, bot.deskFloor);
      if (dst) bot.goTo(this.floors, dst.floor, dst.x, dst.y, { label: zoneType });
    }
  }

  _pickRoom(groupSize, preferred) {
    const want = (groupSize >= 4) ? ['conference_room','meeting_room']
                : (groupSize === 3) ? ['meeting_room','open_collab','lounge']
                : ['meeting_room','lounge','coffee','open_collab'];
    const order = preferred ? [preferred, ...want.filter(x => x !== preferred)] : want;
    for (const t of order) {
      for (const fId of [1,2,3]) {
        for (const z of this.floors[fId].zonesByType(t)) {
          const zid = `${fId}:${z.id}`;
          if (this.reservedRooms.has(zid)) continue;
          const anchors = this._anchorsForZone({ ...z, zoneId: zid, floor: fId });
          if (anchors.length < groupSize) continue;
          return { ...z, zoneId: zid, floor: fId };
        }
      }
    }
    return null;
  }

  _anchorsForZone(zone) {
    return this.floors[zone.floor].zoneAnchors(zone);
  }

  // homeFloor: restrict to that floor only (idle wandering stays on home floor).
  // If no zone of that type exists on homeFloor, returns null rather than crossing floors.
  _pickRandomAnchorOfType(type, homeFloor = null) {
    const floors = homeFloor ? [homeFloor] : [1, 2, 3];
    const candidates = [];
    for (const fId of floors) {
      for (const z of this.floors[fId].zonesByType(type)) {
        const anchors = this.floors[fId].zoneAnchors(z);
        for (const a of anchors) candidates.push({ floor: fId, x: a.x, y: a.y });
      }
    }
    if (!candidates.length) return null;
    return candidates[Math.floor(Math.random() * candidates.length)];
  }

  _sendBotHome(bot) {
    bot.goTo(this.floors, bot.deskFloor, bot.deskTile[0], bot.deskTile[1], { label: 'returning to desk' });
  }

  // Surface upcoming meetings for the HUD.
  upcoming(maxItems = 5) {
    const sim = this.now();
    const out = [];
    for (const ev of this.schedule.daily || []) {
      const [hh, mm] = ev.time.split(':').map(Number);
      const today = new Date(sim);
      const startMs = new Date(today.getFullYear(), today.getMonth(), today.getDate(), hh, mm).getTime();
      const ms = startMs - sim;
      if (ms < -DEFAULT_MEETING_MS) continue;     // already past
      out.push({ ev, startMs, ms });
    }
    out.sort((a,b) => a.ms - b.ms);
    return out.slice(0, maxItems);
  }
}

export async function loadSchedule() {
  try {
    const r = await fetch('data/schedule.json');
    return await r.json();
  } catch (e) {
    console.error('[meetings] failed to load schedule:', e);
    return { daily: [], weekly: [] };
  }
}
