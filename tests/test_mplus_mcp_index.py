from pathlib import Path
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.mplus_mcp.index import KnowledgeIndex, allowed_path, build, sanitize, sanitize_source


class EngineeringMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/"repo"
        self.root.mkdir()
        (self.root/"mvp_v2").mkdir()
        (self.root/"docs/architecture").mkdir(parents=True)
        (self.root/"mvp_v2/service.py").write_text('class Canonical:\n    def read(self):\n        return "确认需求"\n')
        (self.root/"docs/architecture/capabilities.json").write_text(json.dumps({"capabilities":[{
            "id":"formal_generation","name":"规划生成","canonical_entries":["mvp_v2.service.Canonical.read"]}]},ensure_ascii=False))
        (self.root/"README.md").write_text("# Engineering\n"+"\n".join(f"record {i} 需求" for i in range(180)))
        for args in [("init","-q"),("add","."),("-c","user.name=Fixture","-c","user.email=fixture@example.invalid","commit","-qm","fixture")]:
            subprocess.run(["git","-C",str(self.root),*args],check=True,capture_output=True)
        self.snapshot=Path(self.temp.name)/"snapshot.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_build_search_fetch_trace_and_pagination(self):
        report=build(self.root,self.snapshot)
        index=KnowledgeIndex(self.snapshot)
        page=index.search("需求",1)
        self.assertEqual(page['next_offset'],1)
        next_page=index.search("需求",1,1)
        self.assertNotEqual(page['results'][0]['id'],next_page['results'][0]['id'])
        item=index.fetch(page['results'][0]['id'])
        self.assertIn("需求",item['text'])
        self.assertEqual(item['snapshot_id'],report['snapshot_id'])
        trace=index.trace("formal_generation")
        self.assertEqual(trace['entries'][0]['evidence'],"static_symbol")
        self.assertIn('def read',index.fetch(trace['entries'][0]['source_id'])['text'])
        self.assertFalse(trace['runtime_verified'])

    def test_default_commit_does_not_follow_dirty_working_tree(self):
        (self.root/"mvp_v2/service.py").write_text('marker = "uncommitted sentinel"')
        build(self.root,self.snapshot)
        index=KnowledgeIndex(self.snapshot)
        self.assertEqual(index.search('sentinel')['results'],[])
        self.assertFalse(index.metadata['dirty'])
        second=Path(self.temp.name)/"working.sqlite3"
        build(self.root,second,working_tree=True)
        self.assertTrue(KnowledgeIndex(second).search('sentinel')['results'])
        self.assertTrue(KnowledgeIndex(second).metadata['dirty'])

    def test_path_policy_and_symlink_cannot_read_external_files(self):
        outside=Path(self.temp.name)/"secret.py"
        outside.write_text('exfiltration sentinel')
        (self.root/"mvp_v2/service.py").unlink()
        (self.root/"mvp_v2/service.py").symlink_to(outside)
        build(self.root,self.snapshot,working_tree=True)
        index=KnowledgeIndex(self.snapshot)
        self.assertEqual(index.search('exfiltration')['results'],[])
        for path in ['../README.md','/etc/passwd','.env','output/a.md','tests/customer.py','mvp_v2/.env']:
            self.assertFalse(allowed_path(path))
        for source_id in ['../../secret','file:///etc/passwd','https://example.invalid','doc_'+'f'*24+':1']:
            with self.assertRaises(ValueError): index.fetch(source_id)

    def test_redaction_preserves_lines_and_never_indexes_obvious_secrets(self):
        secret='sk-'+'a'*32
        original='MPLUS_API_KEY = "'+secret+'"\nowner = "someone@example.invalid"\npath="/Users/example/private-fixture.txt"\n'
        clean,changed=sanitize(original)
        self.assertTrue(changed)
        self.assertEqual(len(clean.splitlines()),len(original.splitlines()))
        self.assertNotIn(secret,clean)
        self.assertNotIn('someone@example.invalid',clean)
        self.assertNotIn('/Users/example/private-fixture.txt',clean)
        (self.root/"mvp_v2/service.py").write_text(original)
        build(self.root,self.snapshot,working_tree=True)
        self.assertEqual(KnowledgeIndex(self.snapshot).search(secret)['results'],[])

    def test_read_connection_cannot_write_and_source_can_be_removed(self):
        build(self.root,self.snapshot)
        index=KnowledgeIndex(self.snapshot)
        with index.connect() as conn:
            with self.assertRaises(sqlite3.OperationalError): conn.execute('DELETE FROM chunks')
        (self.root/"README.md").unlink()
        self.assertTrue(index.search('Engineering')['results'])
        with self.assertRaisesRegex(ValueError,'OUTPUT_EXISTS'): build(self.root,self.snapshot)

    def test_snapshot_replacement_detected(self):
        build(self.root,self.snapshot)
        index=KnowledgeIndex(self.snapshot)
        replacement=Path(self.temp.name)/'new.sqlite3'
        build(self.root,replacement)
        os.replace(replacement,self.snapshot)
        with self.assertRaisesRegex(ValueError,'SNAPSHOT_CHANGED'): index.search('Engineering')

    def test_input_limits_and_sql_payload_are_data(self):
        build(self.root,self.snapshot)
        index=KnowledgeIndex(self.snapshot)
        for query,limit,offset in [('',10,0),('a'*161,10,0),('a',21,0),('a',10,-1)]:
            with self.assertRaises(ValueError): index.search(query,limit,offset)
        self.assertEqual(index.search("x' OR 1=1 --")['results'],[])
        self.assertTrue(index.search('Engineering')['results'])

    def test_multiline_credentials_and_metadata_are_redacted(self):
        samples=[('password = (\n    "ANON_CREDENTIAL"\n)\n','.py'),
                 ('const apiKey =\n "ANON_CREDENTIAL";','.js'),
                 ('DATABASE_URL="postgresql://user:ANON_CREDENTIAL@localhost/db"','.py'),
                 ('{\n "password": {\n "value": "ANON_CREDENTIAL"\n }\n}','.json')]
        for raw,suffix in samples:
            clean,changed=sanitize_source(raw,suffix)
            self.assertTrue(changed)
            self.assertNotIn('ANON_CREDENTIAL',clean)
            self.assertEqual(raw.count('\n'),clean.count('\n'))
        data={"capabilities":[{"id":"fixture","name":"Fixture","password":"ANON_CREDENTIAL","canonical_entries":[]}]}
        (self.root/'docs/architecture/capabilities.json').write_text(json.dumps(data))
        (self.root/'docs/plans').mkdir()
        (self.root/'docs/plans/frontend-design-coverage.json').write_text(json.dumps({'areas':[{'page':'owner@example.invalid'}]}))
        build(self.root,self.snapshot,working_tree=True)
        index=KnowledgeIndex(self.snapshot)
        self.assertNotIn('ANON_CREDENTIAL',json.dumps(index.trace('fixture')))
        self.assertNotIn('owner@example.invalid',json.dumps(index.system_map()))

    def test_commit_archive_excludes_oversized_blob_before_read(self):
        (self.root/'mvp_v2/large.py').write_text('x'*5000)
        subprocess.run(['git','-C',str(self.root),'add','.'],check=True)
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','large'],check=True)
        from scripts.mplus_mcp import index as module
        original=module.git
        calls=[]
        def capture(repo,*args):
            if args[0]=='archive': calls.append(args)
            return original(repo,*args)
        with patch.object(module,'MAX_FILE_BYTES',4000),patch.object(module,'git',side_effect=capture):
            build(self.root,self.snapshot)
        self.assertTrue(calls)
        self.assertNotIn('mvp_v2/large.py',calls[0])


if __name__=='__main__':
    unittest.main()
