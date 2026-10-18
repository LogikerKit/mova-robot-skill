// Internal subprocess protocol. Parent captures stdout privately; never expose
// auth output to a terminal, tool result, browser response, or application log.
import { establishVideoIdentity, queryEncryptedVideoStream } from './video_auth.mjs';
import { DEFAULT_PLAYBACK_CACHE_MS, validPlaybackCacheMs } from './video_media/playback.mjs';

const mode = process.argv[2];
const controller = new AbortController();
let terminating = false;
let brokenOutput = false;
const terminate = () => { terminating = true; controller.abort(); process.stdin.destroy(); };
process.once('SIGINT', terminate);
process.once('SIGTERM', terminate);
process.stdout.on('error', () => { brokenOutput = true; process.exitCode = 2; controller.abort(); });
process.stderr.on('error', () => {});

function report(phase, fields = {}) {
  // All call sites use fixed codes or numeric/boolean data. Never log objects
  // returned from authentication, stream/query, or the media implementation.
  process.stderr.write(`${JSON.stringify({ phase, ...fields })}\n`);
}

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}

async function readPrivateInput() {
  const chunks = [];
  let length = 0;
  for await (const chunk of process.stdin) {
    length += chunk.length;
    if (length > 131072) fail('VIDEO_WORKER_INPUT_TOO_LARGE');
    chunks.push(chunk);
  }
  const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!input || typeof input !== 'object' || Array.isArray(input)) fail('VIDEO_WORKER_INVALID_INPUT');
  return input;
}

function safeResult(result) {
  return {
    ...(Number.isInteger(result?.httpStatus) ? { httpStatus: result.httpStatus } : {}),
    ...(Number.isInteger(result?.responseCode) ? { responseCode: result.responseCode } : {}),
  };
}

try {
  if (!['auth', 'stream'].includes(mode) || process.argv.length !== 3) fail('VIDEO_WORKER_INVALID_MODE');
  if (process.stdout.isTTY) fail('VIDEO_WORKER_PRIVATE_PIPE_REQUIRED');
  const input = await readPrivateInput();
  if (terminating || brokenOutput) fail('VIDEO_WORKER_ABORTED');

  if (mode === 'auth') {
    report('authenticating');
    const result = await establishVideoIdentity(input, { signal: controller.signal });
    if (terminating || brokenOutput) fail('VIDEO_WORKER_ABORTED');
    if (!result.ok || !result.identity) {
      report('auth_failed', safeResult(result));
      // Failure output is safe; successful identity below is private IPC only.
      process.stdout.write(`${JSON.stringify({ ok: false, ...safeResult(result) })}\n`);
      process.exitCode = 2;
    } else {
      process.stdout.write(`${JSON.stringify({ ok: true, identity: result.identity })}\n`);
      report('authenticated');
    }
  } else {
    const duration = input.maxDurationMs ?? 60000;
    if (!Number.isInteger(duration) || duration < 1000 || duration > 300000) fail('VIDEO_WORKER_INVALID_DURATION');
    const cacheDurationMs = input.cacheDurationMs === undefined ? DEFAULT_PLAYBACK_CACHE_MS : input.cacheDurationMs;
    if (!validPlaybackCacheMs(cacheDurationMs)) fail('VIDEO_WORKER_INVALID_CACHE');
    report('querying_encrypted_stream');
    const result = await queryEncryptedVideoStream(input.identity, input.credentials, input.iotId,
      { signal: controller.signal, cacheDurationMs });
    if (terminating || brokenOutput) fail('VIDEO_WORKER_ABORTED');
    if (!result.ok || !result.stream?.encrypted) {
      report('stream_query_failed', safeResult(result));
      fail('VIDEO_WORKER_STREAM_QUERY_FAILED');
    }
    const { receiveEncryptedFlv } = await import('./video_media/index.mjs');
    report('receiving_encrypted_stream');
    const stats = await receiveEncryptedFlv({
      url: result.stream.relayUrl,
      relayDecryptKey: result.stream.relayDecryptKey,
      output: process.stdout,
      signal: controller.signal,
      maxDurationMs: duration,
      cacheDurationMs,
      connectTimeoutMs: 10000,
      idleTimeoutMs: 15000,
      endOutput: false,
    });
    report('stream_ended', {
      videoFrames: Number.isInteger(stats.videoFrames) ? stats.videoFrames : 0,
      audioFrames: Number.isInteger(stats.audioFrames) ? stats.audioFrames : 0,
      endedBy: ['aborted', 'duration', 'peer'].includes(stats.endedBy) ? stats.endedBy : 'unknown',
    });
    if (brokenOutput) process.exitCode = 2;
  }
} catch (error) {
  const suppliedCode = error?.code;
  const code = typeof suppliedCode === 'string' && /^(VIDEO_MEDIA|VIDEO_WORKER)_[A-Z0-9_]{1,64}$/.test(suppliedCode)
    ? suppliedCode : 'VIDEO_WORKER_FAILED';
  report('error', { code });
  process.exitCode = terminating ? 130 : 2;
} finally {
  controller.abort();
  process.removeListener('SIGINT', terminate);
  process.removeListener('SIGTERM', terminate);
}
