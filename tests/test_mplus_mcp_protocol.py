"""Run in the separate SDK environment; uses only synthetic engineering data."""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest

SDK_AVAILABLE=importlib.util.find_spec('mcp') is not None
if SDK_AVAILABLE:
    from scripts.mplus_mcp.server import AuditLog, create_server


@unittest.skipUnless(SDK_AVAILABLE,'MCP SDK tests use the isolated prototype environment')
class ProtocolBoundaryTests(unittest.TestCase):
    def test_sdk_validation_errors_are_safe_and_audited(self):
        with tempfile.TemporaryDirectory() as temp:
            audit=AuditLog(Path(temp)/'audit','anonymous-snapshot')
            server=create_server(None,audit)
            secret='ANON_CREDENTIAL_'+'x'*170
            result=asyncio.run(server.call_tool('search',{'query':secret}))
            self.assertEqual(result['code'],'INVALID_TOOL_INPUT')
            audit.close()
            log=audit.path.read_text()
            self.assertNotIn(secret,json.dumps(result)+log)
            self.assertEqual(json.loads(log)['status'],'error')

    def test_audit_failure_returns_no_content(self):
        class FakeIndex:
            def search(self,*args): return {'text':'ANON_PROTECTED_CONTENT'}
        with tempfile.TemporaryDirectory() as temp:
            audit=AuditLog(Path(temp)/'audit','anonymous-snapshot')
            def fail(*args): raise OSError('disk full')
            audit.record=fail
            result=asyncio.run(create_server(FakeIndex(),audit).call_tool('search',{'query':'known'}))
            audit.close()
            self.assertNotIn('ANON_PROTECTED_CONTENT',str(result))
            self.assertIn('AUDIT_UNAVAILABLE',str(result))

    def test_audit_chain_contains_only_hashed_query_and_result(self):
        with tempfile.TemporaryDirectory() as temp:
            audit=AuditLog(Path(temp)/'audit','anonymous-snapshot')
            audit.record('search',{'query':'ANON_QUERY'},{'text':'ANON_CONTENT'},time.monotonic())
            audit.record('fetch',{'id':'ANON_ID'},{'text':'ANON_CONTENT'},time.monotonic())
            audit.close()
            rows=[json.loads(line) for line in audit.path.read_text().splitlines()]
            self.assertEqual(rows[1]['previous_hash'],rows[0]['event_hash'])
            for raw in ['ANON_QUERY','ANON_CONTENT','ANON_ID']:
                self.assertNotIn(raw,audit.path.read_text())


if __name__=='__main__': unittest.main()
