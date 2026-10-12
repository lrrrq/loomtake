"""Bounded, one-attempt image HTTP transports; no project or task writes.

Official API and model tutorial, checked 2026-09-11:
https://www.volcengine.com/docs/82379/1541523
https://www.volcengine.com/docs/82379/1824121

This adapter requests one PNG. It accepts static PNG/JPEG/WebP references, a
subset of the API's formats. The aggregate reference and response byte limits
below are local resource limits, not claims about upstream account quotas.
"""

from __future__ import annotations

import base64
import binascii
import http.client
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import warnings
from dataclasses import dataclass, field
from io import BytesIO
from typing import Mapping, Protocol
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from PIL import Image, UnidentifiedImageError


DEFAULT_SEEDREAM_MODEL = "doubao-seedream-5-0-lite-260128"
SEEDREAM_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
MAX_REFERENCE_BYTES = 30_000_000
MAX_TOTAL_REFERENCE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 65536
MAX_REQUEST_BYTES = 48 * 1024 * 1024
MAX_REFERENCE_PIXELS = 36_000_000
MIN_OUTPUT_PIXELS = 3_686_400
MAX_OUTPUT_PIXELS = 16_777_216
_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_ASPECT_SIZES = {"1:1": (2048, 2048), "16:9": (2560, 1440), "9:16": (1440, 2560),
    "4:3": (2304, 1728), "3:4": (1728, 2304), "3:2": (2496, 1664),
    "2:3": (1664, 2496), "21:9": (3136, 1344)}


class ImageProviderError(RuntimeError):
    """Safe failure metadata; an attempted paid request must not be auto-retried."""

    def __init__(self, code: str, message: str, *, request_id: str | None = None,
                 http_status: int | None = None, request_may_have_succeeded: bool = False):
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.http_status = http_status
        self.request_may_have_succeeded = request_may_have_succeeded


@dataclass(frozen=True)
class ImageProviderResult:
    content: bytes = field(repr=False)
    mime_type: str
    width: int
    height: int
    requested_model: str
    actual_model: str | None
    requested_size: str
    request_id: str | None
    usage: dict


@dataclass(frozen=True)
class ImageHTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = field(repr=False)


class ImageHTTPTransport(Protocol):
    def __call__(self, request: Request, *, timeout_seconds: float, max_bytes: int) -> ImageHTTPResponse: ...


class ImageProvider(Protocol):
    def __call__(self, prompt: str, reference_images: tuple[bytes, ...] = (), *,
                 aspect_ratio: str | None = None, size: str = "2K") -> ImageProviderResult: ...


ZHIPU_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/images/generations"
ZHIPU_PURPOSES = {
    "storyboard": {"label": "普通分镜", "model": "cogview-4-250304", "size": "1792x1008", "quality": "standard", "prompt_limit": 3600},
    "complex": {"label": "文字、多主体、多格画面", "model": "glm-image", "size": "2048x1152", "quality": "hd", "prompt_limit": 1000},
    "rough": {"label": "免费测试、粗构图", "model": "cogview-3-flash", "size": "1792x1008", "quality": "standard", "prompt_limit": 3600},
}


def image_creation_purpose(fields: dict) -> str:
    """Route from the saved visual requirement; free generation is opt-in only."""
    text = " ".join(str(value or "") for value in fields.values())
    return "complex" if any(word in text for word in (
        "指定文字", "准确文字", "中文标题", "多主体", "多格", "两格", "三格", "四格", "六格", "九格", "分屏", "左右关系",
    )) else "storyboard"


