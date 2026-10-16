from __future__ import annotations

import ast
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone


POLICY_VERSION = "engineering-local-v1"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_CORPUS_BYTES = 64 * 1024 * 1024
CHUNK_LINES = 60
MAX_CHUNK_CHARS = 12000
EXPLICIT_DOCS = {
    "AGENTS.md", "README.md", "pyproject.toml",
    "docs/architecture/capabilities.json",
    "docs/plans/frontend-design-coverage.json",
    "docs/requirements/M+需求导航-v1.md",
    "docs/architecture/M+前端协作规则-v1.md",
    "各需求文档/M+脚本项目升级需求规格说明书-v1.0.md",
    "各需求文档/M+脚本项目升级需求规格说明书-v2.0.md",
}
SECRET = re.compile(
    r"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----[\s\S]*?-----END (?:[A-Z ]*PRIVATE KEY)-----"
    r"|\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{16,})"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|(?i:bearer\s+[A-Za-z0-9._~+/-]{16,})"
)
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?im)^.*\b[A-Za-z0-9_-]*(?:api[_-]?key|secret|password|passwd|access[_-]?token|"
    r"refresh[_-]?token|session[_-]?token|client[_-]?secret)[A-Za-z0-9_-]*\s*[\"']?\s*[:=].*$"
)
PRIVATE_PATH = re.compile(r"(?:/Users/|/home/|/Applications/lrq/|[A-Za-z]:\\Users\\)[^\s\"'`<>\])},;]+")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SENSITIVE_KEY = re.compile(r"api[_-]?key|secret|password|passwd|(?:access|refresh|session)[_-]?token|(?:database|postgres)[_-]?(?:url|dsn)", re.I)
URL_CREDENTIAL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@[^\s\"'`<>]+", re.I)
QUOTED_CREDENTIAL = re.compile(
    r'''(?i)\b[A-Za-z0-9_-]*(?:api[_-]?key|secret|password|passwd|access[_-]?token|refresh[_-]?token|session[_-]?token)[A-Za-z0-9_-]*["']?\s*[:=]\s*\(?\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)'''
)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sanitize(text: str) -> tuple[str, bool]:
    original = text
    # Preserve line positions even for multiline secrets.
    text = SECRET.sub(lambda m: "[REDACTED]" + "\n" * m.group().count("\n"), text)
    text = QUOTED_CREDENTIAL.sub(lambda m: "[REDACTED ASSIGNMENT]" + "\n" * m.group().count("\n"), text)
    text = SENSITIVE_ASSIGNMENT.sub("[REDACTED ASSIGNMENT]", text)
    text = PRIVATE_PATH.sub("[LOCAL_PATH]", text)
    text = EMAIL.sub("[EMAIL]", text)
    text = URL_CREDENTIAL.sub("[REDACTED_URL]", text)
    return text, text != original


def sanitize_source(text: str, suffix: str) -> tuple[str, bool]:
    original = text
    if suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            # Nonparseable Python is excluded rather than trusting line regex for multiline literals.
            return "\n".join("[UNPARSEABLE SOURCE]" for _ in text.splitlines()), True
        spans = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(SENSITIVE_KEY.search(ast.unparse(t)) for t in targets):
                    spans.append((node.lineno, node.end_lineno))
            elif isinstance(node, ast.keyword) and node.arg and SENSITIVE_KEY.search(node.arg):
                spans.append((node.lineno, node.end_lineno))
            elif isinstance(node, ast.Dict):
                for key,value in zip(node.keys,node.values):
                    if isinstance(key,ast.Constant) and isinstance(key.value,str) and SENSITIVE_KEY.search(key.value):
                        spans.append((key.lineno,value.end_lineno))
            elif isinstance(node, ast.arguments):
                positional = node.posonlyargs + node.args
                pairs = list(zip(positional[-len(node.defaults):], node.defaults)) if node.defaults else []
                pairs += list(zip(node.kwonlyargs,node.kw_defaults))
                for argument,value in pairs:
                    if value is not None and SENSITIVE_KEY.search(argument.arg):
                        spans.append((value.lineno,value.end_lineno))
        lines=text.splitlines(keepends=True)
        for start,end in spans:
            for i in range(start-1,end):
                lines[i]="[REDACTED ASSIGNMENT]"+("\n" if lines[i].endswith("\n") else "")
        text="".join(lines)
    elif suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError:
            return "\n".join("[UNPARSEABLE SOURCE]" for _ in text.splitlines()), True
        spans=[]
        for match in re.finditer(r'("(?:\\.|[^"\\])*")\s*:\s*',text):
            if SENSITIVE_KEY.search(json.loads(match.group(1))):
                _,end=json.JSONDecoder().raw_decode(text,match.end())
                spans.append((match.end(),end))
        # Merge nested secret spans before replacing from the end.
        merged=[]
        for start,end in sorted(spans):
            if merged and start<=merged[-1][1]: merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
            else: merged.append((start,end))
        for start,end in reversed(merged):
            text=text[:start]+'"[REDACTED]"'+"\n"*text[start:end].count("\n")+text[end:]
    clean,_=sanitize(text)
    return clean,clean!=original


