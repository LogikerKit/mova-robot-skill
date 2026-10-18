#!/usr/bin/env python3
"""Local MOVA controls. Runtime credentials never belong in the skill folder."""
import argparse
import base64
import fcntl
import getpass
import json
import logging
import os
from pathlib import Path
import signal
import struct
import sys
import tempfile
import time
import zlib
from protocol import DreameVacuumDreameHomeCloudProtocol

CONFIG = Path(os.environ.get('MOVA_CONFIG_DIR', Path.home() / '.config/mova-robot'))
PROPS = {'state':(2,1), 'error':(2,2), 'battery_percent':(3,1), 'charging':(3,2),
         'activity':(4,1), 'cleaning_minutes':(4,2), 'cleaned_area_m2':(4,3),
         'water':(4,5), 'task_status':(4,7), 'mode':(4,23), 'task_type':(4,58)}
ROOM_TYPES = {1:'Living room',2:'Bedroom',3:'Study',4:'Kitchen',5:'Dining room',
              6:'Bathroom',7:'Balcony',8:'Corridor',9:'Utility room',10:'Closet',
              11:'Meeting room',12:'Office',13:'Gym',14:'Recreation room',15:'Bedroom'}


def save_session(data):
    CONFIG.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, path = tempfile.mkstemp(dir=CONFIG)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(data, out)
        os.replace(path, CONFIG / 'session.json')
    finally:
        if os.path.exists(path): os.unlink(path)


def summarize(raw):
    out = dict(raw)
    task = raw.get('task_type')
    out['spill_mode'] = 'active task' if task == 15 else 'paused task' if task == 16 else 'not indicated'
    mode = raw.get('mode')
    out['cleaning_mode'] = {0:'vacuum and mop',1:'mop only',2:'vacuum only'}.get(mode & 3, 'unknown') if isinstance(mode,int) else 'unknown'
    out['state_label'] = {1:'sweeping',2:'idle',3:'paused',4:'error',5:'returning',6:'charging',7:'mopping',8:'drying',9:'washing',10:'returning to wash',12:'vacuum and mop',13:'charged'}.get(raw.get('state'),'unknown')
    return out


def decode_rooms(data):
    rooms = []
    for entry in data.get('mapstr', []):
        encoded = entry.get('map', '')
        if not encoded or ',' in encoded:
            raise RuntimeError('Unsupported or encrypted map; room selection not verified.')
        packed = base64.b64decode(encoded.replace('_','/').replace('-','+'))
        decoder = zlib.decompressobj()
        raw = decoder.decompress(packed, 20_000_001)
        if len(raw) > 20_000_000 or not decoder.eof:
            raise RuntimeError('Map exceeds supported size.')
        if len(raw) < 27: raise RuntimeError('Invalid map header')
        map_id = struct.unpack_from('<h',raw,0)[0]
        if map_id != int(data['curr_id']): continue
        width,height = struct.unpack_from('<hh',raw,19)
        offset = 27 + width*height
        if width <= 0 or height <= 0 or offset >= len(raw): raise RuntimeError('Invalid map dimensions')
        meta = json.loads(raw[offset:])
        for room_id, info in meta.get('seg_inf',{}).items():
            name = base64.b64decode(info['name']).decode() if info.get('name') else ROOM_TYPES.get(info.get('type'),'Room')
            rooms.append({'id':int(room_id),'name':name,'type':info.get('type'),'map_id':map_id})
    if not rooms: raise RuntimeError('Current room map unavailable; cannot select room.')
    return rooms


