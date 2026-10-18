import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createCipheriv } from 'node:crypto';
import { EventEmitter } from 'node:events';
import { Writable } from 'node:stream';
import { spawn, spawnSync } from 'node:child_process';
import { RelayMediaDecryptor, VideoMediaError } from '../mova-robot/scripts/video_media/crypto.mjs';
import { encodeFlvTag, FLV_HEADER, receiveEncryptedFlv } from '../mova-robot/scripts/video_media/index.mjs';
import { ChunkReader, MSG, encodeMessage } from '../mova-robot/scripts/video_media/chunk.mjs';
import { LvRtmpClient, parseRelayUrl } from '../mova-robot/scripts/video_media/lv-rtmp-client.mjs';

// Entirely synthetic key, IV and frame fixtures. No account or device access.
const key = Buffer.from('00112233445566778899aabbccddeeff', 'hex');
const iv = Buffer.from('ffeeddccbbaa99887766554433221100', 'hex');
const secrets = { key: key.toString('base64'), iv: iv.toString('base64') };
const url = 'rtmp://relay.example.invalid:8000/live?token=SENSITIVE_TEST_TOKEN/stream';
const config = Buffer.from([1, 0x42, 0, 0x1e, 0xff, 0xe1, 0, 4, 0x67, 0x42, 0, 0x1e, 1, 0, 4, 0x68, 0xce, 6, 0xe2]);
const nals = Buffer.from([0, 0, 0, 4, 0x65, 0x11, 0x22, 0x33]);
const videoHeader = type => Buffer.from([0x17, type, 0, 0, 0]);
function encrypt(data) {
  const cipher = createCipheriv('aes-128-cbc', key, iv);
  return Buffer.concat([cipher.update(data), cipher.final()]);
}
function encryptRaw(data) {
  const cipher = createCipheriv('aes-128-cbc', key, iv).setAutoPadding(false);
  return Buffer.concat([cipher.update(data), cipher.final()]);
}
const encryptedConfig = Buffer.concat([videoHeader(0), encrypt(config)]);
const encryptedKeyframe = Buffer.concat([videoHeader(1), encrypt(nals)]);
const interframe = Buffer.concat([Buffer.from([0x27, 1, 0, 0, 0]), nals]);
function initialized() {
  const decryptor = new RelayMediaDecryptor(secrets);
  decryptor.video(encryptedConfig);
  return decryptor;
}

test('AES-CBC configuration keeps clear FLV header and native trailing padding', () => {
  const decryptor = new RelayMediaDecryptor(secrets);
  const result = decryptor.video(encryptedConfig);
  assert.deepEqual(result.subarray(0, 5), videoHeader(0));
  assert.deepEqual(result.subarray(5, 5 + config.length), config);
  assert.equal(result.length - 5, encrypt(config).length);
  decryptor.close();
});

test('only keyframe payload is decrypted; IV resets independently for every message', () => {
  const decryptor = initialized();
  const expected = Buffer.concat([videoHeader(1), nals]);
  assert.deepEqual(decryptor.video(encryptedKeyframe), expected);
  assert.deepEqual(decryptor.video(encryptedKeyframe), expected);
  assert.deepEqual(decryptor.video(interframe), interframe);
  decryptor.close();
});

test('audio decrypts its complete FLV body and removes final-byte padding', () => {
  const decryptor = initialized();
  for (const plain of [Buffer.from([0x72, 1, 2, 3]), Buffer.from([0x82, 4, 5]), Buffer.from([0xaf, 0, 0x12, 0x10])]) {
    assert.deepEqual(decryptor.audio(encrypt(plain)), plain);
  }
  decryptor.close();
});

test('missing, malformed, noncanonical and wrong-sized keys fail before any transport', () => {
  for (const input of [null, {}, { ...secrets, key: '' }, { ...secrets, key: 'A'.repeat(32) }, { ...secrets, iv: secrets.iv + '\n' }, { ...secrets, iv: 'AAAAAAAAAAAAAAAAAAAAAB==' }]) {
    assert.throws(() => new RelayMediaDecryptor(input), { code: 'VIDEO_MEDIA_KEY' });
  }
});

test('wrong key cannot silently produce an accepted configuration', () => {
  const decryptor = new RelayMediaDecryptor({ ...secrets, key: Buffer.alloc(16, 0x19).toString('base64') });
  assert.throws(() => decryptor.video(encryptedConfig), { code: 'VIDEO_MEDIA_DECRYPT' });
  decryptor.close();
});

