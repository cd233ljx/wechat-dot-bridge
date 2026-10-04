"""Correlation timestamps only: no message bodies, credentials or callback URLs."""
from .errors import BridgeError

STAGES={'received','event_enqueued','event_start','event_result','reply_queued','send_start','send_result','weixin_accepted','read_dispatch','read_thread_start','read_start','read_result','reply_tool_result'}


def record(store,mid,stage,*,at=None,outcome=None,elapsed_ms=None,reply_id=None):
    if stage not in STAGES:raise BridgeError('invalid_timing_stage')
    with store.lock:
        if not store.db.execute('SELECT 1 FROM messages WHERE id=?',(mid,)).fetchone():return
        store.db.execute('INSERT INTO message_timeline(message_id,stage,at,outcome,elapsed_ms,reply_id) VALUES(?,?,?,?,?,?)',
                         (mid,stage,store.clock() if at is None else at,outcome,elapsed_ms,reply_id))


def report(store,limit=10,message_id=None):
    if type(limit) is not int or not 1<=limit<=100:raise BridgeError('invalid_limit')
    with store.lock:
        rows=store.db.execute('SELECT id,created_at FROM messages WHERE id=?' if message_id else 'SELECT id,created_at FROM messages ORDER BY rowid DESC LIMIT ?', (message_id if message_id else limit,)).fetchall()
        output=[]
        for m in rows:
            mid=m['id']
            timeline=[dict(r) for r in store.db.execute('SELECT stage,at,outcome,elapsed_ms,reply_id FROM message_timeline WHERE message_id=? ORDER BY id',(mid,))]
            calls=[dict(r) for r in store.db.execute('SELECT name,outcome,started_at,created_at AS completed_at,elapsed_ms FROM tool_calls WHERE message_id=? ORDER BY id',(mid,))]
            def first(stage,outcome=None):
                return next((r['at'] for r in timeline if r['stage']==stage and (outcome is None or r['outcome']==outcome)),None)
            read_trace=next((r for r in timeline if r['stage']=='read_result' and r['outcome']=='ok'),None)
            read_thread=next((r for r in timeline if r['stage']=='read_thread_start'),{})
            reply_trace=next((r for r in timeline if r['stage']=='reply_tool_result' and r['outcome']=='ok'),None)
            read=next((r for r in calls if r['name']=='read_weixin_message' and r['outcome']=='ok'),{})
            reply=next((r for r in calls if r['name'] in ('reply_weixin_message','reply_weixin_attachment') and r['outcome']=='ok'),{})
            if read_trace:read={'started_at':first('read_start','ok'),'completed_at':read_trace['at'],'elapsed_ms':read_trace['elapsed_ms']}
            if reply_trace:reply={'elapsed_ms':reply_trace['elapsed_ms']}
            times={'wechat_created':m['created_at'],'bridge_received':first('received'),'event_enqueued':first('event_enqueued'),
                   'event_started':first('event_start'),'callback_acknowledged':first('event_result','acknowledged'),
                   'read_dispatched':first('read_dispatch'),'read_thread_started':read_thread.get('at'),
                   'read_started':read.get('started_at'),'read_completed':read.get('completed_at'),
                   'reply_queued':first('reply_queued'),'send_started':first('send_start'),'weixin_first_accepted':first('weixin_accepted')}
            def delta(a,b):
                return round((times[b]-times[a])*1000,3) if times[a] is not None and times[b] is not None else None
            output.append({'message_id':mid,'timestamps_unix':times,'phase_ms':{
                'receive_to_event':delta('bridge_received','event_started'),
                'event_to_ack_including_retries':delta('event_started','callback_acknowledged'),
                'callback_to_read':delta('callback_acknowledged','read_started'),
                'callback_to_dispatch':delta('callback_acknowledged','read_dispatched'),
                'dispatch_to_read_thread':read_thread.get('elapsed_ms'),
                'read_server':read.get('elapsed_ms'),'read_to_reply_queue':delta('read_started','reply_queued'),
                'reply_tool_server':reply.get('elapsed_ms'),'queue_to_send':delta('reply_queued','send_started'),
                'send_to_first_accept':delta('send_started','weixin_first_accepted'),
                'receive_to_first_accept':delta('bridge_received','weixin_first_accepted')},
                'timeline':timeline,'tool_calls':calls})
        return {'messages':output,'notes':['Local timestamps use the bridge clock; wechat_created uses the sender clock and is not used for local durations.',
            'Null means not observed, including messages from before instrumentation. Negative cross-stage durations may occur when a read precedes callback completion.',
            'Dispatch phases describe the first instrumented validated read request; thread queue time uses a monotonic clock. Successful read timing may describe another request.',
            'Tool server time excludes platform scheduling, Tunnel transit and client rendering. First acceptance does not establish final delivery or completion of all reply parts.']}