class Robot:
    def __init__(self, cloud): self.cloud = cloud

    def read(self, props):
        c=self.cloud
        result=c.send('get_properties',[{'did':str(c._did),'siid':s,'piid':p} for s,p in props.values()],0)
        if not isinstance(result,list): raise RuntimeError('No property response')
        output={}
        for name,(s,p) in props.items():
            item=next((x for x in result if x.get('siid')==s and x.get('piid')==p),{})
            if item.get('code') != 0: raise RuntimeError(f'Property unavailable: {name}')
            output[name]=item.get('value')
        return output

    def status(self): return summarize(self.read(PROPS))

    def action(self, s, a, inputs=None):
        result=self.cloud.send('action',{'did':str(self.cloud._did),'siid':s,'aiid':a,'in':inputs or []},0)
        if not isinstance(result,dict) or result.get('code') != 0:
            raise RuntimeError('Action not confirmed. Read status before any retry.')
        return result

    def set(self,p,value):
        result=self.cloud.send('set_properties',[{'did':str(self.cloud._did),'siid':4,'piid':p,'value':value}],0)
        if not isinstance(result,list) or len(result)!=1 or result[0].get('code') != 0:
            raise RuntimeError('Setting not confirmed. Read status before retrying.')

    def rooms(self):
        meta=json.loads(self.read({'map':(6,8)})['map'])
        obj=meta.get('object_name') or meta.get('obj_name')
        if not obj: raise RuntimeError('No room-map reference')
        url=self.cloud.get_interim_file_url(obj)
        if not isinstance(url,str) or not url.startswith('https://'): raise RuntimeError('Map URL unavailable')
        raw=self.cloud.get_file(url,0)
        if raw is None: raise RuntimeError('Room map download failed')
        return decode_rooms(json.loads(raw))

    def mop(self, room, replace):
        if self.cloud._model != 'mova.vacuum.r9540h':
            raise RuntimeError('Mopping commands are verified only for model mova.vacuum.r9540h.')
        if room not in {x['id'] for x in self.rooms()}: raise RuntimeError('Room is not in current map')
        before=self.status()
        if before['error'] != 0: raise RuntimeError('Robot reports an error; resolve it before starting.')
        if before['task_status'] != 0:
            if not replace: raise RuntimeError('Existing task; use --replace-task only if replacing it is authorized.')
            self.action(4,2)
            time.sleep(3)
            if self.read({'task':(4,7)})['task'] != 0: raise RuntimeError('Previous task has not ended')
        self.set(50,json.dumps({'k':'SmartHost','v':0},separators=(',',':')))
        time.sleep(3)
        switches=json.loads(self.read({'auto':(4,50)})['auto'])
        if not any(x.get('k')=='SmartHost' and x.get('v')==0 for x in switches):
            raise RuntimeError('CleanGenius off not confirmed; no start sent')
        old=self.read({'mode':(4,23)})['mode']
        mode=(old & ~3)|1
        self.set(23,mode)
        time.sleep(5)
        if self.read({'mode':(4,23)})['mode'] != mode: raise RuntimeError('Mop-only mode not confirmed; no start sent')
        self.action(4,1,[{'piid':1,'value':18},{'piid':10,'value':json.dumps({'selects':[[room,1,1,1,1]]},separators=(',',':'))}])
        time.sleep(3)
        try:
            after=self.status()
        except Exception:
            try:
                self.action(2,2)
            except Exception:
                raise RuntimeError('Could not verify cleaning after start; pause also unconfirmed. Check the robot in MOVAhome before any retry. CleanGenius remains off.') from None
            raise RuntimeError('Could not verify cleaning after start. Pause acknowledged; check status before any retry. CleanGenius remains off.') from None
        if after['mode'] & 3 != 1 or after['error'] != 0:
            self.action(2,2)
            raise RuntimeError('Unexpected mode/error after start. Pause acknowledged; verify status. CleanGenius remains off.')
        return {'requested_room':room,'passes':1,'requested_water_level':1,'cleangenius':'off',
                'start_acknowledged':True,'status':after}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['login','devices','status','rooms','pause','stop','mop','steer'])
    parser.add_argument('--device',help='Explicit device ID when account has several robots')
    parser.add_argument('--room',type=int)
    parser.add_argument('--execute',action='store_true',help='User has authorized this robot action')
    parser.add_argument('--replace-task',action='store_true')
    parser.add_argument('--direction',choices=['forward','left','right','stop'])
    parser.add_argument('--duration-ms',type=int,default=250,help='Requested steering pulse, 100–750 ms; cloud latency can prolong movement')
    parser.add_argument('--experimental-steering',action='store_true',help='Enable supervised steering; firmware automatic-stop behaviour is unverified')
    args=parser.parse_args()
    if args.command in {'pause','stop','mop','steer'} and not args.execute:
        parser.error('Robot actions require an authorized request and --execute')
    if args.command=='mop' and args.room is None: parser.error('mop requires --room from the current map')
    if args.command=='steer':
        if not args.experimental_steering or args.direction is None:
            parser.error('steer requires --experimental-steering and --direction for supervised operation')
        if not 100 <= args.duration_ms <= 750:
            parser.error('Requested steering duration must be 100–750 ms')
    logging.disable(logging.CRITICAL) # Upstream logs can contain auth responses.
    CONFIG.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (CONFIG/'session.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        cloud=None
        saved_refresh=None
        try:
            if args.command=='login':
                region=input('MOVAhome region [eu]: ').strip() or 'eu'
                if region not in {'eu','cn','us','ru','sg'}: raise RuntimeError('Unsupported region')
                user=input('MOVAhome email/phone: ').strip()
                password=getpass.getpass('MOVAhome password: ')
                cloud=DreameVacuumDreameHomeCloudProtocol(user,password,'mova',region)
            else:
                session=json.loads((CONFIG/'session.json').read_text())
                region=session['region']
                if region not in {'eu','cn','us','ru','sg'}: raise RuntimeError('Invalid session region')
                cloud=DreameVacuumDreameHomeCloudProtocol(None,None,'mova',region,auth_key=session['refresh_token'])
            if not cloud.login(): raise RuntimeError('Login failed; sign in privately using login')
            save_session({'region':region,'refresh_token':cloud._secondary_key})
            saved_refresh=cloud._secondary_key
            cloud._password=None
            data=cloud.get_devices()
            records=data['page']['records']
            if args.command in {'login','devices'}:
                print(json.dumps([{'id':str(x['did']),'name':x.get('customName'),'model':x.get('model')} for x in records]));return
            selected=[x for x in records if str(x['did'])==args.device] if args.device else records
            if len(selected)!=1: raise RuntimeError('Select one robot explicitly with --device; use devices to list them')
            record=selected[0]
            cloud._did=str(record['did']);cloud._host=record.get(cloud._strings[9])
            cloud._model=record[cloud._strings[35]];cloud._uid=record[cloud._strings[8]]
            robot=Robot(cloud)
            if args.command=='status': output=robot.status()
            elif args.command=='rooms': output=robot.rooms()
            elif args.command=='mop': output=robot.mop(args.room,args.replace_task)
            elif args.command=='steer':
                from steering import SteeringController
                def cancel_steering(signum,frame):
                    signal.signal(signal.SIGTERM,signal.SIG_IGN)
                    raise KeyboardInterrupt
                previous=signal.signal(signal.SIGTERM,cancel_steering)
                try:
                    output=SteeringController(robot).move(args.direction,args.duration_ms)
                finally:
                    signal.signal(signal.SIGTERM,previous)
            elif args.command in {'pause','stop'}:
                robot.action(2,2) if args.command=='pause' else robot.action(4,2)
                time.sleep(2);output={'action_acknowledged':args.command,'status':robot.status()}
            else:
                raise RuntimeError('Unsupported robot command')
            print(json.dumps(output,ensure_ascii=False))
        finally:
            if cloud:
                pending_error=sys.exc_info()[0] is not None
                try:
                    # The protocol can rotate tokens while handling expiry/401.
                    # Persist that rotation before releasing the account lock.
                    if cloud._logged_in and cloud._secondary_key and cloud._secondary_key != saved_refresh:
                        try:
                            save_session({'region':region,'refresh_token':cloud._secondary_key})
                        except OSError:
                            if not pending_error:
                                raise RuntimeError('Session update failed. Sign in privately again; read status before retrying any action.') from None
                finally:
                    cloud._session.close()

if __name__=='__main__':
    try: main()
    except (RuntimeError,FileNotFoundError) as exc: raise SystemExit(str(exc))
    except (KeyboardInterrupt,EOFError): raise SystemExit('Cancelled')
    except Exception: raise SystemExit('Request failed; check connectivity and read status before retrying any action. Private details omitted.')