def safe_metadata(value):
    if isinstance(value,str): return sanitize(value)[0]
    if isinstance(value,list): return [safe_metadata(v) for v in value]
    if isinstance(value,dict):
        return {k:("[REDACTED]" if SENSITIVE_KEY.search(k) else safe_metadata(v)) for k,v in value.items()}
    return value


def allowed_path(value: str) -> bool:
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in {".", ".."} or part.startswith(".") for part in p.parts):
        return False
    if value in EXPLICIT_DOCS:
        return True
    if p.parts[:1] == ("mvp_v2",):
        return p.suffix in {".py", ".js", ".html", ".css"} and "__pycache__" not in p.parts
    return p.parts[:1] == ("schemas",) and p.suffix == ".json"


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL)


def source_files(repo: Path, working_tree: bool, commit: str):
    if not working_tree:
        paths=[]; total=0
        for record in git(repo,"ls-tree","-r","-l","-z",commit).decode().split("\0"):
            if not record: continue
            details,path=record.split("\t",1)
            mode,kind,_,size=details.split()
            if kind!="blob" or mode=="120000" or not allowed_path(path) or int(size)>MAX_FILE_BYTES:
                continue
            total+=int(size)
            if total>MAX_CORPUS_BYTES: raise ValueError("CORPUS_LIMIT: narrow the allowlist")
            paths.append(path)
        if not paths:
            return
        archive = git(repo, "archive", "--format=tar", commit, "--", *paths)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar:
                if allowed_path(member.name) and member.isfile() and member.size <= MAX_FILE_BYTES:
                    yield member.name, tar.extractfile(member).read()
        return
    paths = set(git(repo, "ls-files", "-z").decode().split("\0")) | EXPLICIT_DOCS
    for name in sorted(paths):
        if not allowed_path(name):
            continue
        path = repo / name
        if not path.exists():
            continue
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != repo):
            continue
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                continue
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) <= MAX_FILE_BYTES:
            yield name, data


def python_relations(path: str, text: str):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], []
    module = path[:-3].replace("/", ".")
    symbols, routes = [], []

    def visit(node, parents=()):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = (*parents, child.name)
                symbols.append((".".join((module, *qualified)), child.lineno, child.end_lineno))
                for decorator in getattr(child, "decorator_list", []):
                    if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                            and decorator.func.attr in {"get", "post", "put", "patch", "delete"}
                            and decorator.args and isinstance(decorator.args[0], ast.Constant)
                            and isinstance(decorator.args[0].value, str)):
                        calls = sorted({ast.unparse(n.func) for n in ast.walk(child) if isinstance(n, ast.Call)})
                        routes.append({"method": decorator.func.attr.upper(), "route": decorator.args[0].value,
                                       "handler": ".".join((module, *qualified)), "line": child.lineno,
                                       "calls": calls[:60], "evidence": "static_only"})
                visit(child, qualified)
            else:
                visit(child, parents)
    visit(tree)
    return symbols, routes