def zhipu_http_provider(*, api_key_env: str = "ZHIPU_API_KEY", timeout_seconds: float = 180,
                        transport: ImageHTTPTransport | None = None) -> ImageProvider:
    """One text-to-image request, one immediate bounded download, no model fallback.

    API and GLM prompt limits checked against docs.bigmodel.cn on 2026-09-14.
    Download URLs come from the authenticated response; direct TLS transport pins
    a public DNS address and rejects redirects. Credentials stay on the API POST.
    """
    if (not isinstance(api_key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", api_key_env)
            or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 1 <= timeout_seconds <= 600):
        raise ImageProviderError("IMAGE_PROVIDER_CONFIG_INVALID", "图片服务配置无效")
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key or any(ord(c) < 33 or ord(c) > 126 for c in api_key):
        raise ImageProviderError("IMAGE_PROVIDER_CREDENTIALS_MISSING", "智谱图片生成凭据尚未配置或无效")
    send = transport or _https_transport

    def invoke(prompt: str, reference_images: tuple[bytes, ...] = (), *,
               aspect_ratio: str | None = None, size: str | None = None,
               purpose: str = "storyboard", response_observer=None) -> ImageProviderResult:
        if not isinstance(purpose, str) or purpose not in ZHIPU_PURPOSES:
            raise ImageProviderError("IMAGE_INPUT_INVALID", "请选择有效的生成用途")
        settings = ZHIPU_PURPOSES[purpose]
        if reference_images or aspect_ratio not in (None, "16:9", "9:16", "1:1"):
            raise ImageProviderError("IMAGE_INPUT_INVALID", "当前服务支持生成新图，原图编辑尚未开放")
        if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt or len(prompt) > settings["prompt_limit"]:
            raise ImageProviderError("IMAGE_INPUT_INVALID", f"当前用途的画面要求须为 1–{settings['prompt_limit']} 字，请保留必要内容后手动缩短")
        selected_size = size or settings["size"]
        if size is None and aspect_ratio == "9:16":
            selected_size = "x".join(reversed(selected_size.split("x")))
        elif size is None and aspect_ratio == "1:1":
            selected_size = "1280x1280"
        if not isinstance(selected_size, str) or not re.fullmatch(r"[1-9][0-9]{2,3}x[1-9][0-9]{2,3}", selected_size):
            raise ImageProviderError("IMAGE_INPUT_INVALID", "请选择模型支持的像素尺寸")
        width, height = map(int, selected_size.split("x"))
        step, minimum, pixels = (32, 1024, 2**22) if purpose == "complex" else (16, 512, 2**21)
        if min(width, height) < minimum or max(width, height) > 2048 or width % step or height % step or width * height > pixels:
            raise ImageProviderError("IMAGE_INPUT_INVALID", "图片尺寸超出所选模型支持范围")
        payload = {"model": settings["model"], "prompt": prompt.strip(), "size": selected_size,
                   "quality": settings["quality"], "watermark_enabled": False}
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except UnicodeError:
            raise ImageProviderError("IMAGE_INPUT_INVALID", "图片创作要求包含无效文本编码") from None
        request = Request(ZHIPU_ENDPOINT, data=body, method="POST", headers={
            "Authorization": "Bearer " + api_key, "Content-Type": "application/json", "Accept": "application/json"})
        request_id = None
        try:
            response = _send(send, request, timeout_seconds=timeout_seconds, max_bytes=MAX_JSON_BYTES)
            request_id = _safe_token(_header(response, "x-request-id"), api_key)
            _require_success(response, request_id)
            if _media_type(response) != "application/json":
                raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片服务未返回有效 JSON")
            try:
                data = json.loads(response.body)
            except (ValueError, UnicodeError):
                raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片服务返回内容无法解析") from None
            if not isinstance(data, dict) or data.get("error"):
                raise ImageProviderError("IMAGE_PROVIDER_REJECTED", "图片服务未完成本次生成")
            items = data.get("data")
            if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片服务未返回唯一完整图片")
            returned_url = items[0].get("url")
            try:
                url = _zhipu_download_url(returned_url)
            except ImageProviderError as exc:
                try:
                    scheme = urlsplit(returned_url).scheme if isinstance(returned_url, str) else ""
                except ValueError:
                    scheme = ""
                # Record only structural diagnostics; never persist the URL,
                # its signed query, or credentials in a failure message.
                exc.download_address_issue = {
                    "value_type": "string" if isinstance(returned_url, str) else "missing" if returned_url is None else "invalid_type",
                    "scheme": scheme if scheme in {"http", "https"} else "other",
                    "has_whitespace": isinstance(returned_url, str) and any(char.isspace() for char in returned_url),
                    "has_non_ascii": isinstance(returned_url, str) and any(ord(char) > 126 for char in returned_url),
                    "has_fragment": isinstance(returned_url, str) and "#" in returned_url,
                }
                raise
            request_id = request_id or _safe_token(data.get("request_id"), api_key)
            receipt = {'url': url, 'requested_model': settings['model'], 'requested_size': selected_size,
                       'actual_model': _safe_token(data.get('model'), api_key), 'request_id': request_id,
                       'usage': _usage(data.get('usage') or {}),
                       'request_parameters': {'watermark_enabled': payload['watermark_enabled'],
                                              'body_sha256': hashlib.sha256(body).hexdigest()}}
            if response_observer is not None:
                response_observer(receipt)
            return zhipu_result_from_receipt(receipt, transport=send, timeout_seconds=timeout_seconds)
        except ImageProviderError as exc:
            exc.request_may_have_succeeded = not getattr(exc, 'request_not_sent', False)
            exc.request_id = exc.request_id or request_id
            raise

    invoke.supports_purpose = True
    invoke.resume = lambda receipt: zhipu_result_from_receipt(receipt, transport=send, timeout_seconds=timeout_seconds)
    return invoke


def zhipu_result_from_receipt(receipt: dict, *, transport: ImageHTTPTransport | None = None,
                            timeout_seconds: float = 180) -> ImageProviderResult:
    """Read the already generated image; this function can only issue a GET."""
    url = _zhipu_download_url(receipt.get('url'))
    if (receipt.get('requested_model') not in {s['model'] for s in ZHIPU_PURPOSES.values()}
            or not re.fullmatch(r'[1-9][0-9]{2,3}x[1-9][0-9]{2,3}', str(receipt.get('requested_size') or ''))):
        raise ImageProviderError('IMAGE_RESPONSE_INVALID', '已记录的图片生成回执无效')
    downloaded = _send(transport or _https_transport,
        Request(url, method='GET', headers={'Accept': 'image/png,image/jpeg'}),
        timeout_seconds=timeout_seconds, max_bytes=MAX_IMAGE_BYTES)
    _require_success(downloaded, receipt.get('request_id'))
    mime, width, height = _image_info(downloaded.body, reference=False)
    # Real Zhipu CDN responses can declare image/png while containing JPEG.
    # Decoded bytes own MIME; the actual format is persisted and served locally.
    if _media_type(downloaded) not in {'image/png', 'image/jpeg', 'application/octet-stream'}:
        raise ImageProviderError('IMAGE_RESPONSE_INVALID', '图片下载未返回支持的图片内容')
    return ImageProviderResult(downloaded.body, mime, width, height,
        receipt['requested_model'], receipt.get('actual_model'), receipt['requested_size'],
        receipt.get('request_id'), _usage(receipt.get('usage') or {}))


def _zhipu_download_url(value) -> str:
    try:
        host = urlsplit(value).hostname if isinstance(value, str) else None
        if not host or "." not in host:
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
    except ValueError:
        raise ImageProviderError("IMAGE_DOWNLOAD_DENIED", "图片下载必须使用有效的公共 HTTPS 地址") from None
    return _download_url(value, (host,))


def seedream_http_provider(*, model: str = DEFAULT_SEEDREAM_MODEL, api_key_env: str = "ARK_API_KEY",
                           timeout_seconds: float = 180, transport: ImageHTTPTransport | None = None,
                           allowed_download_hosts: tuple[str, ...] = ()) -> ImageProvider:
    """Configure the provider on the server; business calls cannot override its route.

    Generation defaults to 16:9. Editing defaults to the first reference's
    aspect ratio, scaled to the selected resolution with integer pixel rounding.
    Explicit WxH sizes must satisfy the documented Seedream 5.0 lite bounds.
    URL fallback is opt-in to exact Beijing TOS hosts; redirects are never used.
    Missing actual model, request ID or usage stays missing, not inferred.
    """
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", model):
        raise ImageProviderError("IMAGE_PROVIDER_CONFIG_INVALID", "图片模型配置无效")
    if not isinstance(api_key_env, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", api_key_env):
        raise ImageProviderError("IMAGE_PROVIDER_CONFIG_INVALID", "图片凭据配置无效")
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 600):
        raise ImageProviderError("IMAGE_PROVIDER_CONFIG_INVALID", "图片请求超时配置无效")
    if (not isinstance(allowed_download_hosts, tuple)
            or any(not isinstance(host, str) or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.tos-cn-beijing\.volces\.com", host)
                for host in allowed_download_hosts)
            or len(set(allowed_download_hosts)) != len(allowed_download_hosts)):
        raise ImageProviderError("IMAGE_PROVIDER_CONFIG_INVALID", "图片下载仅允许配置官方 TOS 精确域名")
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key or any(ord(character) < 33 or ord(character) > 126 for character in api_key):
        raise ImageProviderError("IMAGE_PROVIDER_CREDENTIALS_MISSING", "图片生成凭据尚未配置或无效")
    send = transport or _https_transport

    def invoke(prompt: str, reference_images: tuple[bytes, ...] = (), *,
               aspect_ratio: str | None = None, size: str = "2K") -> ImageProviderResult:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000 or "\x00" in prompt:
            raise ImageProviderError("IMAGE_INPUT_INVALID", "请提供有效的图片创作要求")
        if not isinstance(reference_images, tuple) or len(reference_images) > 14:
            raise ImageProviderError("IMAGE_INPUT_INVALID", "参考图片必须为最多 14 张图片的集合")
        if any(not isinstance(content, bytes) or not content or len(content) > MAX_REFERENCE_BYTES for content in reference_images):
            raise ImageProviderError("IMAGE_INPUT_INVALID", "参考图片为空、格式无效或超出单图大小限制")
        if sum(map(len, reference_images)) > MAX_TOTAL_REFERENCE_BYTES:
            raise ImageProviderError("IMAGE_INPUT_INVALID", "参考图片总大小超出本地请求限制")
        references = [_image_info(content, reference=True) for content in reference_images]
        requested_size = _requested_size(size, aspect_ratio, references[0][1:] if references else None)
        payload = {"model": model, "prompt": prompt.strip(), "size": requested_size,
            "sequential_image_generation": "disabled", "stream": False,
            "output_format": "png", "response_format": "b64_json", "watermark": False}
        if references:
            encoded = ["data:" + info[0] + ";base64," + base64.b64encode(content).decode("ascii")
                for info, content in zip(references, reference_images)]
            payload["image"] = encoded[0] if len(encoded) == 1 else encoded
        try:
            request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except UnicodeError:
            raise ImageProviderError("IMAGE_INPUT_INVALID", "图片创作要求包含无效文本编码") from None
        if len(request_body) > MAX_REQUEST_BYTES:
            raise ImageProviderError("IMAGE_INPUT_INVALID", "图片请求超出本地接收范围")
        request = Request(SEEDREAM_ENDPOINT, data=request_body, method="POST", headers={
            "Authorization": "Bearer " + api_key, "Content-Type": "application/json", "Accept": "application/json"})
        request_id = None
        try:
            response = _send(send, request, timeout_seconds=timeout_seconds, max_bytes=MAX_JSON_BYTES)
            request_id = _safe_token(_header(response, "x-request-id") or _header(response, "x-tt-logid"), api_key)
            _require_success(response, request_id)
            if _media_type(response) != "application/json":
                raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片服务未返回有效 JSON")
            try:
                data = json.loads(response.body)
            except (ValueError, UnicodeError):
                raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片服务返回内容无法解析") from None
            if not isinstance(data, dict) or data.get("error"):
                raise ImageProviderError("IMAGE_PROVIDER_REJECTED", "图片服务未完成本次生成")
            request_id = request_id or _safe_token(data.get("request_id"), api_key)
            items = data.get("data")
            if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict) or items[0].get("error"):
                raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片服务未返回唯一完整图片")
            item = items[0]
            encoded = item.get("b64_json")
            if encoded:
                if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
                    raise ImageProviderError("IMAGE_RESPONSE_TOO_LARGE", "图片输出超出本地接收范围")
                try:
                    content = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片数据编码无效") from None
            else:
                url = _download_url(item.get("url"), allowed_download_hosts)
                image_response = _send(send, Request(url, method="GET", headers={"Accept": "image/png,image/jpeg"}),
                    timeout_seconds=timeout_seconds, max_bytes=MAX_IMAGE_BYTES)
                _require_success(image_response, request_id)
                if _media_type(image_response) not in {"image/png", "image/jpeg", "application/octet-stream"}:
                    raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片下载未返回支持的图片格式")
                content = image_response.body
            if not content or len(content) > MAX_IMAGE_BYTES:
                raise ImageProviderError("IMAGE_RESPONSE_TOO_LARGE", "图片输出为空或超出本地接收范围")
            mime, width, height = _image_info(content, reference=False)
            if not encoded and _media_type(image_response) not in {mime, "application/octet-stream"}:
                raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片下载类型与实际内容不一致")
            if item.get("size") is not None and item["size"] != f"{width}x{height}":
                raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片尺寸回执与实际内容不一致")
            if item.get("output_format") is not None and "image/" + str(item["output_format"]).lower() != mime:
                raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片格式回执与实际内容不一致")
            return ImageProviderResult(content=content, mime_type=mime, width=width, height=height,
                requested_model=model, actual_model=_safe_token(data.get("model"), api_key),
                requested_size=requested_size, request_id=request_id, usage=_usage(data.get("usage", {})))
        except ImageProviderError as exc:
            exc.request_may_have_succeeded = not getattr(exc, 'request_not_sent', False)
            exc.request_id = exc.request_id or request_id
            raise

    return invoke


