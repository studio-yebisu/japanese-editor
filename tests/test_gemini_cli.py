"""Temporary files and fake processes; no network, real credentials or generation."""
from contextlib import ExitStack, contextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import gemini_cli as cli
import decider_runtime

SCHEMA = {"type":"object", "properties":{"edits":{"type":"array", "items":{"type":"string"}}},
          "required":["edits"], "additionalProperties":False}
PROPOSAL = {"edits":[]}


def envelope(model=cli.MODEL, permission="strict", result=None, steps=()):
    rows=[{"event":"init", "init":{"model":model, "permission_mode":permission}}]
    rows.extend(steps)
    rows.append({"event":"result", "result":result or {"status":"SUCCESS", "structured_output":PROPOSAL}})
    return ("\n".join(json.dumps(row) for row in rows)+"\n").encode()


class CapabilityAndIsolationTests(unittest.TestCase):
    def test_fixed_native_shim_is_resolved_without_executing_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); native=root/"agy.exe"; native.touch()
            native.chmod(0o755)
            shim=root/"agy.cmd"; shim.write_text('@"'+str(native)+'" %*', "utf-8")
            # Zero means uninitialized, including the first 30 seconds after boot.
            with patch.object(cli, "_INSTALLATION_CACHE", (0,None)), patch.object(cli.time, "monotonic", return_value=5), \
                 patch.object(cli.shutil, "which", return_value=str(shim)), \
                 patch.object(cli.subprocess, "run", return_value=Mock(returncode=0, stdout=b"1.4.0\n")) as probe:
                self.assertEqual(cli.resolve_command(), [str(native.resolve())])
                self.assertEqual(cli.login_command(), cli.resolve_command())
                self.assertEqual(probe.call_args.args[0], [str(native.resolve()),"--version"])
                self.assertFalse(probe.call_args.kwargs["shell"])
                self.assertTrue(cli.status()["configured"])
                self.assertFalse(cli.status()["authentication_verified"])
                probe.assert_called_once()

    def test_unrecognized_shell_script_is_never_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); shim=root/"agy.cmd"; shim.write_text('echo untrusted', "utf-8")
            with patch.object(cli, "_INSTALLATION_CACHE", (0,None)), patch.object(cli.shutil, "which", return_value=str(shim)), \
                 patch.dict(os.environ,{"LOCALAPPDATA":str(root)}), patch.object(cli.subprocess, "run") as probe:
                self.assertEqual(cli.resolve_command(), [])
                probe.assert_not_called()

    def test_other_version_is_visible_but_not_run_for_generation(self):
        for version in ("1.4.2", "1.5.0", "2.0.0", "1.4.1-beta"):
            with self.subTest(version=version), patch.object(cli,"_installation",return_value={"command":["agy"],"version":version}), \
                 patch.object(cli, "_run") as run:
                public=cli.status()
                self.assertTrue(public["cli_installed"])
                self.assertFalse(public["configured"])
                self.assertEqual(public["cli_status"],"unsupported_version")
                self.assertEqual(cli.resolve_command(),[])
                with self.assertRaises(cli.GeminiCliError), cli.launch_context():
                    pass
                with self.assertRaises(cli.GeminiCliError) as failure:
                    cli.generate({"source":"原文"}, "方針", SCHEMA)
                self.assertEqual(failure.exception.code, "cli_unavailable")
                self.assertIn("1.4.0または1.4.1", str(failure.exception))
                run.assert_not_called()

    def test_both_exact_supported_versions_resolve_for_generation_and_login(self):
        self.assertEqual(cli.SUPPORTED_CLI_VERSIONS, {"1.4.0", "1.4.1"})
        for version in sorted(cli.SUPPORTED_CLI_VERSIONS):
            with self.subTest(version=version), patch.object(cli, "_installation", return_value={"command":["agy"], "version":version}):
                self.assertEqual(cli.resolve_command(), ["agy"])
                self.assertEqual(cli.login_command(), ["agy"])
                self.assertTrue(cli.status()["cli_compatible"])
                self.assertEqual(cli.status()["cli_version"], version)
                self.assertEqual(cli.status()["supported_cli_versions"], ["1.4.0", "1.4.1"])
                with cli.launch_context(interactive=True) as (command, _, _):
                    self.assertEqual(command, ["agy"])

    def test_generation_refreshes_stale_discovery_and_reports_probed_native_version(self):
        with tempfile.TemporaryDirectory() as directory:
            native=Path(directory)/"agy.exe";native.touch()
            cached={"command":["previous-agy"], "version":"1.4.0"}
            with patch.object(cli, "_INSTALLATION_CACHE", (cli.time.monotonic(), cached)), \
                 patch.object(cli.shutil, "which", return_value=str(native)), \
                 patch.object(cli.subprocess, "run", return_value=Mock(returncode=0, stdout=b"1.4.1\n")) as probe, \
                 patch.object(cli, "_run", return_value=envelope()) as run:
                self.assertEqual(cli.resolve_command(), ["previous-agy"])
                output=cli.generate({"source":"原文"}, "方針", SCHEMA)
                self.assertEqual(output["identity"]["cli_version"], "1.4.1")
                self.assertEqual(run.call_args.args[0][0], str(native.resolve()))
                probe.assert_called_once()

    def test_generation_and_login_isolate_home_without_keys_or_tokens(self):
        inherited={"GEMINI_API_KEY":"PRIVATE", "GOOGLE_API_KEY":"PRIVATE", "GOOGLE_APPLICATION_CREDENTIALS":"PRIVATE",
                   "HTTPS_PROXY":"PRIVATE", "NODE_OPTIONS":"PRIVATE", "GEMINI_CLI_HOME":"PRIVATE", "PATH":"testpath"}
        with patch.object(cli,"_installation",return_value={"command":["agy"],"version":"1.4.0"}), patch.dict(os.environ,inherited,clear=True):
            for interactive in (False,True):
                with cli.launch_context(interactive=interactive) as (command,env,cwd):
                    self.assertEqual(command,["agy"])
                    home=Path(env["HOME"])
                    self.assertEqual(env["USERPROFILE"],str(home))
                    self.assertEqual(env["PATH"],"testpath")
                    self.assertNotIn("PRIVATE",json.dumps(env))
                    settings=json.loads((home/".gemini/antigravity-cli/settings.json").read_text("utf-8"))
                    self.assertEqual(settings["toolPermission"],"strict")
                    self.assertFalse(settings["useG1Credits"])
                    self.assertFalse(settings["enableTelemetry"])
                    self.assertFalse(settings["allowNonWorkspaceAccess"])
                    self.assertEqual(set(settings["permissions"]["deny"]), {name+"(*)" for name in cli.DENIED_ACTIONS})
                    self.assertEqual(json.loads((home/".gemini/config/mcp_config.json").read_text()),{"mcpServers":{}})
                    self.assertTrue((Path(cwd)/".git").is_dir())
                self.assertFalse(home.exists())