test('no plaintext fallback for bad AES length or invalid padding', () => {
  const decryptor = initialized();
  assert.throws(() => decryptor.video(Buffer.concat([videoHeader(1), nals])), { code: 'VIDEO_MEDIA_DECRYPT' });
  for (const padding of [0, 17, 255]) {
    const raw = Buffer.alloc(16, 0x72); raw[15] = padding;
    assert.throws(() => decryptor.audio(encryptRaw(raw)), { code: 'VIDEO_MEDIA_DECRYPT' });
  }
  decryptor.close();
});

test('H265, unknown codecs and malformed H264 frames fail clearly', () => {
  const decryptor = initialized();
  assert.throws(() => decryptor.video(Buffer.from([0x1c, 0, 0, 0, 0])), /H\.265/);
  assert.throws(() => decryptor.video(Buffer.from([0x90, 0, 0, 0, 0])), /H\.265/);
  assert.throws(() => decryptor.video(Buffer.from([0x16, 0, 0, 0, 0])), { code: 'VIDEO_MEDIA_CODEC' });
  assert.throws(() => decryptor.video(Buffer.alloc(4)), { code: 'VIDEO_MEDIA_FRAME' });
  assert.throws(() => decryptor.video(Buffer.from([0x27, 0, 0, 0, 0])), { code: 'VIDEO_MEDIA_FRAME' });
  decryptor.close();
});

test('requires encrypted configuration before interframes and refuses use after close', () => {
  const decryptor = new RelayMediaDecryptor(secrets);
  assert.throws(() => decryptor.video(interframe), { code: 'VIDEO_MEDIA_DECRYPT' });
  decryptor.close();
  assert.throws(() => decryptor.video(encryptedConfig), { code: 'VIDEO_MEDIA_CLOSED' });
  assert.throws(() => decryptor.audio(encrypt(Buffer.from([0x72, 1]))), { code: 'VIDEO_MEDIA_CLOSED' });
});

test('FLV tags contain correct length, timestamp byte order and previous-size trailer', () => {
  const tag = encodeFlvTag(9, 0x12345678, interframe);
  assert.equal(tag[0], 9);
  assert.equal(tag.readUIntBE(1, 3), interframe.length);
  assert.equal(tag.readUIntBE(4, 3), 0x345678);
  assert.equal(tag[7], 0x12);
  assert.deepEqual(tag.subarray(11, -4), interframe);
  assert.equal(tag.readUInt32BE(tag.length - 4), interframe.length + 11);
});

test('fragmented fmt-1 headers apply timestamp delta exactly once', () => {
  const reader = new ChunkReader();
  const first = encodeMessage({ fmt: 0, csid: 5, msid: 1, timestamp: 100, type: MSG.VIDEO, body: Buffer.from([1, 2]) });
  assert.equal(reader.feed(first)[0].timestamp, 100);
  const second = encodeMessage({ fmt: 1, csid: 5, timestamp: 25, type: MSG.VIDEO, body: Buffer.from([3, 4, 5]) });
  const messages = [];
  for (const byte of second) messages.push(...reader.feed(Buffer.from([byte])));
  assert.equal(messages.length, 1);
  assert.equal(messages[0].timestamp, 125);
  assert.deepEqual(messages[0].body, Buffer.from([3, 4, 5]));
});

test('chunk-size update affects the next message in the same TCP buffer', () => {
  const reader = new ChunkReader();
  const size = Buffer.alloc(4); size.writeUInt32BE(4);
  const control = encodeMessage({ fmt: 0, csid: 2, type: MSG.SET_CHUNK_SIZE, body: size });
  const message = encodeMessage({ fmt: 0, csid: 5, type: MSG.VIDEO, body: Buffer.from('abcdefghij') });
  const chunked = Buffer.concat([message.subarray(0, 16), Buffer.from([0xc5]), message.subarray(16, 20), Buffer.from([0xc5]), message.subarray(20)]);
  const result = reader.feed(Buffer.concat([control, chunked]));
  assert.equal(result.length, 2);
  assert.equal(result[1].body.toString(), 'abcdefghij');
});

test('invalid chunk controls and unknown continuations do not allocate indefinitely', () => {
  assert.throws(() => new ChunkReader().feed(Buffer.from([0xc5])), /continuation/);
  assert.throws(() => new ChunkReader(0).feed(Buffer.from([0])), /chunk size/);
  const reader = new ChunkReader();
  const size = Buffer.alloc(4);
  assert.throws(() => reader.feed(encodeMessage({ fmt: 0, csid: 2, type: MSG.SET_CHUNK_SIZE, body: size })), /chunk size/);
});