def _requested_size(size: str, aspect_ratio: str | None, reference_size: tuple[int, int] | None) -> str:
    if aspect_ratio is not None and (not isinstance(aspect_ratio, str) or aspect_ratio not in {*_ASPECT_SIZES, "reference"}):
        raise ImageProviderError("IMAGE_INPUT_INVALID", "图片比例不在支持范围内")
    ratio_key = aspect_ratio or ("reference" if reference_size else "16:9")
    if ratio_key == "reference" and reference_size is None:
        raise ImageProviderError("IMAGE_INPUT_INVALID", "保留原比例需要参考图片")
    if not isinstance(size, str):
        raise ImageProviderError("IMAGE_INPUT_INVALID", "图片尺寸无效")
    if size in {"2K", "3K", "4K"}:
        scale = {"2K": 1, "3K": 1.5, "4K": 2}[size]
        if ratio_key == "reference":
            ratio = reference_size[0] / reference_size[1]
            pixels = int((2048 * scale) ** 2)
            width = max(1, int(math.sqrt(pixels * ratio)))
            height = max(1, int(pixels / width))
        else:
            width, height = (int(value * scale) for value in _ASPECT_SIZES[ratio_key])
            if width * height > MAX_OUTPUT_PIXELS:
                ratio = width / height
                width = int(math.sqrt(MAX_OUTPUT_PIXELS * ratio))
                height = int(MAX_OUTPUT_PIXELS / width)
    elif re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", size):
        width, height = map(int, size.split("x"))
        if aspect_ratio is not None:
            pair = reference_size if ratio_key == "reference" else _ASPECT_SIZES[ratio_key]
            if abs(width / height - pair[0] / pair[1]) > .002:
                raise ImageProviderError("IMAGE_INPUT_INVALID", "指定尺寸与图片比例不一致")
    else:
        raise ImageProviderError("IMAGE_INPUT_INVALID", "图片尺寸只支持 2K、3K、4K 或合法宽高像素")
    if not MIN_OUTPUT_PIXELS <= width * height <= MAX_OUTPUT_PIXELS or not 1 / 16 <= width / height <= 16:
        raise ImageProviderError("IMAGE_INPUT_INVALID", "图片尺寸超出模型支持的像素或比例范围")
    return f"{width}x{height}"


