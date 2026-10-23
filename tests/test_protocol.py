"""Offline client-identity and correlated-reply tests; no account/network access."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mova-robot/scripts'))
import camera
import mova
import protocol


class ClientIdentityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name)
        config_patch = patch.object(mova, 'CONFIG', self.config)
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def test_identity_persists_with_private_permissions_and_preserves_login(self):
        mova.save_session({'region': 'eu', 'refresh_token': 'OFFLINE-FAKE'})
        original_session = (self.config / 'session.json').read_bytes()
        with patch.object(mova.secrets, 'token_hex', return_value='01a2ff') as random:
            first = mova.load_client_id()
            second = mova.load_client_id()
        self.assertEqual((first, second), ('A01a2ff', 'A01a2ff'))
        random.assert_called_once_with(3)
        self.assertEqual((self.config / 'client-id').read_bytes(), b'A01a2ff\n')
        self.assertEqual(stat.S_IMODE((self.config / 'client-id').stat().st_mode), 0o600)
        self.assertEqual((self.config / 'session.json').read_bytes(), original_session)
        self.assertEqual(list(self.config.glob('.client-id-*')), [])

    def test_invalid_existing_identity_is_never_replaced(self):
        target = self.config / 'client-id'
        for value in (b'', b'Aabcdefx', b'AABCDEF', b'Iabcdef', b'A12345', b'Aabcdef\n\n', b'Aabcdef\x00', b'PRIVATE\xff'):
            target.write_bytes(value)
            target.chmod(0o600)
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'invalid') as raised:
                mova.load_client_id()
            self.assertNotIn('PRIVATE', str(raised.exception))
            self.assertEqual(target.read_bytes(), value)

    def test_public_permissions_are_rejected_without_changing_file(self):
        target = self.config / 'client-id'
        target.write_text('Aabcdef\n')
        target.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'owner-only'):
            mova.load_client_id()
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertEqual(target.read_text(), 'Aabcdef\n')

    def test_symlink_is_rejected_without_reading_or_overwriting_target(self):
        original = self.config / 'unrelated'
        original.write_text('OFFLINE-PRIVATE')
        (self.config / 'client-id').symlink_to(original)
        with self.assertRaisesRegex(RuntimeError, 'safely') as raised:
            mova.load_client_id()
        self.assertNotIn('OFFLINE-PRIVATE', str(raised.exception))
        self.assertEqual(original.read_text(), 'OFFLINE-PRIVATE')

    def test_failed_atomic_replace_leaves_no_partial_identifier(self):
        with patch.object(mova.os, 'replace', side_effect=OSError('offline failure')):
            with self.assertRaises(OSError):
                mova.load_client_id()
        self.assertFalse((self.config / 'client-id').exists())
        self.assertEqual(list(self.config.glob('.client-id-*')), [])

    def test_cli_and_camera_reuse_the_same_persistent_identity(self):
        mova.save_session({'region': 'eu', 'refresh_token': 'OFFLINE-FAKE'})
        cloud = Mock()
        cloud._secondary_key = 'OFFLINE-FAKE'
        cloud._logged_in = True
        cloud._key_expire = time.time() + 3600
        cloud._did = None
        cloud._strings = ['unused'] * 60
        cloud._strings[8] = 'uid'
        cloud._strings[9] = 'bindDomain'
        cloud.login.return_value = True
        cloud.get_devices.return_value = {'page': {'records': [{
            'did': 'offline-device', 'model': 'mova.vacuum.r9540h',
            'uid': 'offline-user', 'bindDomain': 'eu.example.invalid',
        }]}}
        with patch.object(mova, 'DreameVacuumDreameHomeCloudProtocol', return_value=cloud) as ctor:
            with patch.object(sys, 'argv', ['mova.py', 'devices']), contextlib.redirect_stdout(io.StringIO()):
                mova.main()
                mova.main()
            access = camera.CloudAccess()
            with access.transaction() as selected:
                self.assertIs(selected, cloud)
            access.close()
        identities = [call.kwargs['client_id'] for call in ctor.call_args_list]
        self.assertEqual(len(identities), 3)
        self.assertEqual(len(set(identities)), 1)
        self.assertRegex(identities[0], r'^A[0-9a-f]{6}$')
        self.assertEqual(mova.load_client_id(), identities[0])


class ProtocolEnvelopeTests(unittest.TestCase):
    def setUp(self):
        session_patch = patch.object(protocol.requests, 'session', return_value=Mock())
        session_patch.start()
        self.addCleanup(session_patch.stop)
        self.cloud = protocol.DreameVacuumDreameHomeCloudProtocol(
            None, None, 'mova', 'eu', client_id='A01a2ff', did='offline-device')
        self.cloud._strings = ['unused'] * 60
        self.cloud._strings[37] = 'dreame-iot-com'
        self.cloud._strings[27] = 'device'
        self.cloud._strings[38] = 'sendCommand'
        self.cloud._host = 'eu.example.invalid'
        self.cloud._id = 400
        self.cloud._api_call = Mock(side_effect=lambda path, body, retries: {
            'code': 0, 'success': True, 'data': {'id': body['id'], 'result': [{'code': 0}]},
        })

    def test_identity_validation_and_sdk_message_id_seed(self):
        for client_id in ('', 'I123456', 'AABCDEF', 'A1234567', True, 12):
            with self.subTest(client_id=client_id), self.assertRaises(ValueError):
                protocol.DreameVacuumDreameHomeCloudProtocol(None, None, client_id=client_id)
        with patch.object(protocol.random, 'randint', return_value=101) as seed:
            cloud = protocol.DreameVacuumDreameHomeCloudProtocol(None, None)
        seed.assert_called_once_with(101, 10100)
        self.assertEqual(cloud._id, 101)
        self.assertRegex(cloud._client_id, r'^A[0-9a-f]{6}$')

    def test_all_commands_include_stable_client_id_with_unmodified_parameters(self):
        for method, params in (
            ('get_properties', [{'did': 'offline-device', 'siid': 2, 'piid': 1}]),
            ('set_properties', [{'did': 'offline-device', 'siid': 4, 'piid': 15,
                                 'value': '{"spdv":0,"spdw":45,"audio":"true"}'}]),
            ('set_properties', [{'did': 'offline-device', 'siid': 4, 'piid': 15,
                                 'value': '{"spdv":0,"spdw":0,"audio":"false"}'}]),
            ('action', {'did': 'offline-device', 'siid': 10001, 'aiid': 1, 'in': []}),
        ):
            original = json.dumps(params)
            self.assertEqual(self.cloud.send(method, params, 0), [{'code': 0}])
            path, body, retries = self.cloud._api_call.call_args.args
            self.assertEqual(path, 'dreame-iot-com-eu/device/sendCommand')
            self.assertEqual(retries, 0)
            self.assertEqual(body['data']['from'], 'A01a2ff')
            self.assertEqual(body['did'], 'offline-device')
            self.assertEqual(body['data']['did'], body['did'])
            self.assertEqual(body['data']['id'], body['id'])
            self.assertEqual(body['data']['method'], method)
            self.assertIs(body['data']['params'], params)
            self.assertEqual(json.dumps(params), original)
        self.assertEqual([c.args[1]['id'] for c in self.cloud._api_call.call_args_list], [401, 402, 403, 404])

    def test_unbound_host_uses_default_endpoint(self):
        self.cloud._host = None
        self.cloud.send('get_properties', [], 0)
        self.assertEqual(self.cloud._api_call.call_args.args[0], 'dreame-iot-com/device/sendCommand')

    def test_rejects_failed_malformed_or_uncorrelated_envelopes(self):
        valid_data = {'id': 401, 'result': [{'code': 0}]}
        cases = [None, [], 'OFFLINE-PRIVATE', {},
                 {'data': valid_data},
                 *({'code': code, 'data': valid_data} for code in (False, True, 0.0, '0', 1, -1)),
                 *({'code': 0, 'success': value, 'data': valid_data} for value in (False, None, 0, 'true')),
                 {'code': 0, 'error': 'OFFLINE-PRIVATE', 'data': valid_data},
                 *({'code': 0, 'data': value} for value in (None, [], {}, {'id': 401})),
                 {'code': 0, 'data': {**valid_data, 'error': None}},
                 {'code': 0, 'data': {**valid_data, 'error': {'code': 1}}},
                 *({'code': 0, 'data': {**valid_data, 'code': value}} for value in (-1, 1, False, 0.0, '0')),
                 *({'code': 0, 'data': {**valid_data, 'success': value}} for value in (False, None, 0, 'true')),
                 *({'code': 0, 'data': {**valid_data, 'id': value}} for value in (400, 402, '401', 401.0, True)),
                 *({'code': 0, 'id': value, 'data': valid_data} for value in (402, '401', True)),
                 ]
        self.cloud._api_call.side_effect = None
        with patch.object(protocol, '_LOGGER') as logger:
            for response in cases:
                self.cloud._id = 400
                self.cloud._api_call.return_value = response
                with self.subTest(response=response):
                    self.assertIsNone(self.cloud.send('set_properties', [], 0))
            logger.error.assert_not_called()
            logger.warning.assert_not_called()

    def test_accepts_optional_absent_ids_and_success_or_correlated_ids(self):
        result = {'code': 0, 'out': []}
        cases = [
            {'code': 0, 'data': {'result': result}},
            {'code': 0, 'success': True, 'data': {'id': 401, 'result': result}},
            {'code': 0, 'id': 401, 'data': {'id': 401, 'result': result}},
            {'code': 0, 'data': {'id': 401, 'code': 0, 'success': True, 'result': result}},
        ]
        self.cloud._api_call.side_effect = None
        for response in cases:
            self.cloud._id = 400
            self.cloud._api_call.return_value = response
            with self.subTest(response=response):
                self.assertIs(self.cloud.send('action', {}, 0), result)

    def test_failed_transport_consumes_id_without_retry(self):
        self.cloud._api_call.side_effect = TimeoutError('offline timeout')
        with self.assertRaises(TimeoutError):
            self.cloud.send('set_properties', [], 0)
        self.cloud._api_call.assert_called_once()
        self.assertEqual(self.cloud._id, 401)
        self.assertEqual(self.cloud._api_call.call_args.args[2], 0)


if __name__ == '__main__':
    unittest.main()
