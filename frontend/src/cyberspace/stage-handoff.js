const ease = value => { const x = Math.max(0, Math.min(1, value)); return x * x * (3 - 2 * x); };
const SVG = 'http://www.w3.org/2000/svg';

// One frozen ASR frame and a short SVG trace; never a second WebGL loop.
export class StageHandoff {
  constructor(canvas, { high = false, variant = 'gravity', seen = false } = {}) {
    this.canvas = canvas; this.high = high; this.seen = seen;
    this.color = variant === 'gpt' ? '#b6a0ec' : variant.startsWith('dafeiyu') ? '#91dfff' : '#e8cb93';
    this.elapsed = 0; this.waited = 0; this.count = 0;
  }

  observe(snapshot, presentedPhase, capture) {
    if (snapshot.reduced || document.hidden || !['analyzing', 'paused'].includes(snapshot.phase)) this.finish();
    if (snapshot.phase !== 'analyzing' || this.seen) return;
    this.seen = true;
    if (snapshot.reduced || document.hidden || presentedPhase !== 'transcribing' || !this.canvas.parentElement) return;
    try {
      // Redraw the previous uniforms synchronously: the WebGL backbuffer is
      // not guaranteed to survive after its requestAnimationFrame callback.
      if (!capture()) return;
      const copy = document.createElement('canvas');
      const scale = Math.min(1, 1600 / Math.max(1, this.canvas.width));
      copy.width = Math.max(1, Math.round(this.canvas.width * scale));
      copy.height = Math.max(1, Math.round(this.canvas.height * scale));
      const context = copy.getContext('2d');
      if (!context) return;
      context.fillStyle = '#101112'; context.fillRect(0, 0, copy.width, copy.height);
      context.drawImage(this.canvas, 0, 0, copy.width, copy.height);
      const overlay = document.createElement('div');
      overlay.className = 'subtitle-stage-handoff'; overlay.setAttribute('aria-hidden', 'true');
      Object.assign(overlay.style, { position: 'absolute', inset: '0', pointerEvents: 'none', overflow: 'hidden' });
      const still = document.createElement('div');
      Object.assign(still.style, { position: 'absolute', inset: '0', backgroundImage: `url("${copy.toDataURL('image/png')}")`, backgroundSize: '100% 100%' });
      overlay.append(still);
      if (this.high) {
        const glow = document.createElement('div');
        Object.assign(glow.style, { position: 'absolute', inset: '0', opacity: '0',
          background: `radial-gradient(ellipse at 50% 78%, ${this.color}00 8%, ${this.color}38 29%, ${this.color}12 48%, transparent 68%)`,
          transformOrigin: '50% 78%' });
        overlay.append(glow); this.glow = glow;
        const svg = document.createElementNS(SVG, 'svg');
        svg.setAttribute('viewBox', '0 0 1000 1000'); svg.setAttribute('preserveAspectRatio', 'none');
        Object.assign(svg.style, { position: 'absolute', inset: '0', width: '100%', height: '100%' });
        const path = document.createElementNS(SVG, 'path');
        path.setAttribute('fill', 'none'); path.setAttribute('stroke', this.color);
        path.setAttribute('stroke-width', '2'); path.setAttribute('vector-effect', 'non-scaling-stroke');
        path.setAttribute('stroke-linecap', 'round'); svg.append(path); overlay.append(svg); this.trace = path;
        path.setAttribute('pathLength', '100'); path.setAttribute('stroke-dasharray', '28 72');
        path.style.filter = `drop-shadow(0 0 3px ${this.color})`;
      }
      this.canvas.after(overlay); this.overlay = overlay; this.still = still;
      this.elapsed = 0; this.waited = 0; this.active = true; this.count++;
      this.canvas.dataset.stageTransition = 'waiting';
      this.canvas.dataset.stageTransitionCount = String(this.count);
    } catch { this.finish(); } // Decorative failure never interrupts task rendering.
  }

  advance(dt, { frameReady = true, ready = true, paused = false, reduced = false, anchor } = {}) {
    if (!this.active) return;
    if (reduced) { this.finish(); return; }
    if (paused || document.hidden) return;
    if (!frameReady) return;
    this.waited += dt;
    // A missing asset must not indefinitely conceal a valid fallback scene.
    if (!ready && this.waited < 1.5) return;
    this.canvas.dataset.stageTransition = 'running';
    this.elapsed += dt;
    const duration = this.high ? .9 : .18;
    const p = Math.min(1, this.elapsed / duration);
    this.canvas.dataset.stageTransitionProgress = p.toFixed(4);
    const reveal = this.high ? ease((p - .15) / .85) : ease(p);
    this.still.style.opacity = String(1 - reveal);
    if (this.trace) {
      const turn = ease((p - .08) / .57), end = anchor ?? { x: .5, y: .52, radius: .12 };
      if (this.glow) {
        const scale = 1 - turn * .65;
        this.glow.style.transform = `translate(${(end.x - .5) * 100 * turn}%, ${(end.y - .78) * 100 * turn}%) scale(${scale})`;
        this.glow.style.opacity = String(ease(p / .18) * (1 - ease((p - .38) / .62)));
      }
      const x = 500 + (end.x * 1000 - 500) * turn, y = 520 + (end.y * 1000 - 520) * turn;
      const rx = 235 + (end.radius * 1000 - 235) * turn;
      const ry = 150 + (end.radius * 650 - 150) * turn;
      const points = [];
      for (let i = 0; i <= 96; i++) {
        const t = i / 96 * Math.PI * 2;
        const px = Math.cos(t) * rx;
        const py = ((1 - turn) * Math.sin(t * 2) + turn * Math.sin(t)) * ry;
        const angle = -.3 * turn;
        points.push(`${i ? 'L' : 'M'}${(x + px * Math.cos(angle) - py * Math.sin(angle)).toFixed(2)},${(y + px * Math.sin(angle) + py * Math.cos(angle)).toFixed(2)}`);
      }
      this.trace.setAttribute('d', points.join(' '));
      this.trace.setAttribute('stroke-dashoffset', String(-p * 90));
      this.trace.style.opacity = String(.55 * ease(p / .16) * (1 - ease((p - .55) / .45)));
    }
    if (p >= 1) this.finish();
  }

  finish() {
    this.overlay?.remove(); this.overlay = this.still = this.trace = this.glow = null; this.active = false;
    this.canvas.dataset.stageTransition = 'idle';
  }
  dispose() { this.finish(); }
}
