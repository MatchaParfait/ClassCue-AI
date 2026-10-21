const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
export const VIEW_LIMITS = { min: .65, max: 1.6, yaw: Math.PI / 60, pitch: Math.PI / 90 };

// Input and easing share the scene's render loop, including its idle/visibility rules.
export class TaskViewControls {
  constructor(canvas, invalidate, enabled = () => true) {
    this.current = { zoom: 1, yaw: 0, pitch: 0 };
    this.target = { ...this.current };
    this.reduced = false;
    this.move = event => {
      if (!enabled() || this.reduced || event.pointerType === 'touch') return;
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      this.target.yaw = clamp((event.clientX - rect.left) / rect.width * 2 - 1, -1, 1) * VIEW_LIMITS.yaw;
      this.target.pitch = clamp(1 - (event.clientY - rect.top) / rect.height * 2, -1, 1) * VIEW_LIMITS.pitch;
      invalidate();
    };
    this.leave = () => { this.target.yaw = this.target.pitch = 0; invalidate(); };
    this.wheel = event => {
      if (!enabled() || event.ctrlKey || event.metaKey || event.shiftKey || !event.deltaY || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
      event.preventDefault();
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? canvas.clientHeight || 800 : 1;
      const delta = clamp(event.deltaY * unit, -120, 120);
      this.target.zoom = clamp(this.target.zoom * Math.exp(-delta * .0015), VIEW_LIMITS.min, VIEW_LIMITS.max);
      invalidate();
    };
    canvas.addEventListener('pointermove', this.move);
    canvas.addEventListener('pointerleave', this.leave);
    canvas.addEventListener('wheel', this.wheel, { passive: false });
    this.dispose = () => {
      canvas.removeEventListener('pointermove', this.move);
      canvas.removeEventListener('pointerleave', this.leave);
      canvas.removeEventListener('wheel', this.wheel);
    };
    this.invalidate = invalidate;
  }

  get pending() { return Object.keys(this.current).some(key => this.current[key] !== this.target[key]); }

  update(dt) {
    const weight = this.reduced ? 1 : 1 - Math.exp(-Math.max(0, dt) / .15);
    for (const key of Object.keys(this.current)) {
      const value = this.current[key] + (this.target[key] - this.current[key]) * weight;
      this.current[key] = Math.abs(value - this.target[key]) < .0001 ? this.target[key] : value;
    }
    return this.current;
  }

  setReduced(reduced) {
    this.reduced = reduced;
    if (reduced) this.suspend();
  }

  suspend() {
    this.current.yaw = this.current.pitch = this.target.yaw = this.target.pitch = 0;
  }

  reset() {
    Object.assign(this.target, { zoom: 1, yaw: 0, pitch: 0 });
    this.invalidate();
  }
}
