const ease = t => t * t * t * (t * (t * 6 - 15) + 10);

// A finite presentation move, never an inferred backend progress indicator.
export class GravityJourney {
  constructor() {
    this.progress = 0; this.from = 0; this.to = 0; this.elapsed = 4.2;
    this.duration = 1.6; this.serial = null; this.count = 0; this.keys = [];
    this.emphasisDuration = 2.6; this.immediate = false;
    this.resetting = false; this.amount = 1; this.stop = 0;
  }
  get pending() { return this.elapsed < this.duration; }
  get presentationPending() { return this.pending || (!this.resetting && this.elapsed < this.duration + this.emphasisDuration); }
  get emphasisAge() { return Math.max(0, this.elapsed - this.duration); }
  get emphasisRed() {
    if (this.serial === null || this.immediate || this.resetting) return 0;
    return 1 - ease(Math.max(0, Math.min(1, (this.emphasisAge - (this.emphasisDuration - .55)) / .55)));
  }
  cameraScale(from = 1, peak = 1.3) {
    if (this.immediate || this.serial === null) return 1;
    if (this.resetting) return from + (1 - from) * this.amount;
    if (this.pending) return from + (peak - from) * this.amount;
    const retreat = ease(Math.max(0, Math.min(1, (this.emphasisAge - .6) / (this.emphasisDuration - .6))));
    return 1 + (peak - 1) * (1 - retreat);
  }
  start(request) {
    if (!request || request.serial === this.serial) return false;
    this.serial = request.serial; this.from = this.progress; this.elapsed = 0; this.amount = 0;
    this.resetting = Boolean(request.reset);
    if (request.reset) { this.to = 0; this.stop = 0; }
    else {
      // Progress is an event counter, not a reference-camera orbit position.
      this.keys = [...request.keys]; this.to = ++this.count;
    }
    return true;
  }
  advance(dt, immediate = false) {
    this.immediate = immediate;
    this.elapsed = immediate ? this.duration + this.emphasisDuration
      : Math.min(this.duration + this.emphasisDuration, this.elapsed + Math.max(0, dt));
    this.amount = ease(Math.min(1, this.elapsed / this.duration));
    this.progress = this.from + (this.to - this.from) * this.amount;
    if (!this.pending && this.resetting) this.keys = [];
  }
}
