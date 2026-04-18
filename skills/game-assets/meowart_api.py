#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from datetime import datetime
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

DEFAULT_API_BASE = "https://api.meowart.ai"
DEFAULT_API_KEY_ENV = "MEOWART_API_KEY"
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-image-preview"
DEFAULT_WORK_DIR = str(Path(__file__).resolve().parent / ".meow_art")
DEFAULT_TIMEOUT = 240
DEFAULT_MAX_WAIT = 900
DEFAULT_POLL_INTERVAL = 3.0
ACTIVE_JOB_STATUSES = {"queued", "pending", "running"}
TERMINAL_JOB_STATUSES = {"success", "failure", "cancelled"}
TERMINAL_ANIMATE_STATUSES = {"success", "completed", "failure", "failed", "cancelled", "canceled"}
SUCCESS_ANIMATE_STATUSES = {"success", "completed"}


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(line_buffering=True)


def _mime_for_path(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _parse_json_response(response: requests.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type.lower():
        body = response.text[:500].strip()
        raise ValueError(f"expected JSON response, got {content_type or 'unknown'}: {body}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object, got {type(payload).__name__}")
    return payload


def _request_json(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    timeout: int,
    verify: bool,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[requests.Response, dict[str, Any]]:
    response = requests.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        data=data,
        files=files,
        json=json_body,
        timeout=timeout,
        verify=verify,
    )
    return response, _parse_json_response(response)


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _mask_secret(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return raw
    if len(raw) <= 8:
        return "*" * len(raw)
    return f"{raw[:4]}...{raw[-4:]}"


def _sanitize_for_meta(value: Any, *, key: str = "") -> Any:
    lowered_key = key.lower()
    if isinstance(value, dict):
        return {inner_key: _sanitize_for_meta(inner_value, key=str(inner_key)) for inner_key, inner_value in value.items()}
    if isinstance(value, list):
        return [_sanitize_for_meta(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_for_meta(item, key=key) for item in value]
    if isinstance(value, Path):
        return str(value)
    if any(token in lowered_key for token in {"api_key", "token", "authorization", "secret"}):
        return _mask_secret(str(value))
    return value


def _create_run_dir(work_dir: str, command: str) -> Path:
    root = Path(work_dir).expanduser()
    run_dir = root / f"{_timestamp_slug()}_{_safe_slug(command)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _resolve_output_dir(raw_path: str, run_dir: Path) -> Path:
    if str(raw_path or "").strip():
        return Path(raw_path).expanduser()
    return run_dir


def _predict_saved_dir(output_root: str | Path, slug_seed: str) -> Path:
    return Path(output_root).expanduser() / _safe_slug(slug_seed)


def _write_meta(
    *,
    run_dir: Path,
    started_at: str,
    finished_at: str,
    args: argparse.Namespace,
    request_payload: Any | None,
    response_payload: Any | None,
    downloads: list[dict[str, Any]] | None,
    effective_output_dir: str,
    error: str = "",
) -> None:
    meta = {
        "command": args.command,
        "started_at": started_at,
        "finished_at": finished_at,
        "run_dir": str(run_dir),
        "work_dir": str(Path(args.work_dir).expanduser()),
        "effective_output_dir": effective_output_dir,
        "request": {
            "cli_args": _sanitize_for_meta(vars(args)),
            "payload": _sanitize_for_meta(request_payload),
        },
        "response": _sanitize_for_meta(response_payload),
        "downloads": _sanitize_for_meta(downloads or []),
        "error": error,
    }
    _save_json(run_dir / "meta.json", meta)


def _suffix_from_mime(mime_type: str) -> str:
    normalized = str(mime_type or "").split(";", 1)[0].strip().lower()
    if not normalized:
        return ".bin"
    guessed = mimetypes.guess_extension(normalized)
    if guessed == ".jpe":
        return ".jpg"
    return guessed or ".bin"


def _download_file(
    url: str,
    target_path: Path,
    *,
    timeout: int,
    verify: bool,
    headers: dict[str, str] | None = None,
) -> str:
    response = requests.get(url, timeout=timeout, verify=verify, headers=headers or None)
    response.raise_for_status()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(response.content)
    return str(response.headers.get("content-type") or "").strip()


def _safe_slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "output"


def _base_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _should_send_auth_headers(url: str) -> bool:
    host = (urlparse(url).netloc or "").strip().lower()
    if not host:
        return False
    if host.endswith("storage.googleapis.com"):
        return False
    return host.endswith("meowart.ai") or host.endswith("generativelanguage.googleapis.com")


def _normalize_base_url(api_base: str, endpoint: str) -> str:
    return f"{api_base.rstrip('/')}/{endpoint.lstrip('/')}"


def _print_status(prefix: str, payload: dict[str, Any]) -> None:
    status = str(payload.get("status") or "").strip()
    stage = str(payload.get("stage") or "").strip()
    error = str(payload.get("error") or "").strip()
    progress = payload.get("progress")
    progress_label = ""
    progress_percent = ""
    if isinstance(progress, dict):
        progress_label = str(progress.get("label") or "").strip()
        progress_percent = str(progress.get("percent") or "").strip()
    line = f"{prefix} status={status or '?'}"
    if stage:
        line += f" stage={stage}"
    if progress_label or progress_percent:
        line += f" progress={progress_label or '?'}:{progress_percent or '?'}%"
    if error:
        line += f" error={error}"
    print(line)


def _collect_http_urls(value: Any, *, prefix: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            found.extend(_collect_http_urls(inner, prefix=child_prefix))
        return found
    if isinstance(value, list):
        for index, inner in enumerate(value):
            child_prefix = f"{prefix}[{index}]"
            found.extend(_collect_http_urls(inner, prefix=child_prefix))
        return found
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            found.append((prefix or "url", raw))
    return found


def _suffix_from_url(url: str) -> str:
    path = Path(url.split("?", 1)[0])
    suffix = path.suffix.lower()
    return suffix if suffix else ".bin"


def _filename_from_url_or_key(url: str, key: str) -> str:
    parsed = urlparse(url)
    raw_name = Path(parsed.path).name.strip()
    if raw_name and "." in raw_name and raw_name not in {".", ".."}:
        return raw_name
    fallback = _safe_slug(key.replace(".", "_").replace("[", "_").replace("]", ""))
    return f"{fallback}{_suffix_from_url(url)}"


def _unique_target_path(output_dir: Path, filename: str) -> Path:
    candidate = output_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        alternative = output_dir / f"{stem}_{counter}{suffix}"
        if not alternative.exists():
            return alternative
        counter += 1


def _collect_gemini_inline_images(value: Any, *, prefix: str = "") -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    if isinstance(value, dict):
        mime_type = str(value.get("mimeType") or value.get("mime_type") or "").strip()
        data = str(value.get("data") or "").strip()
        if mime_type.startswith("image/") and data:
            found.append((prefix or "image", mime_type, data))
        for key, inner in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            found.extend(_collect_gemini_inline_images(inner, prefix=child_prefix))
        return found
    if isinstance(value, list):
        for index, inner in enumerate(value):
            child_prefix = f"{prefix}[{index}]"
            found.extend(_collect_gemini_inline_images(inner, prefix=child_prefix))
    return found


def _save_gemini_response_assets(
    *,
    payload: dict[str, Any],
    output_dir: str,
    timeout: int,
    verify: bool,
    api_key: str,
    save_json: bool = True,
) -> tuple[Path | None, list[dict[str, Any]]]:
    target_dir = Path(output_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    wrote_any = False
    downloads: list[dict[str, Any]] = []
    if save_json:
        _save_json(target_dir / "response.json", payload)
        wrote_any = True
        downloads.append({"type": "json", "path": str(target_dir / "response.json")})

    inline_images = _collect_gemini_inline_images(payload)
    for index, (key, mime_type, data) in enumerate(inline_images, start=1):
        filename = _safe_slug(key.replace(".", "_").replace("[", "_").replace("]", "")) or f"image_{index}"
        target_path = target_dir / f"{filename}_{index:02d}{_suffix_from_mime(mime_type)}"
        try:
            target_path.write_bytes(base64.b64decode(data, validate=True))
            wrote_any = True
            downloads.append({"type": "inline_image", "key": key, "mime_type": mime_type, "path": str(target_path)})
            print(f"[INFO] downloaded={target_path}")
        except ValueError as exc:
            print(f"[WARN] failed to decode inline image {key}: {exc}", file=sys.stderr)

    http_urls = [(key, url) for key, url in _collect_http_urls(payload) if any(token in key.lower() for token in {"image", "inline", "file", "uri", "url"})]
    if http_urls:
        downloaded_urls = _download_named_urls(
            urls=http_urls,
            output_dir=target_dir,
            timeout=timeout,
            verify=verify,
            headers=_base_headers(api_key),
        )
        downloads.extend(downloaded_urls)
        if downloaded_urls:
            wrote_any = True

    return (target_dir if wrote_any else None, downloads)


def _download_named_urls(
    *,
    urls: list[tuple[str, str]],
    output_dir: Path,
    timeout: int,
    verify: bool,
    headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    downloads: list[dict[str, Any]] = []
    for key, url in urls:
        if url in seen:
            continue
        seen.add(url)
        target = _unique_target_path(output_dir, _filename_from_url_or_key(url, key))
        try:
            request_headers = headers if headers and _should_send_auth_headers(url) else None
            mime_type = _download_file(url, target, timeout=timeout, verify=verify, headers=request_headers)
            if target.suffix == ".bin":
                resolved_suffix = _suffix_from_mime(mime_type)
                if resolved_suffix != ".bin":
                    renamed_target = _unique_target_path(output_dir, f"{target.stem}{resolved_suffix}")
                    target.rename(renamed_target)
                    target = renamed_target
            downloads.append({"type": "url_download", "key": key, "url": url, "path": str(target)})
            print(f"[INFO] downloaded={target}")
        except requests.RequestException as exc:
            print(f"[WARN] download failed for {url}: {exc}", file=sys.stderr)
    return downloads


def image_file_to_data_url(image_path: str) -> str:
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"image not found: {path}")
    mime = _mime_for_path(path)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def gemini_proxy_request(
    *,
    api_base: str,
    api_key: str,
    path: str,
    method: str = "POST",
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> dict[str, Any]:
    url = _normalize_base_url(api_base, f"/api/gemini/{path.lstrip('/')}")
    response, payload = _request_json(
        method=method.upper(),
        url=url,
        headers=_base_headers(api_key),
        json_body=json_body,
        params=params,
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 400:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def gemini_generate_content(
    *,
    api_base: str,
    api_key: str,
    model: str,
    contents: list[dict[str, Any]],
    generation_config: dict[str, Any] | None = None,
    safety_settings: list[dict[str, Any]] | None = None,
    system_instruction: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> dict[str, Any]:
    body: dict[str, Any] = {"contents": contents}
    if generation_config:
        body["generationConfig"] = generation_config
    if safety_settings:
        body["safetySettings"] = safety_settings
    if system_instruction:
        body["systemInstruction"] = system_instruction
    return gemini_proxy_request(
        api_base=api_base,
        api_key=api_key,
        path=f"v1beta/models/{model}:generateContent",
        method="POST",
        json_body=body,
        timeout=timeout,
        verify=verify,
    )


def get_credits_balance(
    *,
    api_base: str,
    api_key: str,
    timeout: int = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> dict[str, Any]:
    url = _normalize_base_url(api_base, "/api/credits/balance")
    response, payload = _request_json(
        method="GET",
        url=url,
        headers=_base_headers(api_key),
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 400:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def poll_job_until_done(
    *,
    jobs_url: str,
    api_key: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_wait: int = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    verify: bool = True,
) -> dict[str, Any]:
    deadline = time.time() + max(max_wait, 1)
    headers = _base_headers(api_key)
    final_payload: dict[str, Any] | None = None
    while time.time() <= deadline:
        try:
            _, payload = _request_json(
                method="GET",
                url=jobs_url,
                headers=headers,
                timeout=timeout,
                verify=verify,
            )
        except (requests.RequestException, ValueError) as exc:
            print(f"[WARN] poll request failed: {exc}", file=sys.stderr)
            time.sleep(max(poll_interval, 0.1))
            continue

        _print_status("[INFO]", payload)
        status = str(payload.get("status") or "").strip().lower()
        if status in TERMINAL_JOB_STATUSES:
            final_payload = payload
            break
        if status not in ACTIVE_JOB_STATUSES:
            print(f"[WARN] unexpected intermediate status: {status}", file=sys.stderr)
        time.sleep(max(poll_interval, 0.1))

    if final_payload is None:
        raise TimeoutError(f"polling timed out after {max_wait}s")
    return final_payload


def pixel_gen_template_info(
    *,
    api_base: str,
    api_key: str,
    timeout: int = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> dict[str, Any]:
    url = _normalize_base_url(api_base, "/api/pixel-gen/template-info")
    response, payload = _request_json(
        method="GET",
        url=url,
        headers=_base_headers(api_key),
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 400:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def submit_pixel_gen(
    *,
    api_base: str,
    api_key: str,
    template_name: str,
    requirement: str,
    template_config: dict[str, Any] | None = None,
    job_name: str = "",
    model_name: str = "",
    resolution: str = "1K",
    aspect_ratio: str = "1:1",
    temperature: float = 0.0,
    include_base64: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> dict[str, Any]:
    submit_url = _normalize_base_url(api_base, "/api/pixel-gen")
    data: dict[str, str] = {
        "template_name": template_name,
        "template_config": json.dumps(template_config or {}, ensure_ascii=False),
        "requirement": requirement,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "temperature": str(temperature),
        "include_base64": "true" if include_base64 else "false",
    }
    if job_name:
        data["job_name"] = job_name
    if model_name:
        data["model_name"] = model_name

    response, payload = _request_json(
        method="POST",
        url=submit_url,
        headers=_base_headers(api_key),
        data=data,
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 400:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def poll_pixel_gen_job(
    *,
    api_base: str,
    api_key: str,
    api_job_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> dict[str, Any]:
    url = _normalize_base_url(api_base, "/api/pixel-gen/jobs")
    response, payload = _request_json(
        method="GET",
        url=url,
        headers=_base_headers(api_key),
        params={"id": api_job_id},
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 400:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def wait_pixel_gen_job(
    *,
    api_base: str,
    api_key: str,
    api_job_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_wait: int = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    verify: bool = True,
) -> dict[str, Any]:
    deadline = time.time() + max(max_wait, 1)
    final_payload: dict[str, Any] | None = None
    while time.time() <= deadline:
        payload = poll_pixel_gen_job(
            api_base=api_base,
            api_key=api_key,
            api_job_id=api_job_id,
            timeout=timeout,
            verify=verify,
        )
        _print_status("[INFO]", payload)
        status = str(payload.get("status") or "").strip().lower()
        if status in TERMINAL_JOB_STATUSES:
            final_payload = payload
            break
        time.sleep(max(poll_interval, 0.1))
    if final_payload is None:
        raise TimeoutError(f"pixel-gen polling timed out after {max_wait}s")
    return final_payload


def run_pixel_gen(
    *,
    api_base: str,
    api_key: str,
    template_name: str,
    requirement: str,
    template_config: dict[str, Any] | None = None,
    job_name: str = "",
    model_name: str = "",
    resolution: str = "1K",
    aspect_ratio: str = "1:1",
    temperature: float = 0.0,
    include_base64: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    max_wait: int = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    verify: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    submit_payload = submit_pixel_gen(
        api_base=api_base,
        api_key=api_key,
        template_name=template_name,
        requirement=requirement,
        template_config=template_config,
        job_name=job_name,
        model_name=model_name,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        temperature=temperature,
        include_base64=include_base64,
        timeout=timeout,
        verify=verify,
    )
    api_job_id = str(submit_payload.get("api_job_id") or "").strip()
    if not api_job_id:
        raise RuntimeError("pixel-gen submit response missing api_job_id")
    print(f"[INFO] submitted api_job_id={api_job_id}")
    final_payload = wait_pixel_gen_job(
        api_base=api_base,
        api_key=api_key,
        api_job_id=api_job_id,
        timeout=timeout,
        max_wait=max_wait,
        poll_interval=poll_interval,
        verify=verify,
    )
    return submit_payload, final_payload


def pixel_gen_history(
    *,
    api_base: str,
    api_key: str,
    limit: int = 20,
    offset: int = 0,
    status: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> dict[str, Any]:
    url = _normalize_base_url(api_base, "/api/pixel-gen/history")
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    response, payload = _request_json(
        method="GET",
        url=url,
        headers=_base_headers(api_key),
        params=params,
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 400:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def pixel_gen_cancel(
    *,
    api_base: str,
    api_key: str,
    api_job_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> dict[str, Any]:
    url = _normalize_base_url(api_base, f"/api/pixel-gen/jobs/{api_job_id}/cancel")
    response, payload = _request_json(
        method="POST",
        url=url,
        headers=_base_headers(api_key),
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 400:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def pixel_gen_download(
    *,
    api_base: str,
    api_key: str,
    api_job_id: str,
    output_dir: str,
    output_index: int | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> Path:
    if output_index is None:
        url = _normalize_base_url(api_base, f"/api/pixel-gen/jobs/{api_job_id}/download")
    else:
        url = _normalize_base_url(api_base, f"/api/pixel-gen/jobs/{api_job_id}/outputs/{output_index}/download")
    target_dir = Path(output_dir).expanduser()
    suffix = ".png"
    if output_index is None:
        filename = f"{api_job_id}{suffix}"
    else:
        filename = f"{api_job_id}_output_{output_index}{suffix}"
    path = target_dir / filename
    _download_file(url, path, timeout=timeout, verify=verify, headers=_base_headers(api_key))
    return path


def submit_animate(
    *,
    api_base: str,
    api_key: str,
    image_data_url: str,
    prompt: str = "",
    is_pixel: bool = False,
    optimize_prompt: bool = True,
    model: str = "",
    negative_prompt: str = "",
    pixel_config: dict[str, Any] | None = None,
    output_frames: int = 8,
    seed: int | None = None,
    output_format: str = "webp",
    matte_color: str = "#808080",
    project_id: str | None = None,
    thread_id: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> dict[str, Any]:
    url = _normalize_base_url(api_base, "/api/animate")
    payload: dict[str, Any] = {
        "image": image_data_url,
        "prompt": prompt,
        "is_pixel": is_pixel,
        "optimize_prompt": optimize_prompt,
        "output_frames": output_frames,
        "output_format": output_format,
        "matte_color": matte_color,
    }
    if model:
        payload["model"] = model
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt
    if pixel_config:
        payload["pixel_config"] = pixel_config
    if seed is not None:
        payload["seed"] = seed
    if project_id is not None:
        payload["project_id"] = project_id
    if thread_id is not None:
        payload["thread_id"] = thread_id

    response, body = _request_json(
        method="POST",
        url=url,
        headers=_base_headers(api_key),
        json_body=payload,
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 400:
        raise RuntimeError(json.dumps(body, ensure_ascii=False, indent=2))
    return body


def submit_remove_background(
    *,
    api_base: str,
    api_key: str,
    image_file: str,
    method: str = "hd",
    enable_perfect_pixel: bool = True,
    is_white_bg: bool = True,
    prompt: str = "",
    ai_api_key: str = "",
    ai_model_name: str = "gemini-3.1-flash-image-preview",
    ai_resolution: str = "1K",
    ai_aspect_ratio: str = "1:1",
    ai_temperature: float = 0.0,
    ai_background_diff_threshold: int = 120,
    photoroom_api_key: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> dict[str, Any]:
    path = Path(image_file).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"image file not found: {path}")
    data = {
        "method": method,
        "enable_perfect_pixel": "true" if enable_perfect_pixel else "false",
        "is_white_bg": "true" if is_white_bg else "false",
        "prompt": prompt,
        "ai_api_key": ai_api_key,
        "ai_model_name": ai_model_name,
        "ai_resolution": ai_resolution,
        "ai_aspect_ratio": ai_aspect_ratio,
        "ai_temperature": str(ai_temperature),
        "ai_background_diff_threshold": str(ai_background_diff_threshold),
        "photoroom_api_key": photoroom_api_key,
    }
    files = {"file": (path.name, path.read_bytes(), _mime_for_path(path))}
    url = _normalize_base_url(api_base, "/api/image/remove-background")
    response, payload = _request_json(
        method="POST",
        url=url,
        headers=_base_headers(api_key),
        data=data,
        files=files,
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 400:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def run_remove_background(
    *,
    api_base: str,
    api_key: str,
    image_file: str,
    method: str = "hd",
    enable_perfect_pixel: bool = True,
    is_white_bg: bool = True,
    prompt: str = "",
    ai_api_key: str = "",
    ai_model_name: str = "gemini-3.1-flash-image-preview",
    ai_resolution: str = "1K",
    ai_aspect_ratio: str = "1:1",
    ai_temperature: float = 0.0,
    ai_background_diff_threshold: int = 120,
    photoroom_api_key: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    max_wait: int = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    verify: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    submit_payload = submit_remove_background(
        api_base=api_base,
        api_key=api_key,
        image_file=image_file,
        method=method,
        enable_perfect_pixel=enable_perfect_pixel,
        is_white_bg=is_white_bg,
        prompt=prompt,
        ai_api_key=ai_api_key,
        ai_model_name=ai_model_name,
        ai_resolution=ai_resolution,
        ai_aspect_ratio=ai_aspect_ratio,
        ai_temperature=ai_temperature,
        ai_background_diff_threshold=ai_background_diff_threshold,
        photoroom_api_key=photoroom_api_key,
        timeout=timeout,
        verify=verify,
    )
    jobs_url = str(submit_payload.get("jobs_url") or "").strip()
    if not jobs_url:
        raise RuntimeError("remove-background submit response missing jobs_url")
    final_payload = poll_job_until_done(
        jobs_url=jobs_url,
        api_key=api_key,
        timeout=timeout,
        max_wait=max_wait,
        poll_interval=poll_interval,
        verify=verify,
    )
    return submit_payload, final_payload


def submit_pixelate(
    *,
    api_base: str,
    api_key: str,
    image_file: str,
    pixel_size: str = "",
    alpha_threshold: int = 128,
    sample_method: str = "majority",
    min_size: float = 2.0,
    peak_width: int = 6,
    refine_intensity: float = 0.25,
    fix_square: bool = True,
    pad_pow2_square: bool = True,
    crop_border: bool = False,
    crop_color_thr: int = 20,
    crop_bg_ratio: float = 0.995,
    crop_edge_width: int = 10,
    timeout: int = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> dict[str, Any]:
    path = Path(image_file).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"image file not found: {path}")
    data = {
        "pixel_size": pixel_size,
        "alpha_threshold": str(alpha_threshold),
        "sample_method": sample_method,
        "min_size": str(min_size),
        "peak_width": str(peak_width),
        "refine_intensity": str(refine_intensity),
        "fix_square": "true" if fix_square else "false",
        "pad_pow2_square": "true" if pad_pow2_square else "false",
        "crop_border": "true" if crop_border else "false",
        "crop_color_thr": str(crop_color_thr),
        "crop_bg_ratio": str(crop_bg_ratio),
        "crop_edge_width": str(crop_edge_width),
    }
    files = {"file": (path.name, path.read_bytes(), _mime_for_path(path))}
    url = _normalize_base_url(api_base, "/api/image/pixelate")
    response, payload = _request_json(
        method="POST",
        url=url,
        headers=_base_headers(api_key),
        data=data,
        files=files,
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 400:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def run_pixelate(
    *,
    api_base: str,
    api_key: str,
    image_file: str,
    pixel_size: str = "",
    alpha_threshold: int = 128,
    sample_method: str = "majority",
    min_size: float = 2.0,
    peak_width: int = 6,
    refine_intensity: float = 0.25,
    fix_square: bool = True,
    pad_pow2_square: bool = True,
    crop_border: bool = False,
    crop_color_thr: int = 20,
    crop_bg_ratio: float = 0.995,
    crop_edge_width: int = 10,
    timeout: int = DEFAULT_TIMEOUT,
    max_wait: int = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    verify: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    submit_payload = submit_pixelate(
        api_base=api_base,
        api_key=api_key,
        image_file=image_file,
        pixel_size=pixel_size,
        alpha_threshold=alpha_threshold,
        sample_method=sample_method,
        min_size=min_size,
        peak_width=peak_width,
        refine_intensity=refine_intensity,
        fix_square=fix_square,
        pad_pow2_square=pad_pow2_square,
        crop_border=crop_border,
        crop_color_thr=crop_color_thr,
        crop_bg_ratio=crop_bg_ratio,
        crop_edge_width=crop_edge_width,
        timeout=timeout,
        verify=verify,
    )
    jobs_url = str(submit_payload.get("jobs_url") or "").strip()
    if not jobs_url:
        raise RuntimeError("pixelate submit response missing jobs_url")
    final_payload = poll_job_until_done(
        jobs_url=jobs_url,
        api_key=api_key,
        timeout=timeout,
        max_wait=max_wait,
        poll_interval=poll_interval,
        verify=verify,
    )
    return submit_payload, final_payload


def submit_pixel_gen_self_loop(
    *,
    api_base: str,
    api_key: str,
    image_file: str,
    requirement: str = "",
    job_name: str = "",
    model_name: str = "gemini-3.1-flash-image-preview",
    resolution: str = "1K",
    temperature: float = 0.0,
    direction: str = "horizontal",
    region_percent: float = 20.0,
    restore_shifted: bool = True,
    prepare_only: bool = False,
    include_base64: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> dict[str, Any]:
    path = Path(image_file).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"image file not found: {path}")
    data = {
        "requirement": requirement,
        "job_name": job_name,
        "model_name": model_name,
        "resolution": resolution,
        "temperature": str(temperature),
        "direction": direction,
        "region_percent": str(region_percent),
        "restore_shifted": "true" if restore_shifted else "false",
        "prepare_only": "true" if prepare_only else "false",
        "include_base64": "true" if include_base64 else "false",
    }
    files = {"file": (path.name, path.read_bytes(), _mime_for_path(path))}
    url = _normalize_base_url(api_base, "/api/workflows/pixel_gen_self_loop/run")
    response, payload = _request_json(
        method="POST",
        url=url,
        headers=_base_headers(api_key),
        data=data,
        files=files,
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 400:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def run_pixel_gen_self_loop(
    *,
    api_base: str,
    api_key: str,
    image_file: str,
    job_name: str = "",
    model_name: str = "gemini-3.1-flash-image-preview",
    resolution: str = "1K",
    temperature: float = 0.0,
    direction: str = "horizontal",
    region_percent: float = 20.0,
    restore_shifted: bool = True,
    prepare_only: bool = False,
    include_base64: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    max_wait: int = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    verify: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    submit_payload = submit_pixel_gen_self_loop(
        api_base=api_base,
        api_key=api_key,
        image_file=image_file,
        job_name=job_name,
        model_name=model_name,
        resolution=resolution,
        temperature=temperature,
        direction=direction,
        region_percent=region_percent,
        restore_shifted=restore_shifted,
        prepare_only=prepare_only,
        include_base64=include_base64,
        timeout=timeout,
        verify=verify,
    )
    jobs_url = str(submit_payload.get("jobs_url") or "").strip()
    if not jobs_url:
        raise RuntimeError("self-loop submit response missing jobs_url")
    final_payload = poll_job_until_done(
        jobs_url=jobs_url,
        api_key=api_key,
        timeout=timeout,
        max_wait=max_wait,
        poll_interval=poll_interval,
        verify=verify,
    )
    return submit_payload, final_payload


def poll_animate_job(
    *,
    api_base: str,
    api_key: str,
    api_job_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> dict[str, Any]:
    url = _normalize_base_url(api_base, "/api/jobs")
    response, payload = _request_json(
        method="GET",
        url=url,
        headers=_base_headers(api_key),
        params={"id": api_job_id},
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 400:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False, indent=2))
    returned_job_id = str(payload.get("job_id") or payload.get("api_job_id") or "").strip()
    if returned_job_id == api_job_id:
        return payload

    items = payload.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and str(item.get("job_id") or "").strip() == api_job_id:
                return item
    raise RuntimeError(f"animate job not found in /api/jobs response: {api_job_id}")


def wait_animate_job(
    *,
    api_base: str,
    api_key: str,
    api_job_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_wait: int = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    verify: bool = True,
) -> dict[str, Any]:
    deadline = time.time() + max(max_wait, 1)
    final_payload: dict[str, Any] | None = None
    while time.time() <= deadline:
        payload = poll_animate_job(
            api_base=api_base,
            api_key=api_key,
            api_job_id=api_job_id,
            timeout=timeout,
            verify=verify,
        )
        _print_status("[INFO]", payload)
        status = str(payload.get("status") or "").strip().lower()
        if status in TERMINAL_ANIMATE_STATUSES:
            final_payload = payload
            break
        time.sleep(max(poll_interval, 0.1))
    if final_payload is None:
        raise TimeoutError(f"animate polling timed out after {max_wait}s")
    return final_payload


def _save_run_outputs(
    *,
    output_root: str,
    slug_seed: str,
    submit_payload: dict[str, Any],
    final_payload: dict[str, Any],
    timeout: int,
    verify: bool,
    api_key: str = "",
    no_download: bool = False,
) -> tuple[Path, list[dict[str, Any]]]:
    output_dir = _predict_saved_dir(output_root, slug_seed)
    _save_json(output_dir / "submit_response.json", submit_payload)
    _save_json(output_dir / "job_response.json", final_payload)
    downloads: list[dict[str, Any]] = [
        {"type": "json", "path": str(output_dir / "submit_response.json")},
        {"type": "json", "path": str(output_dir / "job_response.json")},
    ]
    urls = _collect_http_urls(final_payload)
    if not no_download and urls:
        print(f"[INFO] downloading_outputs count={len(urls)} to={output_dir}")
        headers = _base_headers(api_key) if api_key else None
        downloads.extend(_download_named_urls(
            urls=urls,
            output_dir=output_dir,
            timeout=timeout,
            verify=verify,
            headers=headers,
        ))
    return output_dir, downloads


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone MeowArt API test CLI.")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="API base URL")
    parser.add_argument(
        "--api-key",
        default="",
        help=f"User API key, e.g. ma_live_xxx. Defaults to ${DEFAULT_API_KEY_ENV} or .env when omitted.",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Per-request timeout in seconds")
    parser.add_argument("--max-wait", type=int, default=DEFAULT_MAX_WAIT, help="Max polling wait in seconds")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help="Polling interval in seconds")
    parser.add_argument(
        "--work-dir",
        "--work_dir",
        dest="work_dir",
        default=DEFAULT_WORK_DIR,
        help="Base directory for per-run logs and metadata",
    )
    parser.add_argument(
        "--output-dir",
        "--output_dir",
        dest="output_dir",
        default="",
        help="Directory to save generated files; defaults to this run directory",
    )
    parser.add_argument("--no-download", action="store_true", help="Skip downloading remote files")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification")

    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_shared_path_args(command_parser: argparse.ArgumentParser) -> None:
        # Mirror common path flags on subcommands so users can place them either
        # before or after the command name, which matches typical CLI habits.
        command_parser.add_argument(
            "--work-dir",
            "--work_dir",
            dest="work_dir",
            default=DEFAULT_WORK_DIR,
            help="Base directory for per-run logs and metadata",
        )
        command_parser.add_argument(
            "--output-dir",
            "--output_dir",
            dest="output_dir",
            default="",
            help="Directory to save generated files; defaults to this run directory",
        )

    def add_shared_runtime_args(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Per-request timeout in seconds")
        command_parser.add_argument("--max-wait", type=int, default=DEFAULT_MAX_WAIT, help="Max polling wait in seconds")
        command_parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help="Polling interval in seconds")
        command_parser.add_argument("--no-download", action="store_true", help="Skip downloading remote files")
        command_parser.add_argument("--insecure", action="store_true", help="Disable TLS verification")

    pixel_templates = subparsers.add_parser("pixel-gen-template-info", help="Get pixel-gen template info")
    add_shared_path_args(pixel_templates)

    pixel_submit = subparsers.add_parser("pixel-gen-submit", help="Submit a pixel-gen job")
    add_shared_path_args(pixel_submit)
    pixel_submit.add_argument("--template-name", required=True)
    pixel_submit.add_argument("--requirement", required=True)
    pixel_submit.add_argument("--template-config", default="{}", help="JSON object string")
    pixel_submit.add_argument("--job-name", default="")
    pixel_submit.add_argument("--model-name", default="")
    pixel_submit.add_argument("--resolution", default="1K")
    pixel_submit.add_argument("--aspect-ratio", default="1:1")
    pixel_submit.add_argument("--temperature", type=float, default=0.0)
    pixel_submit.add_argument("--include-base64", action="store_true")

    pixel_run = subparsers.add_parser("pixel-gen-run", help="Submit and wait for pixel-gen")
    for action in pixel_submit._actions[1:]:
        if action.dest not in {"help"}:
            pixel_run._add_action(action)
    add_shared_runtime_args(pixel_run)

    pixel_poll = subparsers.add_parser("pixel-gen-poll", help="Poll one pixel-gen job")
    add_shared_path_args(pixel_poll)
    pixel_poll.add_argument("--api-job-id", required=True)

    pixel_history = subparsers.add_parser("pixel-gen-history", help="Query pixel-gen history")
    add_shared_path_args(pixel_history)
    pixel_history.add_argument("--limit", type=int, default=20)
    pixel_history.add_argument("--offset", type=int, default=0)
    pixel_history.add_argument("--status", default="")

    pixel_download = subparsers.add_parser("pixel-gen-download", help="Download pixel-gen output")
    add_shared_path_args(pixel_download)
    pixel_download.add_argument("--api-job-id", required=True)
    pixel_download.add_argument("--output-index", type=int, default=None)

    pixel_cancel = subparsers.add_parser("pixel-gen-cancel", help="Cancel one pixel-gen job")
    add_shared_path_args(pixel_cancel)
    pixel_cancel.add_argument("--api-job-id", required=True)

    remove_bg_submit = subparsers.add_parser("remove-background-submit", help="Submit a remove-background job")
    add_shared_path_args(remove_bg_submit)
    remove_bg_submit.add_argument("--image-file", required=True)
    remove_bg_submit.add_argument("--method", default="hd")
    remove_bg_submit.add_argument("--enable-perfect-pixel", action="store_true", default=True)
    remove_bg_submit.add_argument("--no-enable-perfect-pixel", action="store_false", dest="enable_perfect_pixel")
    remove_bg_submit.add_argument("--is-white-bg", action="store_true", default=True)
    remove_bg_submit.add_argument("--no-is-white-bg", action="store_false", dest="is_white_bg")
    remove_bg_submit.add_argument("--prompt", default="")
    remove_bg_submit.add_argument("--ai-api-key", default="")
    remove_bg_submit.add_argument("--ai-model-name", default="gemini-3.1-flash-image-preview")
    remove_bg_submit.add_argument("--ai-resolution", default="1K")
    remove_bg_submit.add_argument("--ai-aspect-ratio", default="1:1")
    remove_bg_submit.add_argument("--ai-temperature", type=float, default=0.0)
    remove_bg_submit.add_argument("--ai-background-diff-threshold", type=int, default=120)
    remove_bg_submit.add_argument("--photoroom-api-key", default="")

    remove_bg_run = subparsers.add_parser("remove-background-run", help="Submit and wait for remove-background")
    for action in remove_bg_submit._actions[1:]:
        if action.dest not in {"help"}:
            remove_bg_run._add_action(action)

    pixelate_submit = subparsers.add_parser("pixelate-submit", help="Submit a pixelate job")
    add_shared_path_args(pixelate_submit)
    pixelate_submit.add_argument("--image-file", required=True)
    pixelate_submit.add_argument("--pixel-size", default="")
    pixelate_submit.add_argument("--alpha-threshold", type=int, default=128)
    pixelate_submit.add_argument("--sample-method", default="majority")
    pixelate_submit.add_argument("--min-size", type=float, default=2.0)
    pixelate_submit.add_argument("--peak-width", type=int, default=6)
    pixelate_submit.add_argument("--refine-intensity", type=float, default=0.25)
    pixelate_submit.add_argument("--fix-square", action="store_true", default=True)
    pixelate_submit.add_argument("--no-fix-square", action="store_false", dest="fix_square")
    pixelate_submit.add_argument("--pad-pow2-square", action="store_true", default=True)
    pixelate_submit.add_argument("--no-pad-pow2-square", action="store_false", dest="pad_pow2_square")
    pixelate_submit.add_argument("--crop-border", action="store_true", default=False)
    pixelate_submit.add_argument("--crop-color-thr", type=int, default=20)
    pixelate_submit.add_argument("--crop-bg-ratio", type=float, default=0.995)
    pixelate_submit.add_argument("--crop-edge-width", type=int, default=10)

    pixelate_run = subparsers.add_parser("pixelate-run", help="Submit and wait for pixelate")
    for action in pixelate_submit._actions[1:]:
        if action.dest not in {"help"}:
            pixelate_run._add_action(action)

    self_loop_submit = subparsers.add_parser("self-loop-submit", help="Submit a pixel_gen_self_loop job")
    add_shared_path_args(self_loop_submit)
    self_loop_submit.add_argument("--image-file", required=True)
    self_loop_submit.add_argument("--requirement", default="")
    self_loop_submit.add_argument("--job-name", default="")
    self_loop_submit.add_argument("--model-name", default="gemini-3.1-flash-image-preview")
    self_loop_submit.add_argument("--resolution", default="1K")
    self_loop_submit.add_argument("--temperature", type=float, default=0.0)
    self_loop_submit.add_argument("--direction", default="horizontal")
    self_loop_submit.add_argument("--region-percent", type=float, default=20.0)
    self_loop_submit.add_argument("--restore-shifted", action="store_true", default=True)
    self_loop_submit.add_argument("--no-restore-shifted", action="store_false", dest="restore_shifted")
    self_loop_submit.add_argument("--prepare-only", action="store_true", default=False)
    self_loop_submit.add_argument("--include-base64", action="store_true")

    self_loop_run = subparsers.add_parser("self-loop-run", help="Submit and wait for pixel_gen_self_loop")
    for action in self_loop_submit._actions[1:]:
        if action.dest not in {"help", "requirement"}:
            self_loop_run._add_action(action)

    gemini_post = subparsers.add_parser("gemini-post", help="Call a generic Gemini proxy POST endpoint")
    add_shared_path_args(gemini_post)
    gemini_post.add_argument(
        "--path",
        required=True,
        help=f"Gemini proxy path, e.g. v1beta/models/{DEFAULT_GEMINI_MODEL}:generateContent",
    )
    gemini_post.add_argument("--json-body", required=True, help="Raw JSON string")

    gemini_generate = subparsers.add_parser("gemini-generate-content", help="Call Gemini generateContent")
    add_shared_path_args(gemini_generate)
    gemini_generate.add_argument("--model", default=DEFAULT_GEMINI_MODEL)
    gemini_generate.add_argument("--text", required=True, help="Prompt text")
    gemini_generate.add_argument("--generation-config", default="", help="JSON object string")

    credits_balance = subparsers.add_parser("credits-balance", help="Get current credits balance")
    add_shared_path_args(credits_balance)

    animate_submit_parser = subparsers.add_parser("animate-submit", help="Submit an animate job")
    add_shared_path_args(animate_submit_parser)
    animate_submit_parser.add_argument("--image-file", required=True)
    animate_submit_parser.add_argument("--prompt", default="")
    animate_submit_parser.add_argument("--is-pixel", action="store_true")
    animate_submit_parser.add_argument("--optimize-prompt", action="store_true", default=True)
    animate_submit_parser.add_argument("--no-optimize-prompt", action="store_false", dest="optimize_prompt")
    animate_submit_parser.add_argument("--model", default="")
    animate_submit_parser.add_argument("--negative-prompt", default="")
    animate_submit_parser.add_argument("--pixel-config", default="", help="JSON object string")
    animate_submit_parser.add_argument("--output-frames", type=int, default=8)
    animate_submit_parser.add_argument("--seed", type=int, default=None)
    animate_submit_parser.add_argument("--output-format", default="webp")
    animate_submit_parser.add_argument("--matte-color", default="#808080")

    animate_run_parser = subparsers.add_parser("animate-run", help="Submit and wait for animate")
    for action in animate_submit_parser._actions[1:]:
        if action.dest not in {"help"}:
            animate_run_parser._add_action(action)

    animate_poll_parser = subparsers.add_parser("animate-poll", help="Poll one animate job")
    add_shared_path_args(animate_poll_parser)
    animate_poll_parser.add_argument("--api-job-id", required=True)

    return parser.parse_args()


def _parse_json_arg(raw: str, *, name: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must be a JSON object")
    return payload


def _read_dotenv_value(key: str) -> str:
    candidate_paths = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    seen: set[Path] = set()
    for path in candidate_paths:
        resolved = path.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        for line in resolved.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, value = stripped.split("=", 1)
            if name.strip() != key:
                continue
            return value.strip().strip("'\"")
    return ""


def _resolve_api_key(raw_api_key: str) -> str:
    api_key = (raw_api_key or "").strip()
    if api_key:
        return api_key

    env_api_key = os.getenv(DEFAULT_API_KEY_ENV, "").strip()
    if env_api_key:
        return env_api_key

    dotenv_api_key = _read_dotenv_value(DEFAULT_API_KEY_ENV).strip()
    if dotenv_api_key:
        return dotenv_api_key

    raise ValueError(
        f"missing API key: pass --api-key, set the {DEFAULT_API_KEY_ENV} environment variable, "
        f"or add {DEFAULT_API_KEY_ENV}=... to .env"
    )

def main() -> int:
    _configure_stdio()
    args = parse_args()
    started_at = datetime.now().isoformat(timespec="seconds")
    run_dir = _create_run_dir(args.work_dir, args.command)
    effective_output_dir = _resolve_output_dir(args.output_dir, run_dir)
    try:
        args.api_key = _resolve_api_key(args.api_key)
        verify = not args.insecure

        if args.command == "pixel-gen-template-info":
            payload = pixel_gen_template_info(
                api_base=args.api_base,
                api_key=args.api_key,
                timeout=args.timeout,
                verify=verify,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={},
                response_payload=payload,
                downloads=[],
                effective_output_dir=str(effective_output_dir),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "pixel-gen-submit":
            payload = submit_pixel_gen(
                api_base=args.api_base,
                api_key=args.api_key,
                template_name=args.template_name,
                requirement=args.requirement,
                template_config=_parse_json_arg(args.template_config, name="template_config"),
                job_name=args.job_name,
                model_name=args.model_name,
                resolution=args.resolution,
                aspect_ratio=args.aspect_ratio,
                temperature=args.temperature,
                include_base64=args.include_base64,
                timeout=args.timeout,
                verify=verify,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={
                    "template_name": args.template_name,
                    "requirement": args.requirement,
                    "template_config": _parse_json_arg(args.template_config, name="template_config"),
                    "job_name": args.job_name,
                    "model_name": args.model_name,
                    "resolution": args.resolution,
                    "aspect_ratio": args.aspect_ratio,
                    "temperature": args.temperature,
                    "include_base64": args.include_base64,
                },
                response_payload=payload,
                downloads=[],
                effective_output_dir=str(effective_output_dir),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "pixel-gen-run":
            template_config = _parse_json_arg(args.template_config, name="template_config")
            request_payload = {
                "template_name": args.template_name,
                "requirement": args.requirement,
                "template_config": template_config,
                "job_name": args.job_name,
                "model_name": args.model_name,
                "resolution": args.resolution,
                "aspect_ratio": args.aspect_ratio,
                "temperature": args.temperature,
                "include_base64": args.include_base64,
            }
            predicted_output_dir = _predict_saved_dir(effective_output_dir, args.job_name or args.requirement)
            print(f"[INFO] planned_output_dir={predicted_output_dir}")
            submit_payload = submit_pixel_gen(
                api_base=args.api_base,
                api_key=args.api_key,
                template_name=args.template_name,
                requirement=args.requirement,
                template_config=template_config,
                job_name=args.job_name,
                model_name=args.model_name,
                resolution=args.resolution,
                aspect_ratio=args.aspect_ratio,
                temperature=args.temperature,
                include_base64=args.include_base64,
                timeout=args.timeout,
                verify=verify,
            )
            api_job_id = str(submit_payload.get("api_job_id") or "").strip()
            if not api_job_id:
                raise RuntimeError("pixel-gen submit response missing api_job_id")
            _save_json(predicted_output_dir / "submit_response.json", submit_payload)
            print(f"[INFO] submitted api_job_id={api_job_id}")
            print(f"[INFO] waiting_for_completion poll_interval={args.poll_interval}s max_wait={args.max_wait}s")
            final_payload = wait_pixel_gen_job(
                api_base=args.api_base,
                api_key=args.api_key,
                api_job_id=api_job_id,
                timeout=args.timeout,
                max_wait=args.max_wait,
                poll_interval=args.poll_interval,
                verify=verify,
            )
            output_dir, downloads = _save_run_outputs(
                output_root=str(effective_output_dir),
                slug_seed=args.job_name or args.requirement,
                submit_payload=submit_payload,
                final_payload=final_payload,
                timeout=args.timeout,
                verify=verify,
                api_key=args.api_key,
                no_download=args.no_download,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload=request_payload,
                response_payload={"submit": submit_payload, "final": final_payload},
                downloads=downloads,
                effective_output_dir=str(output_dir),
            )
            print(f"[INFO] saved_dir={output_dir}")
            print(json.dumps(final_payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "pixel-gen-poll":
            payload = poll_pixel_gen_job(
                api_base=args.api_base,
                api_key=args.api_key,
                api_job_id=args.api_job_id,
                timeout=args.timeout,
                verify=verify,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={"api_job_id": args.api_job_id},
                response_payload=payload,
                downloads=[],
                effective_output_dir=str(effective_output_dir),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "pixel-gen-history":
            payload = pixel_gen_history(
                api_base=args.api_base,
                api_key=args.api_key,
                limit=args.limit,
                offset=args.offset,
                status=args.status,
                timeout=args.timeout,
                verify=verify,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={"limit": args.limit, "offset": args.offset, "status": args.status},
                response_payload=payload,
                downloads=[],
                effective_output_dir=str(effective_output_dir),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "pixel-gen-download":
            path = pixel_gen_download(
                api_base=args.api_base,
                api_key=args.api_key,
                api_job_id=args.api_job_id,
                output_dir=args.output_dir or str(effective_output_dir),
                output_index=args.output_index,
                timeout=args.timeout,
                verify=verify,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={"api_job_id": args.api_job_id, "output_dir": args.output_dir, "output_index": args.output_index},
                response_payload={"downloaded_path": str(path)},
                downloads=[{"type": "explicit_download", "path": str(path)}],
                effective_output_dir=str(path.parent),
            )
            print(f"[INFO] downloaded={path}")
            return 0

        if args.command == "pixel-gen-cancel":
            payload = pixel_gen_cancel(
                api_base=args.api_base,
                api_key=args.api_key,
                api_job_id=args.api_job_id,
                timeout=args.timeout,
                verify=verify,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={"api_job_id": args.api_job_id},
                response_payload=payload,
                downloads=[],
                effective_output_dir=str(effective_output_dir),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "remove-background-submit":
            payload = submit_remove_background(
                api_base=args.api_base,
                api_key=args.api_key,
                image_file=args.image_file,
                method=args.method,
                enable_perfect_pixel=args.enable_perfect_pixel,
                is_white_bg=args.is_white_bg,
                prompt=args.prompt,
                ai_api_key=args.ai_api_key,
                ai_model_name=args.ai_model_name,
                ai_resolution=args.ai_resolution,
                ai_aspect_ratio=args.ai_aspect_ratio,
                ai_temperature=args.ai_temperature,
                ai_background_diff_threshold=args.ai_background_diff_threshold,
                photoroom_api_key=args.photoroom_api_key,
                timeout=args.timeout,
                verify=verify,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={
                    "image_file": args.image_file,
                    "method": args.method,
                    "enable_perfect_pixel": args.enable_perfect_pixel,
                    "is_white_bg": args.is_white_bg,
                    "prompt": args.prompt,
                    "ai_model_name": args.ai_model_name,
                },
                response_payload=payload,
                downloads=[],
                effective_output_dir=str(effective_output_dir),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "remove-background-run":
            print(f"[INFO] planned_output_dir={_predict_saved_dir(effective_output_dir, args.prompt or Path(args.image_file).stem)}")
            submit_payload, final_payload = run_remove_background(
                api_base=args.api_base,
                api_key=args.api_key,
                image_file=args.image_file,
                method=args.method,
                enable_perfect_pixel=args.enable_perfect_pixel,
                is_white_bg=args.is_white_bg,
                prompt=args.prompt,
                ai_api_key=args.ai_api_key,
                ai_model_name=args.ai_model_name,
                ai_resolution=args.ai_resolution,
                ai_aspect_ratio=args.ai_aspect_ratio,
                ai_temperature=args.ai_temperature,
                ai_background_diff_threshold=args.ai_background_diff_threshold,
                photoroom_api_key=args.photoroom_api_key,
                timeout=args.timeout,
                max_wait=args.max_wait,
                poll_interval=args.poll_interval,
                verify=verify,
            )
            output_dir, downloads = _save_run_outputs(
                output_root=str(effective_output_dir),
                slug_seed=args.prompt or Path(args.image_file).stem,
                submit_payload=submit_payload,
                final_payload=final_payload,
                timeout=args.timeout,
                verify=verify,
                no_download=args.no_download,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={
                    "image_file": args.image_file,
                    "method": args.method,
                    "enable_perfect_pixel": args.enable_perfect_pixel,
                    "is_white_bg": args.is_white_bg,
                    "prompt": args.prompt,
                    "ai_model_name": args.ai_model_name,
                },
                response_payload={"submit": submit_payload, "final": final_payload},
                downloads=downloads,
                effective_output_dir=str(output_dir),
            )
            print(f"[INFO] saved_dir={output_dir}")
            print(json.dumps(final_payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "pixelate-submit":
            payload = submit_pixelate(
                api_base=args.api_base,
                api_key=args.api_key,
                image_file=args.image_file,
                pixel_size=args.pixel_size,
                alpha_threshold=args.alpha_threshold,
                sample_method=args.sample_method,
                min_size=args.min_size,
                peak_width=args.peak_width,
                refine_intensity=args.refine_intensity,
                fix_square=args.fix_square,
                pad_pow2_square=args.pad_pow2_square,
                crop_border=args.crop_border,
                crop_color_thr=args.crop_color_thr,
                crop_bg_ratio=args.crop_bg_ratio,
                crop_edge_width=args.crop_edge_width,
                timeout=args.timeout,
                verify=verify,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={"image_file": args.image_file},
                response_payload=payload,
                downloads=[],
                effective_output_dir=str(effective_output_dir),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "pixelate-run":
            print(f"[INFO] planned_output_dir={_predict_saved_dir(effective_output_dir, Path(args.image_file).stem)}")
            submit_payload, final_payload = run_pixelate(
                api_base=args.api_base,
                api_key=args.api_key,
                image_file=args.image_file,
                pixel_size=args.pixel_size,
                alpha_threshold=args.alpha_threshold,
                sample_method=args.sample_method,
                min_size=args.min_size,
                peak_width=args.peak_width,
                refine_intensity=args.refine_intensity,
                fix_square=args.fix_square,
                pad_pow2_square=args.pad_pow2_square,
                crop_border=args.crop_border,
                crop_color_thr=args.crop_color_thr,
                crop_bg_ratio=args.crop_bg_ratio,
                crop_edge_width=args.crop_edge_width,
                timeout=args.timeout,
                max_wait=args.max_wait,
                poll_interval=args.poll_interval,
                verify=verify,
            )
            output_dir, downloads = _save_run_outputs(
                output_root=str(effective_output_dir),
                slug_seed=Path(args.image_file).stem,
                submit_payload=submit_payload,
                final_payload=final_payload,
                timeout=args.timeout,
                verify=verify,
                no_download=args.no_download,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={"image_file": args.image_file},
                response_payload={"submit": submit_payload, "final": final_payload},
                downloads=downloads,
                effective_output_dir=str(output_dir),
            )
            print(f"[INFO] saved_dir={output_dir}")
            print(json.dumps(final_payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "self-loop-submit":
            payload = submit_pixel_gen_self_loop(
                api_base=args.api_base,
                api_key=args.api_key,
                image_file=args.image_file,
                requirement=args.requirement,
                job_name=args.job_name,
                model_name=args.model_name,
                resolution=args.resolution,
                temperature=args.temperature,
                direction=args.direction,
                region_percent=args.region_percent,
                restore_shifted=args.restore_shifted,
                prepare_only=args.prepare_only,
                include_base64=args.include_base64,
                timeout=args.timeout,
                verify=verify,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={"image_file": args.image_file, "requirement": args.requirement, "direction": args.direction},
                response_payload=payload,
                downloads=[],
                effective_output_dir=str(effective_output_dir),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "self-loop-run":
            print(f"[INFO] planned_output_dir={_predict_saved_dir(effective_output_dir, args.job_name or Path(args.image_file).stem)}")
            submit_payload, final_payload = run_pixel_gen_self_loop(
                api_base=args.api_base,
                api_key=args.api_key,
                image_file=args.image_file,
                job_name=args.job_name,
                model_name=args.model_name,
                resolution=args.resolution,
                temperature=args.temperature,
                direction=args.direction,
                region_percent=args.region_percent,
                restore_shifted=args.restore_shifted,
                prepare_only=args.prepare_only,
                include_base64=args.include_base64,
                timeout=args.timeout,
                max_wait=args.max_wait,
                poll_interval=args.poll_interval,
                verify=verify,
            )
            output_dir, downloads = _save_run_outputs(
                output_root=str(effective_output_dir),
                slug_seed=args.job_name or Path(args.image_file).stem,
                submit_payload=submit_payload,
                final_payload=final_payload,
                timeout=args.timeout,
                verify=verify,
                no_download=args.no_download,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={"image_file": args.image_file, "direction": args.direction},
                response_payload={"submit": submit_payload, "final": final_payload},
                downloads=downloads,
                effective_output_dir=str(output_dir),
            )
            print(f"[INFO] saved_dir={output_dir}")
            print(json.dumps(final_payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "gemini-post":
            payload = gemini_proxy_request(
                api_base=args.api_base,
                api_key=args.api_key,
                path=args.path,
                method="POST",
                json_body=_parse_json_arg(args.json_body, name="json_body"),
                timeout=args.timeout,
                verify=verify,
            )
            output_dir, downloads = _save_gemini_response_assets(
                payload=payload,
                output_dir=str(effective_output_dir),
                timeout=args.timeout,
                verify=verify,
                api_key=args.api_key,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={"path": args.path, "json_body": _parse_json_arg(args.json_body, name="json_body")},
                response_payload=payload,
                downloads=downloads,
                effective_output_dir=str(output_dir or effective_output_dir),
            )
            if output_dir is not None:
                print(f"[INFO] saved_dir={output_dir}")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "gemini-generate-content":
            generation_config = _parse_json_arg(args.generation_config or "{}", name="generation_config")
            payload = gemini_generate_content(
                api_base=args.api_base,
                api_key=args.api_key,
                model=args.model,
                contents=[{"parts": [{"text": args.text}]}],
                generation_config=generation_config or None,
                timeout=args.timeout,
                verify=verify,
            )
            output_dir, downloads = _save_gemini_response_assets(
                payload=payload,
                output_dir=str(effective_output_dir),
                timeout=args.timeout,
                verify=verify,
                api_key=args.api_key,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={
                    "model": args.model,
                    "text": args.text,
                    "generation_config": generation_config,
                },
                response_payload=payload,
                downloads=downloads,
                effective_output_dir=str(output_dir or effective_output_dir),
            )
            if output_dir is not None:
                print(f"[INFO] saved_dir={output_dir}")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "credits-balance":
            payload = get_credits_balance(
                api_base=args.api_base,
                api_key=args.api_key,
                timeout=args.timeout,
                verify=verify,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={},
                response_payload=payload,
                downloads=[],
                effective_output_dir=str(effective_output_dir),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "animate-submit":
            payload = submit_animate(
                api_base=args.api_base,
                api_key=args.api_key,
                image_data_url=image_file_to_data_url(args.image_file),
                prompt=args.prompt,
                is_pixel=args.is_pixel,
                optimize_prompt=args.optimize_prompt,
                model=args.model,
                negative_prompt=args.negative_prompt,
                pixel_config=_parse_json_arg(args.pixel_config or "{}", name="pixel_config"),
                output_frames=args.output_frames,
                seed=args.seed,
                output_format=args.output_format,
                matte_color=args.matte_color,
                timeout=args.timeout,
                verify=verify,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={"image_file": args.image_file, "prompt": args.prompt, "model": args.model},
                response_payload=payload,
                downloads=[],
                effective_output_dir=str(effective_output_dir),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "animate-run":
            print(f"[INFO] planned_output_dir={_predict_saved_dir(effective_output_dir, args.prompt or Path(args.image_file).stem)}")
            submit_payload = submit_animate(
                api_base=args.api_base,
                api_key=args.api_key,
                image_data_url=image_file_to_data_url(args.image_file),
                prompt=args.prompt,
                is_pixel=args.is_pixel,
                optimize_prompt=args.optimize_prompt,
                model=args.model,
                negative_prompt=args.negative_prompt,
                pixel_config=_parse_json_arg(args.pixel_config or "{}", name="pixel_config"),
                output_frames=args.output_frames,
                seed=args.seed,
                output_format=args.output_format,
                matte_color=args.matte_color,
                timeout=args.timeout,
                verify=verify,
            )
            api_job_id = str(submit_payload.get("api_job_id") or "").strip()
            if not api_job_id:
                raise RuntimeError("animate submit response missing api_job_id")
            print(f"[INFO] submitted api_job_id={api_job_id}")
            try:
                final_payload = wait_animate_job(
                    api_base=args.api_base,
                    api_key=args.api_key,
                    api_job_id=api_job_id,
                    timeout=args.timeout,
                    max_wait=args.max_wait,
                    poll_interval=args.poll_interval,
                    verify=verify,
                )
            except (RuntimeError, TimeoutError) as exc:
                output_dir = Path(str(effective_output_dir)).expanduser() / _safe_slug(args.prompt or Path(args.image_file).stem)
                _save_json(output_dir / "submit_response.json", submit_payload)
                downloads = [{"type": "json", "path": str(output_dir / "submit_response.json")}]
                _write_meta(
                    run_dir=run_dir,
                    started_at=started_at,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                    args=args,
                    request_payload={"image_file": args.image_file, "prompt": args.prompt, "model": args.model},
                    response_payload={"submit": submit_payload},
                    downloads=downloads,
                    effective_output_dir=str(output_dir),
                    error=str(exc),
                )
                print(f"[WARN] animate submitted but polling did not complete: {exc}")
                print(f"[INFO] saved_dir={output_dir}")
                print(json.dumps(submit_payload, ensure_ascii=False, indent=2))
                return 1
            output_dir, downloads = _save_run_outputs(
                output_root=str(effective_output_dir),
                slug_seed=args.prompt or Path(args.image_file).stem,
                submit_payload=submit_payload,
                final_payload=final_payload,
                timeout=args.timeout,
                verify=verify,
                no_download=args.no_download,
            )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={"image_file": args.image_file, "prompt": args.prompt, "model": args.model},
                response_payload={"submit": submit_payload, "final": final_payload},
                downloads=downloads,
                effective_output_dir=str(output_dir),
            )
            print(f"[INFO] saved_dir={output_dir}")
            print(json.dumps(final_payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "animate-poll":
            payload = poll_animate_job(
                api_base=args.api_base,
                api_key=args.api_key,
                api_job_id=args.api_job_id,
                timeout=args.timeout,
                verify=verify,
            )
            downloads: list[dict[str, Any]] = []
            effective_poll_output_dir = Path(str(effective_output_dir)).expanduser()
            if str(payload.get("status") or "").strip().lower() in SUCCESS_ANIMATE_STATUSES:
                effective_poll_output_dir, downloads = _save_run_outputs(
                    output_root=str(effective_output_dir),
                    slug_seed=args.api_job_id,
                    submit_payload={"api_job_id": args.api_job_id},
                    final_payload=payload,
                    timeout=args.timeout,
                    verify=verify,
                    no_download=args.no_download,
                )
            _write_meta(
                run_dir=run_dir,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                args=args,
                request_payload={"api_job_id": args.api_job_id},
                response_payload=payload,
                downloads=downloads,
                effective_output_dir=str(effective_poll_output_dir),
            )
            if downloads:
                print(f"[INFO] saved_dir={effective_poll_output_dir}")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        print(f"[ERROR] unknown command: {args.command}", file=sys.stderr)
        return 2
    except (RuntimeError, ValueError, FileNotFoundError, TimeoutError) as exc:
        _write_meta(
            run_dir=run_dir,
            started_at=started_at,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            args=args,
            request_payload={},
            response_payload=None,
            downloads=[],
            effective_output_dir=str(effective_output_dir),
            error=str(exc),
        )
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
