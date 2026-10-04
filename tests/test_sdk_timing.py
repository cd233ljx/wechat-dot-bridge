"""Validated read dispatch and AnyIO worker queue timing over SDK streams."""

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import anyio
from mcp.shared.message import SessionMessage
import mcp_types as types

from wechat_dot_bridge.events import Events
from wechat_dot_bridge.mcp import MCPServer
from wechat_dot_bridge.sdk_adapter import SDKAdapter
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.timing import record, report


class SDKReadTimingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = 1000000.0
        self.store = Store(Path(self.directory.name) / 'state', clock=lambda: self.now)
        self.store.import_binding(dict(id='fixture-bot', owner='fixture-owner',
            token='fixture-token', base_url='https://ilinkai.weixin.qq.com'))
        self.store.set_enabled(True)
        self.store.ingest('fixture-bot', [dict(source_id='fixture-source', peer='fixture-owner',
            text='fixture-private-body', created_at=self.now, context_token='fixture-context')], 'fixture-cursor')
        self.mid = self.store.list_pending()[0]['message_id']
        self.adapter = SDKAdapter(MCPServer(self.store, Events(self.store)))

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_sdk_limiter_wait_is_measured_without_blocking_protocol(self):
        record(self.store, self.mid, 'event_result', at=self.now, outcome='acknowledged')
        self.now += 2
        release = threading.Event()

        async def verify():
            limiter = anyio.to_thread.current_default_thread_limiter()
            previous_tokens = limiter.total_tokens
            limiter.total_tokens = 1
            occupied = anyio.Event()

            def occupy_worker():
                anyio.from_thread.run_sync(occupied.set)
                if not release.wait(5):
                    raise RuntimeError('fixture-worker-timeout')

            async def occupy():
                await anyio.to_thread.run_sync(occupy_worker)

            send, incoming = anyio.create_memory_object_stream(1)
            outgoing, receive = anyio.create_memory_object_stream(1)
            try:
                async with send, receive, anyio.create_task_group() as tasks:
                    tasks.start_soon(occupy)
                    await occupied.wait()
                    tasks.start_soon(self.adapter.run_streams, incoming, outgoing)
                    await send.send(SessionMessage(types.JSONRPCRequest(jsonrpc='2.0', id=1,
                        method='tools/call', params={'name': 'read_weixin_message',
                                                   'arguments': {'message_id': self.mid}})))
                    with anyio.fail_after(2):
                        while limiter.statistics().tasks_waiting != 1:
                            await anyio.sleep(.005)
                    # Both timestamps are written by the existing read worker,
                    # so waiting for the limiter never writes SQLite on the loop.
                    with self.store.lock:
                        self.assertFalse(self.store.db.execute(
                            "SELECT 1 FROM message_timeline WHERE stage IN ('read_dispatch','read_thread_start')").fetchone())
                    await send.send(SessionMessage(types.JSONRPCRequest(jsonrpc='2.0', id=2,
                                                                         method='tools/list')))
                    with anyio.fail_after(2):
                        listed = await receive.receive()
                    self.assertEqual(listed.message.id, 2)
                    self.assertEqual(len(listed.message.result['tools']), 11)
                    await anyio.sleep(.05)
                    self.now += 300  # A wall-clock jump while the worker is queued.
                    release.set()
                    with anyio.fail_after(2):
                        result = (await receive.receive()).message
                    self.assertEqual(result.id, 1)
                    self.assertFalse(result.result['isError'])
                    self.assertEqual(result.result['structuredContent']['text'], 'fixture-private-body')
                    tasks.cancel_scope.cancel()
            finally:
                release.set()
                limiter.total_tokens = previous_tokens

        anyio.run(verify)
        value = report(self.store)['messages'][0]
        phase = value['phase_ms']
        self.assertEqual(phase['callback_to_dispatch'], 2000)
        self.assertGreaterEqual(phase['dispatch_to_read_thread'], 50)
        self.assertLess(phase['dispatch_to_read_thread'], 2000)
        self.assertEqual(value['timestamps_unix']['read_thread_started'], self.now)
        self.assertLess(phase['read_server'], phase['dispatch_to_read_thread'])
        self.assertNotIn('fixture-private', json.dumps(value))

    def test_invalid_arguments_and_failed_diagnostics_preserve_tool_contract(self):
        rejected = self.adapter.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'read_weixin_message',
                       'arguments': {'message_id': self.mid, 'unexpected': 'fixture-private'}}})
        self.assertEqual(rejected['error']['code'], -32602)
        self.assertEqual(self.store.work_status(self.mid)['state'], 'waiting')
        with patch('wechat_dot_bridge.timing.record', side_effect=RuntimeError('fixture-private-timing-error')):
            result = self.adapter.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                'params': {'name': 'read_weixin_message', 'arguments': {'message_id': self.mid}}})
        self.assertFalse(result['result']['isError'])
        self.assertEqual(result['result']['structuredContent']['text'], 'fixture-private-body')
        value = report(self.store)['messages'][0]
        self.assertIsNone(value['phase_ms']['callback_to_dispatch'])
        self.assertIsNone(value['phase_ms']['dispatch_to_read_thread'])
        self.assertNotIn('fixture-private', json.dumps(value))

    def test_second_trace_failure_rolls_back_pair_and_preserves_read_measurement(self):
        def failing_trace(store, mid, stage, **kwargs):
            if stage == 'read_thread_start':
                raise RuntimeError('fixture-private-second-trace-error')
            return record(store, mid, stage, **kwargs)

        with patch('wechat_dot_bridge.timing.record', side_effect=failing_trace):
            result = self.adapter.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'read_weixin_message', 'arguments': {'message_id': self.mid}}})
        self.assertFalse(result['result']['isError'])
        self.assertEqual(result['result']['structuredContent']['text'], 'fixture-private-body')
        value = report(self.store)['messages'][0]
        self.assertFalse([item for item in value['timeline'] if item['stage'] in ('read_dispatch', 'read_thread_start')])
        self.assertIsNone(value['phase_ms']['callback_to_dispatch'])
        self.assertIsNone(value['phase_ms']['dispatch_to_read_thread'])
        self.assertIsNotNone(value['phase_ms']['read_server'])
        self.assertNotIn('fixture-private', json.dumps(value))

    def test_concurrent_reads_commit_pairs_without_exposing_partial_dispatch(self):
        first_dispatched = self.now
        first_written, release = threading.Event(), threading.Event()
        second_entered, query_entered, query_finished = (threading.Event() for _ in range(3))
        results, errors, snapshots = [], [], []
        raw = {'name': 'read_weixin_message', 'arguments': {'message_id': self.mid}}

        def paused_trace(store, mid, stage, **kwargs):
            record(store, mid, stage, **kwargs)
            if stage == 'read_dispatch' and kwargs['at'] == first_dispatched:
                first_written.set()
                if not release.wait(5):
                    raise RuntimeError('fixture-trace-timeout')

        def read(dispatched_at, entered=None):
            if entered:
                entered.set()
            try:
                results.append(self.adapter._read_tool(raw, dispatched_at, time.perf_counter()))
            except Exception as error:
                errors.append(error)

        def query():
            query_entered.set()
            try:
                snapshots.append(report(self.store))
            except Exception as error:
                errors.append(error)
            finally:
                query_finished.set()

        threads = []
        with patch('wechat_dot_bridge.timing.record', side_effect=paused_trace):
            try:
                threads.append(threading.Thread(target=read, args=(first_dispatched,)))
                threads[0].start()
                self.assertTrue(first_written.wait(2))
                self.now += 2
                threads.append(threading.Thread(target=read, args=(self.now, second_entered)))
                threads.append(threading.Thread(target=query))
                for thread in threads[1:]:
                    thread.start()
                self.assertTrue(second_entered.wait(2))
                self.assertTrue(query_entered.wait(2))
                self.assertFalse(query_finished.wait(.1), 'Status query observed an uncommitted dispatch')
            finally:
                release.set()
                for thread in threads:
                    thread.join(5)
        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not value['isError'] for value in results))
        traces = [item for item in report(self.store)['messages'][0]['timeline']
                  if item['stage'] in ('read_dispatch', 'read_thread_start')]
        self.assertEqual([item['stage'] for item in traces],
                         ['read_dispatch', 'read_thread_start', 'read_dispatch', 'read_thread_start'])
        self.assertEqual([item['at'] for item in traces],
                         [first_dispatched, first_dispatched, self.now, self.now])
        for value in (snapshots[0]['messages'][0], report(self.store)['messages'][0]):
            self.assertEqual(value['timestamps_unix']['read_dispatched'], first_dispatched)
            self.assertEqual(value['timestamps_unix']['read_thread_started'], first_dispatched)
            self.assertEqual(value['phase_ms']['dispatch_to_read_thread'], traces[1]['elapsed_ms'])


if __name__ == '__main__':
    unittest.main()
