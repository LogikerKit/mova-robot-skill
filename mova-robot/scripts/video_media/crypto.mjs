// Independently recovered from MOVAhome's LinkVisual native receiver.
// The AES framing is documented in README.md; no vendor executable code is copied.
import { createDecipheriv } from 'node:crypto';

export const MAX_MEDIA_BYTES = 8 * 1024 * 1024;

export class VideoMediaError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'VideoMediaError';
    this.code = `VIDEO_MEDIA_${code}`;
  }
}

function decodeSecret(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9+/]{22}(==)?$/.test(value)) {
    throw new VideoMediaError('KEY', 'Encrypted relay requires a valid 16-byte key and IV.');
  }
  const decoded = Buffer.from(value, 'base64');
  if (decoded.length !== 16 || decoded.toString('base64').replace(/=+$/, '') !== value.replace(/=+$/, '')) {
    decoded.fill(0);
    throw new VideoMediaError('KEY', 'Encrypted relay requires a valid 16-byte key and IV.');
  }
  return decoded;
}

function checkAvcConfig(data) {
  if (data.length < 7 || data[0] !== 1 || ![1, 2, 4].includes((data[4] & 3) + 1)) {
    throw new VideoMediaError('DECRYPT', 'Invalid decrypted H.264 configuration.');
  }
  let p = 6;
  const readNals = (count, type) => {
    if (count === 0) throw new VideoMediaError('DECRYPT', 'Invalid decrypted H.264 configuration.');
    for (let i = 0; i < count; i++) {
      if (p + 2 > data.length) throw new VideoMediaError('DECRYPT', 'Truncated H.264 configuration.');
      const length = data.readUInt16BE(p);
      p += 2;
      if (!length || p + length > data.length || (data[p] & 31) !== type) {
        throw new VideoMediaError('DECRYPT', 'Invalid decrypted H.264 configuration.');
      }
      p += length;
    }
  };
  readNals(data[5] & 31, 7);
  if (p >= data.length) throw new VideoMediaError('DECRYPT', 'Truncated H.264 configuration.');
  readNals(data[p++], 8);
}

export class RelayMediaDecryptor {
  #key;
  #iv;
  #closed = false;
  #haveConfig = false;

  constructor(relayDecryptKey) {
    this.#key = decodeSecret(relayDecryptKey?.key);
    try {
      this.#iv = decodeSecret(relayDecryptKey?.iv);
    } catch (error) {
      this.#key.fill(0);
      throw error;
    }
  }

  close() {
    this.#closed = true;
    this.#key.fill(0);
    this.#iv.fill(0);
  }

  #checkBody(body) {
    if (this.#closed) throw new VideoMediaError('CLOSED', 'Media receiver is closed.');
    if (!Buffer.isBuffer(body) || body.length > MAX_MEDIA_BYTES) {
      throw new VideoMediaError('FRAME', 'Invalid or oversized media frame.');
    }
  }

  #decrypt(body, removePadding) {
    if (!body.length || body.length % 16 !== 0) {
      throw new VideoMediaError('DECRYPT', 'Encrypted media has an invalid block length.');
    }
    // Native code resets to the supplied IV for each message. It does not carry
    // CBC state between frames. No automatic plaintext fallback is permitted.
    const decipher = createDecipheriv('aes-128-cbc', this.#key, this.#iv);
    decipher.setAutoPadding(false);
    const plain = Buffer.concat([decipher.update(body), decipher.final()]);
    if (!removePadding) return plain;
    const padding = plain[plain.length - 1];
    // This mirrors the native final-byte length convention, with bounds checks.
    // It is not an authenticated cipher or a proof that the key was correct.
    if (padding < 1 || padding > 16 || padding >= plain.length) {
      plain.fill(0);
      throw new VideoMediaError('DECRYPT', 'Encrypted media has invalid padding.');
    }
    return plain.subarray(0, plain.length - padding);
  }

  video(body) {
    this.#checkBody(body);
    if (body.length < 5) throw new VideoMediaError('FRAME', 'Truncated video frame.');
    const codec = body[0] & 15;
    if (codec === 12 || (body[0] & 0x80)) {
      throw new VideoMediaError('CODEC', 'H.265 video is not supported by this receiver.');
    }
    if (codec !== 7) throw new VideoMediaError('CODEC', 'Only H.264 video is supported.');
    const frameType = body[0] >> 4;
    const packetType = body[1];
    if (![1, 2].includes(frameType) || ![0, 1, 2].includes(packetType)) {
      throw new VideoMediaError('FRAME', 'Unsupported H.264 frame format.');
    }
    if (packetType === 0) {
      if (frameType !== 1) throw new VideoMediaError('FRAME', 'Invalid H.264 sequence header.');
      const plain = this.#decrypt(body.subarray(5), false);
      checkAvcConfig(plain);
      this.#haveConfig = true;
      // The native sequence-header parser keeps padding and follows NAL lengths.
      return Buffer.concat([body.subarray(0, 5), plain]);
    }
    if (!this.#haveConfig) {
      throw new VideoMediaError('DECRYPT', 'Encrypted H.264 configuration has not arrived.');
    }
    if (frameType === 1 && packetType === 1) {
      return Buffer.concat([body.subarray(0, 5), this.#decrypt(body.subarray(5), true)]);
    }
    // The vendor encrypts configuration/keyframes, not interframes or end tags.
    return body;
  }

  audio(body) {
    this.#checkBody(body);
    const plain = this.#decrypt(body, true);
    const format = plain[0] >> 4;
    if (![7, 8, 10].includes(format) || plain.length < 2 ||
        (format === 10 && ![0, 1].includes(plain[1]))) {
      throw new VideoMediaError('DECRYPT', 'Invalid decrypted audio frame.');
    }
    return plain;
  }
}
