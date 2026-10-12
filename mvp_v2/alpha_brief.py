"""Isolated 48H-Alpha document-to-confirmed-Brief workflow.

This is deliberately independent of WorkspaceStore and the production brief
snapshot.  Its root must be a fresh ``mplus-alpha-*`` directory under the
system temporary directory, so this module cannot accidentally become a
production import path.
"""

from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from PIL import Image
from pypdf import PdfReader


ALPHA_BRIEF_SCHEMA = "mplus-alpha-brief/v1"
SUPPORTED_SUFFIXES = frozenset({
    ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md",
    ".jpg", ".jpeg", ".png", ".webp",
})
_FIELD_ALIASES = {
    "Project": "项目名称", "Brand": "品牌或客户", "Script Type": "脚本类型",
    "项目名称": "项目名称", "项目": "项目名称", "名称": "项目名称",
    "品牌": "品牌或客户", "客户": "品牌或客户", "品牌或客户": "品牌或客户",
    "脚本类型": "脚本类型", "类型": "脚本类型",
    "视频目标": "视频目标", "使用场景": "使用场景", "目标时长": "目标时长",
    "时长": "目标时长", "必须出现": "必须出现的内容",
    "必须出现的内容": "必须出现的内容", "禁止": "禁止或不能出现的内容",
    "禁止或不能出现的内容": "禁止或不能出现的内容",
    "风格调性": "风格调性", "调性": "风格调性",
    "关键事件": "关键事件与流程", "流程": "关键事件与流程",
}
_KEY_VALUE = re.compile(r"^\s*([^：:#]{1,40})\s*[：:]\s*(.+?)\s*$")
_OFFICE_MEDIA_PREFIXES = ("word/media/", "xl/media/", "ppt/media/")


class AlphaBriefError(ValueError):
    """A visible, non-silent Alpha import or confirmation failure."""


