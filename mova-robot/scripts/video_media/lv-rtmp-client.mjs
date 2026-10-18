// Adapted from @apocaliss92/nodedreame 1.12.4. MIT license: ./LICENSE.
// Upstream: https://github.com/apocaliss92/nodedreame
/**
 * Minimal Node RTMP client that replicates the Aliyun LinkVisual viewer ("private mode" librtmp)
 * exactly as observed on the wire (findings/dreame.pcap, findings/dr3.pcap — client->relay bytes,
 * VERIFIED) and in liblvmedia.so / liblvrtmp.so (VERIFIED by disassembly).
 *
 * Wire sequence (all four written back-to-back, BEFORE any server byte is read — like the app):
 *   C0 = 0x04, C1 = [uptime_ms u32][0 0 0 0][1528 random]           (no C2 is ever sent)
 *   connect      (txn 1, csid 3, msid 0, fmt 0) {app, tcUrl, fpad:false, capabilities:15,
 *                 audioCodecs:3191, videoCodecs:252, videoFunction:1}   — no flashVer, no objectEncoding
 *   play         (txn 2, csid 8, msid 1, fmt 0) null, <playpath>, -1000  — NO createStream (msid fixed = 1)
 *   onStatus     (txn 3, csid 3, fmt 1) null, {level:"status", code:"NetStream.RequestAudioType",
 *                 description:"desc", audio_type:"1"}
 * Then the server answers S0(0x04)+S1(1536) followed directly by RTMP messages (no S2), chunked with an
 * implicit 60000-byte chunk size. After the first server message is processed the app sends
 *   onStatus     (txn 4, csid 3, fmt 1) null, {level:"status", code:"NetStream.Ping.Start", description:"desc"}
 * and repeats it every 30 s (CStreamUnit::ProcessRtmpStream). The client never replies to server onStatus.
 *
 * What is ASSUMED (not observable in the pcaps, which lack the server->client direction): that the
 * server sends onStatus NetStream.Play.Start (librtmp requires it before accepting media), and that this
 * exact sequence — rather than one specific element of it — is what makes the robot publish VIDEO.
 */
import net from 'node:net';
import { EventEmitter } from 'node:events';
import { randomBytes } from 'node:crypto';
import { amfBoolean, amfNull, amfNumber, amfObject, amfString, decodeAmf0, } from './amf0.mjs';
import { ChunkReader, MSG, PRIVATE_MODE_CHUNK_SIZE, encodeMessage, } from './chunk.mjs';
import { DEFAULT_PLAYBACK_CACHE_MS, validPlaybackCacheMs } from './playback.mjs';

export const LV_STATUS = {
  REQUEST_AUDIO_TYPE: 'NetStream.RequestAudioType',
  FORCE_IFRAME_START: 'NetStream.ForceIFrame.Start',
  PING_START: 'NetStream.Ping.Start',
  PING_RESPONSE: 'NetStream.Ping.Response',
  PRE_CONNECT: 'NetStream.PreConnect',
  KEEPALIVE: 'NetStream.Keepalive',
  SERVER_INFO: 'NetStream.ServerInfo',
  TRANSFER_DATA: 'NetStream.TransferData',
  PLAY_START: 'NetStream.Play.Start',
} ;

/** librtmp constants observed in the app's connect (RTMP_Connect1/SendConnectPacket). */
const CONNECT_CAPABILITIES = 15;
const CONNECT_AUDIO_CODECS = 3191;
const CONNECT_VIDEO_CODECS = 252;
const CONNECT_VIDEO_FUNCTION = 1;
const PLAY_START_LIVE = -1000; // RTMP_LF_LIVE -> start = -1000
const HANDSHAKE_C0_PRIVATE = 0x04;
const HANDSHAKE_S0S1_LEN = 1537;
const DEFAULT_PING_INTERVAL_MS = 30_000; // ProcessRtmpStream: 30000
const ACK_THRESHOLD_BYTES = 250_000; // librtmp: m_nClientBW(2500000)/10
const CSID_CONTROL = 2;
const CSID_COMMAND = 3;
const CSID_PLAY = 8;
const PLAY_STREAM_ID = 1; // RTMP_InitPrivateConfig: m_stream_id = 1

