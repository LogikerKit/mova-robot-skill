import base64,json,os,struct,sys,tempfile,unittest,zlib
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]/'mova-robot/scripts'))
import mova

class Tests(unittest.TestCase):
    def test_spill_distinct_from_low_water(self):
        self.assertEqual(mova.summarize({'task_type':15,'mode':5121,'water':1})['spill_mode'],'active task')
        self.assertEqual(mova.summarize({'task_type':3,'mode':5121,'water':1})['spill_mode'],'not indicated')
    def test_private_session(self):
        with tempfile.TemporaryDirectory() as d, patch.object(mova,'CONFIG',Path(d)):
            mova.save_session({'refresh_token':'dummy'})
            self.assertEqual(os.stat(Path(d)/'session.json').st_mode & 0o777,0o600)
    def test_current_map_only(self):
        def entry(mid,rid):
            raw=bytearray(28);struct.pack_into('<h',raw,0,mid);struct.pack_into('<hh',raw,19,1,1)
            raw+=json.dumps({'seg_inf':{str(rid):{'type':6}}}).encode()
            return {'map':base64.b64encode(zlib.compress(raw)).decode()}
        rooms=mova.decode_rooms({'curr_id':9,'mapstr':[entry(8,2),entry(9,7)]})
        self.assertEqual([r['id'] for r in rooms],[7])
    def test_encrypted_map_rejected(self):
        with self.assertRaises(RuntimeError): mova.decode_rooms({'curr_id':1,'mapstr':[{'map':'encoded,key'}]})
    def test_execute_gate_before_connection(self):
        with patch.object(sys,'argv',['mova.py','pause']), patch.object(mova,'DreameVacuumDreameHomeCloudProtocol') as cloud:
            with self.assertRaises(SystemExit): mova.main()
            cloud.assert_not_called()
    def test_mismatch_pauses(self):
        class Cloud: _model='mova.vacuum.r9540h'
        robot=mova.Robot(Cloud()); actions=[]
        robot.rooms=lambda:[{'id':7}]
        states=iter([{'error':0,'task_status':0},{'mode':5120,'error':0}])
        robot.status=lambda:next(states)
        reads=iter([{'auto':'[{"k":"SmartHost","v":0}]'},{'mode':5120},{'mode':5121}])
        robot.read=lambda p:next(reads)
        robot.set=lambda p,v:None
        robot.action=lambda *a:actions.append(a)
        with patch.object(mova.time,'sleep'),self.assertRaises(RuntimeError): robot.mop(7,False)
        self.assertEqual(actions[-1],(2,2))
    def test_existing_task_not_cancelled_without_authorization(self):
        class Cloud: _model='mova.vacuum.r9540h'
        robot=mova.Robot(Cloud());robot.rooms=lambda:[{'id':7}]
        robot.status=lambda:{'error':0,'task_status':3}
        with patch.object(robot,'action') as action,self.assertRaises(RuntimeError): robot.mop(7,False)
        action.assert_not_called()

if __name__=='__main__':unittest.main()
