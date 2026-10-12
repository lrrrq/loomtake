#!/usr/bin/env python3
"""Measure the actual picker and image service with isolated, anonymous project state.

The real package is read-only. Generated inputs, derivatives and reports stay below
--output. Browser cold means a new browser context after package/thumbnail preparation;
it does not mean cold OS caches or an unprepared package. This is a performance fixture,
not production authentication, human approval or the complete delivery journey.

Example (Python environment must include Pillow and Playwright):
  python scripts/benchmark_image_library.py --real-package /path/to/immutable/package \
    --synthetic-count 500 --samples 10 --output /path/to/evidence
Reuse generated inputs with --phase measure. --phase prepare stops after preparation.
"""
from __future__ import annotations

import argparse
import builtins
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import faulthandler
from hashlib import sha256
from http.server import ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import platform
import random
import shutil
import signal
import sqlite3
import statistics
import sys
from threading import Lock, Thread, local
import time
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from PIL import Image, PngImagePlugin
from playwright.sync_api import expect, sync_playwright

from mvp_v2.asset_package import bundle_asset_package_v3, create_asset_package
from mvp_v2.formal_default_images import FormalDefaultImageService
from mvp_v2.image_preview import image_preview_bytes
from mvp_v2.package_registry import LocalPackageRegistry, build_package_record
from mvp_v2.project_image_authority import ProjectImagePoolService
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests.test_mvp_v2_creative_workspace_browser import _CreativeFixture
from tests.test_mvp_v2_formal_script_workspace import FormalScriptWorkspaceTests
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID, UI_ROOT, _handler


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _source_fingerprint() -> dict:
    paths = ("mvp_v2/ui/app.js", "mvp_v2/ui/app.css", "mvp_v2/formal_default_images.py",
        "mvp_v2/version_delivery.py", "mvp_v2/image_preview.py", "mvp_v2/project_image_authority.py",
        "mvp_v2/package_registry.py", "mvp_v2/formal_generation_service.py", "mvp_v2/formal_console.py")
    return {path: sha256((REPOSITORY_ROOT / path).read_bytes()).hexdigest() for path in paths}


def _distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"samples": len(values), "median": round(statistics.median(values), 3),
        "p95": round(ordered[math.ceil(.95 * len(ordered)) - 1], 3),
        "maximum": round(max(values), 3)} if values else {"samples": 0}