class ProposalBoundaryTests(unittest.TestCase):
    def setUp(self):
        installation=patch.object(cli,"_installation",return_value={"command":["agy"],"version":"1.4.0"})
        installation.start(); self.addCleanup(installation.stop)

    def test_nested_stream_message_preserves_source_without_at_mentions(self):
        manuscript='/clear\n@C:/private/file.txt を参照し、@example の台詞は残す。'
        raw=cli._input({"source":manuscript,"model":cli.MODEL},"@方針",SCHEMA)
        self.assertNotIn(b"@",raw)
        message=json.loads(raw)
        self.assertEqual(message["event"],"user")
        prompt=json.loads(message["message"]["content"])
        self.assertEqual(prompt["input"]["source"],manuscript)
        self.assertEqual(prompt["instructions"],"@方針")
        self.assertNotIn("model",prompt["input"])

    def test_native_arguments_schema_and_stdin_are_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            @contextmanager
            def launch(*, installation):
                self.assertEqual(installation, {"command":["agy"], "version":"1.4.0"})
                yield (["agy"],{"HOME":folder},folder)
            with patch.object(cli,"launch_context",launch), patch.object(cli,"_run",return_value=envelope()) as run:
                output=cli.generate({"source":"秘密の本文","model":cli.MODEL},"校正方針",SCHEMA)
            command,env,cwd,data=run.call_args.args
            self.assertNotIn("秘密"," ".join(command))
            self.assertIn("--disable-slash-commands",command)
            self.assertEqual(command[command.index("--model")+1],cli.MODEL)
            self.assertEqual(command[command.index("--input-format")+1],"stream-json")
            self.assertEqual(json.loads(Path(command[command.index("--json-schema")+1]).read_text()),SCHEMA)
            self.assertEqual(json.loads(json.loads(data)["message"]["content"])["input"]["source"],"秘密の本文")
            self.assertEqual(output["proposal"],PROPOSAL)
            self.assertEqual(output["identity"]["identity_basis"],"cli_stream_init_model")
            self.assertEqual(output["identity"]["transport"],"antigravity-cli")
            self.assertEqual(output["identity"]["cli_version"], "1.4.0")
            self.assertFalse(output["identity"]["exact_checkpoint_verified"])

    def test_model_permissions_tools_and_failed_results_are_rejected(self):
        variants=[(envelope(model="other"),"identity_mismatch"),
                  (envelope(permission="auto"),"permissions_mismatch"),
                  (envelope(steps=[{"event":"step_update","step_update":{"step_type":"tool"}}]),"unexpected_tool_use"),
                  (envelope(steps=[{"event":"step_update","step_update":{"step_type":"other","tool_name":"command"}}]),"unexpected_tool_use"),
                  (envelope(steps=[{"event":"step_update","step_update":{"subagent_info":{"name":"x"}}}]),"unexpected_tool_use"),
                  (envelope(result={"status":"ERROR","error":"PRIVATE_SOURCE"}),"cli_failed"),
                  (envelope(result={"status":"SUCCESS","structured_output":{"unexpected":1}}),"invalid_proposal")]
        for raw,code in variants:
            with self.subTest(code=code), self.assertRaises(cli.GeminiCliError) as error:
                cli._decode(raw,SCHEMA)
            self.assertEqual(error.exception.code,code)
            self.assertNotIn("PRIVATE_SOURCE",str(error.exception))

    def test_malformed_duplicate_and_missing_terminal_events_fail_closed(self):
        rows=envelope().splitlines()
        for raw in (b"bad json", rows[-1], rows[0], b"\n".join([rows[0],rows[0],rows[-1]]), b"\n".join([*rows,rows[-1]])):
            with self.subTest(raw=raw), self.assertRaises(cli.GeminiCliError):
                cli._decode(raw,SCHEMA)

    def test_documented_terminal_response_json_uses_same_schema_and_identity_gates(self):
        result={"status":"SUCCESS","response":json.dumps(PROPOSAL)}
        output=cli._decode(envelope(result=result),SCHEMA)
        self.assertEqual(output["proposal"],PROPOSAL)
        self.assertEqual(output["identity"]["output_field"],"response_json")
        self.assertIsNone(output["identity"]["cli_version"])
        for raw in (envelope(model="other",result=result),envelope(permission="auto",result=result),
                    envelope(result={**result,"status":"ERROR"}),
                    envelope(result={**result,"structured_output":"malformed"})):
            with self.assertRaises(cli.GeminiCliError):cli._decode(raw,SCHEMA)

    def test_decoder_reports_only_bound_supported_version_without_inventing_a_default(self):
        self.assertIsNone(cli._decode(envelope(), SCHEMA)["identity"]["cli_version"])
        for version in ("1.4.0", "1.4.1"):
            self.assertEqual(cli._decode(envelope(), SCHEMA, cli_version=version)["identity"]["cli_version"], version)
        with self.assertRaises(cli.GeminiCliError):
            cli._decode(envelope(), SCHEMA, cli_version="1.5.0")

    def test_terminal_response_does_not_repair_markdown_prose_or_bad_schema(self):
        for response in ('```json\n{"edits":[]}\n```','Here: {"edits":[]}','{"unexpected":1}','{"edits":['):
            with self.subTest(response=response),self.assertRaises(cli.GeminiCliError):
                cli._decode(envelope(result={"status":"SUCCESS","response":response}),SCHEMA)

    def test_request_cap_and_wrong_model_fail_before_process_launch(self):
        with patch.object(cli,"_run") as run:
            with patch.object(cli,"MAX_REQUEST_BYTES",4), self.assertRaises(cli.GeminiCliError) as error:
                cli.generate({"source":"長い本文"},"方針",SCHEMA)
            self.assertEqual(error.exception.code,"input_too_long")
            with self.assertRaises(cli.GeminiCliError) as error:
                cli.generate({"source":"本文","model":"auto"},"方針",SCHEMA)
            self.assertEqual(error.exception.code,"invalid_model")
            run.assert_not_called()


