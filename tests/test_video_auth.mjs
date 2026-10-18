// Offline protocol tests: every fetch is replaced before any helper is called.
import assert from 'node:assert/strict';
import { createHash, createHmac } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import {
  establishVideoIdentity, queryEncryptedVideoStream,
  getUserDeviceMappingRequest, parseUserDeviceMapping, getUserDeviceMapping,
} from '../mova-robot/scripts/video_auth.mjs';

const credentials = { appKey: 'FAKE-APPLICATION-KEY', appSecret: 'FAKE-PRIVATE-APPLICATION-SECRET' };
const input = { ...credentials, authCode: 'FAKE-PRIVATE-MOVA-CODE+&=', region: 'eu' };
const identity = {
  regionId: 'eu-central-1', openId: 'FAKE-PRIVATE-OPEN-ID', iotToken: 'FAKE-PRIVATE-IOT-TOKEN',
  refreshToken: 'FAKE-PRIVATE-REFRESH-TOKEN', identityId: 'FAKE-PRIVATE-IDENTITY-ID',
  expiresAt: Date.now() + 3600000,
};
const oauth = { code: 200, data: { data: { loginSuccessResult: {
  sid: 'FAKE-PRIVATE-OA-SESSION', openAccount: { openId: identity.openId },
} } } };
const session = { code: 200, data: { ...identity, iotTokenExpire: 3600 } };
const encrypted = { code: 200, data: {
  relayUrl: 'rtmps://relay.invalid/live/FAKE-PRIVATE-STREAM?token=FAKE-PRIVATE-URL-TOKEN',
  relayDecryptKey: { key: 'FAKE-PRIVATE-KEY', iv: 'FAKE-PRIVATE-IV' },
} };
const script = fileURLToPath(new URL('../mova-robot/scripts/video_auth.mjs', import.meta.url));
const worker = fileURLToPath(new URL('../mova-robot/scripts/video_worker.mjs', import.meta.url));

function mockFetch(t, responses) {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, options });
    assert.ok(calls.length <= responses.length, 'Unexpected request or retry');
    const next = responses[calls.length - 1];
    if (next instanceof Error) throw next;
    if (typeof next === 'function') return next(url, options);
    return new Response(JSON.stringify(next.json), { status: next.status ?? 200 });
  });
  return calls;
}

function assertPrivate(value) {
  assert.doesNotMatch(JSON.stringify(value), /FAKE-PRIVATE|relay\.invalid|password|response-secret/i);
}

function verifySignature({ url, options }, form) {
  assert.equal(options.method, 'POST');
  assert.equal(options.redirect, 'error');
  assert.ok(options.signal instanceof AbortSignal);
  const headers = options.headers;
  const signedNames = headers['X-Ca-Signature-Headers'].split(',');
  assert.deepEqual(signedNames, Object.keys(headers).filter(name =>
    /^X-Ca-(?:Key|Nonce|Timestamp|Version|Stage)$/.test(name)
  ).sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase())));
  assert.equal(headers['X-Ca-Key'], credentials.appKey);
  assert.match(headers['X-Ca-Nonce'], /^[0-9A-F-]{36}$/);
  assert.match(headers['X-Ca-Timestamp'], /^\d{13}$/);
  assert.ok(Number.isFinite(Date.parse(headers.Date)));
  assert.equal(headers['X-Ca-Signature-Method'], 'HmacSHA1');
  let resource = new URL(url).pathname;
  let md5 = '';
  if (form) {
    const fields = [...new URLSearchParams(options.body).entries()].sort(([a], [b]) => a.localeCompare(b));
    resource += '?' + fields.map(([k, v]) => `${k}=${v}`).join('&');
    assert.equal(headers['Content-MD5'], undefined);
    assert.equal(headers['Content-Type'], 'application/x-www-form-urlencoded; charset=UTF-8');
  } else {
    md5 = createHash('md5').update(options.body).digest('base64');
    assert.equal(headers['Content-MD5'], md5);
    assert.equal(headers['X-Ca-Stage'], 'RELEASE');
    assert.equal(headers['Content-Type'], 'application/json; charset=UTF-8');
  }
  const canonical = [
    'POST', headers.Accept, md5, headers['Content-Type'], headers.Date,
    signedNames.map(k => `${k}:${headers[k]}\n`).join('') + resource,
  ].join('\n');
  assert.equal(headers['X-Ca-Signature'], createHmac('sha1', credentials.appSecret).update(canonical).digest('base64'));
  assert.doesNotMatch(JSON.stringify(headers), /FAKE-PRIVATE/);
}

