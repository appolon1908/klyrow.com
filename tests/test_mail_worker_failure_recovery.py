"""Isolated reliability regressions; never import the application composition root.

Production transport/worker functions are executed with fake network, credential,
and persistence boundaries. Full application/PostgreSQL/CI verification remains
separate. Set KLYROW_CANDIDATE_SOURCE_ROOT to the app module directory.
"""
from __future__ import annotations
import ast
import asyncio
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import smtplib
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = Path(os.environ.get("KLYROW_CANDIDATE_SOURCE_ROOT", Path(__file__).resolve().parents[1] / "apps/gateway/app"))


class Harness:
    def __init__(self):
        self.package = "_klyrow_reliability_fixture"
        self.registered = []
        self.module(self.package, __path__=[str(ROOT)])
        self.live = True
        self.module(self.package + ".delivery_safety", live_email_delivery_enabled=lambda: self.live)
        self.outcome = self.load("provider_outcome")
        self.ses = self.load("ses_transport")
        self.ses._secret_file = lambda _: "SYNTHETIC_NOT_A_CREDENTIAL"
        self.env = {"KLYROW_EMAIL_TRANSPORT": "ses", "KLYROW_SES_REGION": "us-east-1", "KLYROW_SES_CONFIGURATION_SET": "codestra-klyrow-transactional", "KLYROW_SES_CANARY_ONLY": "true"}
        self.ses.os = types.SimpleNamespace(getenv=lambda k, default=None: self.env.get(k, default))
        self.payload = {"from": "fixture@klyrow.com", "to": ["success@simulator.amazonses.com"], "subject": "synthetic", "plain_body": "not sent", "stream": "transactional"}
        self.calls = []
        self.fault = None
        self.stage = "data"
        self.reply = (250, b"Ok fixture-provider-id-0001")
        self.close_fault = False
        outer = self
        class SMTPFixture:
            def __init__(self, host, port, timeout):
                outer.calls.append(("connect", host, port, timeout))
                if outer.stage == "connect" and outer.fault: raise outer.fault
            def __enter__(self): return self
            def __exit__(self, *args):
                if outer.close_fault: raise OSError("PRIVATE_FIXTURE_ERROR_MUST_NOT_BE_LOGGED")
                if outer.stage == "quit" and outer.fault: raise outer.fault
                return False
            def ehlo(self): return (250, b"fixture")
            def starttls(self, context): outer.calls.append(("starttls",)); return (220, b"fixture")
            def login(self, *args):
                outer.calls.append(("login",))
                if outer.stage == "auth" and outer.fault: raise outer.fault
                return (235, b"fixture")
            def mail(self, sender): return (250, b"fixture")
            def rcpt(self, recipient): return (250, b"fixture")
            def data(self, body):
                outer.calls.append(("data", body))
                if outer.stage == "data" and outer.fault: raise outer.fault
                return outer.reply
            def close(self):
                outer.calls.append(("close",))
                if outer.close_fault: raise OSError("PRIVATE_FIXTURE_ERROR_MUST_NOT_BE_LOGGED")
        self.smtp_type = SMTPFixture
    def module(self, name, **values):
        m = types.ModuleType(name)
        m.__dict__.update(values)
        sys.modules[name] = m
        self.registered.append(name)
        return m
    def load(self, name):
        p = ROOT / (name + ".py")
        spec = importlib.util.spec_from_file_location(self.package + "." + name, p)
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        self.registered.append(spec.name)
        spec.loader.exec_module(m)
        return m
    def close(self):
        for name in reversed(self.registered): sys.modules.pop(name, None)
    def submit(self):
        with patch("smtplib.SMTP", self.smtp_type), patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
            return self.ses._submit_smtp(self.payload, correlation_id="local-correlation-only")
    def main_namespace(self):
        path = ROOT / "main.py"
        if not path.exists(): path = ROOT / "main_functions.py"
        parsed = ast.parse(path.read_text())
        names = {"email_outbox_loop", "start_postal_retry_worker", "start_provider_worker", "_supervise_embedded_worker", "_start_embedded_worker", "stop_embedded_workers"}
        nodes = []
        for n in parsed.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names:
                n.decorator_list = []
                nodes.append(n)
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_EMBEDDED_WORKER_TASKS" for t in n.targets): nodes.append(n)
        ns = {"__package__": self.package, "asyncio": asyncio, "os": types.SimpleNamespace(getenv=lambda k,d=None:self.env.get(k,d)), "json": json, "SAFE_MODE": False, "datetime": datetime, "timezone": timezone, "timedelta": timedelta}
        exec(compile(ast.Module(body=nodes,type_ignores=[]), str(path), "exec"), ns)
        return ns


