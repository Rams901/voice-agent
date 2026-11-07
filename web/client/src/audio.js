const N = 128;
const re = new Float32Array(N);
const im = new Float32Array(N);
const pcmBands = new Float32Array(16);

function fft(real, imag) {
  for (let i = 1, j = 0; i < N; i++) {
    let bit = N >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      const tr = real[i];
      real[i] = real[j];
      real[j] = tr;
      const ti = imag[i];
      imag[i] = imag[j];
      imag[j] = ti;
    }
  }
  for (let len = 2; len <= N; len <<= 1) {
    const ang = (-2 * Math.PI) / len;
    const wlenRe = Math.cos(ang);
    const wlenIm = Math.sin(ang);
    for (let i = 0; i < N; i += len) {
      let wRe = 1;
      let wIm = 0;
      const half = len >> 1;
      for (let k = 0; k < half; k++) {
        const o = i + k + half;
        const uRe = real[i + k];
        const uIm = imag[i + k];
        const vRe = real[o] * wRe - imag[o] * wIm;
        const vIm = real[o] * wIm + imag[o] * wRe;
        real[i + k] = uRe + vRe;
        imag[i + k] = uIm + vIm;
        real[o] = uRe - vRe;
        imag[o] = uIm - vIm;
        const nextRe = wRe * wlenRe - wIm * wlenIm;
        wIm = wRe * wlenIm + wIm * wlenRe;
        wRe = nextRe;
      }
    }
  }
}

/** Agent PCM (Float32, roughly -1..1) → 16 bands and a loudness. Copies required. */
export function bandsFromPcm(samples) {
  const start = Math.max(0, samples.length - N);
  let energy = 0;
  for (let i = 0; i < N; i++) {
    const s = samples[start + i] || 0;
    energy += s * s;
    const w = 0.5 * (1 - Math.cos((2 * Math.PI * i) / (N - 1)));
    re[i] = s * w;
    im[i] = 0;
  }
  fft(re, im);
  for (let b = 0; b < 16; b++) {
    let e = 0;
    const a = 1 + b * 4;
    for (let k = 0; k < 4; k++) e += Math.hypot(re[a + k], im[a + k]);
    pcmBands[b] = Math.min(1, e * 0.42);
  }
  return { bands: pcmBands, amp: Math.min(1, Math.sqrt(energy / N) * 6.5) };
}

/** Mic analyser bins. Writes into `out` and returns loudness. */
export function bandsFromAnalyser(freq, time, out) {
  const usable = Math.min(freq.length, 96);
  const width = usable / 16;
  let energy = 0;
  for (let i = 0; i < time.length; i++) {
    const v = (time[i] - 128) / 128;
    energy += v * v;
  }
  for (let b = 0; b < 16; b++) {
    const a0 = Math.floor(b * width);
    const a1 = Math.max(a0 + 1, Math.floor((b + 1) * width));
    let e = 0;
    for (let k = a0; k < a1; k++) e += freq[k];
    out[b] = Math.min(1, e / (a1 - a0) / 170);
  }
  return Math.min(1, Math.sqrt(energy / time.length) * 5);
}

export function createAudioBus() {
  return {
    bands: new Float32Array(16),
    targetBands: new Float32Array(16),
    amp: 0,
    targetAmp: 0,
    mode: 0,
    modeTarget: 0,
    updated: 0,
    micLive: false,
    phase: "idle",
  };
}

const scratch = new Float32Array(16);

export function pushPcm(bus, samples) {
  const { bands, amp } = bandsFromPcm(samples);
  bus.targetBands.set(bands);
  bus.targetAmp = amp;
  bus.modeTarget = 2;
  bus.updated = performance.now();
}

export function pushMic(bus, freq, time) {
  if (bus.modeTarget === 2) return;
  const amp = bandsFromAnalyser(freq, time, scratch);
  bus.targetBands.set(scratch);
  bus.targetAmp = amp;
  bus.modeTarget = 1;
  bus.updated = performance.now();
  bus.micLive = true;
}
