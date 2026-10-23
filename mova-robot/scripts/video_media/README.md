# Encrypted MOVA video receiver

This internal Node.js 20+ module receives an already-authorized LinkVisual relay
descriptor and writes decrypted FLV into a local `Writable`, such as an ffmpeg
process's standard input. It uses Node builtins only. It does not obtain logins,
activate a camera, alter its PIN, or save recordings.

```js
import { receiveEncryptedFlv } from './video_media/index.mjs';

await receiveEncryptedFlv({
  url: stream.relayUrl,
  relayDecryptKey: stream.relayDecryptKey,
  output: ffmpeg.stdin,
  signal: abortController.signal,
  maxDurationMs: 60000,
});
```

The caller must request `relayEncrypted: true`, keep the descriptor in memory,
and stop the robot's camera session in its own cleanup. Do not pass URLs or keys
on a command line. Both Base64 values must decode to 16 bytes. Missing keys,
unsupported codecs and invalid ciphertext fail without a plaintext fallback.
For a worker writing binary FLV to `process.stdout`, use `endOutput: false`.

The promise returns `{videoFrames, audioFrames, endedBy}` when cancelled, when the
duration expires, or when the peer closes. Cancellation before a frame is allowed;
other normal endings without a video frame raise `VIDEO_MEDIA_EMPTY`. Other
errors have sanitized `VIDEO_MEDIA_*` codes and contain no relay URL or key.
The defaults are 60 seconds total, 10 seconds to connect and 15 seconds without
media. The maximum total duration is five minutes. Backpressure pauses the
socket, and bounded input/output queues prevent unlimited buffering.

## Encryption and limits

This matches the native app's **selective media encryption over RTMP**. It is not
TLS or authenticated encryption: RTMP commands, relay tokens, video headers and
interframes are outside the recovered AES protection. No RTMPS endpoint has
been verified, and the APK's RTMP library rejects TLS connections. The receiver
does not change a relay URL to another protocol.

- AES-128-CBC uses a fresh copy of the supplied IV for every message.
- H.264 configuration messages (`0x17, 0`) preserve five FLV header bytes and
  decrypt the remaining bytes. As in the native receiver, the configuration
  parser follows NAL lengths and retains trailing padding.
- H.264 keyframes (`0x17, 1`) preserve the same header, decrypt the remainder and
  remove the number of bytes specified by the final plaintext byte.
- Audio decrypts the entire message body, including its FLV header, and removes
  that final-byte-defined padding. Bounds and supported audio formats are checked.
- Interframes remain unchanged. H.265 is rejected explicitly.

The native receiver's padding-length convention is reproduced with bounds checks;
the inspected code does not validate repeated padding bytes or authenticate a
frame. Offline tests verify framing and synthetic ciphertext, not successful
playback from a real device. Do not claim a live stream has been verified until
actual frames have been received.

## Sources and verification

`amf0.mjs`, `chunk.mjs` and `lv-rtmp-client.mjs` are adapted from
[@apocaliss92/nodedreame 1.12.4](https://www.npmjs.com/package/@apocaliss92/nodedreame/v/1.12.4)
under the accompanying [MIT license](LICENSE). Local changes require encrypted
transforms, remove diagnostic logging and intercom transmission, add bounded
framing, and fix fragmented-header timestamp handling and chunk-size changes
within a single network read. Upstream packet-capture claims in code comments
describe upstream evidence; those captures are not redistributed here.

The AES framing was independently recovered from MOVAhome 1.6.7.609's ARM
`liblvmedia.so`, SHA-256
`ba2624ee517f59129933f44d31534f1e67daf42fa4b993f845cee2d0071947a9`:

- `LVLivePlayerImpl::parseStreamQueryResponse`, `0x147a68–0x147aae`: Base64 key/IV.
- `CStreamUnit::setIVandKey`, `0x174850`: AES key size 128.
- `CStreamUnit::decryptCbc`, `0x1748a4`: CBC and padding-length handling.
- `ProcessRtmpVideoFrame`, `0x17bf7c`: selective video decryption.
- `ProcessRtmpAudioFrame`, `0x17d0b0`: full audio-body decryption.

No APK, native library, vendor source, live credentials or personal media is
included. Aliyun's [security documentation](https://help.aliyun.com/en/document_detail/2546058.html)
recommends encrypted relay playback and HTTPS for API requests.

Run offline tests from the repository root:

```text
node tests/test_video_media.mjs
```