test('fragmented private handshake and RTMP media pass through AES into clear FLV events', () => {
  const decryptor = new RelayMediaDecryptor(secrets);
  const client = new LvRtmpClient({ url, ping: false, decryptVideo: b => decryptor.video(b), decryptAudio: b => decryptor.audio(b) });
  const events = [];
  client.on('error', error => { throw error; });
  client.on('video', event => events.push(event));
  const handshake = Buffer.alloc(1537); handshake[0] = 4;
  const wire = Buffer.concat([handshake,
    encodeMessage({ fmt: 0, csid: 5, msid: 1, timestamp: 0, type: MSG.VIDEO, body: encryptedConfig }),
    encodeMessage({ fmt: 1, csid: 5, timestamp: 40, type: MSG.VIDEO, body: encryptedKeyframe })]);
  for (let p = 0; p < wire.length; p += 7) client.onData(wire.subarray(p, p + 7));
  assert.equal(events.length, 2);
  assert.equal(events[1].timestamp, 40);
  assert.deepEqual(events[1].body, Buffer.concat([videoHeader(1), nals]));
  client.close(); decryptor.close();
});

function destination() {
  const chunks = [];
  const output = new Writable({ write(chunk, encoding, callback) { chunks.push(Buffer.from(chunk)); callback(); } });
  return { output, bytes: () => Buffer.concat(chunks) };
}
function fixtureFactory(run, holder = {}) {
  return options => {
    const client = new EventEmitter();
    Object.assign(client, {
      options, closed: false, paused: false,
      close() { this.closed = true; },
      pause() { this.paused = true; },
      resume() { this.paused = false; },
      async connect() {
        client.emit('connected');
        try { run(client, options); } catch (error) { client.emit('error', error); }
      },
    });
    holder.client = client;
    return client;
  };
}
function emitVideo(client, options) {
  client.emit('video', { timestamp: 0, body: options.decryptVideo(encryptedConfig) });
  client.emit('video', { timestamp: 40, body: options.decryptVideo(encryptedKeyframe) });
}

test('receiver streams playable-format FLV through supplied Writable and disposes keys', async () => {
  const dest = destination(); const holder = {};
  const result = await receiveEncryptedFlv({ url, relayDecryptKey: secrets, output: dest.output,
    clientFactory: fixtureFactory((client, options) => { emitVideo(client, options); client.emit('close'); }, holder) });
  assert.deepEqual(result, { videoFrames: 1, audioFrames: 0, endedBy: 'peer' });
  assert.deepEqual(dest.bytes().subarray(0, 13), FLV_HEADER);
  assert.ok(dest.bytes().includes(nals));
  assert.equal(holder.client.closed, true);
  assert.throws(() => holder.client.options.decryptAudio(encrypt(Buffer.from([0x72, 1]))), { code: 'VIDEO_MEDIA_CLOSED' });
});

test('abort stops receiver without opening a transport when already cancelled', async () => {
  const controller = new AbortController(); controller.abort();
  const dest = destination(); let connected = false;
  const result = await receiveEncryptedFlv({ url, relayDecryptKey: secrets, output: dest.output,
    signal: controller.signal, clientFactory: fixtureFactory(() => { connected = true; }) });
  assert.equal(result.endedBy, 'aborted'); assert.equal(connected, false); assert.equal(dest.bytes().length, 0);
});

test('runtime cancellation closes the receiver and preserves caller-owned output when requested', async () => {
  const controller = new AbortController(); const dest = destination(); const holder = {};
  const result = await receiveEncryptedFlv({ url, relayDecryptKey: secrets, output: dest.output,
    signal: controller.signal, endOutput: false,
    clientFactory: fixtureFactory((client, options) => { emitVideo(client, options); controller.abort(); }, holder) });
  assert.equal(result.endedBy, 'aborted'); assert.equal(holder.client.closed, true); assert.equal(dest.output.writableEnded, false);
  dest.output.end();
});

test('media decryption errors are preserved and transport errors cannot disclose URLs', async () => {
  for (const error of [new Error(`socket failed: ${url}`), new VideoMediaError('DECRYPT', 'Invalid encrypted frame.')]) {
    const dest = destination();
    await assert.rejects(receiveEncryptedFlv({ url, relayDecryptKey: secrets, output: dest.output,
      clientFactory: fixtureFactory(client => client.emit('error', error)) }), actual => {
      assert.equal(actual.code, error instanceof VideoMediaError ? 'VIDEO_MEDIA_DECRYPT' : 'VIDEO_MEDIA_RELAY');
      assert.ok(!actual.message.includes('SENSITIVE_TEST_TOKEN')); return true;
    });
  }
});