/**
 * Split like librtmp/ffmpeg do: playpath = last path segment, app = everything between host and it
 * (query string included). Verified against the app's connect/play in both pcaps.
 */
export function parseRelayUrl(url, cacheDurationMs = DEFAULT_PLAYBACK_CACHE_MS) {
  if (!validPlaybackCacheMs(cacheDurationMs)) throw new Error('Invalid video playback cache');
  const m = /^rtmp:\/\/([^/:]+)(?::(\d+))?\/(.*)$/.exec(url);
  if (!m) throw new Error('Invalid relay URL; expected rtmp://');
  const host = m[1] ;
  const port = m[2] ? Number(m[2]) : 1935;
  const path = m[3] ;
  const slash = path.lastIndexOf('/');
  if (slash < 0) throw new Error('Invalid relay URL; missing stream path');
  let app = path.slice(0, slash);
  const playpath = path.slice(slash + 1);
  // Preserve signed/token URL bytes. Only the explicit playback-cache field
  // changes; a cached descriptor must not silently reinstate the old delay.
  const cacheFields = app.match(/[?&]cacheDuration(?:=[^&]*)?(?=&|$)/g) ?? [];
  if (cacheFields.length > 1) throw new Error('Ambiguous video playback cache');
  if (cacheFields.length) app = app.replace(/([?&])cacheDuration(?:=[^&]*)?(?=&|$)/,
    (_, separator) => `${separator}cacheDuration=${cacheDurationMs}`);
  else app += `${app.includes('?') ? '&' : '?'}cacheDuration=${cacheDurationMs}`;
  return { host, port, app, playpath, tcUrl: `rtmp://${host}:${port}/${app}` };
}

export class LvRtmpClient extends EventEmitter {
                   opts ;
                   endpoint ;
          socket = null;
          handshakeBuf = Buffer.alloc(0);
          handshakeDone = false;
          c1Time = 0;
          reader = new ChunkReader(PRIVATE_MODE_CHUNK_SIZE);
          transactionId = 0;
          bytesIn = 0;
          bytesAcked = 0;
          ackWindow = ACK_THRESHOLD_BYTES;
          pingTimer = null;
          firstMessageSeen = false;
          closed = false;

  constructor(options ) {
    super();
    if (typeof options.decryptAudio !== 'function' || typeof options.decryptVideo !== 'function') {
      throw new Error('Encrypted media transforms are required');
    }
    this.opts = {
      decryptAudio: options.decryptAudio,
      decryptVideo: options.decryptVideo,
      url: options.url,
      cacheDurationMs: options.cacheDurationMs === undefined ? DEFAULT_PLAYBACK_CACHE_MS : options.cacheDurationMs,
      requestAudioType: options.requestAudioType ?? true,
      ping: options.ping ?? true,
      pingIntervalMs: options.pingIntervalMs ?? DEFAULT_PING_INTERVAL_MS,
      forceIFrameOnStart: options.forceIFrameOnStart ?? false,
      connectTimeoutMs: options.connectTimeoutMs ?? 10_000,
    };
    this.endpoint = parseRelayUrl(this.opts.url, this.opts.cacheDurationMs);
  }

  /** Open TCP, write C0+C1 and the pipelined connect/play/RequestAudioType. Resolves once written. */
  connect() {
    return new Promise((resolve, reject) => {
      const sock = net.createConnection({ host: this.endpoint.host, port: this.endpoint.port });
      this.socket = sock;
      sock.setNoDelay(true);
      sock.setTimeout(this.opts.connectTimeoutMs, () => { if (!this.handshakeDone) this.fail(new Error('handshake timeout')); });
      sock.once('error', (err) => { this.emit('error', err); reject(err); });
      sock.on('data', (d) => this.onData(d));
      sock.on('close', () => this.onClose());
      sock.once('connect', () => {
        if (this.closed) return;
        sock.write(Buffer.concat([this.buildC0C1(), ...this.buildOpeningSequence()]));
        this.emit('connected');
        resolve();
      });
    });
  }