class SMTPReliability(unittest.TestCase):
    def setUp(self): self.h = Harness()
    def tearDown(self): self.h.close()
    def test_success_requires_verified_config_and_tls(self):
        self.assertEqual(self.h.submit(), "fixture-provider-id-0001")
        self.assertIn(("connect", "email-smtp.us-east-1.amazonaws.com", 587, 12), self.h.calls)
        self.assertIn(("starttls",), self.h.calls)
        body = next(c[1] for c in self.h.calls if c[0] == "data")
        self.assertIn(b"X-SES-CONFIGURATION-SET: codestra-klyrow-transactional", body)
    def test_closed_live_gate_never_opens_smtp(self):
        self.h.live = False
        with self.assertRaises(RuntimeError): self.h.submit()
        self.assertEqual(self.h.calls, [])
    def test_wrong_region_is_denied_before_network(self):
        self.h.env["KLYROW_SES_REGION"] = "us-west-2"
        with self.assertRaises(RuntimeError): self.h.submit()
        self.assertEqual(self.h.calls, [])
    def test_wrong_configuration_set_is_denied_before_network(self):
        self.h.env["KLYROW_SES_CONFIGURATION_SET"] = "wrong"
        with self.assertRaises(RuntimeError): self.h.submit()
        self.assertEqual(self.h.calls, [])
    def test_unknown_transport_is_denied_without_postal_fallback(self):
        self.h.env["KLYROW_EMAIL_TRANSPORT"] = "other"
        with self.assertRaises(RuntimeError): self.h.submit()
        self.assertEqual(self.h.calls, [])
    def test_post_data_timeout_requires_reconciliation(self):
        self.h.fault = TimeoutError("synthetic post-DATA loss")
        with self.assertRaises(Exception) as raised: self.h.submit()
        self.assertTrue(self.h.outcome.provider_outcome_is_ambiguous(raised.exception))
    def test_post_data_disconnect_requires_reconciliation(self):
        self.h.fault = smtplib.SMTPServerDisconnected("synthetic post-DATA loss")
        with self.assertRaises(Exception) as raised: self.h.submit()
        self.assertTrue(self.h.outcome.provider_outcome_is_ambiguous(raised.exception))
    def test_post_data_connection_reset_requires_reconciliation(self):
        self.h.fault = ConnectionResetError("synthetic post-DATA loss")
        with self.assertRaises(Exception) as raised: self.h.submit()
        self.assertTrue(self.h.outcome.provider_outcome_is_ambiguous(raised.exception))
    def test_auth_timeout_is_not_submission_uncertainty(self):
        self.h.stage = "auth"; self.h.fault = TimeoutError("synthetic pre-DATA loss")
        with self.assertRaises(Exception) as raised: self.h.submit()
        self.assertFalse(self.h.outcome.provider_outcome_is_ambiguous(raised.exception))
        self.assertFalse(any(c[0] == "data" for c in self.h.calls))
    def test_explicit_data_rejection_is_not_indeterminate(self):
        self.h.reply = (451, b"synthetic rejection")
        with self.assertRaises(smtplib.SMTPDataError) as raised: self.h.submit()
        self.assertFalse(self.h.outcome.provider_outcome_is_ambiguous(raised.exception))
    def test_initial_data_command_rejection_is_not_indeterminate(self):
        self.h.fault = smtplib.SMTPDataError(554, b"synthetic rejection")
        with self.assertRaises(smtplib.SMTPDataError) as raised: self.h.submit()
        self.assertFalse(self.h.outcome.provider_outcome_is_ambiguous(raised.exception))
    def test_accepted_response_without_id_never_fabricates_provider_id(self):
        self.h.reply = (250, b"Accepted but provider id missing")
        error = None
        try: self.h.submit()
        except Exception as exc: error = exc
        self.assertIsNotNone(error)
        self.assertTrue(self.h.outcome.provider_outcome_is_ambiguous(error))
    def test_quit_error_cannot_turn_accepted_message_into_retry(self):
        self.h.stage = "quit"; self.h.fault = smtplib.SMTPResponseException(500, b"synthetic quit failure")
        error = None; value = None
        try: value = self.h.submit()
        except Exception as exc: error = exc
        self.assertIsNone(error)
        self.assertEqual(value, "fixture-provider-id-0001")
    def test_close_error_does_not_hide_accepted_provider_id(self):
        self.h.close_fault = True
        error = None; value = None
        try: value = self.h.submit()
        except Exception as exc: error = exc
        self.assertIsNone(error, "cleanup replaced accepted outcome")
        self.assertEqual(value, "fixture-provider-id-0001")
    def test_cancellation_is_not_swallowed_by_transport(self):
        self.h.fault = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): self.h.submit()