def _image_info(content: bytes, *, reference: bool) -> tuple[str, int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                width, height = image.size
                allowed = _MIME if reference else {"PNG", "JPEG"}
                if image.format not in allowed or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("unsupported image")
                if width * height > (MAX_REFERENCE_PIXELS if reference else MAX_OUTPUT_PIXELS):
                    raise ValueError("too many pixels")
                if reference and (min(width, height) <= 14 or not 1 / 16 <= width / height <= 16):
                    raise ValueError("unsupported reference dimensions")
                mime = _MIME[image.format]
                image.verify()
            with Image.open(BytesIO(content)) as image:
                image.load()
        return mime, width, height
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageProviderError("IMAGE_INPUT_INVALID" if reference else "IMAGE_RESPONSE_INVALID",
            "参考图片必须是有效的静态 PNG、JPEG 或 WebP，并符合尺寸限制" if reference else "生成图片无法解码或不符合接收限制") from None


def _safe_token(value, secret: str) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", value) or secret in value:
        return None
    return value


def _usage(value, depth=0) -> dict:
    if not isinstance(value, dict) or len(value) > 32 or depth > 2:
        raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片服务用量回执无效")
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z_]{1,64}", key):
            raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片服务用量回执无效")
        if isinstance(item, dict):
            result[key] = _usage(item, depth + 1)
        elif type(item) is int and 0 <= item <= 2**63 - 1:
            result[key] = item
        else:
            raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片服务用量回执无效")
    return result


