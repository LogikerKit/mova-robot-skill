// Adapted from @apocaliss92/nodedreame 1.12.4. MIT license: ./LICENSE.
// Upstream: https://github.com/apocaliss92/nodedreame
/**
 * RTMP chunk-stream framing (encoder + incremental reader).
 *
 * LinkVisual "private mode" facts (liblvrtmp.so RTMP_InitPrivateConfig, VERIFIED by disassembly):
 *   m_inChunkSize = m_outChunkSize = 60000, m_stream_id = 1 — and NO SetChunkSize is exchanged.
 *   Hence: we send every message as a single chunk (all control messages are < 1 KB) and we must
 *   parse inbound chunks with a 60000-byte chunk size from the start (a 128 default would desync).
 */

export const MSG = {
  SET_CHUNK_SIZE: 1,
  ABORT: 2,
  ACK: 3,
  USER_CONTROL: 4,
  WINDOW_ACK_SIZE: 5,
  SET_PEER_BW: 6,
  AUDIO: 8,
  VIDEO: 9,
  DATA_AMF0: 18,
  COMMAND_AMF0: 20,
} ;

export const PRIVATE_MODE_CHUNK_SIZE = 60000;

function basicHeader(fmt , csid ) {
  if (csid < 64) return Buffer.from([(fmt << 6) | csid]);
  if (csid < 320) return Buffer.from([fmt << 6, csid - 64]);
  return Buffer.from([(fmt << 6) | 1, (csid - 64) & 0xff, (csid - 64) >> 8]);
}

/** Encode one message as ONE chunk (no splitting) — exactly what the LinkVisual SDK does on the wire. */
export function encodeMessage(o ) {
  if (o.body.length > PRIVATE_MODE_CHUNK_SIZE) {
    throw new Error(`message body ${o.body.length} exceeds private-mode chunk size ${PRIVATE_MODE_CHUNK_SIZE}`);
  }
  const ts = o.timestamp ?? 0;
  const hdrLen = o.fmt === 0 ? 11 : 7;
  const hdr = Buffer.alloc(hdrLen);
  hdr.writeUIntBE(ts >= 0xffffff ? 0xffffff : ts, 0, 3);
  hdr.writeUIntBE(o.body.length, 3, 3);
  hdr[6] = o.type;
  if (o.fmt === 0) hdr.writeUInt32LE(o.msid ?? 0, 7);
  const ext = ts >= 0xffffff ? (() => { const e = Buffer.alloc(4); e.writeUInt32BE(ts, 0); return e; })() : Buffer.alloc(0);
  return Buffer.concat([basicHeader(o.fmt, o.csid), hdr, ext, o.body]);
}

function freshState() {
  return { timestamp: 0, delta: 0, length: 0, type: 0, msid: 0, extTs: false, partial: [], received: 0 };
}

/**
 * Incremental chunk reader. Feed raw TCP bytes; get complete messages back.
 * Handles fmt 0..3, 1/2/3-byte basic headers, extended timestamps (also on fmt-3
 * continuations, per spec/librtmp), and dynamic chunk-size changes.
 */
export class ChunkReader {
  constructor(chunkSize = PRIVATE_MODE_CHUNK_SIZE) {
    this.chunkSize = chunkSize;
    this.pending = Buffer.alloc(0);
    this.streams = new Map();
    this.bufferedBytes = 0;
  }

  clear() {
    this.pending = Buffer.alloc(0);
    this.streams.clear();
    this.bufferedBytes = 0;
  }

  feed(data) {
    if (!Buffer.isBuffer(data) || this.bufferedBytes + this.pending.length + data.length > 16 * 1024 * 1024) {
      throw new Error('RTMP receive buffer limit exceeded');
    }
    this.pending = this.pending.length ? Buffer.concat([this.pending, data]) : data;
    const out = [];
    for (;;) {
      const consumed = this.tryReadChunk(out);
      if (consumed === 0) break;
      this.pending = this.pending.subarray(consumed);
    }
    return out;
  }

  tryReadChunk(out) {
    if (!Number.isInteger(this.chunkSize) || this.chunkSize < 1 || this.chunkSize > 8 * 1024 * 1024) {
      throw new Error('Invalid RTMP chunk size');
    }
    const buf = this.pending;
    if (!buf.length) return 0;
    const fmt = buf[0] >> 6;
    let csid = buf[0] & 0x3f;
    let p = 1;
    if (csid === 0) {
      if (buf.length < 2) return 0;
      csid = 64 + buf[1]; p = 2;
    } else if (csid === 1) {
      if (buf.length < 3) return 0;
      csid = 64 + buf[1] + buf[2] * 256; p = 3;
    }
    const prior = this.streams.get(csid);
    if ((!prior && fmt !== 0) || (prior?.received && fmt !== 3)) {
      throw new Error('Invalid RTMP chunk continuation');
    }
    if (!prior && this.streams.size >= 128) throw new Error('Too many RTMP chunk streams');
    // Do not mutate committed state until all bytes of this chunk are present.
    // In particular, a split fmt-1 header must not add its time delta twice.
    const st = prior ? { ...prior } : freshState();
    const hdrLen = fmt === 0 ? 11 : fmt === 1 ? 7 : fmt === 2 ? 3 : 0;
    if (buf.length < p + hdrLen) return 0;
    let tsField = 0;
    if (fmt <= 2) {
      tsField = buf.readUIntBE(p, 3);
      if (fmt <= 1) { st.length = buf.readUIntBE(p + 3, 3); st.type = buf[p + 6]; }
      if (fmt === 0) st.msid = buf.readUInt32LE(p + 7);
      st.extTs = tsField === 0xffffff;
    }
    if (st.length > 8 * 1024 * 1024) throw new Error('RTMP message limit exceeded');
    p += hdrLen;
    if (st.extTs) {
      if (buf.length < p + 4) return 0;
      tsField = buf.readUInt32BE(p); p += 4;
    }
    if (st.received === 0) {
      if (fmt === 0) { st.timestamp = tsField; st.delta = 0; }
      else if (fmt <= 2) { st.delta = tsField; st.timestamp += tsField; }
      else st.timestamp += st.delta;
    }
    const take = Math.min(this.chunkSize, st.length - st.received);
    if (buf.length < p + take) return 0;
    st.partial = [...st.partial, Buffer.from(buf.subarray(p, p + take))];
    st.received += take;
    this.bufferedBytes += take;
    if (st.received >= st.length) {
      const message = { csid, msid: st.msid, timestamp: st.timestamp, type: st.type, body: Buffer.concat(st.partial) };
      out.push(message);
      this.bufferedBytes -= st.received;
      st.partial = []; st.received = 0;
      // Apply framing controls during parsing: the following chunk can be in
      // this same TCP read, before the caller dispatches any returned messages.
      if (st.type === MSG.SET_CHUNK_SIZE) {
        if (message.body.length !== 4) throw new Error('Invalid RTMP chunk-size message');
        const size = message.body.readUInt32BE(0);
        if (size < 1 || size > 8 * 1024 * 1024) throw new Error('Invalid RTMP chunk size');
        this.chunkSize = size;
      } else if (st.type === MSG.ABORT) {
        if (message.body.length !== 4) throw new Error('Invalid RTMP abort message');
        const aborted = this.streams.get(message.body.readUInt32BE(0));
        if (aborted) {
          this.bufferedBytes -= aborted.received;
          aborted.partial = []; aborted.received = 0;
        }
      }
    }
    this.streams.set(csid, st);
    return p + take;
  }
}
