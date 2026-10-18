// Adapted from @apocaliss92/nodedreame 1.12.4. MIT license: ./LICENSE.
// Upstream: https://github.com/apocaliss92/nodedreame
/**
 * Minimal AMF0 encoder/decoder — only the types LinkVisual's librtmp uses
 * (number, boolean, string, object, null, ecma-array, strict-array, long string).
 * Erasable-syntax TypeScript: runs directly under Node >= 22.6 (`node file.ts`).
 */

export const AMF0 = {
  NUMBER: 0x00,
  BOOLEAN: 0x01,
  STRING: 0x02,
  OBJECT: 0x03,
  NULL: 0x05,
  UNDEFINED: 0x06,
  ECMA_ARRAY: 0x08,
  OBJECT_END: 0x09,
  STRICT_ARRAY: 0x0a,
  LONG_STRING: 0x0c,
} ;

export function amfNumber(n ) {
  const b = Buffer.alloc(9);
  b[0] = AMF0.NUMBER;
  b.writeDoubleBE(n, 1);
  return b;
}

export function amfBoolean(v ) {
  return Buffer.from([AMF0.BOOLEAN, v ? 1 : 0]);
}

export function amfString(s ) {
  const str = Buffer.from(s, 'utf8');
  const b = Buffer.alloc(3 + str.length);
  b[0] = AMF0.STRING;
  b.writeUInt16BE(str.length, 1);
  str.copy(b, 3);
  return b;
}

export function amfNull() {
  return Buffer.from([AMF0.NULL]);
}

/** Named property: u16 key length + key + encoded value. Order is preserved (matters for byte-exactness). */
function amfNamed(key , value ) {
  const k = Buffer.from(key, 'utf8');
  const b = Buffer.alloc(2 + k.length);
  b.writeUInt16BE(k.length, 0);
  k.copy(b, 2);
  return Buffer.concat([b, value]);
}

/** AMF0 object from an ordered list of [key, encodedValue] pairs. */
export function amfObject(pairs ) {
  const parts = [Buffer.from([AMF0.OBJECT])];
  for (const [k, v] of pairs) parts.push(amfNamed(k, v));
  parts.push(Buffer.from([0x00, 0x00, AMF0.OBJECT_END]));
  return Buffer.concat(parts);
}

function decodeOne(buf , pos ) {
  const type = buf[pos];
  let p = pos + 1;
  switch (type) {
    case AMF0.NUMBER:
      return { value: buf.readDoubleBE(p), next: p + 8 };
    case AMF0.BOOLEAN:
      return { value: buf[p] !== 0, next: p + 1 };
    case AMF0.STRING: {
      const len = buf.readUInt16BE(p);
      return { value: buf.toString('utf8', p + 2, p + 2 + len), next: p + 2 + len };
    }
    case AMF0.LONG_STRING: {
      const len = buf.readUInt32BE(p);
      return { value: buf.toString('utf8', p + 4, p + 4 + len), next: p + 4 + len };
    }
    case AMF0.NULL:
      return { value: null, next: p };
    case AMF0.UNDEFINED:
      return { value: undefined, next: p };
    case AMF0.ECMA_ARRAY:
    case AMF0.OBJECT: {
      if (type === AMF0.ECMA_ARRAY) p += 4; // associative count, then same as object
      const obj = {};
      for (;;) {
        const klen = buf.readUInt16BE(p);
        if (klen === 0 && buf[p + 2] === AMF0.OBJECT_END) return { value: obj, next: p + 3 };
        const key = buf.toString('utf8', p + 2, p + 2 + klen);
        const r = decodeOne(buf, p + 2 + klen);
        obj[key] = r.value;
        p = r.next;
      }
    }
    case AMF0.STRICT_ARRAY: {
      const count = buf.readUInt32BE(p);
      p += 4;
      const arr = [];
      for (let i = 0; i < count; i++) {
        const r = decodeOne(buf, p);
        arr.push(r.value);
        p = r.next;
      }
      return { value: arr, next: p };
    }
    default:
      throw new Error(`AMF0: unsupported type 0x${(type ?? -1).toString(16)} at ${pos}`);
  }
}

/** Decode a whole message body into its top-level values (command name, txid, args...). */
export function decodeAmf0(buf ) {
  const out = [];
  let pos = 0;
  while (pos < buf.length) {
    const r = decodeOne(buf, pos);
    out.push(r.value);
    pos = r.next;
  }
  return out;
}