def _media_records(package: Path) -> list[dict]:
    manifest = package / "asset_package.json" if package.is_dir() else package
    value = json.loads(manifest.read_text(encoding="utf-8"))
    index = manifest.parent / value["index"]
    with sqlite3.connect(f"{index.resolve().as_uri()}?mode=ro&immutable=1", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT media_id,current_path,content_sha256 FROM media_assets ORDER BY media_id").fetchall()
    records = []
    for row in rows:
        source = Path(row["current_path"])
        source = source if source.is_absolute() else manifest.parent / source
        records.append({"media_id": row["media_id"], "content_sha256": row["content_sha256"],
            "path": str(source.resolve()), "size_bytes": source.stat().st_size})
    return records


def _noise_image(index: int, kind: str) -> Image.Image:
    size = (625, 350) if kind == "JPEG" else (250, 140)
    noise = Image.frombytes("RGB", size, random.Random(20260910 + index).randbytes(size[0] * size[1] * 3))
    if kind == "JPEG":
        return noise.resize((2500, 1400), Image.Resampling.BICUBIC)
    return noise.quantize(colors=64).resize((2500, 1400), Image.Resampling.NEAREST)


def _create_synthetic(output: Path, source_root: Path, count: int, max_bytes: int) -> dict:
    root = output / "synthetic"
    if root.exists():
        raise ValueError("匿名素材目录已存在；请用 --phase measure 复用，或指定新的 --output")
    media = root / "inputs"
    media.mkdir(parents=True)
    manifests, analyses, sizes, transparent_paths = [], [], [], []
    formats = {"JPEG": 0, "PNG": 0, "GIF": 0}
    start = time.perf_counter()
    animation_count = min(14, max(1, count // 30))
    for index in range(count):
        kind = "GIF" if index < animation_count else "PNG" if index % 20 == 0 else "JPEG"
        image = _noise_image(index, kind)
        path = media / f"anonymous-{index:04d}.{ {'JPEG': 'jpg', 'PNG': 'png', 'GIF': 'gif'}[kind]}"
        if kind == "JPEG":
            image.save(path, format="JPEG", quality=50, subsampling=2)
        elif kind == "PNG":
            image = image.convert("RGBA")
            image.putalpha(168)
            image.save(path, format="PNG", compress_level=3)
            transparent_paths.append(path)
        else:
            image.save(path, format="GIF", save_all=True, duration=80, loop=0,
                append_images=[image.transpose(Image.Transpose.FLIP_LEFT_RIGHT),
                    image.transpose(Image.Transpose.FLIP_TOP_BOTTOM), image.transpose(Image.Transpose.ROTATE_180)])
        size = path.stat().st_size
        sizes.append(size)
        if sum(sizes) > max_bytes:
            raise ValueError(f"匿名样本达到大小上限，已生成 {index + 1} 张；已有文件保留在 {root}")
        formats[kind] += 1
        identity = f"anonymous-{index:04d}"
        manifests.append({"image_id": identity, "原图路径": str(path)})
        analyses.append({"image_id": identity, "caption": f"匿名纹理性能样本{index:04d}：发布会展台与观众交流",
            "scene": "发布会现场", "subjects": ["展台", "观众"], "tags": ["发布会", "交流", "现场"],
            "purpose": "匿名规模与传输性能检验", "brand_names": ["无"], "quality_flags": [],
            "source": "anonymous_performance_fixture", "vision_recognition_status": "succeeded"})
        if index < animation_count or (index + 1) % 50 == 0:
            print(f"synthetic {index + 1}/{count}: {sum(sizes) / 2**20:.1f} MiB", flush=True)
    # Different encoded identities preserve identical static pixels and independent origins.
    duplicates = min(12, len(transparent_paths))
    for index, source in enumerate(transparent_paths[:duplicates]):
        path = media / f"duplicate-origin-{index:02d}.png"
        with Image.open(source) as image:
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text("anonymous_source", f"second-origin-{index}")
            image.save(path, pnginfo=metadata, compress_level=3)
        sizes.append(path.stat().st_size)
        if sum(sizes) > max_bytes:
            raise ValueError("含重复来源的样本超过 --max-fixture-mib；已生成文件保留")
        identity = f"duplicate-origin-{index:02d}"
        manifests.append({"image_id": identity, "原图路径": str(path)})
        analyses.append({**analyses[int(source.stem.split('-')[-1])], "image_id": identity})
    for name, values in (("image_manifest.jsonl", manifests), ("image_analysis.minimax.jsonl", analyses)):
        (media / name).write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values), encoding="utf-8")
    generation_seconds = time.perf_counter() - start
    start = time.perf_counter()
    seed = create_asset_package(root / "seed", package_id="anonymous-performance-gallery", source_root=source_root,
        category_id="launch", media_roots=[media], metadata_roots=[media])
    copy2 = shutil.copy2

    def link_generated(source, destination, *args, **kwargs):
        if Path(source).resolve().is_relative_to(media):
            os.link(source, destination)
            return str(destination)
        return copy2(source, destination, *args, **kwargs)

    with patch("mvp_v2.asset_package.shutil.copy2", side_effect=link_generated):
        package = bundle_asset_package_v3(seed, root / "package", package_version=1)
    return {"package": str(package.parent), "generated_unique_images": count, "duplicate_pixel_origins": duplicates,
        "formats": formats, "animation_frames": 4, "dimensions": [2500, 1400],
        "input_bytes": sum(sizes), "input_size_bytes": _distribution(sizes),
        "generation_seconds": round(generation_seconds, 3), "packaging_seconds": round(time.perf_counter() - start, 3),
        "texture": "seeded correlated RGB noise; large JPEGs plus block-noise transparent PNG and animated GIF",
        "storage": "generated source pixels hardlinked into immutable fixture package; real package is never copied"}


class _ReadCounter:
    def __init__(self, paths: list[str]):
        self.paths = set(paths)
        self.lock = Lock()
        self.state = local()
        self.records = []
        self.active_previews = 0
        self.max_previews = 0

    @contextmanager
    def measure(self, kind: str, request: str = ""):
        record = {"kind": kind, "request": request, "original_file_opens": 0, "original_bytes_read": 0}
        self.state.record = record
        start = time.perf_counter()
        if kind == "preview":
            with self.lock:
                self.active_previews += 1
                self.max_previews = max(self.max_previews, self.active_previews)
                record["server_previews_inflight_at_start"] = self.active_previews
        try:
            yield record
        finally:
            record["server_ms"] = round((time.perf_counter() - start) * 1000, 3)
            with self.lock:
                self.records.append(record)
                if kind == "preview":
                    self.active_previews -= 1
            if kind.startswith("prepare"):
                print(f"{kind}: {record['server_ms'] / 1000:.2f}s, source opens {record['original_file_opens']}", flush=True)
            self.state.record = None

    def wrapper(self, original):
        counter = self

        def open_counted(file, mode="r", *args, **kwargs):
            nested = getattr(counter.state, "opening", False)
            counter.state.opening = True
            try:
                handle = original(file, mode, *args, **kwargs)
            finally:
                counter.state.opening = nested
            path = os.path.abspath(os.fsdecode(file)) if isinstance(file, (str, bytes, os.PathLike)) else ""
            record = getattr(counter.state, "record", None)
            if nested or record is None or path not in counter.paths or not ("r" in mode or "+" in mode):
                return handle
            record["original_file_opens"] += 1

            class Reader:
                def __getattr__(self, name):
                    return getattr(handle, name)

                def read(self, *values):
                    result = handle.read(*values)
                    record["original_bytes_read"] += len(result)
                    return result

                def readinto(self, value):
                    count = handle.readinto(value)
                    record["original_bytes_read"] += count or 0
                    return count

                def __enter__(self):
                    return self

                def __exit__(self, *values):
                    return handle.__exit__(*values)

            return Reader()

        return open_counted


def _service(base, package: Path, output: Path, counter: _ReadCounter):
    generated = base.generated
    with counter.measure("prepare_package_record"):
        record = build_package_record(package)
    registry = LocalPackageRegistry(output / "registry.json")
    registry.register_records([record])
    with base.store.transaction() as connection:
        connection.execute("UPDATE projects SET template_package_ref=?,template_package_sha256=? WHERE project_id=?",
            (f"{record['package_id']}@{record['package_version']}", record["package_sha256"], generated.project["project_id"]))
    pool = ProjectImagePoolService(base.store, data_root=output / "cache-runtime")
    service = FormalDefaultImageService(projects=base.projects, tasks=TaskService(base.store), delivery=base.delivery,
        artifacts=base.resolver, image_pool=pool, image_workspace=None,
        asset_package_resolver=lambda _: package, writes_enabled=False, package_registry=registry)
    project = base.projects.get_project(generated.token, generated.project["project_id"])
    scope = dict(workspace_id=generated.workspace_id, project_id=generated.project["project_id"],
        expected_project_version=project["project_version"])
    with counter.measure("prepare_verified_media_and_first_library_page"):
        first = service.candidates(generated.token, **scope, view="library", match_scope="all", page_size=12, shot_no="1")
    return service, scope, first


def _server(base, service, scope: dict, counter: _ReadCounter):
    fixture = _CreativeFixture()
    fixture.project_version = scope["expected_project_version"]

    class Handler(_handler(fixture)):
        def _file(self):
            if urlparse(self.path).path.startswith("/assets/"):
                return super()._file()
            content = (UI_ROOT / "index.html").read_text().replace("__MPLUS_UI_BUILD__", "updated-anonymous-build").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _api(self, method: str):
            route, query = urlparse(self.path).path, parse_qs(urlparse(self.path).query)
            if method != "GET" or "/images/default-candidates" not in route:
                return super()._api(method)
            kind = "preview" if route.endswith("/preview") else "list"
            with counter.measure(kind, self.path) as measured:
                try:
                    if self.headers.get("X-MPlus-Agent-Auth") != "browser-session":
                        raise WorkspaceServiceError("AUTH_REQUIRED", "匿名性能夹具需要测试会话")
                    current = {**scope, "expected_project_version": int(query.get("expected_project_version", [scope["expected_project_version"]])[0])}
                    if kind == "preview":
                        body, mime = service.preview(base.generated.token, **current, media_id=route.split("/")[-2],
                            variant=query.get("variant", ["original"])[0])
                    else:
                        model = service.candidates(base.generated.token, **current,
                            view=query.get("view", ["library"])[0], match_scope=query.get("match_scope", ["all"])[0],
                            shot_no=query.get("shot_no", ["1"])[0], query=query.get("query", [""])[0],
                            page=int(query.get("page", [1])[0]), page_size=int(query.get("page_size", [12])[0]))
                        body, mime = json.dumps(model, ensure_ascii=False).encode(), "application/json; charset=utf-8"
                    status = 200
                except (WorkspaceServiceError, ValueError) as error:
                    status, mime = 409, "application/json; charset=utf-8"
                    body = json.dumps({"detail": {"code": getattr(error, "code", "INVALID_INPUT"), "message": str(error)}}).encode()
                measured.update(status=status, response_bytes=len(body))
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    measured["client_cancelled"] = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _browser_round(page, counter: _ReadCounter, *, screenshot: Path | None = None) -> dict:
    network, active, maximum = [], set(), [0]
    start_index = len(counter.records)

    def started(request):
        if "/images/default-candidates/" in request.url and "/preview" in request.url:
            active.add(request)
            maximum[0] = max(maximum[0], len(active))

    def finished(request):
        active.discard(request)
        if "/images/default-candidates" in request.url:
            timing = request.timing
            network.append({"url": request.url, "response_end_ms": timing.get("responseEnd"),
                "response_body_bytes": request.sizes().get("responseBodySize", 0)})

    def failed(request):
        active.discard(request)
        network.append({"url": request.url, "failure": request.failure})

    for name, callback in (("request", started), ("requestfinished", finished), ("requestfailed", failed)):
        page.on(name, callback)
    start = time.perf_counter()
    try:
        page.locator('[data-image-picker="0"]').click()
        dialog = page.locator("#creative-image-picker")
        dialog.wait_for(state="visible")
        library = dialog.locator('[data-picker-source="library"]')
        if library.get_attribute("aria-pressed") != "true":
            with page.expect_response(lambda response: "/images/default-candidates?" in response.url
                    and parse_qs(urlparse(response.url).query).get("view") == ["library"]):
                library.click()
        # The app clears this only after its visible-card promises finish decoding.
        # Stop timing here; per-image remote assertions below are verification overhead.
        expect(dialog.locator(".creative-picker-results")).not_to_have_attribute("aria-busy", "true", timeout=30_000)
        elapsed_ms = (time.perf_counter() - start) * 1000
        images = dialog.locator(".creative-picker-card img")
        expect(images).to_have_count(12, timeout=30_000)
        for index in range(12):
            expect(images.nth(index)).to_have_js_property("complete", True, timeout=30_000)
            expect(images.nth(index)).not_to_have_js_property("naturalWidth", 0, timeout=30_000)
        validation_complete_ms = (time.perf_counter() - start) * 1000
        records = deepcopy(counter.records[start_index:])
        if screenshot is not None:
            page.screenshot(path=str(screenshot), full_page=False)
        dialog.locator("[data-picker-close]").click()
        return {"first_12_loaded_ms": round(elapsed_ms, 3), "network": network,
            "runner_validation_complete_ms": round(validation_complete_ms, 3),
            "max_browser_preview_inflight": maximum[0], "server_requests": records,
            "max_server_preview_inflight": max((row.get("server_previews_inflight_at_start", 0) for row in records), default=0),
            "original_file_opens": sum(record["original_file_opens"] for record in records),
            "original_bytes_read": sum(record["original_bytes_read"] for record in records),
            "response_bytes": sum(record.get("response_bytes", 0) for record in records)}
    finally:
        for name, callback in (("request", started), ("requestfinished", finished), ("requestfailed", failed)):
            page.remove_listener(name, callback)


def _measure_browser(url: str, screenshot: Path, counter: _ReadCounter, samples: int) -> dict:
    cold, warm, errors = [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for index in range(samples):
                context = browser.new_context(viewport={"width": 1440, "height": 1100})
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("localStorage.setItem('mplus.agent.auth','browser-session');localStorage.setItem('mplus.agent.workspace','browser-workspace');")
                page.goto(f"{url}/projects/{PROJECT_ID}/script?panel=images")
                page.locator('[data-image-picker="0"]').wait_for(state="visible", timeout=30_000)
                cold.append(_browser_round(page, counter, screenshot=screenshot if index == 0 else None))
                warm.append(_browser_round(page, counter))
                context.close()
                print(f"browser {index + 1}/{samples}: cold {cold[-1]['first_12_loaded_ms']:.0f}ms / warm {warm[-1]['first_12_loaded_ms']:.0f}ms", flush=True)
        finally:
            browser.close()
    return {"cold_browser_cache": cold, "warm_picker_cache": warm, "page_errors": errors,
        "cold_first_12_ms": _distribution([row["first_12_loaded_ms"] for row in cold]),
        "warm_first_12_ms": _distribution([row["first_12_loaded_ms"] for row in warm]),
        "cold_response_bytes": _distribution([row["response_bytes"] for row in cold]),
        "cold_original_file_opens": _distribution([row["original_file_opens"] for row in cold]),
        "warm_response_bytes": _distribution([row["response_bytes"] for row in warm]),
        "warm_original_file_opens": _distribution([row["original_file_opens"] for row in warm])}


def _measure_api(url: str, scope: dict, samples: int) -> dict:
    result = {}
    for view in ("library", "recommended"):
        measurements = []
        primer = None
        for index in range(samples + 1):
            params = urlencode({**scope, "view": view, "match_scope": "all" if view == "library" else "current_shot",
                "shot_no": "1", "page": 1, "page_size": 12})
            request = Request(f"{url}/api/formal/projects/{quote(PROJECT_ID, safe='')}/images/default-candidates?{params}",
                headers={"X-MPlus-Agent-Auth": "browser-session"})
            start = time.perf_counter()
            with urlopen(request, timeout=60) as response:
                body = response.read()
            model = json.loads(body)
            measured = {"elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
                "response_bytes": len(body), "inventory_records": model.get("inventory_records", model.get("candidate_count")),
                "returned_shots": len(model.get("shots", []))}
            if index == 0:
                primer = measured
            else:
                measurements.append(measured)
        result[view] = {"primer": primer, "primer_in_warm_distribution": False,
            "primer_definition": "first request of this API stage; recommended first matching is after library-only browser rounds",
            "requests": measurements, "elapsed_ms": _distribution([row["elapsed_ms"] for row in measurements])}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-package", type=Path)
    parser.add_argument("--output", type=Path, default=Path("/tmp") / f"mplus-image-library-{time.time_ns()}")
    parser.add_argument("--synthetic-count", type=int, default=500)
    parser.add_argument("--max-fixture-mib", type=int, default=500)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--phase", choices=("prepare", "measure", "all"), default="all")
    parser.add_argument("--diagnostic-stack-interval", type=int, default=0,
        help="emit Python thread stacks every N seconds for diagnosis; disabled by default")
    args = parser.parse_args()
    signal.signal(signal.SIGINT, signal.default_int_handler)
    if args.diagnostic_stack_interval > 0:
        faulthandler.dump_traceback_later(args.diagnostic_stack_interval, repeat=True)
    if args.samples < 1 or args.synthetic_count < 0:
        parser.error("samples must be positive and synthetic-count nonnegative")
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_stamp = str(time.time_ns())
    manifest_path = output / "fixture-manifest.json"
    base = FormalScriptWorkspaceTests("runTest")
    base.setUp()
    report = {"schema_version": "mplus-image-library-performance/v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": platform.python_version(), "platform": platform.platform(), "viewport": [1440, 1100]},
        "scope": "real app picker, real FormalDefaultImageService, anonymous saved-version backend and fixture authentication/other DTOs",
        "cold_definition": "package and thumbnails prepared; new browser context, normal OS caches",
        "first_12_definition": "real clicks to library results aria-busy removed after card decode promises; twelve complete/nonzero-width images are then independently asserted outside the timer",
        "not_proven": ["production authentication and multiuser journey", "human image quality", "complete adoption/review/delivery journey"],
        "samples": args.samples, "datasets": {}}
    report["source_start"] = _source_fingerprint()
    try:
        if args.phase == "measure":
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            if manifest_path.exists():
                raise ValueError("准备清单已存在，请用 --phase measure 或指定新目录")
            manifest = {}
            if args.real_package:
                manifest["real"] = {"package": str(args.real_package.expanduser().resolve()), "read_only": True}
            if args.synthetic_count:
                manifest["synthetic"] = _create_synthetic(output, base.generated.source_root,
                    args.synthetic_count, args.max_fixture_mib * 2**20)
            _write_json(manifest_path, manifest)
        for label, dataset in manifest.items():
            print(f"prepare {label}: package verification and thumbnails are measured separately", flush=True)
            target = output / label
            target.mkdir(exist_ok=True)
            package = Path(dataset["package"])
            media = _media_records(package)
            counter = _ReadCounter([item["path"] for item in media])
            with ExitStack() as stack:
                stack.enter_context(patch("builtins.open", counter.wrapper(builtins.open)))
                stack.enter_context(patch("io.open", counter.wrapper(io.open)))
                stack.enter_context(patch.object(Path, "open", counter.wrapper(Path.open)))
                service, scope, first = _service(base, package, target, counter)
                with counter.measure("prepare_all_thumbnails") as prepared:
                    total_preview_bytes = 0
                    for index, item in enumerate(media):
                        content, _ = image_preview_bytes(Path(item["path"]).read_bytes(), content_sha256=item["content_sha256"],
                            variant="thumbnail", cache_root=target / "cache-runtime" / "derived-image-previews")
                        total_preview_bytes += len(content)
                        if (index + 1) % 100 == 0:
                            print(f"prepared previews {index + 1}/{len(media)}", flush=True)
                    prepared["thumbnail_bytes"] = total_preview_bytes
                selected = [item for item in first.get("library_candidates", []) if not item.get("duplicate_of")][:12]
                result = report["datasets"][label] = {**dataset, "media_records": len(media),
                    "total_original_bytes": sum(item["size_bytes"] for item in media),
                    "original_size_bytes": _distribution([item["size_bytes"] for item in media]),
                    "first_12_sources": [{key: item.get(key) for key in ("candidate_key", "media_id", "content_sha256", "sources", "source")} for item in selected],
                    "preparation": deepcopy(counter.records), "prepared_thumbnail_bytes": total_preview_bytes}
                print(f"prepared {label}: {len(media)} images, originals {result['total_original_bytes'] / 2**20:.1f} MiB, thumbnails {total_preview_bytes / 2**20:.2f} MiB", flush=True)
                _write_json(target / "preparation.json", result)
                if args.phase != "prepare":
                    result["measurement_source_start"] = _source_fingerprint()
                    screenshot = target / f"{args.phase}-{run_stamp}-first-screen.png"
                    server, url = _server(base, service, scope, counter)
                    try:
                        result["browser"] = _measure_browser(url, screenshot, counter, args.samples)
                        result["screenshot"] = {"path": str(screenshot), "sha256": sha256(screenshot.read_bytes()).hexdigest()}
                        result["api"] = _measure_api(url, scope, args.samples)
                        result["http_requests"] = counter.records[len(result["preparation"]):]
                    finally:
                        server.shutdown()
                        server.server_close()
                    result["measurement_source_end"] = _source_fingerprint()
                    result["measurement_source_unchanged"] = result["measurement_source_start"] == result["measurement_source_end"]
                    result["loaded_source_matches_measurement"] = report["source_start"] == result["measurement_source_start"]
                    result["targets"] = {"cold_first_12_p95_ms": {"maximum": 3000,
                        "actual": result["browser"]["cold_first_12_ms"]["p95"]},
                        "warm_first_12_p95_ms": {"maximum": 1500,
                        "actual": result["browser"]["warm_first_12_ms"]["p95"]},
                        **{f"{view}_api_p95_ms": {"maximum": 500,
                            "actual": result["api"][view]["elapsed_ms"]["p95"]} for view in ("library", "recommended")}}
                    for criterion in result["targets"].values():
                        criterion["met"] = criterion["actual"] <= criterion["maximum"]
        report["state"] = "prepared" if args.phase == "prepare" else "measured"
    except BaseException as error:
        report.update(state="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        path = output / f"{args.phase}-{run_stamp}.json"
        report["source_end"] = _source_fingerprint()
        report["source_unchanged"] = report["source_start"] == report["source_end"]
        _write_json(path, report)
        base.doCleanups()
        faulthandler.cancel_dump_traceback_later()
        print(f"report: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
