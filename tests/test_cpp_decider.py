"""Exercise real JSONL process supervision with controlled OOM failures."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cpp_decider as cpp
import decider_runtime as runtime

FIXTURE = r'''
import json,sys,pathlib,os,time
cfg=json.loads(pathlib.Path(sys.argv[1]).read_text('utf-8'));cap=int(sys.argv[2])
layers=8 if cap<0 else cap
def record(item):
 with pathlib.Path(cfg['record']).open('a',encoding='utf-8') as f:f.write(json.dumps(item,ensure_ascii=False)+'\n')
identity={**cfg['identity'],'device':'cuda:0' if layers else 'cpu','requested_device':'auto',
          'gpu_layers':layers,'total_layers':32,'cpu_assisted':0<layers<33}
if cfg['mode']=='wrong_identity':identity['llama_commit']='wrong'
def emit(item):print(json.dumps({**identity,**item}),flush=True)
def fail():
 print('YEBISU_PPDD_OOM',file=sys.stderr,flush=True);os._exit(137)
record({'event':'start','cap':cap,'pid':os.getpid()})
emit({'event':'loading','ok':True})
if cfg['mode']=='startup_oom' and layers:fail()
if cfg['mode']=='startup_json_oom' and layers:
 print('YEBISU_PPDD_OOM',file=sys.stderr,flush=True)
 emit({'event':'startup_error','ok':False,'error':{'code':'allocation_failed','recoverable':False}})
 os._exit(1)
if cfg['mode']=='stale_oom':print('YEBISU_PPDD_OOM',file=sys.stderr,flush=True)
emit({'event':'ready','ok':True})
for line in sys.stdin:
 e=json.loads(line);record({'event':'request','request':e['request'],'cap':cap})
 if cfg['mode']=='stale_oom' and e['request'].get('bad'):
  emit({'id':e['id'],'ok':False,'error':{'code':'invalid_request','recoverable':True}});continue
 if cfg['mode']=='decode_oom' and layers:fail()
 if cfg['mode']=='timeout':time.sleep(30)
 emit({'id':e['id'],'ok':True,'result':{'answers':{'meaning_shift':{'type':'choice','choice':'preserved'}}}})
'''


class CppWorkerTests(unittest.TestCase):
    def fixture(self, mode):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        script = directory / 'worker.py'
        script.write_text(FIXTURE, 'utf-8')
        log = directory / 'events.jsonl'
        cfg = {'backend': 'llama_cpp', 'device': 'auto', 'max_length': 2048,
               'startup_timeout_seconds': 10, 'request_timeout_seconds': 10,
               'cpp_executable': sys.executable, 'record': str(log), 'mode': mode,
               'identity': {**cpp.IDENTITY, 'model': runtime.MODEL, 'revision': runtime.REVISION}}
        config = directory / 'config.json'
        config.write_text(json.dumps(cfg), 'utf-8')
        self.enterContext(patch.object(runtime, '_configuration', return_value=cfg))
        self.enterContext(patch.object(cpp, 'command', side_effect=lambda cfg, cap=-1, inspect=False:
                                     [sys.executable, '-u', str(script), str(config), str(cap)]))
        owner = runtime.DeciderRuntime(config)
        self.addCleanup(owner.stop)
        return owner, log

    def test_decode_oom_retries_same_request_at_half_layers_then_cpu_and_retains_cap(self):
        owner, log = self.fixture('decode_oom')
        request = {'state': '原文\r\n𠮷と😀', 'questions': {'meaning_shift': {'type': 'choice'}}}
        result = owner.decide(request, timeout=10)
        self.assertEqual(result['device'], 'cpu')
        self.assertEqual(result['oom_retries'], 2)
        owner.stop()  # A released model must also retain its reduced layer cap.
        owner.decide(request, timeout=10)
        events = [json.loads(line) for line in log.read_text('utf-8').splitlines()]
        self.assertEqual([x['cap'] for x in events if x['event'] == 'start'], [-1, 4, 0, 0])
        self.assertEqual([x['request'] for x in events if x['event'] == 'request'], [request] * 4)

    def test_startup_abort_retries_before_sending_text_and_only_cpu_receives_it(self):
        owner, log = self.fixture('startup_oom')
        self.assertEqual(owner.decide({'state': 'fixture'}, timeout=10)['oom_retries'], 2)
        events = [json.loads(line) for line in log.read_text('utf-8').splitlines()]
        self.assertEqual([x['cap'] for x in events if x['event'] == 'start'], [-1, 4, 0])
        self.assertEqual([x['cap'] for x in events if x['event'] == 'request'], [0])

    def test_identity_mismatch_never_retries_or_receives_the_request(self):
        owner, log = self.fixture('wrong_identity')
        with self.assertRaises(runtime.DeciderRuntimeError) as failure:
            owner.decide({'state': 'fixture'}, timeout=10)
        self.assertEqual(failure.exception.code, 'identity_mismatch')
        self.assertIsNone(owner._process)
        events = [json.loads(line) for line in log.read_text('utf-8').splitlines()]
        self.assertEqual(len(events), 1)

    def test_oom_rejection_waits_for_the_stderr_reader(self):
        owner, log = self.fixture('startup_json_oom')
        reader = owner._read_stderr
        def delayed(process, events):
            time.sleep(.06)
            reader(process, events)
        with patch.object(owner, '_read_stderr', side_effect=delayed):
            self.assertEqual(owner.decide({'state': 'fixture'}, timeout=10)['oom_retries'], 2)
        events = [json.loads(line) for line in log.read_text('utf-8').splitlines()]
        self.assertEqual([x['cap'] for x in events if x['event'] == 'start'], [-1, 4, 0])

    def test_old_warning_does_not_turn_invalid_input_into_an_oom_retry(self):
        owner, log = self.fixture('stale_oom')
        owner.decide({'state': 'warmup'}, timeout=10)
        with self.assertRaises(runtime.DeciderRuntimeError) as failure:
            owner.decide({'bad': True}, timeout=10)
        self.assertEqual(failure.exception.code, 'invalid_request')
        owner.decide({'state': 'still usable'}, timeout=10)
        events = [json.loads(line) for line in log.read_text('utf-8').splitlines()]
        self.assertEqual(sum(x['event'] == 'start' for x in events), 1)

    def test_timeout_does_not_trigger_an_oom_retry(self):
        owner, log = self.fixture('timeout')
        with self.assertRaises(runtime.DeciderRuntimeError) as failure:
            owner.decide({'state': 'fixture'}, timeout=.5)
        self.assertEqual(failure.exception.code, 'timeout')
        self.assertIsNone(owner._process)
        events = [json.loads(line) for line in log.read_text('utf-8').splitlines()]
        self.assertEqual(sum(x['event'] == 'start' for x in events), 1)

    def test_public_identity_rejects_false_device_and_cpu_assistance(self):
        ident = {**cpp.IDENTITY, 'model': runtime.MODEL, 'revision': runtime.REVISION,
                 'device': 'cpu', 'requested_device': 'auto', 'gpu_layers': 0, 'total_layers': 32, 'cpu_assisted': False}
        cpp.validate_identity(ident, {'device': 'auto'})
        for changed in ({'device': 'cuda:0'}, {'gpu_layers': 1}, {'cpu_assisted': True}, {'total_layers': 31}, {'gguf_sha256': 'wrong'}):
            with self.subTest(changed=changed), self.assertRaises(runtime.DeciderRuntimeError):
                cpp.validate_identity({**ident, **changed}, {'device': 'auto'})


class BackendSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.active = directory / 'config.json'
        self.old = {'enabled': True, 'backend': 'pytorch', 'device': 'cpu', 'service_timeout_seconds': 700}
        self.new = {'enabled': True, 'backend': 'llama_cpp', 'device': 'auto'}
        self.active.write_text(json.dumps(self.old), 'utf-8')
        self.candidate = directory / 'llama_cpp-config.json'
        self.candidate.write_text(json.dumps(self.new), 'utf-8')
        self.owner = runtime.DeciderRuntime(self.active)
        self.enterContext(patch.object(runtime, '_RUNTIME', self.owner))
        self.enterContext(patch.object(runtime, '_configuration', side_effect=lambda path: json.loads(Path(path).read_text('utf-8'))))

    def test_switch_keeps_reference_config_and_shared_service_settings(self):
        with patch.object(runtime, '_inspect_device', return_value={'ram_available_bytes': 20 * 2**30}), patch.object(self.owner, 'stop') as stop:
            self.assertEqual(runtime.local_set_backend('llama_cpp')['status'], 'saved')
            stop.assert_called_once()
        self.assertEqual(json.loads(self.active.read_text('utf-8'))['service_timeout_seconds'], 700)
        self.assertEqual(json.loads(self.active.with_name('pytorch-config.json').read_text('utf-8')), self.old)
        self.assertEqual(runtime.prepared_backends(), {'llama_cpp': True, 'pytorch': True})

    def test_failed_resource_check_keeps_active_settings(self):
        original = self.active.read_bytes()
        with patch.object(runtime, '_inspect_device', side_effect=runtime.DeciderRuntimeError('insufficient_memory', '')):
            with self.assertRaises(runtime.DeciderRuntimeError):
                runtime.local_set_backend('llama_cpp')
        self.assertEqual(self.active.read_bytes(), original)

    def test_prepared_updated_binary_keeps_cpu_preference_and_old_config(self):
        old = {**self.new, 'device': 'cpu', 'cpp_executable': 'old-worker', 'gguf_path': 'same-model'}
        new = {**self.new, 'cpp_executable': 'new-worker', 'gguf_path': 'same-model'}
        self.active.write_text(json.dumps(old), 'utf-8')
        self.candidate.write_text(json.dumps(new), 'utf-8')
        with patch.object(cpp, 'configuration', side_effect=lambda value: value), patch.object(runtime, '_inspect_device', return_value={}):
            self.assertEqual(runtime.local_set_backend('llama_cpp')['status'], 'saved')
        saved = json.loads(self.active.read_text('utf-8'))
        self.assertEqual((saved['cpp_executable'], saved['device']), ('new-worker', 'cpu'))
        self.assertEqual(json.loads(self.active.with_name('llama_cpp-previous-config.json').read_text('utf-8')), old)
        self.assertEqual(json.loads(self.candidate.read_text('utf-8')), new)

    def test_prepared_cpp_can_replace_a_missing_old_python_environment(self):
        def configure(path):
            value = json.loads(Path(path).read_text('utf-8'))
            if value.get('backend') == 'pytorch':
                raise runtime.DeciderRuntimeError('missing_runtime', '')
            return value
        with patch.object(runtime, '_configuration', side_effect=configure), patch.object(runtime, '_inspect_device', return_value={}):
            self.assertEqual(runtime.local_set_backend('llama_cpp')['status'], 'saved')
        self.assertEqual(json.loads(self.active.read_text('utf-8'))['backend'], 'llama_cpp')

    def test_concurrent_settings_change_is_not_overwritten(self):
        def change(*args):
            self.active.write_text(json.dumps({**self.old, 'service_timeout_seconds': 800}), 'utf-8')
            return {}
        with patch.object(runtime, '_inspect_device', side_effect=change):
            with self.assertRaises(runtime.DeciderRuntimeError) as failure:
                runtime.local_set_backend('llama_cpp')
        self.assertEqual(failure.exception.code, 'settings_changed')
        self.assertEqual(json.loads(self.active.read_text('utf-8'))['service_timeout_seconds'], 800)


if __name__ == '__main__':
    unittest.main()
