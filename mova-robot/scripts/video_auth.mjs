// Video authentication and encrypted relay request helpers.
// The command-line entry point performs authentication only, never camera calls.
// Adapted from @apocaliss92/nodedreame 1.12.4 src/video/aliyun identity/signing.
// MIT notice: see video_auth.LICENSE.
// Source: https://www.npmjs.com/package/@apocaliss92/nodedreame/v/1.12.4
// MOVA app credentials remain outside this file, extracted from user-supplied APK.
import { createHash, createHmac, randomUUID } from 'node:crypto';
import { pathToFileURL } from 'node:url';

const REGION_IDS = ['eu-central-1', 'us-east-1', 'cn-shanghai', 'ap-southeast-1'];
const httpOk = status => Number.isInteger(status) && status >= 200 && status < 300;
const responseCode = json => typeof json?.code === 'number' ? json.code : null;
const acceptedCode = code => code === undefined || code === 0 || code === 200;
const validText = (value, max = 32768) => typeof value === 'string' && value.length > 0 && value.length <= max;
const requestSignal = signal => signal
  ? AbortSignal.any([signal, AbortSignal.timeout(20000)]) : AbortSignal.timeout(20000);

function sign(credentials, path, { json, form, stage = false }) {
  const accept = 'application/json; charset=UTF-8';
  const type = json !== undefined ? accept : 'application/x-www-form-urlencoded; charset=UTF-8';
  const body = json !== undefined ? JSON.stringify(json) : Object.entries(form).map(([k,v]) => `${k}=${encodeURIComponent(v)}`).join('&');
  const md5 = json !== undefined ? createHash('md5').update(body).digest('base64') : '';
  const date = new Date().toUTCString();
  const ca = {
    'X-Ca-Key': credentials.appKey,
    'X-Ca-Nonce': randomUUID().toUpperCase(),
    'X-Ca-Timestamp': String(Date.now()),
    'X-Ca-Version': '1',
    ...(stage ? { 'X-Ca-Stage': 'RELEASE' } : {}),
  };
  const names = Object.keys(ca).sort((a,b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  const headerBlock = names.map(k => `${k}:${ca[k]}\n`).join('');
  const resource = form ? `${path}?${Object.keys(form).sort().map(k => `${k}=${form[k]}`).join('&')}` : path;
  const signed = `POST\n${accept}\n${md5}\n${type}\n${date}\n${headerBlock}${resource}`;
  return { body, headers: {
    'Content-Type': type, Accept: accept, Date: date,
    'X-Ca-Signature': createHmac('sha1', credentials.appSecret).update(signed).digest('base64'),
    'X-Ca-Signature-Method': 'HmacSHA1', 'X-Ca-Signature-Headers': names.join(','),
    ...ca, ...(md5 ? { 'Content-MD5': md5 } : {}),
  } };
}

export async function establishVideoIdentity(input, { signal } = {}) {
if (!validText(input.authCode)) throw new Error('Invalid authCode input');
if (!validText(input.appKey, 512) || !validText(input.appSecret, 4096)) throw new Error('Missing application credentials');
const credentials = { appKey: input.appKey, appSecret: input.appSecret };
const regions = { eu: 'eu-central-1', de: 'eu-central-1', us: 'us-east-1', cn: 'cn-shanghai', sg: 'ap-southeast-1' };
const regionId = regions[input.region] ?? input.region;
if (!REGION_IDS.includes(regionId)) throw new Error('Unsupported region');

let stage = 'loginbyoauth';
let status;
try {
  const oauthPath = '/api/prd/loginbyoauth.json';
  const riskControlInfo = {
    createIfNotExist: 'true', sdkVersion: '3.4.0.27', openaccountVersion: '3.4.0.27',
    platformName: 'iOS', netType: 'wifi', brand: 'Apple', USE_H5_NC: 'true',
    deviceId: randomUUID(), locale: 'en-US', platformVersion: '26.5.2',
    platform: 'iPhone', appKey: credentials.appKey, USE_OA_PWD_ENCRYPT: 'true',
    model: 'iPhone16,2', utdid: randomUUID().replaceAll('-', '').slice(0,20), appVersion: '1895',
  };
  const request = JSON.stringify({ riskControlInfo, oauthPlateform: 23, oauthAppKey: credentials.appKey, authCode: input.authCode });
  const response = await fetch(`https://living-account.${regionId}.aliyuncs.com${oauthPath}`, {
    method: 'POST', ...sign(credentials, oauthPath, { form: { loginByOauthRequest: request } }),
    signal: requestSignal(signal), redirect: 'error',
  });
  status = response.status;
  const json = await response.json();
  const oa = json?.data?.data?.loginSuccessResult;
  if (!httpOk(status) || !acceptedCode(json?.code) || !oa || typeof oa.sid !== 'string' || !oa.sid) {
    return { ok: false, stage, httpStatus: status, responseCode: typeof json.code === 'number' ? json.code : null, hasData: !!json.data };
  }
  stage = 'createSessionByAuthCode';
  const path = '/account/createSessionByAuthCode';
  const payload = {
    id: randomUUID().toUpperCase(),
    params: { request: { appKey: credentials.appKey, authCode: oa.sid, accountType: 'OA_SESSION' } },
    request: { apiVer: '1.0.4', language: 'en-US', appKey: credentials.appKey }, version: '1.0.0',
  };
  const sessionResponse = await fetch(`https://${regionId}.api-iot.aliyuncs.com${path}`, {
    method: 'POST', ...sign(credentials, path, { json: payload, stage: true }),
    signal: requestSignal(signal), redirect: 'error',
  });
  status = sessionResponse.status;
  const sessionJson = await sessionResponse.json();
  const data = sessionJson.data;
  const ok = httpOk(status) && [0, 200].includes(sessionJson.code) && typeof data?.iotToken === 'string' && !!data.iotToken;
  return { ok, stage, httpStatus: status, responseCode: typeof sessionJson.code === 'number' ? sessionJson.code : null,
    hasOpenId: !!oa.openAccount?.openId, hasIdentityId: !!data?.identityId,
    // Caller may retain this in memory. CLI below never emits this member.
    ...(ok ? { identity: { regionId, openId: oa.openAccount?.openId ?? '', iotToken: data.iotToken,
      refreshToken: data.refreshToken ?? '', identityId: data.identityId ?? '',
      expiresAt: Date.now() + (data.iotTokenExpire ?? 72000) * 1000 } } : {}) };

} catch (error) {
  // Never print response bodies, auth material, URLs, or raw exception strings.
  return { ok: false, stage, httpStatus: status ?? null, errorType: error?.name ?? 'Error' };
}

}

/**
 * Requests ONE encrypted LinkVisual relay descriptor. This can wake the camera;
 * caller must first authorize and own the device monitor session, then stop it
 * in a finally block. No retries, vendor changes, PIN attempts, or logging occur.
 * Successful `stream` contains private URL/key material: retain only in memory.
 */
export async function queryEncryptedVideoStream(identity, credentials, iotId, { signal } = {}) {
  const stage = 'stream/query';
  if (!identity || !REGION_IDS.includes(identity.regionId) || !validText(identity.iotToken)
      || !credentials || !validText(credentials.appKey, 512) || !validText(credentials.appSecret, 4096)
      || !validText(iotId, 512)) {
    return { ok: false, stage, errorType: 'InvalidInput' };
  }
  if (typeof identity.expiresAt === 'number' && identity.expiresAt <= Date.now()) {
    return { ok: false, stage, errorType: 'ExpiredIdentity' };
  }
  const path = '/vision/customer/stream/query';
  const payload = {
    id: randomUUID().toUpperCase(),
    params: {
      streamType: 0, needDomainName: true, relayEncryptType: 0, clientType: 'iOS', iotId,
      enablePortPredict: true, enableWebSocket: true, forceIFrame: true, cacheDuration: 3000,
      // Mandatory; plaintext relay fallback is deliberately unavailable.
      relayEncrypted: true,
    },
    request: { language: 'en-US', appKey: credentials.appKey, iotToken: identity.iotToken, apiVer: '2.2.0' },
    version: '1.0.0',
  };
  let status;
  try {
    const response = await fetch(`https://${identity.regionId}.api-iot.aliyuncs.com${path}`, {
      method: 'POST', ...sign(credentials, path, { json: payload, stage: true }),
      signal: requestSignal(signal), redirect: 'error',
    });
    status = response.status;
    const json = await response.json();
    const code = responseCode(json);
    if (!httpOk(status) || ![0, 200].includes(code)) {
      return { ok: false, stage, httpStatus: status, responseCode: code };
    }
    const data = json.data;
    if (!validText(data?.relayUrl, 16384)) {
      return { ok: false, stage, httpStatus: status, responseCode: code, errorType: 'MissingRelay' };
    }
    const relay = new URL(data.relayUrl);
    if (!['rtmp:', 'rtmps:'].includes(relay.protocol) || !relay.hostname) {
      return { ok: false, stage, httpStatus: status, responseCode: code, errorType: 'UnsupportedRelay' };
    }
    const key = data.relayDecryptKey;
    if (!validText(key?.key, 4096) || !validText(key?.iv, 4096)) {
      return { ok: false, stage, httpStatus: status, responseCode: code, errorType: 'EncryptionMissing' };
    }
    return { ok: true, stage, httpStatus: status, responseCode: code, stream: {
      relayUrl: data.relayUrl, relayDecryptKey: { key: key.key, iv: key.iv }, encrypted: true,
    } };
  } catch (error) {
    return { ok: false, stage, httpStatus: status ?? null, errorType: error?.name ?? 'Error' };
  }
}

/** The APK's read-only existing-binding lookup; use the established MOVA client. */
export function getUserDeviceMappingRequest(did, openId) {
  if (!validText(did, 512) || !validText(openId, 512)) throw new Error('Invalid mapping identifiers');
  return { path: '/dreame-user-iot/aliiotcloud/getUserDevice', body: { did, openId } };
}

/** Never binds, re-pairs, migrates a region, or exposes the raw server response. */
export function parseUserDeviceMapping(json) {
  const stage = 'getUserDevice';
  const code = responseCode(json);
  if (![0, 200].includes(code)) return { ok: false, stage, responseCode: code };
  const data = json?.data;
  if (typeof data?.bind !== 'boolean') return { ok: false, stage, errorType: 'InvalidMappingResponse' };
  if (!data.bind) return { ok: false, stage, bound: false, errorType: 'DeviceNotBound' };
  if (!validText(data.iotId, 512)) return { ok: false, stage, bound: true, errorType: 'MissingIotId' };
  return { ok: true, stage, bound: true, device: { iotId: data.iotId } };
}

/** `cloudPost(path, body)` must use the caller's existing authenticated MOVA API. */
export async function getUserDeviceMapping(identity, did, cloudPost) {
  const stage = 'getUserDevice';
  try {
    const request = getUserDeviceMappingRequest(did, identity?.openId);
    if (typeof cloudPost !== 'function') return { ok: false, stage, errorType: 'InvalidCloudClient' };
    return parseUserDeviceMapping(await cloudPost(request.path, request.body));
  } catch (error) {
    return { ok: false, stage, errorType: error?.name ?? 'Error' };
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const chunks = [];
    let count = 0;
    for await (const chunk of process.stdin) {
      count += chunk.length;
      if (count > 65536) throw new Error('Input too large');
      chunks.push(chunk);
    }
    const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const result = await establishVideoIdentity(input);
    const { identity, ...publicResult } = result;
    console.log(JSON.stringify({ ...publicResult, cameraRequests: 0 }));
    if (!result.ok) process.exitCode = 2;
  } catch (error) {
    console.log(JSON.stringify({ ok: false, stage: 'input', errorType: error?.name ?? 'Error', cameraRequests: 0 }));
    process.exitCode = 2;
  }
}