def build(repo: Path, output: Path, *, working_tree: bool = False) -> dict:
    repo = repo.resolve(strict=True)
    output = output.absolute()
    if output.exists():
        raise ValueError("OUTPUT_EXISTS: use a new snapshot filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    commit = git(repo, "rev-parse", "HEAD").decode().strip()
    started = time.monotonic()
    fd, temporary = tempfile.mkstemp(prefix=".mplus-index-", dir=output.parent)
    os.chmod(temporary, 0o600)
    os.close(fd)
    connection = sqlite3.connect(temporary)
    manifest, capabilities, pages, routes = [], [], [], []
    raw_total = 0
    try:
        connection.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE documents(id TEXT PRIMARY KEY, path TEXT UNIQUE, sha256 TEXT, redacted INTEGER);
            CREATE TABLE chunks(id TEXT PRIMARY KEY, document_id TEXT, start_line INTEGER,
                                end_line INTEGER, text TEXT, truncated INTEGER);
            CREATE INDEX chunk_document ON chunks(document_id,start_line);
            CREATE TABLE symbols(name TEXT, document_id TEXT, start_line INTEGER, end_line INTEGER);
            CREATE INDEX symbol_name ON symbols(name);
        """)
        for path, data in source_files(repo, working_tree, commit):
            raw_total += len(data)
            if raw_total > MAX_CORPUS_BYTES:
                raise ValueError("CORPUS_LIMIT: narrow the allowlist")
            try:
                original = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            clean, redacted = sanitize_source(original, PurePosixPath(path).suffix)
            doc_id = "doc_" + digest(path.encode())[:24]
            connection.execute("INSERT INTO documents VALUES (?,?,?,?)", (doc_id, sanitize(path)[0], digest(clean.encode()), redacted))
            manifest.append({"path": path, "raw_sha256": digest(data), "sha256": digest(clean.encode()), "redacted": redacted})
            lines = clean.splitlines()
            for start in range(0, len(lines), CHUNK_LINES):
                body = "\n".join(lines[start:start + CHUNK_LINES])
                connection.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                                   (f"{doc_id}:{start+1}", doc_id, start+1, min(start+CHUNK_LINES, len(lines)),
                                    body[:MAX_CHUNK_CHARS], len(body) > MAX_CHUNK_CHARS))
            if path.endswith(".py"):
                symbols, found = python_relations(path, original)
                connection.executemany("INSERT INTO symbols VALUES (?,?,?,?)", [(n,doc_id,s,e) for n,s,e in symbols])
                routes.extend({**r, "document_id": doc_id} for r in found)
            if path == "docs/architecture/capabilities.json":
                # Redact string leaves, preserving the source JSON structure.
                fields = {"id","name","user_outcome","canonical_entries","required_invariants",
                          "forbidden_parallel_implementations","golden_tests","authoritative_data","authoritative_identity"}
                capabilities = [{k:v for k,v in c.items() if k in fields} for c in json.loads(original).get("capabilities", [])]
            if path == "docs/plans/frontend-design-coverage.json":
                areas = json.loads(original).get("areas", [])
                pages = sorted({a["page"] for a in areas})
        if not manifest:
            raise ValueError("EMPTY_CORPUS")
        if working_tree:
            verified = {p:digest(data) for p,data in source_files(repo, True, commit)}
            expected = {m['path']:m['raw_sha256'] for m in manifest}
            if verified != expected or git(repo, "rev-parse", "HEAD").decode().strip() != commit:
                raise ValueError("SOURCE_CHANGED_DURING_BUILD: retry after edits settle")
        metadata = {"schema_version": 1, "policy_version": POLICY_VERSION,
                    "source_commit": commit, "source_kind": "working_tree" if working_tree else "git_commit",
                    "dirty": bool(git(repo, "status", "--porcelain", "--untracked-files=no")) if working_tree else False,
                    "snapshot_id": digest(canonical({"policy":POLICY_VERSION,"commit":commit,"manifest":manifest})),
                    "generated_at": datetime.now(timezone.utc).isoformat(), "document_count":len(manifest),
                    "capabilities":safe_metadata(capabilities), "pages":safe_metadata(pages), "routes":safe_metadata(routes),
                    "excluded_scopes":["business_database", "customer_assets", "runtime", "logs", "tests", "history", "unlisted_documents"],
                    "coverage_note":"Allowlisted static sources only. Not a live runtime or exhaustive call graph.",
                    "source_manifest":manifest,
                    "redacted_document_count":sum(m['redacted'] for m in manifest)}
        for key,value in metadata.items():
            connection.execute("INSERT INTO metadata VALUES (?,?)",(key,json.dumps(value,ensure_ascii=False)))
        connection.commit()
        connection.close()
        # Do not overwrite a snapshot created concurrently.
        os.link(temporary, output)
        os.chmod(output,0o400)
        return {k:v for k,v in metadata.items() if k not in {"capabilities","routes","source_manifest"}} | {"build_seconds":round(time.monotonic()-started,3)}
    finally:
        connection.close()
        Path(temporary).unlink(missing_ok=True)


class KnowledgeIndex:
    def __init__(self, path: Path):
        self.path = path.resolve(strict=True)
        self.fingerprint = self._fingerprint()
        with self.connect() as conn:
            self.metadata = {r[0]:json.loads(r[1]) for r in conn.execute("SELECT key,value FROM metadata")}
        if self.metadata.get("schema_version") != 1:
            raise ValueError("UNSUPPORTED_SNAPSHOT")

    def _fingerprint(self):
        value = self.path.stat()
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)

    @contextmanager
    def connect(self):
        if self._fingerprint() != self.fingerprint:
            raise ValueError("SNAPSHOT_CHANGED: restart with a new verified snapshot")
        conn = sqlite3.connect(self.path.as_uri()+"?mode=ro", uri=True)
        conn.row_factory=sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        deadline=time.monotonic()+1.0
        conn.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
        try:
            yield conn
        finally:
            conn.close()

    def envelope(self, **fields):
        keys=("snapshot_id","source_commit","source_kind","dirty","generated_at","policy_version")
        return {**{k:self.metadata[k] for k in keys},"runtime_verified":False,**fields}

    def search(self, query: str, limit: int = 10, offset: int = 0):
        if not isinstance(query,str) or not 1<=len(query.strip())<=160 or not 1<=limit<=20 or not 0<=offset<=10000:
            raise ValueError("INVALID_INPUT")
        tokens=re.findall(r"[A-Za-z0-9_.-]+|[\u3400-\u9fff]+",query.strip())[:8]
        if not tokens: raise ValueError("INVALID_INPUT")
        conditions=[]; values=[]
        for token in tokens:
            conditions.append("(c.text LIKE ? ESCAPE '\\' OR d.path LIKE ? ESCAPE '\\')")
            pattern="%"+token.replace("\\","\\\\").replace("%","\\%").replace("_","\\_")+"%"
            values.extend([pattern,pattern])
        with self.connect() as conn:
            rows=conn.execute("SELECT c.*,d.path,d.redacted FROM chunks c JOIN documents d ON d.id=c.document_id WHERE "+
                              " AND ".join(conditions)+" ORDER BY d.path,c.start_line LIMIT ? OFFSET ?",values+[limit+1,offset]).fetchall()
        results=[]
        for row in rows[:limit]:
            text=row['text']; position=max(0,text.casefold().find(tokens[0].casefold())-100)
            results.append({"id":row['id'],"title":f"{row['path']}:{row['start_line']}",
                            "url":f"mplus-source://{self.metadata['snapshot_id']}/{row['id']}",
                            "snippet":text[position:position+500],"start_line":row['start_line'],
                            "end_line":row['end_line'],"redacted":bool(row['redacted'])})
        return self.envelope(results=results,next_offset=offset+limit if len(rows)>limit else None,
                             citation_mode="local_only",truncated=len(rows)>limit)

    def fetch(self, source_id: str):
        if not re.fullmatch(r"doc_[a-f0-9]{24}:[1-9][0-9]{0,6}",source_id):
            raise ValueError("INVALID_SOURCE_ID")
        with self.connect() as conn:
            row=conn.execute("SELECT c.*,d.path,d.sha256,d.redacted FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.id=?",(source_id,)).fetchone()
        if row is None: raise ValueError("SOURCE_NOT_FOUND")
        return self.envelope(id=source_id,title=row['path'],text=row['text'],start_line=row['start_line'],end_line=row['end_line'],
                             content_sha256=digest(row['text'].encode()),document_sha256=row['sha256'],
                             redacted=bool(row['redacted']),truncated=bool(row['truncated']),
                             url=f"mplus-source://{self.metadata['snapshot_id']}/{source_id}",citation_mode="local_only")

    def system_map(self, offset: int = 0, limit: int = 20):
        if not 0<=offset<=10000 or not 1<=limit<=20: raise ValueError("INVALID_INPUT")
        routes=self.metadata['routes']
        return self.envelope(document_count=self.metadata['document_count'],pages=self.metadata['pages'],
                             capabilities=[{"id":c['id'],"name":c['name']} for c in self.metadata['capabilities']],
                             routes=routes[offset:offset+limit],route_count=len(routes),
                             next_offset=offset+limit if offset+limit<len(routes) else None,
                             excluded_scopes=self.metadata['excluded_scopes'],coverage_note=self.metadata['coverage_note'])

    def trace(self, capability_id: str):
        item=next((c for c in self.metadata['capabilities'] if c['id']==capability_id),None)
        if item is None: raise ValueError("UNKNOWN_CAPABILITY: use mplus_get_system_map")
        entries=[]
        with self.connect() as conn:
            for entry in item.get('canonical_entries',[]):
                row=conn.execute("SELECT s.*,d.path FROM symbols s JOIN documents d ON d.id=s.document_id WHERE s.name=?",(entry,)).fetchone()
                entries.append({"entry":entry,"evidence":"static_symbol" if row else "registry_only",
                                "source_id":f"{row['document_id']}:{((row['start_line']-1)//CHUNK_LINES)*CHUNK_LINES+1}" if row else None,
                                "path":row['path'] if row else None,"line":row['start_line'] if row else None})
        return self.envelope(capability=item,entries=entries,verification="registry_and_static_code_only",
                             missing_evidence=["runtime_call_trace","current_browser_journey","human_quality_review"])
