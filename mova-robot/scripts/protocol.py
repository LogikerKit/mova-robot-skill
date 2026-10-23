# Derived from ChuckleDucks/dreame-vacuum_z60; see LICENSE.
from __future__ import annotations
import logging, random, hashlib, json, base64, zlib, queue, time, re, secrets
import requests
from typing import Final, Any
_LOGGER = logging.getLogger(__name__)
DREAME_STRINGS: Final = 'H4sICAAAAAAEAGNsb3VkX3N0cmluZ3MuanNvbgCFU9tuGjEQ/RUUKaiVysIuiKWKeKAgBK2qbbikIVWFBtvLuvFla5sQ+vUd26Q0fek+2HPmPsc7364Srl1CDQPJEsdIdfXuKu1m/RzvxegkNuPrUi4/1fn16ieqJsFxu5RgXKUla2dJmrxvvOFfKq3YTYMXy0Y6SHpJetNYEhCs3U06nbc+KRl0dt18kO+yrN/P8/6OUNIbpGU3p4MSetkO0g50O1AOUujTspVi0AewnDQeZh/F5msq7rPpkcw2z8Xj9PZeLrLbWT1ap9OHO1UVxWQ/xIBO+FCwDtzBoiDBOmbWnKK844pOtASuENRG18y4E4pIwZx6lQBXaiOH82LZtATtQxCiuTeg3NadEKKTYaVhtto6/chU8xXy5hqsPWpDmwfLjAIZYpov2gBCJiBEH5RD3I7st+DgqrYOZ8iGJnRi1sbkl9J/MHuuOSq2PBr3XHvBMeX7DROfU/teWjhlnNUjT0VEkamLhYIDH8meOGEoCG7dXebtqtTRUKzG8wi0I14KcxuQ9uwf8N5HS02ZCN0RHN9eWsJqLaKlfyqm6FhLCS8dlVz4UH95As9v5WM9c3hVWEIb/3J75iah0Uns2v6DJ7HYCElFtPY0jPZMuYtpIQgC76AN/wUucrgKHLbCXzHWyjEEq1gc6lpwEhzbP2zwrrd4bM/t6KMSGujaiKgorE3pK23cOamfoOU3Lok0fEb812KlSZb0/r9Y338DqLVvecIDAAA='
class DreameVacuumDreameHomeCloudProtocol:

    def __init__(self, username: str, password: str, account_type: str='dreame', country: str='cn', auth_key: str=None, did: str=None, client_id: str=None) -> None:
        if client_id is None:
            client_id = 'A' + secrets.token_hex(3)
        if type(client_id) is not str or re.fullmatch(r'A[0-9a-f]{6}', client_id) is None:
            raise ValueError('Invalid MOVA client identifier.')
        self._client_id = client_id
        self._username = username
        self._password = password
        self._account_type = account_type
        self._country = country
        self._did = did
        self._session = requests.session()
        self._queue = queue.Queue()
        self._thread = None
        self._client_queue = queue.Queue()
        self._client_thread = None
        self._id = random.randint(101, 10100)
        self._reconnect_timer = None
        self._host = None
        self._model = None
        self._ti = None
        self._fail_count = 0
        self._connected = False
        self._client_connected = False
        self._client_connecting = False
        self._client = None
        self._message_callback = None
        self._connected_callback = None
        self._logged_in = False
        self._auth_failed = False
        self._stream_key = None
        self._client_key = None
        self._secondary_key = auth_key
        self._key_expire = None
        self._key = None
        self._uid = None
        self._uuid = None
        self._strings = None
        self.verification_url = None
        self.captcha_img = None

    def _api_call(self, url, params=None, retry_count=2):
        return self.request(f'{self.get_api_url()}/{url}', json.dumps(params, separators=(',', ':')) if params is not None else None, retry_count)

    def get_api_url(self) -> str:
        return f'https://{self._country}{self._strings[0]}:{self._strings[1]}'

    def login(self) -> bool:
        self._session.close()
        self._session = requests.session()
        if self._strings is None:
            self._strings = json.loads(zlib.decompress(base64.b64decode(DREAME_STRINGS), zlib.MAX_WBITS | 32))
            if self._account_type != 'dreame':
                self._strings[0] = self._strings[57]
                self._strings[3] = self._strings[58]
                self._strings[6] = f'{self._strings[6][:5]}2'
        self._auth_failed = False
        try:
            if self._secondary_key:
                data = f'{self._strings[12]}{self._strings[13]}{self._secondary_key}'
            else:
                data = f"{self._strings[12]}{self._strings[14]}{self._username}{self._strings[15]}{hashlib.md5((self._password + self._strings[2]).encode('utf-8')).hexdigest()}{self._strings[16]}"
            headers = {'Accept': '*/*', 'Content-Type': 'application/x-www-form-urlencoded', 'Accept-Language': 'en-US;q=0.8', 'Accept-Encoding': 'gzip, deflate', self._strings[47]: self._strings[3], self._strings[49]: self._strings[5], self._strings[50]: self._ti if self._ti else self._strings[6]}
            if self._country == 'cn':
                headers[self._strings[48]] = self._strings[4]
            response = self._session.post(self.get_api_url() + self._strings[17], headers=headers, data=data, timeout=10)
            if response.status_code == 200:
                data = json.loads(response.text)
                if self._strings[18] in data:
                    self._key = data.get(self._strings[18])
                    self._secondary_key = data.get(self._strings[19])
                    self._key_expire = time.time() + data.get(self._strings[20]) - 120
                    self._uuid = data.get('uid')
                    self._ti = data.get(self._strings[22], self._ti)
                    self._logged_in = True
            else:
                if self._username and self._password:
                    try:
                        data = json.loads(response.text)
                        if 'error_description' in data and 'refresh token' in data['error_description']:
                            self._secondary_key = None
                            return self.login()
                    except:
                        pass
                self._logged_in = False
                self._auth_failed = True
                _LOGGER.error('Login failed: %s', response.text)
        except requests.exceptions.Timeout:
            response = None
            self._logged_in = False
            _LOGGER.warning('Login Failed: Read timed out. (read timeout=10)')
        except Exception as ex:
            response = None
            self._logged_in = False
            _LOGGER.error('Login failed: %s', str(ex))
        if self._logged_in:
            self._fail_count = 0
            self._connected = True
        return self._logged_in

    def get_devices(self) -> Any:
        response = self._api_call(f'{self._strings[23]}/{self._strings[24]}/{self._strings[27]}/{self._strings[28]}')
        if response and 'data' in response and (response['code'] == 0):
            return response['data']
        return None

    def request(self, url: str, data, retry_count=2) -> Any:
        retries = 0
        if not retry_count or retry_count < 0:
            retry_count = 0
        while retries < retry_count + 1:
            try:
                if self._key_expire and time.time() > self._key_expire:
                    if not self.login():
                        response = None
                        break
                headers = {'Accept': '*/*', 'Content-Type': 'application/x-www-form-urlencoded', 'Accept-Language': 'en-US;q=0.8', 'Accept-Encoding': 'gzip, deflate', self._strings[47]: self._strings[3], self._strings[49]: self._strings[5], self._strings[50]: self._ti if self._ti else self._strings[6], self._strings[51]: self._strings[52], self._strings[46]: self._key}
                if self._country == 'cn':
                    headers[self._strings[48]] = self._strings[4]
                response = self._session.post(url, headers=headers, data=data, timeout=6)
                break
            except requests.exceptions.Timeout:
                retries = retries + 1
                response = None
                if self._connected:
                    _LOGGER.warning('Error while executing request: Read timed out. (read timeout=6): %s', data)
            except Exception as ex:
                retries = retries + 1
                response = None
                if self._connected:
                    _LOGGER.warning('Error while executing request: %s', str(ex))
        if response is not None:
            if response.status_code == 200:
                self._fail_count = 0
                self._connected = True
                return json.loads(response.text)
            elif response.status_code == 401 and self._secondary_key:
                _LOGGER.warning('Execute api call failed: Token Expired')
                self.login()
            else:
                _LOGGER.warning('Execute api call failed with response: %s', response.text)
        if self._fail_count == 5:
            self._connected = False
        else:
            self._fail_count = self._fail_count + 1
        return None

    def send(self, method, parameters, retry_count: int=2) -> Any:
        assert method in {'get_properties', 'set_properties', 'action'}
        host = ''
        if self._host and len(self._host):
            host = f"-{self._host.split('.')[0]}"
        # The MOVA SDK increments before sending and reuses one installation
        # identifier across commands, including a motion pulse and its stop.
        self._id += 1
        request_id = self._id
        api_response = self._api_call(
            f'{self._strings[37]}{host}/{self._strings[27]}/{self._strings[38]}',
            {'did': str(self._did), 'id': request_id,
             'data': {'did': str(self._did), 'id': request_id, 'method': method,
                      'from': self._client_id, 'params': parameters}}, retry_count)
        if (not isinstance(api_response, dict)
                or type(api_response.get('code')) is not int
                or api_response['code'] != 0
                or ('success' in api_response and api_response['success'] is not True)
                or 'error' in api_response):
            return None
        data = api_response.get('data')
        if not isinstance(data, dict) or 'result' not in data or 'error' in data:
            return None
        if ('code' in data and (type(data['code']) is not int or data['code'] != 0)
                or 'success' in data and data['success'] is not True):
            return None
        for envelope in (api_response, data):
            if 'id' in envelope and (type(envelope['id']) is not int or envelope['id'] != request_id):
                return None
        return data['result']

    def get_file(self, url: str, retry_count: int=4) -> Any:
        retries = 0
        if not retry_count or retry_count < 0:
            retry_count = 0
        while retries < retry_count + 1:
            try:
                response = self._session.get(url, timeout=6)
            except Exception as ex:
                response = None
                _LOGGER.warning('Unable to get file at %s: %s', url, ex)
            if response is not None and response.status_code == 200:
                return response.content
            retries = retries + 1
        return None

    def get_interim_file_url(self, object_name: str='') -> str:
        api_response = self._api_call(f'{self._strings[23]}/{self._strings[39]}/{self._strings[55]}', {'did': str(self._did), self._strings[35]: self._model, self._strings[40]: object_name, self._strings[21]: self._country})
        if api_response is None or 'data' not in api_response:
            return None
        return api_response['data']
