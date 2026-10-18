"""Bounded local-pipe diagnostics; no subprocess, network, or robot access."""
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mova-robot/scripts'))
import camera


class VideoDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.session=camera.CameraSession(Mock())

    def test_only_known_enums_and_bounded_integer_fields_survive(self):
        report=camera._safe_video_report(json.dumps({
            'phase':'stream_query_failed','httpStatus':403,'responseCode':1234,
            'relayUrl':'rtmp://FAKE-SECRET','pin':'1234','iotId':'FAKE-DEVICE',
            'videoFrames':True,'audioFrames':2**80,'endedBy':'FAKE-SECRET',
        }).encode())
        self.assertEqual(report,{'phase':'stream_query_failed','httpStatus':403,'responseCode':1234})
        for value in ('FAKE-SECRET',{},[],{'phase':['error']},{'phase':'FAKE-SECRET'},
                      {'phase':True},{'phase':{'secret':'FAKE-SECRET'}}):
            with self.subTest(value=value):
                self.assertIsNone(camera._safe_video_report(json.dumps(value).encode()))
        self.assertEqual(camera._safe_video_report(b'{"phase":"error","code":"VIDEO_MEDIA_SECRET"}'),
                         {'phase':'error','code':'VIDEO_WORKER_FAILED'})
        self.assertEqual(camera._safe_video_report(b'{"phase":"error","code":"VIDEO_MEDIA_TIMEOUT"}'),
                         {'phase':'error','code':'VIDEO_MEDIA_TIMEOUT'})
        self.assertIsNone(camera._safe_video_report(b'x'*1025))

    def pipe_reader(self):
        read_fd,write_fd=os.pipe()
        pipe=os.fdopen(read_fd,'rb',buffering=0)
        done=threading.Event()
        reader=threading.Thread(target=self.session._read_video_reports,args=(pipe,done))
        reader.start()
        return pipe,write_fd,done,reader

    def test_continuous_draining_drops_oversized_and_private_lines_without_deadlock(self):
        pipe,write_fd,done,reader=self.pipe_reader()
        failures=[]
        def write():
            try:
                for _ in range(512):os.write(write_fd,b'FAKE-PRIVATE'*512)
                os.write(write_fd,b'\nnot json FAKE-PRIVATE\n{"phase":[]}\n')
                for number in range(20):
                    os.write(write_fd,json.dumps({'phase':'stream_ended','videoFrames':number,
                                                 'endedBy':'peer','secret':'FAKE-PRIVATE'}).encode()+b'\n')
            except BaseException as error:failures.append(error)
            finally:os.close(write_fd)
        writer=threading.Thread(target=write)
        try:
            writer.start()
            writer.join(3)
            reader.join(3)
            self.assertFalse(writer.is_alive() or reader.is_alive())
            self.assertEqual(failures,[])
            reports=self.session.status()['video_diagnostics']['worker_reports']
            self.assertEqual([report['videoFrames'] for report in reports],list(range(12,20)))
            self.assertNotIn('FAKE-PRIVATE',json.dumps(self.session.status()))
        finally:
            done.set()
            reader.join(1)
            pipe.close()

    def test_cleanup_stops_reader_even_when_writer_keeps_pipe_open(self):
        pipe,write_fd,done,reader=self.pipe_reader()
        try:
            done.set()
            reader.join(0.5)
            self.assertFalse(reader.is_alive())
        finally:
            os.close(write_fd)
            pipe.close()

    def test_exit_snapshot_checks_both_children_and_never_records_arbitrary_values(self):
        receiver,decoder=Mock(),Mock()
        receiver.poll.return_value=2
        decoder.poll.return_value=1
        self.assertTrue(self.session._record_video_exits(receiver,decoder))
        decoder.poll.assert_called_once_with()
        self.assertEqual(self.session.status()['video_diagnostics']['exit_codes_before_cleanup'],
                         {'receiver':2,'decoder':1})
        receiver.poll.return_value='FAKE-SECRET'
        decoder.poll.return_value=False
        self.assertFalse(self.session._record_video_exits(receiver,decoder))
        self.assertNotIn('FAKE-SECRET',json.dumps(self.session.status()))

    def test_diagnostic_snapshots_are_copies_and_new_session_resets_them(self):
        self.session._store_video_report(b'{"phase":"error","code":"VIDEO_MEDIA_RELAY"}')
        snapshot=self.session.status()['video_diagnostics']
        snapshot['worker_reports'][0]['code']='changed'
        snapshot['exit_codes_before_cleanup']['receiver']=123
        self.assertEqual(self.session.status()['video_diagnostics']['worker_reports'][0]['code'],
                         'VIDEO_MEDIA_RELAY')
        with patch.object(camera.threading,'Thread'):
            self.session.start('1234')
        self.assertEqual(self.session.status()['video_diagnostics'],
                         {'worker_reports':[],'exit_codes_before_cleanup':{'receiver':None,'decoder':None}})


if __name__=='__main__':unittest.main()
