#!/usr/bin/env python3
"""Exercise schema upgrade and restart recovery using synthetic data, never live state."""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--old-revision',default='4d87a28')
args=parser.parse_args()
root=Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory(prefix='bridge-upgrade-') as temporary:
    temp=Path(temporary)
    archive=temp/'old.tar'
    # Revision is passed as an argument, not a shell expression.
    with archive.open('wb') as target:
        subprocess.run(['git','archive',args.old_revision],cwd=root,stdout=target,check=True)
    old=temp/'old';old.mkdir()
    import tarfile
    with tarfile.open(archive) as tar:tar.extractall(old,filter='data')
    state=temp/'state'
    fixture='''
from wechat_dot_bridge.store import Store
s=Store(STATE)
s.import_binding(dict(id='fixture',owner='owner',token='fixture-token',base_url='https://ilinkai.weixin.qq.com'))
s.set_enabled(True)
s.ingest('fixture',[dict(source_id='1',peer='owner',text='fixture',created_at=1,context_token='fixture')],'cursor')
mid=s.list_pending()[0]['message_id'];s.queue_reply(mid,'x'*10000)
r=s.claim_send();s.finish_send(mid,'accepted',reply_id=r['reply_id']);s.claim_send();s.close()
'''.replace('STATE',repr(str(state)))
    subprocess.run([sys.executable,'-c',fixture],cwd=old,check=True)
    sys.path.insert(0,str(root))
    from wechat_dot_bridge.store import Store
    from wechat_dot_bridge.operations import backup,restore
    store=Store(state);store.recover_inflight()
    row=store.db.execute('SELECT client_id,message_id,status FROM replies').fetchone()
    assert row['status']=='uncertain'
    assert store.send_status(row['message_id'])['parts_accepted']==1
    assert store.claim_send() is None
    snapshot=temp/'backup.sqlite3';backup(store,snapshot);store.close()
    recovered=temp/'recovered';recovered.mkdir(mode=0o700);restore(snapshot,recovered)
    store=Store(recovered)
    assert not store.get_binding()['enabled']
    assert store.claim_send() is None
    store.set_enabled(True)
    store.resolve_uncertain(row['client_id'],True)
    assert store.send_status(row['message_id'])['parts_accepted']==2
    assert store.claim_send() is not None
    store.close()
print('Upgrade, uncertain-send preservation, backup/restore and part recovery: passed (synthetic state only).')
