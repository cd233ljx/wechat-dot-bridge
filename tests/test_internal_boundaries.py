"""Import isolation and the real Store host shared by its two mixins."""

import base64
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.store import Store


ROOT = Path(__file__).absolute().parent.parent


class InternalBoundariesTests(unittest.TestCase):
    def test_sdk_and_offline_helpers_work_without_importing_dispatcher_or_store(self):
        # A fresh interpreter avoids passing only because another test imported
        # the old back edges first. Neither the SDK listing nor backup needs a
        # business dispatcher, initialized Store or running receive process.
        code = '''
import importlib.abc
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

class BlockBusinessImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'wechat_dot_bridge.mcp', 'wechat_dot_bridge.store'}:
            raise AssertionError('business import reached from shared helper')

sys.meta_path.insert(0, BlockBusinessImports())
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.sdk_adapter import SDKAdapter, _bridge_error
from wechat_dot_bridge.tool_contract import tool_definitions
from wechat_dot_bridge.maintenance import managed_backup
from wechat_dot_bridge.operations import restore

adapter = SDKAdapter(SimpleNamespace())
listed = adapter.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
assert {tool['name'] for tool in listed['result']['tools']} == {tool['name'] for tool in tool_definitions()}
assert len(listed['result']['tools']) == 11
assert _bridge_error(BridgeError('fixture-private://must-not-escape')).data == {'reason': 'internal_error'}
assert adapter.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'fixture-private://unknown'})['error']['message'] == 'Method not found'
with tempfile.TemporaryDirectory() as directory:
    db = sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE messages(id TEXT); CREATE TABLE replies(id TEXT); CREATE TABLE binding(enabled INTEGER); INSERT INTO binding VALUES(1);')
    db.commit()
    host = SimpleNamespace(db=db, lock=threading.RLock())
    backups = Path(directory) / 'backups'
    result = managed_backup(host, backups)
    assert result['integrity'] == 'ok'
    snapshot = next(backups.glob('bridge-backup-*.sqlite3'))
    restored = Path(directory) / 'restored'
    assert restore(snapshot, restored)['restored']
    with sqlite3.connect(restored / 'bridge.sqlite3') as recovered:
        assert recovered.execute('SELECT enabled FROM binding').fetchone()[0] == 0
    db.close()
assert 'wechat_dot_bridge.mcp' not in sys.modules
assert 'wechat_dot_bridge.store' not in sys.modules
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_both_mixins_share_one_transaction_and_readers_cannot_see_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state', clock=lambda: 1000)
            self.addCleanup(store.close)
            store.import_binding({'id': 'fixture-account', 'owner': 'fixture-owner',
                                  'token': 'fixture-token', 'base_url': 'https://ilinkai.weixin.qq.com'})
            store.set_enabled(True)
            store.ingest('fixture-account', [{'source_id': 'fixture-input', 'peer': 'fixture-owner',
                         'text': 'synthetic input', 'created_at': 1000,
                         'context_token': 'fixture-context'}], 'fixture-cursor')
            mid = store.list_pending()[0]['message_id']
            before = store.work_status(mid)
            reader_started, reader_done = threading.Event(), threading.Event()
            observed, statements = [], []
            store.db.set_trace_callback(statements.append)

            def read():
                reader_started.set()
                observed.append((store.work_status(mid), store.attachment_metadata(mid)))
                reader_done.set()

            thread = threading.Thread(target=read)
            try:
                with self.assertRaisesRegex(BridgeError, '^fixture_abort$'):
                    with store._transaction():
                        store._ingest_attachments(store._binding(), mid, [
                            {'kind': 'file', 'name': 'fixture.json', 'error': 'media_invalid'}])
                        store._start_work(mid)
                        self.assertEqual(store.work_status(mid)['state'], 'active')
                        self.assertEqual(len(store.attachment_metadata(mid)), 1)
                        thread.start()
                        self.assertTrue(reader_started.wait(2))
                        self.assertFalse(reader_done.wait(0.05))
                        raise BridgeError('fixture_abort')
            finally:
                if thread.ident is not None:
                    thread.join(2)
                store.db.set_trace_callback(None)
            self.assertFalse(thread.is_alive())
            self.assertEqual(observed, [(before, [])])
            self.assertEqual(store.db.execute('SELECT count(*) FROM processing').fetchone()[0], 0)
            self.assertEqual(statements.count('BEGIN IMMEDIATE'), 1)
            self.assertEqual(statements.count('ROLLBACK'), 1)
            self.assertNotIn('COMMIT', statements)
            # The same composition also commits a staged attachment and work
            # completion through a normal owner reply, with no nested BEGIN.
            attachment = store.stage_attachment(base64.b64encode(b'{}').decode(), 'file', 'fixture.json')
            reply = store.queue_reply(mid, '', attachment_id=attachment['attachment_id'])
            self.assertEqual(reply['work']['state'], 'completed')
            self.assertEqual(store.read_attachment(attachment['attachment_id'])['data'], b'{}')
            store.close()


if __name__ == '__main__':
    unittest.main()