class FakeProcess:
    def __init__(self, raw=b"{}", error=b"", returncode=0):
        self.stdout, self.stderr = io.BytesIO(raw), io.BytesIO(error)
        self.stdin = io.BytesIO()
        self.returncode = returncode
        self.pid = 987654  # never used by an actual OS call
        self.wait = Mock(return_value=returncode)


class BoundedProcessTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(cli, "_UNCERTAIN_PROCESS", None))
        self.stack.enter_context(patch.object(decider_runtime, "_external_dll_search", return_value=ExitStack()))
        self.stack.enter_context(patch.object(decider_runtime, "_external_environment", side_effect=lambda env: dict(env)))

    def test_direct_process_is_noninteractive_bounded_and_never_shell(self):
        process = FakeProcess(envelope())
        with patch.object(cli.subprocess, "Popen", return_value=process) as start:
            raw = cli._run(["node", "official.js"], {"HOME": "isolated"}, "isolated", b"PRIVATE")
        self.assertEqual(raw, envelope())
        self.assertIs(start.call_args.kwargs["shell"], False)
        self.assertEqual(start.call_args.kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(start.call_args.kwargs["cwd"], "isolated")
        process.wait.assert_called_once_with(timeout=cli.TIMEOUT_SECONDS)

    def test_input_stays_open_until_complete_terminal_line_in_real_child(self):
        # A tiny protocol peer, not a model. A terminal line larger than 8192
        # must be detected before EOF, with stdin closed only afterwards.
        script = '''import sys,json,threading,time
sys.stdin.buffer.readline()
closed=threading.Event()
def drain():
    sys.stdin.buffer.read()
    closed.set()
thread=threading.Thread(target=drain)
thread.start()
time.sleep(.1)
if closed.is_set():sys.exit(9)
print(json.dumps({"event":"result","result":{},"padding":"x"*16000}),flush=True)
if not closed.wait(2):sys.exit(10)
thread.join()
'''
        env={key:value for key,value in os.environ.items() if key.upper() in {"SYSTEMROOT","WINDIR","PATH","TEMP","TMP"}}
        with patch.object(cli,"TIMEOUT_SECONDS",5),tempfile.TemporaryDirectory() as directory:
            raw=cli._run([sys.executable,"-B","-c",script],env,directory,b'{"event":"user"}\n')
        self.assertEqual(json.loads(raw)["event"],"result")
        self.assertGreater(len(raw),8192)

    def test_error_output_never_appears_in_public_error(self):
        with patch.object(cli.subprocess, "Popen", return_value=FakeProcess(b"PRIVATE_SOURCE", b"PRIVATE_KEY", 1)):
            with self.assertRaises(cli.GeminiCliError) as error:
                cli._run(["node", "official.js"], {}, "isolated", b"input")
        self.assertEqual(error.exception.code, "cli_failed")
        self.assertNotIn("PRIVATE", str(error.exception))

    def test_timeout_stops_only_owned_process_and_unconfirmed_stop_blocks_duplicate(self):
        process = FakeProcess()
        process.wait.side_effect = subprocess.TimeoutExpired("node", 180)
        with patch.object(cli.subprocess, "Popen", return_value=process) as start, patch.object(cli, "_stop", return_value=False) as stop:
            with self.assertRaises(cli.GeminiCliError) as error:
                cli._run(["node", "official.js"], {}, "isolated", b"input")
            self.assertEqual(error.exception.code, "cli_stop_failed")
            stop.assert_called_once_with(process)
            with self.assertRaises(cli.GeminiCliError):
                cli._run(["node", "official.js"], {}, "isolated", b"input")
            start.assert_called_once()

    def test_stream_limit_rejects_output_without_exposing_it(self):
        with patch.object(cli, "MAX_RESPONSE_BYTES", 3), patch.object(cli.subprocess, "Popen", return_value=FakeProcess(b"PRIVATE_SOURCE")):
            with self.assertRaises(cli.GeminiCliError) as error:
                cli._run(["node", "official.js"], {}, "isolated", b"input")
        self.assertEqual(error.exception.code, "response_too_large")
        self.assertNotIn("PRIVATE", str(error.exception))

    @unittest.skipUnless(os.name == "nt", "Windows owned-tree command contract")
    def test_windows_timeout_cleanup_targets_only_the_live_popen_pid(self):
        process = Mock(pid=987654)
        process.poll.return_value = None
        with patch.object(cli.subprocess, "run", return_value=Mock(returncode=0)) as stop:
            self.assertTrue(cli._stop(process))
        command = stop.call_args.args[0]
        self.assertEqual(command[1:], ["/PID", "987654", "/T", "/F"])
        self.assertEqual(Path(command[0]).name.lower(), "taskkill.exe")
        self.assertIs(stop.call_args.kwargs["shell"], False)
        process.wait.assert_called_once_with(timeout=10)
        process.poll.return_value = 0
        with patch.object(cli.subprocess, "run") as stop:
            self.assertTrue(cli._stop(process))
            stop.assert_not_called()


class AutomaticRetryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(cli, "_UNCERTAIN_PROCESS", None))
        self.stack.enter_context(patch.object(cli, "_RUN_LOCK", threading.Lock()))
        self.installation = self.stack.enter_context(patch.object(cli, "_installation", return_value={"command":["agy"], "version":"1.4.0"}))
        self.sleep = self.stack.enter_context(patch.object(cli.time, "sleep"))
        self.report = self.stack.enter_context(patch.object(cli.progress, "report"))

    def test_empty_response_then_success_reuses_exact_input_with_fresh_isolated_profiles(self):
        payload = {"source": "原文\r\n@台詞も残す。", "model": cli.MODEL}
        schema = json.loads(json.dumps(SCHEMA))
        seen = []

        def run(command, env, cwd, data):
            for previous in seen:
                self.assertFalse(Path(previous[2]).exists())
            self.assertEqual(Path(env["HOME"]), Path(cwd).parent)
            self.assertEqual(env["USERPROFILE"], env["HOME"])
            self.assertEqual(command[command.index("--model") + 1], cli.MODEL)
            schema_bytes = Path(command[command.index("--json-schema") + 1]).read_bytes()
            seen.append((command, env, cwd, data, schema_bytes))
            if len(seen) == 1:
                return envelope(result={"status": "SUCCESS", "response": ""})
            return envelope()

        def backoff(seconds):
            self.assertEqual(seconds, 2)
            self.assertTrue(cli._RUN_LOCK.locked())
            self.assertFalse(Path(seen[0][2]).exists())
            # Caller mutation during the wait cannot alter a retry's data or schema.
            payload["source"] = "後から変更された原文"
            schema["required"] = ["different"]

        self.sleep.side_effect = backoff
        with patch.object(cli, "_run", side_effect=run) as process:
            result = cli.generate(payload, "元の方針", schema)
        self.assertEqual(process.call_count, 2)
        self.assertNotEqual(seen[0][2], seen[1][2])
        self.assertIs(seen[0][3], seen[1][3])
        self.assertEqual(seen[0][4], seen[1][4])
        prompt = json.loads(json.loads(seen[1][3])["message"]["content"])
        self.assertEqual(prompt["input"]["source"], "原文\r\n@台詞も残す。")
        self.assertEqual(prompt["instructions"], "元の方針")
        self.assertEqual(result["proposal"], PROPOSAL)
        self.assertEqual(result["identity"]["attempts"], 2)
        self.assertEqual(result["identity"]["automatic_retries"], 1)
        self.assertEqual(result["identity"]["attempt_error_codes"], ["cli_no_response"])
        self.assertFalse(cli._RUN_LOCK.locked())
        self.assertTrue(all(not Path(item[2]).exists() for item in seen))
        self.assertEqual(self.report.call_args_list, [
            unittest.mock.call("generation", message="generation", attempt=1, max_attempts=3),
            unittest.mock.call("generation", message="generation_retry_wait", attempt=2, max_attempts=3),
            unittest.mock.call("generation", message="generation_retry", attempt=2, max_attempts=3),
        ])

    def test_timeout_retries_only_after_owned_process_cleanup_is_confirmed(self):
        first, second = FakeProcess(b""), FakeProcess(envelope())
        first.wait.side_effect = subprocess.TimeoutExpired("agy", cli.TIMEOUT_SECONDS)
        stopped = []

        def stop(process):
            self.assertIs(process, first)
            process.returncode = -9
            stopped.append(process)
            return True

        def start(*args, **kwargs):
            if not stopped:
                return first
            self.assertFalse(cli._UNCERTAIN_PROCESS)
            self.assertTrue(cli._RUN_LOCK.locked())
            return second

        with patch.object(decider_runtime, "_external_dll_search", side_effect=ExitStack), \
             patch.object(decider_runtime, "_external_environment", side_effect=lambda env: dict(env)), \
             patch.object(cli.subprocess, "Popen", side_effect=start) as launched, \
             patch.object(cli, "_stop", side_effect=stop) as cleanup:
            result = cli.generate({"source": "原文", "model": cli.MODEL}, "方針", SCHEMA)
        self.assertEqual(launched.call_count, 2)
        cleanup.assert_called_once_with(first)
        self.sleep.assert_called_once_with(2)
        self.assertEqual(result["identity"]["attempts"], 2)
        self.assertEqual(result["identity"]["attempt_error_codes"], ["cli_timeout"])

    def test_exhaustion_is_three_total_attempts_and_busy_lock_covers_both_backoffs(self):
        def backoff(seconds):
            self.assertTrue(cli._RUN_LOCK.locked())
            with self.assertRaises(cli.GeminiCliError) as busy:
                cli.generate({"source": "別の文章"}, "方針", SCHEMA)
            self.assertEqual(busy.exception.code, "cli_busy")

        self.sleep.side_effect = backoff
        with patch.object(cli, "_run", return_value=envelope(result={"status": "SUCCESS", "response": ""})) as run:
            with self.assertRaises(cli.GeminiCliError) as failure:
                cli.generate({"source": "原文"}, "方針", SCHEMA)
        error = failure.exception
        self.assertEqual(run.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [unittest.mock.call(2), unittest.mock.call(5)])
        self.assertEqual(error.code, "cli_no_response")
        self.assertEqual(error.attempts, 3)
        self.assertEqual(error.automatic_retries, 2)
        self.assertEqual(error.attempt_error_codes, ["cli_no_response"] * 3)
        self.assertIn("自動で2回再試行しました", str(error))
        self.assertIn("原文は変更していません", str(error))
        self.assertFalse(cli._RUN_LOCK.locked())
        self.assertTrue(all(call.args == ("generation",) for call in self.report.call_args_list))
        self.assertTrue(all("completed" not in call.kwargs and "total" not in call.kwargs for call in self.report.call_args_list))
        self.assertEqual([call.kwargs["attempt"] for call in self.report.call_args_list], [1, 2, 2, 3, 3])

    def test_nontransient_protocol_and_generation_failures_are_not_retried(self):
        variants = [
            envelope(model="other"), envelope(permission="auto"),
            envelope(steps=[{"event": "step_update", "step_update": {"step_type": "tool"}}]),
            envelope(result={"status": "SUCCESS", "structured_output": {"unexpected": 1}}),
            envelope(result={"status": "ERROR", "error": "PRIVATE_AUTH_OR_QUOTA"}), b"malformed JSON",
        ]
        for raw in variants:
            with self.subTest(raw=raw), patch.object(cli, "_run", return_value=raw) as run:
                with self.assertRaises(cli.GeminiCliError) as failure:
                    cli.generate({"source": "原文"}, "方針", SCHEMA)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(failure.exception.attempts, 1)
                self.assertEqual(failure.exception.automatic_retries, 0)
                self.assertNotIn("PRIVATE", str(failure.exception))
        self.sleep.assert_not_called()

    def test_nontransient_start_setup_and_stop_errors_are_not_retried(self):
        for code in ("cli_failed", "cli_start_failed", "cli_stop_failed", "response_too_large", "invalid_request"):
            with self.subTest(code=code), patch.object(cli, "_run", side_effect=cli.GeminiCliError(code, "安全な診断")) as run:
                with self.assertRaises(cli.GeminiCliError) as failure:
                    cli.generate({"source": "原文"}, "方針", SCHEMA)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(failure.exception.code, code)
        with patch.object(cli, "_installation", return_value=None), patch.object(cli, "_run") as run:
            with self.assertRaises(cli.GeminiCliError) as failure:
                cli.generate({"source": "原文"}, "方針", SCHEMA)
            self.assertEqual(failure.exception.code, "cli_unavailable")
            run.assert_not_called()
        self.sleep.assert_not_called()

    def test_nontransient_failure_after_retry_keeps_actual_counts_and_sanitized_error_history(self):
        results = [envelope(result={"status": "SUCCESS", "response": ""}), envelope(model="other")]
        with patch.object(cli, "_run", side_effect=results) as run:
            with self.assertRaises(cli.GeminiCliError) as failure:
                cli.generate({"source": "原文"}, "方針", SCHEMA)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(failure.exception.code, "identity_mismatch")
        self.assertEqual(failure.exception.attempts, 2)
        self.assertEqual(failure.exception.automatic_retries, 1)
        self.assertEqual(failure.exception.attempt_error_codes, ["cli_no_response", "identity_mismatch"])
        self.assertIn("自動で1回再試行した後に停止", str(failure.exception))
        self.sleep.assert_called_once_with(2)
        with patch.object(cli, "_run", side_effect=cli.GeminiCliError("PRIVATE_FREEFORM_ERROR", "安全な診断")):
            with self.assertRaises(cli.GeminiCliError) as unknown:
                cli.generate({"source": "原文"}, "方針", SCHEMA)
        self.assertEqual(unknown.exception.attempt_error_codes, ["generation_error"])

    def test_unconfirmed_timeout_cleanup_never_launches_a_retry(self):
        process = FakeProcess(b"")
        process.wait.side_effect = subprocess.TimeoutExpired("agy", cli.TIMEOUT_SECONDS)
        with patch.object(decider_runtime, "_external_dll_search", side_effect=ExitStack), \
             patch.object(decider_runtime, "_external_environment", side_effect=lambda env: dict(env)), \
             patch.object(cli.subprocess, "Popen", return_value=process) as launched, \
             patch.object(cli, "_stop", return_value=False) as cleanup:
            with self.assertRaises(cli.GeminiCliError) as failure:
                cli.generate({"source": "原文"}, "方針", SCHEMA)
        self.assertEqual(failure.exception.code, "cli_stop_failed")
        self.assertIs(cli._UNCERTAIN_PROCESS, process)
        self.assertEqual(launched.call_count, 1)
        cleanup.assert_called_once_with(process)
        self.sleep.assert_not_called()

    def test_preexisting_uncertain_process_blocks_context_and_all_retry_attempts(self):
        with patch.object(cli, "_UNCERTAIN_PROCESS", object()), patch.object(cli, "launch_context") as launch, patch.object(cli, "_run") as run:
            with self.assertRaises(cli.GeminiCliError) as failure:
                cli.generate({"source": "原文"}, "方針", SCHEMA)
        self.assertEqual(failure.exception.code, "cli_stop_failed")
        launch.assert_not_called()
        run.assert_not_called()
        self.sleep.assert_not_called()

    def test_unfinished_reader_or_writer_blocks_retry_even_after_confirmed_parent_stop(self):
        for pending_index in (0, 2):  # stdout reader, then stdin writer
            with self.subTest(pending_index=pending_index):
                process = FakeProcess(b"")
                process.wait.side_effect = subprocess.TimeoutExpired("agy", cli.TIMEOUT_SECONDS)
                threads = [Mock(), Mock(), Mock()]
                for index, thread in enumerate(threads):
                    thread.is_alive.return_value = index == pending_index
                with patch.object(cli, "_UNCERTAIN_PROCESS", None), \
                     patch.object(decider_runtime, "_external_dll_search", side_effect=ExitStack), \
                     patch.object(decider_runtime, "_external_environment", side_effect=lambda env: dict(env)), \
                     patch.object(cli.subprocess, "Popen", return_value=process) as launched, \
                     patch.object(cli, "_stop", return_value=True), \
                     patch.object(cli.threading, "Thread", side_effect=threads):
                    with self.assertRaises(cli.GeminiCliError) as failure:
                        cli.generate({"source": "原文"}, "方針", SCHEMA)
                    self.assertIs(cli._UNCERTAIN_PROCESS, process)
                self.assertEqual(failure.exception.code, "cli_stop_failed")
                self.assertEqual(failure.exception.automatic_retries, 0)
                self.assertEqual(launched.call_count, 1)
        self.sleep.assert_not_called()

    def test_success_without_retry_has_explicit_zero_retry_metadata(self):
        with patch.object(cli, "_run", return_value=envelope()) as run:
            result = cli.generate({"source": "原文"}, "方針", SCHEMA)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["identity"]["attempts"], 1)
        self.assertEqual(result["identity"]["automatic_retries"], 0)
        self.assertEqual(result["identity"]["attempt_error_codes"], [])
        self.assertEqual(result["identity"]["cli_version"], "1.4.0")
        self.sleep.assert_not_called()

    def test_141_binary_and_version_are_frozen_for_retries_and_result_identity(self):
        installed={"command":["agy-1.4.1"], "version":"1.4.1"}
        self.installation.return_value=installed
        def change_discovery(_):
            installed["command"][:]=["later-unsupported-agy"]
            installed["version"]="1.5.0"
        self.sleep.side_effect=change_discovery
        with patch.object(cli, "_run", side_effect=[envelope(result={"status":"SUCCESS","response":""}),envelope()]) as run:
            result=cli.generate({"source":"原文"}, "方針", SCHEMA)
        self.installation.assert_called_once_with(refresh=True)
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ["agy-1.4.1", "agy-1.4.1"])
        self.assertEqual(result["identity"]["cli_version"], "1.4.1")
        self.assertEqual(result["identity"]["attempts"], 2)
        self.assertEqual(result["identity"]["automatic_retries"], 1)


if __name__ == "__main__":
    unittest.main()