test('invalid inputs and plaintext request cannot start a transport', async () => {
  for (const changes of [{ relayDecryptKey: null }, { url: 'rtmps://bad/SENSITIVE_TEST_TOKEN' }, { url: 'http://bad/SENSITIVE_TEST_TOKEN' }, { maxDurationMs: 300001 }, { maxDurationMs: true }]) {
    const dest = destination(); let created = false;
    await assert.rejects(receiveEncryptedFlv({ url, relayDecryptKey: secrets, output: dest.output,
      ...changes, clientFactory: () => { created = true; } }), error => {
      assert.ok(!error.message.includes('SENSITIVE_TEST_TOKEN')); return true;
    });
    assert.equal(created, false); dest.output.end();
  }
  assert.throws(() => parseRelayUrl('SENSITIVE_TEST_TOKEN', 3000), error => !error.message.includes('SENSITIVE_TEST_TOKEN'));
});

test('empty relay and rejected playback return explicit sanitized failures', async () => {
  for (const [run, code] of [
    [client => client.emit('close'), 'VIDEO_MEDIA_EMPTY'],
    [client => client.emit('status', { level: 'error', code: 'NetStream.Play.Failed', description: url }), 'VIDEO_MEDIA_REJECTED'],
  ]) {
    const dest = destination();
    await assert.rejects(receiveEncryptedFlv({ url, relayDecryptKey: secrets, output: dest.output,
      clientFactory: fixtureFactory(run) }), { code });
  }
});

test('idle timeout terminates a stalled relay without leaking request details', async () => {
  const dest = destination(); const holder = {};
  await assert.rejects(receiveEncryptedFlv({ url, relayDecryptKey: secrets, output: dest.output, idleTimeoutMs: 100,
    clientFactory: fixtureFactory(() => {}, holder) }), { code: 'VIDEO_MEDIA_TIMEOUT' });
  assert.equal(holder.client.closed, true);
});

test('generated H264/AAC survives vendor-style encryption and decodes as FLV in ffmpeg', async t => {
  const generated = spawnSync('ffmpeg', ['-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=160x120:r=5',
    '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono', '-t', '0.6', '-c:v', 'libx264', '-preset', 'ultrafast',
    '-pix_fmt', 'yuv420p', '-g', '2', '-c:a', 'aac', '-f', 'flv', 'pipe:1'],
    { timeout: 10000, maxBuffer: 16 * 1024 * 1024 });
  if (generated.error?.code === 'ENOENT') { t.skip('ffmpeg is not installed'); return; }
  assert.equal(generated.status, 0, generated.stderr?.toString());
  const source = generated.stdout;
  const decryptor = new RelayMediaDecryptor(secrets);
  const decodedTags = [FLV_HEADER];
  let p = source.readUInt32BE(5) + 4;
  let frames = 0;
  let audio = 0;
  while (p + 15 <= source.length) {
    const type = source[p];
    const length = source.readUIntBE(p + 1, 3);
    const timestamp = source.readUIntBE(p + 4, 3) + source[p + 7] * 0x1000000;
    const body = source.subarray(p + 11, p + 11 + length);
    if (type === 9) {
      const encrypted = body[0] === 0x17 && [0, 1].includes(body[1]) ?
        Buffer.concat([body.subarray(0, 5), encrypt(body.subarray(5))]) : body;
      decodedTags.push(encodeFlvTag(type, timestamp, decryptor.video(encrypted)));
      if (body[1] === 1) frames++;
    } else if (type === 8) {
      decodedTags.push(encodeFlvTag(type, timestamp, decryptor.audio(encrypt(body))));
      audio++;
    }
    p += 11 + length + 4;
  }
  decryptor.close();
  assert.ok(frames >= 2); assert.ok(audio >= 2);
  const decoded = await new Promise((resolve, reject) => {
    const child = spawn('ffmpeg', ['-v', 'error', '-nostdin', '-i', 'pipe:0', '-f', 'null', '-']);
    const diagnostics = [];
    const timer = setTimeout(() => child.kill(), 10000);
    child.stderr.on('data', data => diagnostics.push(data));
    child.stdout.resume();
    child.on('error', error => { clearTimeout(timer); reject(error); });
    child.on('close', status => { clearTimeout(timer); resolve({ status, stderr: Buffer.concat(diagnostics) }); });
    child.stdin.on('error', reject);
    child.stdin.end(Buffer.concat(decodedTags));
  });
  assert.equal(decoded.status, 0, decoded.stderr.toString());
});