def _header(response: ImageHTTPResponse, key: str) -> str:
    return next((str(value) for name, value in response.headers.items() if name.lower() == key), "")


def _media_type(response: ImageHTTPResponse) -> str:
    return _header(response, "content-type").split(";", 1)[0].strip().lower()


def _require_success(response: ImageHTTPResponse, request_id: str | None) -> None:
    if response.status != 200:
        raise ImageProviderError("IMAGE_HTTP_FAILED", "图片请求未完成，请核对本次任务后再决定是否重试",
            request_id=request_id, http_status=response.status, request_may_have_succeeded=True)


def _download_url(value, allowed_hosts: tuple[str, ...]) -> str:
    if not isinstance(value, str) or len(value) > 8192 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ImageProviderError("IMAGE_DOWNLOAD_DENIED", "图片下载地址无效或不在允许范围内")
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == "https" and parts.hostname in allowed_hosts and parts.port in (None, 443)
            and parts.username is None and parts.password is None and not parts.fragment and "\\" not in value)
    except ValueError:
        valid = False
    if not valid:
        raise ImageProviderError("IMAGE_DOWNLOAD_DENIED", "图片下载地址未匹配配置的官方 HTTPS 域名")
    return value


def _send(transport: ImageHTTPTransport, request: Request, *, timeout_seconds: float, max_bytes: int) -> ImageHTTPResponse:
    try:
        response = transport(request, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
    except (OSError, http.client.HTTPException):
        raise ImageProviderError("IMAGE_TRANSPORT_FAILED", "图片连接中断，本次请求可能已执行，请先核对任务结果",
            request_may_have_succeeded=True) from None
    if (not isinstance(response, ImageHTTPResponse) or type(response.status) is not int
            or not isinstance(response.headers, Mapping) or not isinstance(response.body, bytes)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in response.headers.items())):
        raise ImageProviderError("IMAGE_RESPONSE_INVALID", "图片服务响应结构无效")
    if len(response.body) > max_bytes:
        raise ImageProviderError("IMAGE_RESPONSE_TOO_LARGE", "图片响应超出本地接收范围")
    return response


