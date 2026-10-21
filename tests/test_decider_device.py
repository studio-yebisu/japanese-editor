"""Device changes never overwrite unrelated settings or stop a busy model."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decider_runtime as runtime
from tools import decider_worker as worker


class DeviceSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / 'config.json'
        self.saved = {'device': 'cpu', 'cpu_dtype': 'bfloat16', 'enabled': True, 'model_path': 'preserve', 'max_length': 2048}
        self.path.write_text(json.dumps(self.saved), 'utf-8')
        self.owner = runtime.DeciderRuntime(self.path)
        self.enterContext(patch.object(runtime, '_RUNTIME', self.owner))
        self.enterContext(patch.object(runtime, '_configuration', side_effect=lambda path: json.loads(path.read_text('utf-8'))))

    def test_resource_failure_preserves_config_and_waiting_model(self):
        original = self.path.read_bytes()
        with patch.object(runtime, '_inspect_device', side_effect=runtime.DeciderRuntimeError('insufficient_memory', '', {'cuda_available_bytes': 2 * 2**30, 'cuda_required_bytes': 11 * 2**30})), \
             patch.object(self.owner, 'stop') as stop:
            with self.assertRaises(runtime.DeciderRuntimeError) as failure:
                runtime.local_set_device('cuda:0')
            self.assertEqual(failure.exception.code, 'insufficient_memory')
            stop.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_success_changes_only_device_after_resource_checks(self):
        with patch.object(runtime, '_inspect_device', return_value={'cuda_available_bytes': 12 * 2**30}) as inspect, patch.object(self.owner, 'stop') as stop:
            self.assertEqual(runtime.local_set_device('cuda:0')['status'], 'saved')
        self.assertEqual(inspect.call_count, 2)
        stop.assert_called_once()
        self.assertEqual(json.loads(self.path.read_text('utf-8')), {**self.saved, 'device': 'cuda:0'})
        self.assertEqual(list(self.path.parent.glob('.device-*')), [])

    def test_busy_worker_rejects_device_changes_and_release(self):
        entered, leave = threading.Event(), threading.Event()
        def hold():
            with self.owner._lock:
                entered.set()
                leave.wait(2)
        worker = threading.Thread(target=hold)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            for operation in (lambda: runtime.local_set_device('cuda:0'), runtime.local_release):
                with self.assertRaises(runtime.DeciderRuntimeError) as failure:
                    operation()
                self.assertEqual(failure.exception.code, 'busy')
        finally:
            leave.set()
            worker.join(2)

    def test_recheck_failure_and_external_config_update_never_save_device(self):
        original = self.path.read_bytes()
        with patch.object(runtime, '_inspect_device', side_effect=[{}, runtime.DeciderRuntimeError('insufficient_memory', '')]), patch.object(self.owner, 'stop'):
            with self.assertRaises(runtime.DeciderRuntimeError):
                runtime.local_set_device('cuda:0')
        self.assertEqual(self.path.read_bytes(), original)
        def external_change(*_args):
            self.path.write_text(json.dumps({**self.saved, 'max_length': 8192}), 'utf-8')
            return {}
        with patch.object(runtime, '_inspect_device', side_effect=external_change), patch.object(self.owner, 'stop') as stop:
            with self.assertRaises(runtime.DeciderRuntimeError) as failure:
                runtime.local_set_device('cuda:0')
            self.assertEqual(failure.exception.code, 'settings_changed')
            stop.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text('utf-8'))['max_length'], 8192)

    def test_windows_checks_dedicated_vram_when_cuda_reports_larger_budget(self):
        torch = Mock()
        torch.cuda.is_available.return_value = True
        torch.cuda.mem_get_info.return_value = (13 * 2**30, 24 * 2**30)
        torch.cuda.get_device_properties.return_value.uuid = '11111111-2222-3333-4444-555555555555'
        with patch.object(worker.sys, 'platform', 'win32'), patch.object(worker, 'available_ram', return_value=20 * 2**30), \
             patch.object(worker.subprocess, 'run', return_value=Mock(returncode=0, stdout='2048, 24564')) as query:
            with self.assertRaises(worker.WorkerError) as failure:
                worker.preflight({'device': 'cuda:0'}, torch)
        self.assertEqual(failure.exception.code, 'insufficient_memory')
        self.assertEqual(failure.exception.details['cuda_available_bytes'], 2 * 2**30)
        self.assertIn('--id=GPU-11111111-2222-3333-4444-555555555555', query.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