class AlphaBriefStore:
    """Persists only anonymous Alpha artifacts below an explicitly safe root."""

    def __init__(self, root: str | Path, *, max_file_bytes: int = 5 * 1024 * 1024):
        candidate = Path(root).expanduser().resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if candidate.parent != temporary_root or not candidate.name.startswith("mplus-alpha-"):
            raise AlphaBriefError("Alpha 存储根必须是系统临时目录下的 mplus-alpha-* 隔离目录")
        if max_file_bytes <= 0:
            raise AlphaBriefError("max_file_bytes 必须大于 0")
        self.root = candidate
        self.inputs_root = self.root / "inputs"
        self.max_file_bytes = max_file_bytes
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def freeze_documents(self, documents: list[str | Path | dict]) -> dict:
        """Freeze anonymous files already placed below this store's inputs root."""
        if not isinstance(documents, list) or not documents:
            raise AlphaBriefError("documents 至少需要一个匿名文件")
        records = []
        for raw in documents:
            path, critical = _document_input(raw, self.inputs_root)
            records.append({
                "relative_path": path.relative_to(self.root).as_posix(),
                "sha256": _sha_file(path),
                "critical": critical,
            })
        if len({item["relative_path"] for item in records}) != len(records):
            raise AlphaBriefError("Alpha 冻结输入不能重复")
        payload = {"schema_version": ALPHA_BRIEF_SCHEMA, "documents": sorted(records, key=lambda item: item["relative_path"])}
        digest = _sha_json(payload)
        manifest = {**payload, "inputs_ref": f"artifact://alpha-inputs/{digest}", "inputs_sha256": digest}
        path = self.root / "frozen-inputs.json"
        if path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != manifest:
                raise AlphaBriefError("Alpha 输入已冻结；请创建新的隔离根")
            return existing
        self._write_once_path(path, manifest)
        return manifest

    def import_documents(self, project_id: str, documents: list[str | Path | dict]) -> dict:
        project = _required(project_id, "project_id")
        if not isinstance(documents, list):
            raise AlphaBriefError("documents 必须是冻结输入清单的完整列表")
        frozen = self._read_frozen_inputs()
        frozen_by_path = {item["relative_path"]: item for item in frozen["documents"]}
        submitted = []
        for raw in documents:
            path, _ = _document_input(raw, self.inputs_root)
            submitted.append(path.relative_to(self.root).as_posix())
        if len(submitted) != len(set(submitted)):
            raise AlphaBriefError("导入提交集合不能重复")
        if set(submitted) != set(frozen_by_path):
            raise AlphaBriefError("导入提交集合必须与冻结清单完整一致")
        source_documents: list[dict] = []
        extractions: list[dict] = []
        images: list[dict] = []
        for raw in documents:
            path, _ = _document_input(raw, self.inputs_root)
            record = frozen_by_path.get(path.relative_to(self.root).as_posix())
            if record is None or record["sha256"] != _sha_file(path):
                raise AlphaBriefError("导入文件未冻结或冻结后已变化")
            critical = bool(record["critical"])
            source, extraction, found_images = self._extract(project, path, critical)
            source_documents.append(source)
            extractions.append(extraction)
            images.extend(found_images)
        facts, conflicts = _build_facts(extractions)
        result = {
            "schema_version": ALPHA_BRIEF_SCHEMA,
            "project_id": project,
            "source_documents": source_documents,
            "extractions": extractions,
            "images": _dedupe_images(images),
            "facts": facts,
            "conflicts": conflicts,
            "confirmation_status": "pending",
        }
        result["brief_sha256"] = _sha_json(_brief_payload(result))
        self._write_json("imports", result["brief_sha256"], result)
        return result

    def confirm(self, imported: dict, *, confirmed_by: str) -> dict:
        if not isinstance(imported, dict) or imported.get("schema_version") != ALPHA_BRIEF_SCHEMA:
            raise AlphaBriefError("只能确认 Alpha 结构化 Brief")
        imported = self._read_imported(imported.get("project_id"), imported.get("brief_sha256"), expected=imported)
        actor = _required(confirmed_by, "confirmed_by")
        unresolved = [item["conflict_id"] for item in imported.get("conflicts", []) if item.get("status") == "unresolved"]
        critical_failures = [
            item.get("document_id", "") for item in imported.get("extractions", [])
            if item.get("status") == "failed" and item.get("critical")
        ]
        if unresolved or critical_failures:
            details = []
            if unresolved:
                details.append("未解决冲突=" + ",".join(unresolved))
            if critical_failures:
                details.append("关键文件失败=" + ",".join(critical_failures))
            raise AlphaBriefError("Brief 不可确认：" + "；".join(details))
        payload = _brief_payload(imported)
        version_sha = _sha_json(payload)
        target = self.root / "confirmed" / f"{version_sha}.json"
        if target.is_file():
            return json.loads(target.read_text(encoding="utf-8"))
        version = {
            "schema_version": ALPHA_BRIEF_SCHEMA,
            "brief_version_id": f"alpha_brief_{version_sha[:20]}",
            "project_id": imported["project_id"],
            "brief_ref": f"artifact://alpha-brief/{version_sha}",
            "brief_sha256": version_sha,
            "confirmation_status": "confirmed",
            "confirmed_by": actor,
            "confirmed_at": _utc_now(),
            "brief": payload,
        }
        version["artifact_sha256"] = _sha_json(version)
        self._write_once_json("confirmed", version_sha, version)
        return version

    def resolve_conflicts(self, imported: dict, resolutions: dict[str, str], *, resolved_by: str) -> dict:
        if not isinstance(imported, dict):
            raise AlphaBriefError("只能解决 Alpha 结构化 Brief 的冲突")
        persisted = self._read_imported(imported.get("project_id"), imported.get("brief_sha256"), expected=imported)
        resolved = resolve_conflicts(persisted, resolutions, resolved_by=resolved_by)
        self._write_json("imports", resolved["brief_sha256"], resolved)
        return resolved

    def read_confirmed(self, project_id: str, brief_ref: str, expected_sha256: str) -> dict:
        project = _required(project_id, "project_id")
        prefix = "artifact://alpha-brief/"
        digest = str(brief_ref or "").removeprefix(prefix)
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or digest != str(expected_sha256 or "").lower():
            raise AlphaBriefError("Alpha Brief 引用或 SHA-256 无效")
        path = self.root / "confirmed" / f"{digest}.json"
        if not path.is_file():
            raise AlphaBriefError("已确认 Alpha Brief 不可用")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            value.get("brief_sha256") != digest
            or value.get("confirmation_status") != "confirmed"
            or _sha_json(value.get("brief") or {}) != digest
            or value.get("artifact_sha256") != _sha_json({key: item for key, item in value.items() if key != "artifact_sha256"})
        ):
            raise AlphaBriefError("已确认 Alpha Brief 合同无效")
        if value.get("project_id") != project:
            raise AlphaBriefError("Alpha Brief 不属于当前隔离项目")
        return value

    def planning_input(self, project_id: str, brief_ref: str, expected_sha256: str) -> dict:
        return _planning_input(self.read_confirmed(project_id, brief_ref, expected_sha256))


    def _extract(self, project_id: str, path: Path, critical: bool) -> tuple[dict, dict, list[dict]]:
        suffix = path.suffix.lower()
        source_sha = _sha_file(path)
        document_id = f"alpha_doc_{source_sha[:20]}"
        source = {
            "document_id": document_id,
            "project_id": project_id,
            "filename": path.name,
            "mime_type": _mime_type(suffix),
            "size_bytes": path.stat().st_size,
            "sha256": source_sha,
            "critical": critical,
        }
        if suffix not in SUPPORTED_SUFFIXES:
            return source, _failure(document_id, critical, "UNSUPPORTED_TYPE", f"不支持 {suffix or '无扩展名'}"), []
        if source["size_bytes"] > self.max_file_bytes:
            return source, _failure(document_id, critical, "FILE_TOO_LARGE", f"文件超过 {self.max_file_bytes} bytes"), []
        try:
            locations, images = _extract_file(path, suffix, document_id)
        except AlphaBriefError as exc:
            return source, _failure(document_id, critical, "EXTRACTION_FAILED", str(exc)), []
        except Exception as exc:
            return source, _failure(document_id, critical, "EXTRACTION_FAILED", str(exc)), []
        return source, {
            "document_id": document_id,
            "status": "succeeded",
            "critical": critical,
            "locations": locations,
            "error": None,
        }, images

    def _write_json(self, category: str, digest: str, value: dict) -> None:
        directory = self.root / category
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        (directory / f"{digest}.json").write_text(_json(value), encoding="utf-8")

    def _write_once_json(self, category: str, digest: str, value: dict) -> None:
        directory = self.root / category
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = directory / f"{digest}.json"
        if target.exists():
            return
        temporary = directory / f".{digest}.tmp"
        temporary.write_text(_json(value), encoding="utf-8")
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
        finally:
            temporary.unlink(missing_ok=True)

    def _write_once_path(self, target: Path, value: dict) -> None:
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(_json(value), encoding="utf-8")
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
        finally:
            temporary.unlink(missing_ok=True)

    def _read_frozen_inputs(self) -> dict:
        path = self.root / "frozen-inputs.json"
        if not path.is_file():
            raise AlphaBriefError("Alpha 输入尚未冻结")
        value = json.loads(path.read_text(encoding="utf-8"))
        payload = {"schema_version": value.get("schema_version"), "documents": value.get("documents")}
        if value.get("inputs_sha256") != _sha_json(payload) or value.get("inputs_ref") != f"artifact://alpha-inputs/{value.get('inputs_sha256')}":
            raise AlphaBriefError("Alpha 输入冻结清单无效")
        return value

    def _read_imported(self, project_id: str, brief_sha256: str, *, expected: dict) -> dict:
        project = _required(project_id, "project_id")
        digest = str(brief_sha256 or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AlphaBriefError("Alpha 导入 Brief SHA-256 无效")
        path = self.root / "imports" / f"{digest}.json"
        if not path.is_file():
            raise AlphaBriefError("Alpha 导入 Brief 不存在或未持久化")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            value.get("project_id") != project
            or value.get("brief_sha256") != digest
            or _sha_json(_brief_payload(value)) != digest
            or _json(value) != _json(expected)
        ):
            raise AlphaBriefError("Alpha 导入 Brief 合同无效或已篡改")
        return value


