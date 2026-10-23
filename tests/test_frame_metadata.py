"""Atomic decoded-frame metadata; no camera, network, or real footage."""
from email.message import Message
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mova-robot/scripts'))
import camera
import viewer


class FrameMetadataTests(unittest.TestCase):
    def setUp(self):
        self.manager=camera.CameraSession(Mock(),True)

    def get(self,token='test-token'):
        handler=object.__new__(viewer.make_handler(self.manager,'test-token'))
        handler.server=Mock(server_port=12345)
        handler.command='GET'
        handler.path='/frame'
        handler.headers=Message()
        handler.headers['Host']='127.0.0.1:12345'
        handler.headers['X-Viewer-Token']=token
        handler.wfile=io.BytesIO()
        handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock()
        with patch.object(viewer.time,'monotonic',return_value=10.5):handler.do_GET()
        return (handler.send_response.call_args.args[0],handler.wfile.getvalue(),
                dict(call.args for call in handler.send_header.call_args_list))

    def test_headers_describe_the_same_latest_jpeg(self):
        self.manager.frame=b'\xff\xd8test\xff\xd9'
        self.manager.frame_at=10.0;self.manager.frame_seq=7;self.manager.frame_epoch=2
        code,body,headers=self.get()
        self.assertEqual((code,body),(200,self.manager.frame))
        self.assertEqual(headers['X-Frame-Sequence'],'7')
        self.assertEqual(headers['X-Frame-Epoch'],'2')
        self.assertEqual(float(headers['X-Frame-Decoded-Monotonic-Ms']),10000)
        self.assertEqual(float(headers['X-Frame-Age-Ms']),500)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertNotIn('X-Frame-Captured-Ms',headers)

    def test_private_frame_headers_require_viewer_access(self):
        self.manager.frame=b'private'
        code,body,headers=self.get('wrong')
        self.assertEqual(code,403)
        self.assertNotEqual(body,b'private')
        self.assertNotIn('X-Frame-Sequence',headers)

    def test_each_complete_jpeg_advances_sequence_and_keeps_only_latest(self):
        first=b'\xff\xd8one\xff\xd9';second=b'\xff\xd8two\xff\xd9'
        with patch.object(camera.time,'monotonic',side_effect=[10.0,10.1]):
            self.manager._read_frames(io.BytesIO(first+second))
        self.assertEqual(self.manager.frame,second)
        self.assertEqual(self.manager.frame_seq,2)
        self.assertEqual(self.manager.frame_at,10.1)


if __name__=='__main__':unittest.main()