test('identity exchange signs two region-specific requests and returns private identity only in memory', async t => {
  const calls = mockFetch(t, [{ json: oauth }, { json: session }]);
  const before = Date.now();
  const result = await establishVideoIdentity(input);
  assert.equal(result.ok, true);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls.map(c => c.url), [
    'https://living-account.eu-central-1.aliyuncs.com/api/prd/loginbyoauth.json',
    'https://eu-central-1.api-iot.aliyuncs.com/account/createSessionByAuthCode',
  ]);
  verifySignature(calls[0], true);
  verifySignature(calls[1], false);
  const first = JSON.parse(new URLSearchParams(calls[0].options.body).get('loginByOauthRequest'));
  assert.equal(first.authCode, input.authCode);
  assert.equal(first.oauthPlateform, 23);
  assert.equal(first.oauthAppKey, credentials.appKey);
  assert.doesNotMatch(calls[0].options.body, /FAKE-PRIVATE-APPLICATION-SECRET/);
  const second = JSON.parse(calls[1].options.body);
  assert.deepEqual(second.params.request, {
    appKey: credentials.appKey, authCode: oauth.data.data.loginSuccessResult.sid, accountType: 'OA_SESSION',
  });
  assert.equal(second.request.apiVer, '1.0.4');
  assert.equal(result.identity.openId, identity.openId);
  assert.equal(result.identity.iotToken, identity.iotToken);
  assert.equal(result.identity.refreshToken, identity.refreshToken);
  assert.ok(result.identity.expiresAt >= before + 3600000);
  assert.ok(result.identity.expiresAt <= Date.now() + 3600000);
  const { identity: omitted, ...publicResult } = result;
  assertPrivate(publicResult);
});

test('unsupported regions and malformed credentials fail before a request', async t => {
  const calls = mockFetch(t, []);
  for (const bad of [
    { authCode: '' }, { authCode: true }, { authCode: 'x'.repeat(32769) },
    { appKey: '' }, { appSecret: [] }, { region: 'https://attacker.invalid' },
  ]) await assert.rejects(establishVideoIdentity({ ...input, ...bad }));
  assert.equal(calls.length, 0);
});

test('OAuth error codes, failed HTTP status, or missing sessions never advance to token exchange', async t => {
  for (const response of [
    { json: oauth, status: 403 }, { json: oauth, status: 500 },
    { json: { ...oauth, code: 401 } }, { json: { code: 200, data: {} } },
    { json: { code: 200, data: { data: { loginSuccessResult: { sid: 123 } } } } },
  ]) await t.test(`reject ${response.status ?? response.json.code} ${JSON.stringify(response.json.data).length}`, async t => {
    const calls = mockFetch(t, [response]);
    const result = await establishVideoIdentity(input);
    assert.equal(result.ok, false);
    assert.equal(result.stage, 'loginbyoauth');
    assert.equal(calls.length, 1);
    assert.equal(result.identity, undefined);
    assertPrivate(result);
  });
});

