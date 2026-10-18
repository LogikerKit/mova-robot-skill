// Encrypted FLV receiver. Node builtins only; never writes credentials or logs URLs.
// RTMP primitives are adapted from nodedreame 1.12.4; see LICENSE and README.md.
import { LvRtmpClient, parseRelayUrl } from './lv-rtmp-client.mjs';
import { MAX_MEDIA_BYTES, RelayMediaDecryptor, VideoMediaError } from './crypto.mjs';

export { RelayMediaDecryptor, VideoMediaError } from './crypto.mjs';

export const FLV_HEADER = Buffer.from([0x46, 0x4c, 0x56, 1, 5, 0, 0, 0, 9, 0, 0, 0, 0]);
const MAX_OUTPUT_QUEUE = 16 * 1024 * 1024;

export function encodeFlvTag(type, timestamp, body) {
  if (![8, 9].includes(type) || !Number.isInteger(timestamp) || timestamp < 0 ||
      !Buffer.isBuffer(body) || body.length > MAX_MEDIA_BYTES) {
    throw new VideoMediaError('FRAME', 'Invalid FLV media tag.');
  }
  const header = Buffer.alloc(11);
  header[0] = type;
  header.writeUIntBE(body.length, 1, 3);
  header.writeUIntBE(timestamp & 0xffffff, 4, 3);
  header[7] = (timestamp >>> 24) & 0xff;
  const previousSize = Buffer.alloc(4);
  previousSize.writeUInt32BE(11 + body.length);
  return Buffer.concat([header, body, previousSize]);
}

function duration(value, fallback, min, max) {
  const selected = value ?? fallback;
  if (!Number.isInteger(selected) || selected < min || selected > max) {
    throw new VideoMediaError('INPUT', 'Invalid video receiver time limit.');
  }
  return selected;
}

function checkUrl(url) {
  if (typeof url !== 'string' || url.length > 16384 || /[\s\x00-\x1f\x7f]/.test(url)) {
    throw new VideoMediaError('URL', 'Invalid encrypted relay address.');
  }
  try {
    const endpoint = parseRelayUrl(url, 3000);
    if (!/^[A-Za-z0-9.-]+$/.test(endpoint.host) || !Number.isInteger(endpoint.port) ||
        endpoint.port < 1 || endpoint.port > 65535 || !endpoint.playpath || !endpoint.app) {
      throw new Error();
    }
  } catch {
    throw new VideoMediaError('URL', 'Unsupported encrypted relay address.');
  }
}

/**
 * Receive a descriptor returned by an authenticated encrypted stream query.
 * Writes decrypted FLV to a caller-owned Writable, e.g. ffmpeg.stdin, in memory.
 * Completion occurs on cancellation, deadline, peer close, or a sanitized error.
 * The caller owns camera activation/stop and must clean those up separately.
 * clientFactory exists for offline transport fixtures; production uses TCP.
 */