  close() {
    this.closed = true;
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.pingTimer = null;
    this.socket?.destroy();
    this.opts.url = '';
    this.endpoint.app = '';
    this.endpoint.tcUrl = '';
    this.endpoint.playpath = '';
    this.handshakeBuf = Buffer.alloc(0);
    this.reader.clear();
  }

  /** onStatus encoder — byte-exact replica of liblvmedia IOT_RTMP_SendMessage (csid 3, fmt 1, ts 0). */
  buildStatus(code , extra ) {
    const pairs = [
      ['level', amfString('status')],
      ['code', amfString(code)],
      ['description', amfString('desc')],
    ];
    if (extra) pairs.push([extra[0], amfString(extra[1])]);
    const body = Buffer.concat([amfString('onStatus'), amfNumber(++this.transactionId), amfNull(), amfObject(pairs)]);
    return encodeMessage({ fmt: 1, csid: CSID_COMMAND, type: MSG.COMMAND_AMF0, body });
  }

  sendStatus(code , extra ) {
    this.write(this.buildStatus(code, extra));
  }

  forceIFrame() {
    this.sendStatus(LV_STATUS.FORCE_IFRAME_START);
  }

  // ---------------------------------------------------------------- outgoing

          buildC0C1() {
    this.c1Time = Math.floor(performance.now()) >>> 0;
    const c1 = Buffer.alloc(1536);
    c1.writeUInt32BE(this.c1Time, 0);
    c1.writeUInt32BE(0, 4);
    randomBytes(1528).copy(c1, 8);
    return Buffer.concat([Buffer.from([HANDSHAKE_C0_PRIVATE]), c1]);
  }

          buildConnect() {
    const obj = amfObject([
      ['app', amfString(this.endpoint.app)],
      ['tcUrl', amfString(this.endpoint.tcUrl)],
      ['fpad', amfBoolean(false)],
      ['capabilities', amfNumber(CONNECT_CAPABILITIES)],
      ['audioCodecs', amfNumber(CONNECT_AUDIO_CODECS)],
      ['videoCodecs', amfNumber(CONNECT_VIDEO_CODECS)],
      ['videoFunction', amfNumber(CONNECT_VIDEO_FUNCTION)],
    ]);
    const body = Buffer.concat([amfString('connect'), amfNumber(++this.transactionId), obj]);
    return encodeMessage({ fmt: 0, csid: CSID_COMMAND, msid: 0, type: MSG.COMMAND_AMF0, body });
  }

          buildPlay() {
    const body = Buffer.concat([
      amfString('play'), amfNumber(++this.transactionId), amfNull(), amfString(this.endpoint.playpath), amfNumber(PLAY_START_LIVE),
    ]);
    return encodeMessage({ fmt: 0, csid: CSID_PLAY, msid: PLAY_STREAM_ID, type: MSG.COMMAND_AMF0, body });
  }

  /** Exposed for the byte-exact self-test against the pcap. */
  buildOpeningSequence() {
    const seq = [this.buildConnect(), this.buildPlay()];
    if (this.opts.requestAudioType) seq.push(this.buildStatus(LV_STATUS.REQUEST_AUDIO_TYPE, ['audio_type', '1']));
    return seq;
  }

          write(buf ) {
    if (!this.socket || this.socket.destroyed) return;
    this.socket.write(buf);
  }

          sendAck() {
    const b = Buffer.alloc(4);
    b.writeUInt32BE(this.bytesIn >>> 0, 0);
    this.write(encodeMessage({ fmt: 0, csid: CSID_CONTROL, msid: 0, type: MSG.ACK, body: b }));
    this.bytesAcked = this.bytesIn;
  }

          sendPingResponse(serverTs ) {
    const body = Buffer.concat([Buffer.from([0, 7]), serverTs]);
    this.write(encodeMessage({ fmt: 0, csid: CSID_CONTROL, msid: 0, type: MSG.USER_CONTROL, body }));
  }

          startPinging() {
    if (!this.opts.ping || this.pingTimer) return;
    this.sendStatus(LV_STATUS.PING_START);
    this.pingTimer = setInterval(() => this.sendStatus(LV_STATUS.PING_START), this.opts.pingIntervalMs);
  }

  // ---------------------------------------------------------------- incoming

