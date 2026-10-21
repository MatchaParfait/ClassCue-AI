class GTCRNPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capacity = 4096;
    this.wet = new Float32Array(this.capacity);
    this.input = new Float32Array(256);
    this.mix = 0;
    this.reset(0, false);
    this.port.onmessage = ({ data }) => {
      if (data.type === "reset") {
        this.reset(data.epoch, data.active);
      } else if (data.type === "output" && data.epoch === this.epoch && this.active) {
        const samples = new Float32Array(data.buffer, 8);
        if (this.available + samples.length > this.capacity) {
          this.port.postMessage({ type: "overflow", epoch: this.epoch });
          this.reset(this.epoch, false);
          return;
        }
        for (const sample of samples) {
          this.wet[this.write] = sample;
          this.write = (this.write + 1) % this.capacity;
        }
        this.available += samples.length;
      }
    };
  }

  reset(epoch, active) {
    this.epoch = epoch;
    this.active = active;
    this.sequence = this.inputCount = this.write = this.read = this.available = 0;
    this.started = false;
    this.underruns = this.blocks = this.wetSamples = 0;
    this.lastWet = 0;
  }

  process(inputs, outputs) {
    const channels = inputs[0];
    const output = outputs[0];
    const count = output[0].length;
    if (this.active) {
      for (let i = 0; i < count; i++) {
        let sample = 0;
        for (const channel of channels) sample += channel[i] || 0;
        this.input[this.inputCount++] = channels.length ? sample / channels.length : 0;
        if (this.inputCount === 256) {
          const packet = new ArrayBuffer(8 + 256 * 4);
          const header = new DataView(packet);
          header.setUint32(0, this.epoch, true);
          header.setUint32(4, this.sequence++, true);
          new Float32Array(packet, 8).set(this.input);
          this.port.postMessage({ type: "input", epoch: this.epoch, buffer: packet }, [packet]);
          this.inputCount = 0;
        }
      }
    }
    if (!this.started && this.available >= 512) this.started = true;
    if (this.started && this.available < count) {
      this.started = false;
      if (++this.underruns >= 6) {
        this.port.postMessage({ type: "stalled", epoch: this.epoch });
        this.active = false;
      }
    }
    const useWet = this.active && this.started;
    for (let i = 0; i < count; i++) {
      // Keep original stereo audible while initializing, switching, or recovering.
      this.mix += Math.max(-1 / 320, Math.min(1 / 320, (useWet ? 1 : 0) - this.mix));
      if (useWet) {
        this.lastWet = this.wet[this.read];
        this.read = (this.read + 1) % this.capacity;
      }
      for (let channel = 0; channel < output.length; channel++) {
        const dry = channels[channel]?.[i] ?? channels[0]?.[i] ?? 0;
        output[channel][i] = dry * (1 - this.mix) + this.lastWet * this.mix;
      }
    }
    if (useWet) { this.available -= count; this.wetSamples += count; }
    if (++this.blocks % 125 === 0) {
      this.port.postMessage({ type: "stats", epoch: this.epoch, wetSamples: this.wetSamples, underruns: this.underruns });
    }
    return true;
  }
}

registerProcessor("gtcrn-playback", GTCRNPlaybackProcessor);