test('IoT exchange refuses HTTP, application, and token-shape failures without retry', async t => {
  for (const response of [
    { json: session, status: 503 }, { json: { ...session, code: 401 } },
    { json: { code: 200, data: { iotToken: 42, message: 'response-secret' } } },
    { json: { code: 200, data: { iotToken: '' } } },
  ]) await t.test(`reject token ${response.status ?? response.json.code} ${JSON.stringify(response.json.data).length}`, async t => {
    const calls = mockFetch(t, [{ json: oauth }, response]);
    const result = await establishVideoIdentity(input);
    assert.equal(result.ok, false);
    assert.equal(result.stage, 'createSessionByAuthCode');
    assert.equal(result.identity, undefined);
    assert.equal(calls.length, 2);
    assertPrivate(result);
  });
});

test('network and non-JSON failures expose no exception body or credential material', async t => {
  for (const response of [new Error('FAKE-PRIVATE-PASSWORD response-secret'), () =>
    new Response('FAKE-PRIVATE-TOKEN response-secret', { status: 502 })]) {
    await t.test(typeof response, async t => {
      const calls = mockFetch(t, [response]);
      const result = await establishVideoIdentity(input);
      assert.equal(result.ok, false);
      assert.equal(calls.length, 1);
      assertPrivate(result);
    });
  }
});

test('caller cancellation is passed to fetch and cannot advance authentication', async t => {
  const controller = new AbortController();
  controller.abort();
  const calls = mockFetch(t, [(url, options) => {
    assert.equal(options.signal.aborted, true);
    throw new DOMException('FAKE-PRIVATE-CANCEL-DETAIL', 'AbortError');
  }]);
  const result = await establishVideoIdentity(input, { signal: controller.signal });
  assert.equal(result.ok, false);
  assert.equal(result.errorType, 'AbortError');
  assert.equal(calls.length, 1);
  assertPrivate(result);
});

test('encrypted relay uses one signed request with mandatory encryption and no credential logs', async t => {
  const calls = mockFetch(t, [{ json: encrypted }]);
  const result = await queryEncryptedVideoStream(identity, credentials, 'FAKE-IOT-DEVICE');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://eu-central-1.api-iot.aliyuncs.com/vision/customer/stream/query');
  verifySignature(calls[0], false);
  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.params.iotId, 'FAKE-IOT-DEVICE');
  assert.equal(body.params.relayEncrypted, true);
  assert.equal(body.request.apiVer, '2.2.0');
  assert.equal(body.request.iotToken, identity.iotToken);
  assert.equal(result.ok, true);
  assert.deepEqual(result.stream, { ...encrypted.data, encrypted: true });
  const { stream: omitted, ...publicResult } = result;
  assertPrivate(publicResult);
});

test('invalid or expired identity cannot make a relay request', async t => {
  const calls = mockFetch(t, []);
  for (const args of [
    [null, credentials, 'device'], [{ ...identity, regionId: 'evil.invalid' }, credentials, 'device'],
    [{ ...identity, iotToken: '' }, credentials, 'device'],
    [{ ...identity, expiresAt: Date.now() - 1 }, credentials, 'device'],
    [identity, null, 'device'], [identity, credentials, ''], [identity, credentials, 123],
  ]) {
    const result = await queryEncryptedVideoStream(...args);
    assert.equal(result.ok, false);
    assertPrivate(result);
  }
  assert.equal(calls.length, 0);
});

test('relay rejects plaintext, missing keys, unsupported URLs, HTTP and API errors', async t => {
  const variants = [
    { json: { code: 200, data: { relayUrl: encrypted.data.relayUrl } }, expected: 'EncryptionMissing' },
    { json: { code: 200, data: { ...encrypted.data, relayDecryptKey: { key: 'fake' } } }, expected: 'EncryptionMissing' },
    { json: { code: 200, data: { ...encrypted.data, relayDecryptKey: { key: '', iv: 'fake' } } }, expected: 'EncryptionMissing' },
    { json: { code: 200, data: { ...encrypted.data, relayUrl: 'file:///FAKE-PRIVATE' } }, expected: 'UnsupportedRelay' },
    { json: { code: 200, data: { ...encrypted.data, relayUrl: 'https://relay.invalid/FAKE-PRIVATE' } }, expected: 'UnsupportedRelay' },
    { json: { code: 200, data: {} }, expected: 'MissingRelay' },
    { json: encrypted, status: 500 }, { json: { ...encrypted, code: 401 } },
    { json: { code: '200', data: encrypted.data } },
  ];
  for (const [index, variant] of variants.entries()) await t.test(`refusal ${index}`, async t => {
    const calls = mockFetch(t, [variant]);
    const result = await queryEncryptedVideoStream(identity, credentials, 'device');
    assert.equal(result.ok, false);
    assert.equal(result.stream, undefined);
    if (variant.expected) assert.equal(result.errorType, variant.expected);
    assert.equal(calls.length, 1);
    assertPrivate(result);
  });
});