          onData(data ) {
    this.bytesIn += data.length;
    let payload = data;
    if (!this.handshakeDone) {
      this.handshakeBuf = Buffer.concat([this.handshakeBuf, data]);
      if (this.handshakeBuf.length < HANDSHAKE_S0S1_LEN) return;
      try {
        this.onHandshake(this.handshakeBuf.subarray(0, HANDSHAKE_S0S1_LEN));
      } catch (err) {
        this.fail(err);
        return;
      }
      payload = this.handshakeBuf.subarray(HANDSHAKE_S0S1_LEN);
      this.handshakeBuf = Buffer.alloc(0);
      this.handshakeDone = true;
      if (payload.length === 0) return;
    }
    let messages ;
    try {
      messages = this.reader.feed(payload);
    } catch (err) {
      this.fail(err instanceof Error ? err : new Error(String(err)));
      return;
    }
    try {
      for (const m of messages) {
        if (this.closed) break;
        this.dispatch(m);
      }
    } catch (err) {
      this.fail(err instanceof Error ? err : new Error('Invalid relay media'));
      return;
    }
    if (this.bytesIn - this.bytesAcked >= this.ackWindow) this.sendAck();
  }

          onHandshake(s0s1 ) {
    const s0 = s0s1[0] ;
    if (s0 !== HANDSHAKE_C0_PRIVATE) throw new Error('Unsupported relay handshake');
    // This implementation accepts only the verified private handshake.

  }

          dispatch(m ) {
    if (!this.firstMessageSeen) {
      this.firstMessageSeen = true;
      this.startPinging(); // ProcessRtmpStream sends Ping.Start right after processing the first packet
    }
    switch (m.type) {
      case MSG.SET_CHUNK_SIZE: {
        const size = m.body.readUInt32BE(0) & 0x7fffffff;
        this.reader.chunkSize = size;
        return;
      }
      case MSG.WINDOW_ACK_SIZE:
        this.ackWindow = Math.min(ACK_THRESHOLD_BYTES, m.body.readUInt32BE(0));
        return;
      case MSG.SET_PEER_BW:
        return;
      case MSG.ACK:
      case MSG.ABORT:
        return;
      case MSG.USER_CONTROL:
        return this.onUserControl(m.body);
      case MSG.COMMAND_AMF0:
        return this.onCommand(m);
      case MSG.DATA_AMF0:
        return this.onDataMessage(m.body);
      case MSG.AUDIO:
        return this.onAudio(m);
      case MSG.VIDEO:
        return this.onVideo(m);
      default:
    }
  }

          onUserControl(body ) {
    const event = body.readUInt16BE(0);
    if (event === 6) this.sendPingResponse(body.subarray(2, 6));
  }

          onCommand(m ) {
    let values ;
    try {
      values = decodeAmf0(m.body);
    } catch (err) {
      return;
    }
    const [name, txn, , info] = values;
    this.emit('command', String(name), values);
    if (name === 'onStatus' && info && typeof info === 'object' && !Array.isArray(info)) {
      const fields = info ;
      this.emit('status', {
        code: String(fields.code ?? ''),
        level: String(fields.level ?? ''),
        description: String(fields.description ?? ''),
        fields,
        transactionId: typeof txn === 'number' ? txn : 0,
      });
      if (fields.code === LV_STATUS.PLAY_START && this.opts.forceIFrameOnStart) this.forceIFrame();
    }
  }

          onDataMessage(body ) {
    try {
      const values = decodeAmf0(body);
      const obj = values.find((v) => v && typeof v === 'object' && !Array.isArray(v));
      if (obj) this.emit('metadata', obj );
    } catch {
    }
  }

  onAudio(m) {
    const body = this.opts.decryptAudio(m.body);
    this.emit('audio', { timestamp: m.timestamp, body });
  }

  onVideo(m) {
    const body = this.opts.decryptVideo(m.body);
    this.emit('video', { timestamp: m.timestamp, body });
  }

  pause() { this.socket?.pause(); }
  resume() { if (!this.closed) this.socket?.resume(); }

          fail(err ) {
    this.emit('error', err);
    this.close();
  }

          onClose() {
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.pingTimer = null;
    this.closed = true;
    this.emit('close');
  }
}
