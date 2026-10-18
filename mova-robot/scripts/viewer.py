#!/usr/bin/env python3
"""Private loopback-only MOVA camera viewer. Opens no camera until Start is clicked."""
import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import shutil
import signal
import threading
from urllib.parse import urlsplit

from camera import CameraSession, CloudAccess, CameraError
from steering import SteeringError

ASSETS = Path(__file__).resolve().parent/'viewer_assets'


def make_handler(manager, token, closing=None, start_gate=None):
    closing = closing if closing is not None else threading.Event()
    start_gate = start_gate if start_gate is not None else threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def _trusted(self, authenticated=False):
            expected = f'127.0.0.1:{self.server.server_port}'
            origin = self.headers.get('Origin')
            if self.headers.get('Host') != expected or origin not in (None,f'http://{expected}'):
                return False
            if self.command=='POST' and origin != f'http://{expected}':
                return False
            supplied = self.headers.get('X-Viewer-Token','').encode('utf-8')
            return not authenticated or secrets.compare_digest(supplied,token.encode('utf-8'))

        def _reply(self, code, body, content_type='application/json'):
            if not isinstance(body,bytes):body=json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Cross-Origin-Resource-Policy','same-origin')
            self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass

        def do_GET(self):
            if not self._trusted():return self._reply(403,{'error':'Local viewer access only.'})
            path=urlsplit(self.path).path
            files={'/':('index.html','text/html; charset=utf-8'),'/viewer.js':('viewer.js','text/javascript; charset=utf-8'),
                '/viewer.css':('viewer.css','text/css; charset=utf-8')}
            if path in files:
                name,mime=files[path]
                return self._reply(200,(ASSETS/name).read_bytes(),mime)
            if not self._trusted(True):return self._reply(403,{'error':'Open the private viewer link printed at startup.'})
            if path=='/status':return self._reply(200,manager.status(touch=True))
            if path=='/frame':
                with manager.lock:
                    frame=manager.frame
                return self._reply(200,frame,'image/jpeg') if frame else self._reply(204,b'')
            return self._reply(404,{'error':'Not found.'})

        def do_POST(self):
            if not self._trusted(True):return self._reply(403,{'error':'Private local viewer access required.'})
            try:
                if self.headers.get('Content-Type')!='application/json':
                    return self._reply(415,{'error':'JSON required.'})
                length=int(self.headers.get('Content-Length','0'))
                if not 1<=length<=1024:return self._reply(413,{'error':'Invalid request size.'})
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):raise ValueError()
                if self.path=='/start':
                    # Serialize admission with shutdown; a request accepted
                    # before server_close must not create a new camera worker.
                    with start_gate:
                        if closing.is_set():return self._reply(503,{'error':'Viewer is closing.'})
                        manager.start(data.get('pin'))
                        data.clear()
                    return self._reply(202,{'ok':True})
                if self.path=='/stop':
                    manager.stop()
                    return self._reply(202,{'ok':True})
                if self.path=='/steer':
                    if closing.is_set():return self._reply(503,{'error':'Viewer is closing.'})
                    result=manager.steer(data.get('direction'),data.get('supervised') is True)
                    return self._reply(200,result)
                if self.path=='/steer/hold':
                    if closing.is_set():return self._reply(503,{'error':'Viewer is closing.'})
                    result=manager.hold(data.get('direction'),data.get('supervised') is True,data.get('hold_id'))
                    return self._reply(200,result)
                if self.path=='/steer/heartbeat':
                    if closing.is_set():return self._reply(503,{'error':'Viewer is closing.'})
                    return self._reply(200,manager.hold_heartbeat(data.get('hold_id')))
                if self.path=='/steer/release':
                    return self._reply(200,manager.hold_release(data.get('hold_id')))
                return self._reply(404,{'error':'Not found.'})
            except (CameraError,SteeringError) as exc:
                return self._reply(409,{'error':str(exc)})
            except (ValueError,TypeError):
                return self._reply(400,{'error':'Invalid request.'})
            except Exception:
                return self._reply(500,{'error':'Request failed. Check MOVAhome; private details omitted.'})
    return Handler


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device')
    parser.add_argument('--allow-steering',action='store_true',help='Expose experimental buttons for a person supervising the robot')
    args=parser.parse_args()
    for executable in ('node','ffmpeg'):
        if not shutil.which(executable):raise SystemExit(f'{executable} is required for this local viewer.')
    logging.disable(logging.CRITICAL)
    manager=CameraSession(CloudAccess(args.device),args.allow_steering)
    token=secrets.token_urlsafe(32)
    closing=threading.Event()
    start_gate=threading.Lock()
    server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(manager,token,closing,start_gate))
    # Reject slow uploads rather than allowing an indefinitely blocked handler.
    original_get_request=server.get_request
    def get_request():
        connection,address=original_get_request()
        connection.settimeout(5)
        return connection,address
    server.get_request=get_request
    def cancel(signum,frame):
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        signal.signal(signal.SIGINT,signal.SIG_IGN)
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,cancel)
    signal.signal(signal.SIGINT,cancel)
    print(f'Private viewer: http://127.0.0.1:{server.server_port}/#{token}',flush=True)
    print('Camera stays off until you enter its existing PIN and click Open live view. Ctrl+C closes the viewer.',flush=True)
    try:server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:pass
    finally:
        # A second Ctrl+C must not interrupt the camera's finally-stop.
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        signal.signal(signal.SIGINT,signal.SIG_IGN)
        with start_gate:
            closing.set()
            manager.stop()
        server.server_close()
        manager.close()


if __name__=='__main__':main()