class WorkerReliability(unittest.TestCase):
    def setUp(self): self.h = Harness()
    def tearDown(self): self.h.close()
    def test_initial_db_outage_is_retried_without_dying(self):
        ns = self.h.main_namespace(); attempts=[]; ticks=[]
        class DBDown(Exception): pass
        def bad_db(): attempts.append(1); raise DBDown("PRIVATE_FIXTURE_ERROR_MUST_NOT_BE_LOGGED")
        async def sleep(delay):
            ticks.append(delay)
            if len(ticks)>2: raise asyncio.CancelledError()
        ns.update(DB=bad_db,asyncio=types.SimpleNamespace(sleep=sleep,CancelledError=asyncio.CancelledError))
        async def run():
            error = None
            try: await ns["email_outbox_loop"]()
            except asyncio.CancelledError: pass
            except Exception as exc: error=exc
            return error
        log=io.StringIO()
        with contextlib.redirect_stdout(log): error=asyncio.run(run())
        self.assertIsNone(error, "DB error escaped the outbox loop")
        self.assertEqual(len(attempts),2)
        self.assertEqual(ticks,[2,2,2])
        self.assertNotIn("PRIVATE_FIXTURE",log.getvalue())
    def _outbox_fixture(self, mode):
        ns=self.h.main_namespace()
        class Expr:
            def __getattr__(self,name):return self
            def __call__(self,*a,**k):return self
            def __eq__(self,other):return self
            def __lt__(self,other):return self
            def __le__(self,other):return self
            def __and__(self,other):return self
        class ModelMeta(type):
            def __getattr__(cls,name):return Expr()
        class Outbox(metaclass=ModelMeta):pass
        class Message(metaclass=ModelMeta):pass
        class Tenant(metaclass=ModelMeta):pass
        item=types.SimpleNamespace(id="outbox",message_id="message",tenant_id="tenant-a",operation_id="op",correlation_id="correlation",trace_context_json="{}",payload=json.dumps(self.h.payload),state="pending",attempts=0,next_attempt_at=None,provider_message_id=None,updated_at=datetime.now(timezone.utc))
        message=types.SimpleNamespace(status="queued",recipient="synthetic@example.invalid")
        attempts=[];sends=[];ticks=[];events=[]
        class Session:
            def __init__(self):self.selects=0
            def __enter__(self):return self
            def __exit__(self,*a):return False
            def scalar(self,*a):
                self.selects+=1
                if self.selects==1:return None
                return item if item.state in {"pending","retry"} else None
            def get(self,model,key):
                if model is Tenant:return types.SimpleNamespace(enabled=True)
                if model is Message:return message
                if model is Outbox:return item
                return None
            def commit(self):pass
            def add(self,value):pass
        class DBDown(Exception):pass
        def db():
            attempts.append(1)
            if mode=="post_accept" and len(attempts)==2:raise DBDown("synthetic post-accept persistence outage")
            if mode=="recovery_down" and len(attempts)>1:raise DBDown("PRIVATE_FIXTURE_ERROR_MUST_NOT_BE_LOGGED")
            return Session()
        async def submit(*a,**k):
            sends.append(1)
            if mode=="recovery_down":raise RuntimeError("synthetic delivery failure")
            return "fixture-accepted-provider-id"
        async def sleep(delay):
            ticks.append(delay)
            if len(ticks)>2:raise asyncio.CancelledError()
        self.h.ses.send_ses_message=submit
        self.h.module(self.h.package+".production_authorization",provider_payload_from_outbox=lambda payload,**kw:(payload,True))
        self.h.module(self.h.package+".operations",enforce_tenant_send_gate=lambda *a:None)
        self.h.module(self.h.package+".delivery_controls",enforce_delivery_controls=lambda *a:None)
        ns.update(DB=db,EmailOutbox=Outbox,Message=Message,Tenant=Tenant,select=lambda *a:Expr(),or_=lambda *a:Expr(),canary_gate_key=lambda:"fixture",canary_configuration=lambda:(None,None,None,1),queue_email_lifecycle_event=lambda *a,**kw:events.append(kw.get("kind")),set_core_message_status=lambda obj,status:setattr(obj,"status",status),traced=lambda *a:contextlib.nullcontext(),stored_carrier=lambda *a:{},HTTPException=type("FixtureHTTPException",(Exception,),{}),asyncio=types.SimpleNamespace(sleep=sleep,CancelledError=asyncio.CancelledError))
        async def run():
            try:await ns["email_outbox_loop"]()
            except asyncio.CancelledError:return None
            except Exception as exc:return exc
        logs=io.StringIO()
        with contextlib.redirect_stdout(logs):error=asyncio.run(run())
        return error,item,sends,events,logs.getvalue()
    def test_known_acceptance_then_db_failure_never_resubmits(self):
        error,item,sends,events,log=self._outbox_fixture("post_accept")
        self.assertIsNone(error)
        self.assertEqual(len(sends),1,"accepted message was submitted a second time")
        self.assertEqual(item.state,"INDETERMINATE")
        self.assertEqual(item.provider_message_id,"fixture-accepted-provider-id")
        self.assertIsNone(item.next_attempt_at)
    def test_recovery_db_failure_does_not_kill_loop_or_resubmit(self):
        error,item,sends,events,log=self._outbox_fixture("recovery_down")
        self.assertIsNone(error,"error-reporting DB failure killed worker")
        self.assertEqual(len(sends),1)
        self.assertEqual(item.state,"sending")
        self.assertNotIn("PRIVATE_FIXTURE",log)
    def test_safe_mode_does_not_open_db(self):
        ns=self.h.main_namespace();ns["SAFE_MODE"]=True
        def forbidden(): raise AssertionError("DB not allowed while safe")
        ticks=[]
        async def sleep(delay):
            ticks.append(delay)
            if len(ticks)>2:raise asyncio.CancelledError()
        ns.update(DB=forbidden,asyncio=types.SimpleNamespace(sleep=sleep,CancelledError=asyncio.CancelledError))
        with self.assertRaises(asyncio.CancelledError):asyncio.run(ns["email_outbox_loop"]())
    def test_embedded_tasks_retained_deduplicated_and_shutdown(self):
        ns=self.h.main_namespace()
        self.assertTrue(callable(ns.get("stop_embedded_workers")),"shutdown lifecycle is missing")
        async def run():
            async def parked(): await asyncio.Event().wait()
            ns.update(postal_retry_loop=parked,email_outbox_loop=parked,provider_worker_loop=parked)
            await ns["start_postal_retry_worker"]();await ns["start_provider_worker"]()
            await ns["start_postal_retry_worker"]();await ns["start_provider_worker"]()
            tasks=list(ns["_EMBEDDED_WORKER_TASKS"].values())
            self.assertEqual(len(tasks),3)
            await asyncio.sleep(0)
            await ns["stop_embedded_workers"]()
            self.assertTrue(all(t.done() for t in tasks))
            self.assertEqual(ns["_EMBEDDED_WORKER_TASKS"],{})
        asyncio.run(run())
    def test_explicit_disabled_embedded_workers_start_nothing(self):
        self.h.env["KLYROW_EMBEDDED_WORKERS"]="false";ns=self.h.main_namespace()
        def forbidden():raise AssertionError("worker launched")
        ns.update(postal_retry_loop=forbidden,email_outbox_loop=forbidden,provider_worker_loop=forbidden)
        async def run():
            await ns["start_postal_retry_worker"]();await ns["start_provider_worker"]()
        asyncio.run(run())
        self.assertFalse(ns.get("_EMBEDDED_WORKER_TASKS",{}))
    def test_supervisor_restarts_failures_with_bounded_backoff(self):
        ns=self.h.main_namespace()
        self.assertTrue(callable(ns.get("_supervise_embedded_worker")),"supervisor is missing")
        calls=[];delays=[]
        async def bad():calls.append(1);raise RuntimeError("PRIVATE_FIXTURE_ERROR_MUST_NOT_BE_LOGGED")
        async def sleep(delay):
            delays.append(delay)
            if len(delays)==7:raise asyncio.CancelledError()
        ns["asyncio"]=types.SimpleNamespace(sleep=sleep,CancelledError=asyncio.CancelledError)
        log=io.StringIO()
        with contextlib.redirect_stdout(log), self.assertRaises(asyncio.CancelledError):asyncio.run(ns["_supervise_embedded_worker"]("email_outbox",bad))
        self.assertEqual(delays,[2,4,8,16,30,30,30])
        self.assertEqual(len(calls),7)
        self.assertNotIn("PRIVATE_FIXTURE",log.getvalue())
    def test_supervisor_propagates_cancellation_without_restart(self):
        ns=self.h.main_namespace()
        self.assertTrue(callable(ns.get("_supervise_embedded_worker")),"supervisor is missing")
        calls=[]
        async def cancelled():calls.append(1);raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):asyncio.run(ns["_supervise_embedded_worker"]("email_outbox",cancelled))
        self.assertEqual(len(calls),1)



if __name__ == "__main__":unittest.main(verbosity=2)