def _public_address(host: str) -> str:
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = list(dict.fromkeys(item[4][0] for item in addresses))
        # Fake-IP DNS used by local network software cannot be connected to
        # under the public-address rule. Resolve the same hostname over a
        # verified HTTPS connection instead; the image connection still pins
        # a public address and verifies the original hostname.
        benchmark = ipaddress.ip_network('198.18.0.0/15')
        if (ips and all(ipaddress.ip_address(ip) in benchmark for ip in ips)
                and re.fullmatch(r'(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}', host)):
            ips = _public_address_from_https_dns(host)
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise ValueError("nonpublic address")
        return ips[0]
    except (OSError, ValueError):
        raise ImageProviderError("IMAGE_DOWNLOAD_DENIED", "无法确认图片服务为允许的公共地址") from None


def _public_address_from_https_dns(host: str) -> list[str]:
    connection = _PinnedHTTPSConnection('cloudflare-dns.com', '1.1.1.1', timeout=10)
    try:
        connection.request('GET', '/dns-query?' + urlencode({'name': host, 'type': 'A'}),
                           headers={'Accept': 'application/dns-json'})
        response = connection.getresponse()
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise ValueError('dns response unavailable')
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get('Status')) is not int or value['Status'] != 0
                or value.get('TC') is True
                or value.get('Question') not in ([{'name': host, 'type': 1}], [{'name': host + '.', 'type': 1}])):
            raise ValueError('dns response invalid')
        answers = value.get('Answer')
        if not isinstance(answers, list) or len(answers) > 64:
            raise ValueError('dns answers invalid')
        ips = [str(item['data']) for item in answers if isinstance(item, dict) and item.get('type') == 1]
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise ValueError('nonpublic dns answer')
        return list(dict.fromkeys(ips))
    except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError):
        raise ImageProviderError('IMAGE_DOWNLOAD_DENIED', '公共 DNS 核对未完成，图片请求未继续') from None
    finally:
        connection.close()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, *, timeout: float):
        super().__init__(hostname, 443, timeout=timeout, context=ssl.create_default_context())
        self._verified_address = address

    def connect(self):
        connection = socket.create_connection((self._verified_address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
        except Exception:
            connection.close()
            raise


def _https_transport(request: Request, *, timeout_seconds: float, max_bytes: int) -> ImageHTTPResponse:
    """One direct TLS connection to a checked public IP; no proxies or redirects."""
    parts = urlsplit(request.full_url)
    try:
        address = _public_address(parts.hostname)
    except ImageProviderError as exc:
        if request.get_method() == 'POST':
            exc.request_not_sent = True
        raise
    connection = _PinnedHTTPSConnection(parts.hostname, address, timeout=timeout_seconds)
    try:
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        connection.request(request.get_method(), target, body=request.data, headers=dict(request.header_items()))
        response = connection.getresponse()
        headers = dict(response.getheaders())
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > max_bytes):
            raise ImageProviderError("IMAGE_RESPONSE_TOO_LARGE", "图片响应超出本地接收范围")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ImageProviderError("IMAGE_RESPONSE_TOO_LARGE", "图片响应超出本地接收范围")
        return ImageHTTPResponse(response.status, headers, body)
    finally:
        connection.close()