test('relay abort propagates through the fetch signal and has no retry or secret diagnostics', async t => {
  const controller = new AbortController();
  const calls = mockFetch(t, [(url, options) => {
    controller.abort();
    assert.equal(options.signal.aborted, true);
    throw new DOMException('FAKE-PRIVATE-RELAY-DETAIL', 'AbortError');
  }]);
  const result = await queryEncryptedVideoStream(identity, credentials, 'device', { signal: controller.signal });
  assert.equal(result.ok, false);
  assert.equal(result.errorType, 'AbortError');
  assert.equal(calls.length, 1);
  assertPrivate(result);
});

test('mapping only queries an existing binding and projects the one needed identifier', async t => {
  const fetchCalls = mockFetch(t, []);
  const expected = { path: '/dreame-user-iot/aliiotcloud/getUserDevice', body: { did: 'device', openId: identity.openId } };
  assert.deepEqual(getUserDeviceMappingRequest('device', identity.openId), expected);
  const cloudCalls = [];
  const result = await getUserDeviceMapping(identity, 'device', async (...args) => {
    cloudCalls.push(args);
    return { code: 200, data: { bind: true, iotId: 'fake-iot-id', secret: 'FAKE-PRIVATE' } };
  });
  assert.deepEqual(cloudCalls, [[expected.path, expected.body]]);
  assert.deepEqual(result, { ok: true, stage: 'getUserDevice', bound: true, device: { iotId: 'fake-iot-id' } });
  assert.equal(fetchCalls.length, 0);
  assertPrivate(result);
});

test('unbound or malformed mapping cannot trigger binding or expose responses', async t => {
  mockFetch(t, []);
  for (const data of [{ bind: false }, { bind: 'true', iotId: 'fake' }, { bind: true }, { bind: true, iotId: 123 }]) {
    const result = parseUserDeviceMapping({ code: 200, data: { ...data, private: 'FAKE-PRIVATE' } });
    assert.equal(result.ok, false);
    assertPrivate(result);
  }
  let called = false;
  assert.equal((await getUserDeviceMapping({}, 'device', () => { called = true; })).ok, false);
  assert.equal(called, false);
  assert.equal((await getUserDeviceMapping(identity, 'device', undefined)).errorType, 'InvalidCloudClient');
  const error = await getUserDeviceMapping(identity, 'device', () => { throw new Error('FAKE-PRIVATE-CLOUD-DETAIL'); });
  assert.equal(error.ok, false);
  assertPrivate(error);
});

