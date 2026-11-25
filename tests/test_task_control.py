"""Stop actual tool loops and restore request status without replaying work."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from labagent.investigation import investigate
from labagent.cloud_workflow import CloudWorkflow
from labagent.material_service import MaterialService
from labagent.progress import progress_to, execution_summary
from labagent.server import create_server, Handler
from labagent.task_control import task_control
from labagent.tasks import Tasks
from test_investigation import ENV, completion, stages
from test_personal import RequestSocket, PASSWORD
from test_workspace_archive import workspace

ROOT = Path(__file__).resolve().parents[1]


class TaskControlTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / 'runtime/tests'
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_cancel_after_inflight_model_prevents_returned_tool_calls(self):
        stop = threading.Event()
        calls = []
        def model(messages):
            calls.append('model')
            stop.set()
            return completion(messages)
        with patch.dict(os.environ, ENV), task_control(stop.is_set):
            result = investigate(stages()[0], completion=model)
        self.assertEqual(calls, ['model'])
        self.assertEqual(result['llm']['status'], 'cancelled')
        self.assertEqual(result['llm']['tool_trace'], [])
        self.assertEqual(result['status'], 'cancelled')

    def test_cancel_between_device_calls_preserves_first_output(self):
        stop = threading.Event()
        class Device:
            tools = [{'type':'function', 'function': {'name':'lab_fixture', 'description':'fixture',
                     'parameters': {'type':'object', 'properties':{}, 'required':[], 'additionalProperties':False}}}]
            bindings = {'lab_fixture': {}}
            warnings = []
            calls = 0
            def execute(self, name, arguments):
                self.calls += 1
                stop.set()
                return {'device':'R1', 'command':'display ip interface brief', 'text':'synthetic up up'}
        device = Device()
        def model(_):
            return {'tool_calls':[{'id':str(i),'function':{'name':'lab_fixture','arguments':'{}'}} for i in range(2)]}
        with patch.dict(os.environ, ENV), task_control(stop.is_set), progress_to(None):
            result = investigate(stages()[0], completion=model, lab_tools=device)
            result['execution'] = execution_summary(result)
        self.assertEqual(device.calls, 1)
        self.assertEqual(result['execution']['status'], 'cancelled')
        self.assertEqual(len(result['llm']['tool_trace']), 1)
        self.assertIn('synthetic up up', str(result['collected_materials']))

    def test_workspace_restore_retains_interrupted_input_without_credentials(self):
        source, target = workspace(self.folder/'source'), workspace(self.folder/'target')
        tasks = Tasks(source.store)
        payload = stages()[0]
        payload.update(learning={'level':3,'access_token':'private-grant'}, personal_memory=[{'secret':'private-memory'}])
        task_id = tasks.create(payload)
        with source.export() as archive:
            content = archive.read_bytes()
        backup = self.folder/'backup.zip'
        backup.write_bytes(content)
        target.restore(backup)
        restored = Tasks(target.store).get(task_id)
        self.assertEqual(restored['state'], 'interrupted')
        self.assertEqual(restored['input']['configs'], payload['configs'])
        self.assertNotIn('private-grant', json.dumps(restored))
        self.assertNotIn('private-memory', json.dumps(restored))

    def test_completed_result_wins_restart_before_terminal_event(self):
        space = workspace(self.folder/'race')
        tasks = Tasks(space.store)
        payload = stages()[0]
        task_id = tasks.create(payload)
        with patch.dict(os.environ, ENV), progress_to(None, turn_id=task_id):
            result = investigate(payload, completion=completion)
            result['execution'] = execution_summary(result)
        saved = space.store.save(payload, result)
        recovered = Tasks(space.store).get(task_id)
        self.assertEqual(recovered['state'], 'completed')
        self.assertEqual(recovered['result']['session_id'], saved['session_id'])
        self.assertEqual(len(recovered['result']['dialogue']), 1)


class TaskHandlerTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / 'runtime/tests'
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {**ENV, 'LABAGENT_MCP_CONFIG':'', 'LABAGENT_GUIDED_WORKFLOW':'', 'LABAGENT_CLOUD_PERSISTENCE':''})
        environment.start()
        self.addCleanup(environment.stop)
        self.server = create_server(0, Path(self.temp.name)/'web.sqlite3', diagnosis_backend='local')
        self.addCleanup(self.server.server_close)
        self.owner, self.token = self.server.personal.authenticate({'username':'alice','password':PASSWORD}, register=True)
        _, self.other_token = self.server.personal.authenticate({'username':'bob','password':PASSWORD}, register=True)

    def request(self, method, path, payload=None, token=None):
        body = json.dumps(payload or {}).encode() if method == 'POST' else b''
        cookie = 'Cookie: labagent_personal=' + token + '\r\n' if token else ''
        raw = (f'{method} {path} HTTP/1.0\r\nHost: 127.0.0.1:{self.server.server_port}\r\n'
               f'{cookie}Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body
        sock = RequestSocket(raw)
        with patch.object(Handler, 'log_message'):
            self.server.RequestHandlerClass(sock, ('127.0.0.1', 12345), self.server)
        head, body = sock.output.getvalue().split(b'\r\n\r\n',1)
        status = int(head.split(b' ')[1])
        if b'application/x-ndjson' in head:
            return status, [json.loads(line) for line in body.splitlines() if line]
        return status, json.loads(body)

    def test_refresh_cancel_owner_isolation_and_duplicate_request(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def model(messages, **kwargs):
            calls.append('model')
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test did not release model')
            return completion(messages)
        payload = {**stages()[0], 'request_id':'a'*32}
        with patch('labagent.investigation.chat_completion', side_effect=model), ThreadPoolExecutor(1) as executor:
            future = executor.submit(self.request, 'POST', '/api/diagnose/stream', payload, self.token)
            try:
                self.assertTrue(entered.wait(10))
                status, snapshot = self.request('GET', '/api/tasks/'+'a'*32, token=self.token)
                self.assertEqual(status, 200)
                self.assertEqual(snapshot['input']['configs'], payload['configs'])
                self.assertEqual(snapshot['state'], 'running')
                status, small = self.request('GET', '/api/tasks/'+'a'*32+'?status_only=1', token=self.token)
                self.assertNotIn('input', small)
                self.assertEqual(self.request('GET', '/api/tasks/'+'a'*32, token=self.other_token)[0], 404)
                self.assertEqual(self.request('POST', '/api/tasks/'+'a'*32+'/cancel', {}, self.other_token)[0], 404)
                status, cancelled = self.request('POST', '/api/tasks/'+'a'*32+'/cancel', {}, self.token)
                self.assertEqual((status, cancelled['state']), (200, 'cancelling'))
            finally:
                release.set()
            status, events = future.result(10)
        self.assertEqual(events[-1]['result']['execution']['status'], 'cancelled')
        self.assertEqual(events[-1]['result']['llm']['tool_trace'], [])
        status, recovered = self.request('GET', '/api/tasks/'+'a'*32, token=self.token)
        self.assertEqual(recovered['state'], 'cancelled')
        self.assertEqual(len(recovered['result']['dialogue']), 1)
        self.assertEqual(self.request('POST','/api/diagnose/stream',payload,self.token)[0], 409)
        self.assertEqual(calls, ['model'])

    def test_runtime_token_only_reads_its_own_cancellation_and_is_revoked(self):
        task_id = self.server.tasks.create(stages()[0], owner=self.owner['id'])
        with self.server.tasks.runtime_connection(task_id, 'https://lab.invalid') as connection:
            self.assertEqual(self.server.tasks.runtime_status(connection['token']), {'cancelled':False})
            self.server.tasks.cancel(task_id)
            self.assertEqual(self.server.tasks.runtime_status(connection['token']), {'cancelled':True})
        with self.assertRaises(ValueError):
            self.server.tasks.runtime_status(connection['token'])
        snapshot = self.server.tasks.get(task_id)
        self.assertNotIn(connection['token'], json.dumps(snapshot))

    def test_cloud_runtime_observes_stop_through_actual_http_callback(self):
        serving = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.05), daemon=True)
        serving.start()
        self.addCleanup(lambda: (self.server.shutdown(), serving.join(5)))
        entered, release = threading.Event(), threading.Event()
        messages_seen = []
        def model(messages):
            messages_seen.extend(deepcopy(messages))
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test did not release runtime model')
            return completion(messages)
        service = MaterialService(Path(self.temp.name)/'runtime.sqlite3', model)
        class Client:
            def invoke(self, request):
                return service.invoke(request)
        workflow = CloudWorkflow(self.server.tasks.store, lambda _: Client())
        payload = {**stages()[0], 'learning':{'level':1}}
        task_id = self.server.tasks.create(payload, owner=self.owner['id'])
        base = 'http://127.0.0.1:' + str(self.server.server_port)
        with patch.dict(os.environ, {'LABAGENT_LAB_RELAY_URL':base+'/api/lab/runtime/collect'}), self.server.tasks.runtime_connection(task_id, base) as connection, ThreadPoolExecutor(1) as executor:
            future = executor.submit(workflow.run, payload, task_connection=connection)
            try:
                self.assertTrue(entered.wait(10))
                self.server.tasks.cancel(task_id)
            finally:
                release.set()
            result = future.result(10)
        self.assertEqual(result['llm']['status'], 'cancelled')
        self.assertEqual(result['llm']['tool_trace'], [])
        history = service.store.history(result['agentarts']['session_id'])
        self.assertEqual(len(history['attempts']), 1)
        self.assertNotIn(connection['token'], json.dumps(history))
        self.assertNotIn(connection['token'], json.dumps(messages_seen))


if __name__ == '__main__':
    unittest.main()