def _planning_input(confirmed: dict) -> dict:
    """Adapt a confirmed Alpha Brief to the existing planner's input shape.

    It performs no generation and does not mutate a project.  The caller still
    has to pass this input through the separately-owned planner/generation
    gates.  Crucially, an incomplete confirmed Brief cannot be adapted.
    """
    if not isinstance(confirmed, dict) or confirmed.get("confirmation_status") != "confirmed":
        raise AlphaBriefError("只有人工确认的 Alpha Brief 才能进入规划")
    brief = confirmed.get("brief", {})
    facts_by_id = {item["fact_id"]: item for item in brief.get("facts", [])}
    facts = {}
    for fact in facts_by_id.values():
        facts.setdefault(fact["field"], fact["value"])
    for conflict in brief.get("conflicts", []):
        selected = facts_by_id.get(conflict.get("resolved_fact_id"))
        if conflict.get("status") == "resolved" and selected:
            facts[conflict["field"]] = selected["value"]
    required = ("项目名称", "脚本类型", "目标时长", "风格调性", "必须出现的内容")
    missing = [field for field in required if not str(facts.get(field) or "").strip()]
    if missing:
        raise AlphaBriefError("Alpha Brief 缺少规划必填事实: " + "、".join(missing))
    evidence = [ref for fact in brief["facts"] for ref in fact.get("evidence_refs", [])]
    return {
        "项目名称": facts["项目名称"], "脚本类型": facts["脚本类型"],
        "品牌或客户": facts.get("品牌或客户", ""), "行业或活动类别": facts.get("行业或活动类别", ""),
        "视频时长": facts["目标时长"], "风格调性": facts["风格调性"],
        "必须出现的内容": facts["必须出现的内容"],
        "禁忌或不能出现的内容": facts.get("禁止或不能出现的内容", ""),
        "活动流程": facts.get("关键事件与流程", facts["必须出现的内容"]),
        "Brief来源文件": "alpha-confirmed-brief", "Brief来源 sheet": "Alpha", "Brief来源行": 1,
        "Brief来源 SHA256": confirmed["brief_sha256"], "Brief来源证据": "；".join(sorted(set(evidence))),
        "Alpha Brief版本": confirmed["brief_version_id"], "Alpha Brief引用": confirmed["brief_ref"],
    }