// Run the real CLI/worker entry points in a fresh process, but preload a strict
// fake fetch. It records only endpoint counts, never secret request contents.
function runEntry(entry, argv, stdin, responses) {
  const setup = `
    import { pathToFileURL } from 'node:url';
    const responses = ${JSON.stringify(responses)};
    let calls = 0;
    globalThis.fetch = async (url, options) => {
      if (!/^https:\\/\\/(?:living-account\\.)?eu-central-1\\.(?:aliyuncs\\.com|api-iot\\.aliyuncs\\.com)\\//.test(url)) throw new Error('Unexpected endpoint');
      const next = responses[calls++];
      if (!next) throw new Error('Unexpected request');
      return new Response(JSON.stringify(next.json), { status: next.status ?? 200 });
    };
    process.on('exit', () => process.stderr.write('MOCK_REQUEST_COUNT=' + calls + '\\n'));
    process.argv = [process.execPath, ${JSON.stringify(entry)}, ...${JSON.stringify(argv)}];
    await import(pathToFileURL(${JSON.stringify(entry)}).href);
  `;
  const result = spawnSync(process.execPath, ['--input-type=module', '--eval', setup], {
    input: stdin, encoding: 'utf8', timeout: 5000, maxBuffer: 262144,
  });
  assert.equal(result.error, undefined);
  assert.equal(result.signal, null);
  return result;
}

test('auth CLI completes a mocked sign-in but prints only safe metadata and no camera calls', () => {
  const result = runEntry(script, [], JSON.stringify(input), [{ json: oauth }, { json: session }]);
  assert.equal(result.status, 0, result.stderr);
  const output = JSON.parse(result.stdout);
  assert.equal(output.ok, true);
  assert.equal(output.cameraRequests, 0);
  assert.equal(output.identity, undefined);
  assertPrivate(result.stdout + result.stderr);
  assert.match(result.stderr, /MOCK_REQUEST_COUNT=2/);
});

test('auth CLI rejects oversized or malformed input before requests', () => {
  for (const stdin of ['{FAKE-PRIVATE', 'x'.repeat(65537), JSON.stringify({ ...input, authCode: '' })]) {
    const result = runEntry(script, [], stdin, []);
    assert.equal(result.status, 2);
    assert.equal(JSON.parse(result.stdout).ok, false);
    assert.match(result.stderr, /MOCK_REQUEST_COUNT=0/);
    assertPrivate(result.stdout + result.stderr);
  }
});

test('worker validates mode, JSON input, size, and duration without network or media startup', () => {
  for (const [args, stdin, expected] of [
    [['invalid'], '{}', 'VIDEO_WORKER_INVALID_MODE'],
    [['auth', 'extra'], '{}', 'VIDEO_WORKER_INVALID_MODE'],
    [['auth'], '[]', 'VIDEO_WORKER_INVALID_INPUT'],
    [['auth'], '{FAKE-PRIVATE', 'VIDEO_WORKER_FAILED'],
    [['auth'], 'x'.repeat(131073), 'VIDEO_WORKER_INPUT_TOO_LARGE'],
    [['stream'], JSON.stringify({ maxDurationMs: 999 }), 'VIDEO_WORKER_INVALID_DURATION'],
    [['stream'], JSON.stringify({ maxDurationMs: 300001 }), 'VIDEO_WORKER_INVALID_DURATION'],
    [['stream'], JSON.stringify({ maxDurationMs: true }), 'VIDEO_WORKER_INVALID_DURATION'],
    [['stream'], JSON.stringify({ maxDurationMs: '1000' }), 'VIDEO_WORKER_INVALID_DURATION'],
  ]) {
    const result = runEntry(worker, args, stdin, []);
    assert.equal(result.status, 2);
    assert.equal(result.stdout, '');
    assert.match(result.stderr, new RegExp(expected));
    assert.match(result.stderr, /MOCK_REQUEST_COUNT=0/);
    assertPrivate(result.stderr);
  }
});

test('worker refuses a plaintext relay without writing camera data or private URL diagnostics', () => {
  const result = runEntry(worker, ['stream'], JSON.stringify({ identity, credentials, iotId: 'device' }), [
    { json: { code: 200, data: { relayUrl: encrypted.data.relayUrl } } },
  ]);
  assert.equal(result.status, 2);
  assert.equal(result.stdout, '');
  assert.match(result.stderr, /VIDEO_WORKER_STREAM_QUERY_FAILED/);
  assert.match(result.stderr, /MOCK_REQUEST_COUNT=1/);
  assertPrivate(result.stderr);
});