export async function receiveEncryptedFlv({
  url, relayDecryptKey, output, signal,
  maxDurationMs, connectTimeoutMs, idleTimeoutMs, endOutput = true,
  clientFactory = options => new LvRtmpClient(options),
} = {}) {
  checkUrl(url);
  if (!output || typeof output.write !== 'function' || typeof output.on !== 'function' ||
      typeof output.off !== 'function' || typeof output.end !== 'function' || output.destroyed) {
    throw new VideoMediaError('OUTPUT', 'A writable local video destination is required.');
  }
  if (signal !== undefined && (typeof signal.addEventListener !== 'function' ||
      typeof signal.removeEventListener !== 'function')) {
    throw new VideoMediaError('INPUT', 'Invalid cancellation signal.');
  }
  const maxDuration = duration(maxDurationMs, 60000, 1000, 300000);
  const connectTimeout = duration(connectTimeoutMs, 10000, 100, 30000);
  const idleTimeout = duration(idleTimeoutMs, 15000, 100, 60000);
  const decryptor = new RelayMediaDecryptor(relayDecryptKey);
  let client;
  try {
    client = clientFactory({
      url, connectTimeoutMs: connectTimeout,
      decryptVideo: body => decryptor.video(body),
      decryptAudio: body => decryptor.audio(body),
    });
  } catch {
    decryptor.close();
    throw new VideoMediaError('INPUT', 'Could not initialize the encrypted receiver.');
  }

  return new Promise((resolve, reject) => {
    let finished = false;
    let videoFrames = 0;
    let audioFrames = 0;
    let idleTimer;
    let maxTimer;
    let connectTimer;
    let paused = false;
    const finish = (endedBy, error) => {
      if (finished) return;
      finished = true;
      clearTimeout(idleTimer);
      clearTimeout(maxTimer);
      clearTimeout(connectTimer);
      signal?.removeEventListener('abort', onAbort);
      output.off('drain', onDrain);
      output.off('error', onOutputError);
      output.off('close', onOutputClose);
      client.close();
      decryptor.close();
      if (endOutput && !output.destroyed) {
        // The destination remains caller-owned; do not destroy another process.
        try { output.end(); } catch { /* A prior error already describes failure. */ }
      }
      if (error) reject(error);
      else if (!videoFrames && endedBy !== 'aborted') {
        reject(new VideoMediaError('EMPTY', 'The encrypted relay ended before any video frames arrived.'));
      } else resolve({ videoFrames, audioFrames, endedBy });
    };
    const onAbort = () => finish('aborted');
    const onOutputError = () => finish('output', new VideoMediaError('OUTPUT', 'The local video destination failed.'));
    const onOutputClose = () => finish('output', new VideoMediaError('OUTPUT', 'The local video destination closed.'));
    const onDrain = () => {
      paused = false;
      client.resume();
    };
    const resetIdle = () => {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(() => finish('timeout', new VideoMediaError('TIMEOUT', 'The encrypted video relay stopped sending media.')), idleTimeout);
    };
    const write = data => {
      if (finished) return;
      try {
        if ((output.writableLength ?? 0) + data.length > MAX_OUTPUT_QUEUE) {
          finish('output', new VideoMediaError('BACKPRESSURE', 'The local video destination is too slow.'));
          return;
        }
        if (!output.write(data) && !paused) {
          paused = true;
          client.pause();
        }
      } catch { onOutputError(); }
    };
    client.on('video', ({ timestamp, body }) => {
      if (finished) return;
      resetIdle();
      if (body[1] === 1) videoFrames++;
      write(encodeFlvTag(9, timestamp, body));
    });
    client.on('audio', ({ timestamp, body }) => {
      if (finished) return;
      resetIdle();
      audioFrames++;
      write(encodeFlvTag(8, timestamp, body));
    });
    client.on('status', ({ level, code }) => {
      if (level === 'error' || /(?:Failed|Rejected|StreamNotFound)$/.test(code)) {
        finish('relay', new VideoMediaError('REJECTED', 'The encrypted video relay rejected playback.'));
      }
    });
    client.on('error', error => finish('error', error instanceof VideoMediaError ? error :
      new VideoMediaError('RELAY', 'The encrypted video relay connection failed.')));
    client.on('close', () => finish('peer'));
    client.on('connected', () => {
      clearTimeout(connectTimer);
      if (paused) client.pause();
      resetIdle();
    });
    output.on('drain', onDrain);
    output.on('error', onOutputError);
    output.on('close', onOutputClose);
    signal?.addEventListener('abort', onAbort, { once: true });
    if (signal?.aborted) { onAbort(); return; }
    maxTimer = setTimeout(() => finish('duration'), maxDuration);
    connectTimer = setTimeout(() => finish('timeout', new VideoMediaError('TIMEOUT', 'The encrypted relay connection timed out.')), connectTimeout);
    write(FLV_HEADER);
    if (finished) return;
    Promise.resolve().then(() => {
      if (!finished) return client.connect();
    }).catch(() => finish('error', new VideoMediaError('RELAY', 'The encrypted video relay connection failed.')));
  });
}