def resolve_conflicts(imported: dict, resolutions: dict[str, str], *, resolved_by: str) -> dict:
    """Return a new pending Brief with explicit manager choices for conflicts."""
    if not isinstance(imported, dict) or imported.get("schema_version") != ALPHA_BRIEF_SCHEMA:
        raise AlphaBriefError("只能解决 Alpha 结构化 Brief 的冲突")
    actor = _required(resolved_by, "resolved_by")
    result = json.loads(_json(imported))
    for conflict in result.get("conflicts", []):
        selected = str(resolutions.get(conflict["conflict_id"], "")).strip()
        if not selected:
            continue
        if selected not in conflict["fact_ids"]:
            raise AlphaBriefError(f"冲突 {conflict['conflict_id']} 的选择不属于候选事实")
        conflict.update({"status": "resolved", "resolved_fact_id": selected, "resolved_by": actor})
    unresolved = [item["conflict_id"] for item in result.get("conflicts", []) if item.get("status") == "unresolved"]
    if unresolved:
        raise AlphaBriefError("仍有未解决冲突=" + ",".join(unresolved))
    result["brief_sha256"] = _sha_json(_brief_payload(result))
    return result


def _extract_file(path: Path, suffix: str, document_id: str) -> tuple[list[dict], list[dict]]:
    if suffix == ".pdf":
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise AlphaBriefError("PDF 已加密")
        return _pdf_locations(reader, document_id), []
    if suffix in {".txt", ".md"}:
        return _text_locations(path.read_text(encoding="utf-8"), document_id, "line"), []
    if suffix == ".xlsx":
        return _xlsx_locations(path, document_id), _office_images(path, document_id, "xlsx")
    if suffix == ".docx":
        return _office_xml_locations(path, document_id, "word/document.xml", "paragraph"), _office_images(path, document_id, "docx")
    if suffix == ".pptx":
        return _pptx_locations(path, document_id), _office_images(path, document_id, "pptx")
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        text = f"图片 {image.format}，尺寸 {image.width}x{image.height}"
    return [{"evidence_ref": _evidence_ref(document_id, {"image": 1}), "location": {"image": 1}, "text": text}], [
        _image_record(path.read_bytes(), document_id, {"image": 1}, path.name)
    ]


def _pdf_locations(reader: PdfReader, document_id: str) -> list[dict]:
    locations = []
    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            locations.append(_location(document_id, {"page": number}, text))
    if not locations:
        raise AlphaBriefError("PDF 没有可复制文本；扫描件 OCR 已延期")
    return locations


def _text_locations(text: str, document_id: str, key: str) -> list[dict]:
    locations = [
        _location(document_id, {key: number}, line.strip())
        for number, line in enumerate(text.splitlines(), start=1) if line.strip()
    ]
    if not locations:
        raise AlphaBriefError("文档没有可提取文本")
    return locations


def _xlsx_locations(path: Path, document_id: str) -> list[dict]:
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl exposes multiple reader exceptions
        raise AlphaBriefError(f"XLSX 无法读取: {exc}") from exc
    locations = []
    try:
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is not None and str(cell.value).strip():
                        locations.append(_location(document_id, {"sheet": sheet.title, "row": cell.row, "column": cell.column}, str(cell.value).strip()))
    finally:
        workbook.close()
    if not locations:
        raise AlphaBriefError("XLSX 没有非空单元格")
    return locations


def _office_xml_locations(path: Path, document_id: str, member: str, position_key: str) -> list[dict]:
    try:
        with ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read(member))
    except (BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise AlphaBriefError(f"Office 文档无法读取: {exc}") from exc
    values = ["".join(node.itertext()).strip() for node in root.iter() if node.tag.endswith("}p")]
    locations = [_location(document_id, {position_key: index}, value) for index, value in enumerate(values, start=1) if value]
    if not locations:
        raise AlphaBriefError("DOCX 没有可提取文本")
    return locations


def _pptx_locations(path: Path, document_id: str) -> list[dict]:
    try:
        with ZipFile(path) as archive:
            members = sorted(name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
            locations = []
            for index, member in enumerate(members, start=1):
                root = ElementTree.fromstring(archive.read(member))
                text = " ".join(value.strip() for value in root.itertext() if value.strip())
                if text:
                    locations.append(_location(document_id, {"slide": index}, text))
    except (BadZipFile, ElementTree.ParseError) as exc:
        raise AlphaBriefError(f"PPTX 无法读取: {exc}") from exc
    if not locations:
        raise AlphaBriefError("PPTX 没有可提取文本")
    return locations


def _office_images(path: Path, document_id: str, kind: str) -> list[dict]:
    try:
        with ZipFile(path) as archive:
            return [
                _image_record(archive.read(member), document_id, {"embedded_image": index}, Path(member).name)
                for index, member in enumerate(sorted(name for name in archive.namelist() if name.startswith(_OFFICE_MEDIA_PREFIXES)), start=1)
            ]
    except BadZipFile as exc:
        raise AlphaBriefError(f"Office 嵌图无法读取: {exc}") from exc


def _build_facts(extractions: list[dict]) -> tuple[list[dict], list[dict]]:
    candidates: dict[str, list[dict]] = defaultdict(list)
    for extraction in extractions:
        if extraction["status"] != "succeeded":
            continue
        for location in extraction["locations"]:
            match = _KEY_VALUE.match(location["text"])
            key = match.group(1).strip() if match else ""
            if match and _FIELD_ALIASES.get(key):
                field = _FIELD_ALIASES[key]
                value = match.group(2).strip()
                candidates[field].append({"value": value, "evidence_ref": location["evidence_ref"]})
    facts, conflicts = [], []
    for field, entries in sorted(candidates.items()):
        values = defaultdict(list)
        for entry in entries:
            values[entry["value"]].append(entry["evidence_ref"])
        fact_ids = []
        for value, refs in sorted(values.items()):
            fact_id = f"alpha_fact_{_sha_json({'field': field, 'value': value})[:20]}"
            fact_ids.append(fact_id)
            facts.append({"fact_id": fact_id, "field": field, "value": value, "confidence": "direct", "evidence_refs": sorted(set(refs))})
        if len(values) > 1:
            conflicts.append({
                "conflict_id": f"alpha_conflict_{_sha_json({'field': field, 'fact_ids': fact_ids})[:20]}",
                "field": field, "fact_ids": fact_ids, "status": "unresolved",
            })
    return facts, conflicts


def _document_input(raw: str | Path | dict, inputs_root: Path) -> tuple[Path, bool]:
    if isinstance(raw, dict):
        value, critical = raw.get("path"), bool(raw.get("critical", True))
    else:
        value, critical = raw, True
    raw_path = Path(str(value or ""))
    _reject_symlink_ancestors(inputs_root, raw_path)
    path = raw_path.expanduser().resolve()
    if not path.is_file():
        raise AlphaBriefError(f"导入文件不存在: {path.name or value}")
    try:
        path.relative_to(inputs_root.resolve())
    except ValueError as exc:
        raise AlphaBriefError("Alpha 导入只接受隔离 inputs 目录内的冻结文件") from exc
    return path, critical


def _reject_symlink_ancestors(inputs_root: Path, raw_path: Path) -> None:
    root = inputs_root.resolve()
    raw = Path(os.path.abspath(str(raw_path.expanduser())))
    if inputs_root.is_symlink():
        raise AlphaBriefError("Alpha 输入路径不允许符号链接")
    raw_root = next((item for item in (raw, *raw.parents) if item.exists() and item.samefile(root)), None)
    if raw_root is None:
        raise AlphaBriefError("Alpha 导入只接受隔离 inputs 目录内的冻结文件")
    relative = raw.relative_to(raw_root)
    candidate = raw_root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            raise AlphaBriefError("Alpha 输入路径不允许符号链接")


def _failure(document_id: str, critical: bool, code: str, message: str) -> dict:
    return {"document_id": document_id, "status": "failed", "critical": critical, "locations": [], "error": {"code": code, "message": message}}


def _location(document_id: str, location: dict, text: str) -> dict:
    return {"evidence_ref": _evidence_ref(document_id, location), "location": location, "text": text}


def _evidence_ref(document_id: str, location: dict) -> str:
    parts = ["artifact://alpha-evidence", document_id]
    for key, value in location.items():
        parts.extend((key, quote(str(value), safe="")))
    return "/".join(parts)


def _image_record(content: bytes, document_id: str, location: dict, filename: str) -> dict:
    digest = sha256(content).hexdigest()
    return {"image_id": f"alpha_image_{digest[:20]}", "sha256": digest, "filename": filename, "source_document_id": document_id, "location": location, "evidence_ref": _evidence_ref(document_id, location)}


def _dedupe_images(images: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for image in images:
        existing = grouped.setdefault(image["sha256"], {**image, "source_links": []})
        existing["source_links"].append({"source_document_id": image["source_document_id"], "location": image["location"]})
    return list(grouped.values())


def _brief_payload(imported: dict) -> dict:
    return {key: imported.get(key, []) for key in ("project_id", "source_documents", "extractions", "images", "facts", "conflicts")}


def _required(value: str, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise AlphaBriefError(f"{name} 不能为空")
    return normalized


def _mime_type(suffix: str) -> str:
    return {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".txt": "text/plain", ".md": "text/markdown", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(suffix, "application/octet-stream")


def _sha_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha_json(value: dict) -> str:
    return sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
