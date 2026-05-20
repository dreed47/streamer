import re
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import monitor as _monitor
import state as _state

_RECORDING_RE = re.compile(r'^(.+)_(\d{8}_\d{6})(?:_tc)?\.mp4$')

app = FastAPI()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

CONFIG_PATH = Path("config.yml")


def _load_cfg() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def _save_cfg(config: dict):
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _model_status(name: str, model: dict) -> tuple[str, str, str]:
    """Returns (dot_class, display_label, badge_class)."""
    t = _state.active_recordings.get(name)
    if t and t.is_alive():
        return "recording", "Recording", "badge-warning"
    if name in _state.resume_after:
        dt = _state.resume_after[name]
        return "cooldown", f"Cooldown · {dt.strftime('%H:%M')}", "badge-warning"
    if not model.get("enabled", True):
        return "idle", "Disabled", "badge-neutral"
    if not _monitor._in_poll_window(model):
        return "idle", "Outside window", "badge-neutral"
    reason = _state.idle_reason.get(name, "")
    if reason == "offline":
        return "idle", "Offline", "badge-danger"
    if reason == "max_concurrent":
        return "idle", "Max concurrent", "badge-neutral"
    if reason == "starting":
        return "idle", "Starting…", "badge-warning"
    if reason == "no_stream":
        return "idle", "No stream", "badge-neutral"
    return "idle", "Idle", "badge-neutral"


def _video_counts() -> dict[str, int]:
    rdir = _monitor.RECORDINGS_DIR
    counts: dict[str, int] = {}
    if rdir.exists():
        for p in rdir.glob("*.mp4"):
            m = _RECORDING_RE.match(p.name)
            if m:
                counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


@app.get("/", response_class=RedirectResponse)
async def root():
    return RedirectResponse(url="/models")


@app.get("/models", response_class=HTMLResponse)
async def models_list(request: Request, success: str = "", error: str = ""):
    with _state.config_lock:
        config = _load_cfg()
    model_list = config.get("models", [])
    video_counts = _video_counts()
    for m in model_list:
        status, label, badge = _model_status(m["name"], m)
        m["_status"] = status
        m["_status_label"] = label
        m["_status_badge"] = badge
        m["_video_count"] = video_counts.get(m["name"], 0)
    return templates.TemplateResponse(request, "models.html", {
        "models": model_list,
        "success": success,
        "error": error,
    })


@app.get("/models/new", response_class=HTMLResponse)
async def model_new_form(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "model_edit.html", {
        "model": None,
        "is_new": True,
        "success": "",
        "error": error,
    })


@app.post("/models/new")
async def model_create(
    name: str = Form(...),
    enabled: str = Form("off"),
    poll_start_time: str = Form("08:00:00"),
    poll_stop_time: str = Form("23:00:00"),
    recording_time_limit: str = Form(""),
    recording_file_size_limit: str = Form(""),
    on_limit_reached: str = Form("stop_for_day"),
    rollover_max_files: str = Form(""),
    cooldown_minutes: str = Form(""),
    transcode_no_audio: str = Form("off"),
):
    with _state.config_lock:
        config = _load_cfg()
        existing = [m["name"] for m in config.get("models", [])]
        if name in existing:
            return RedirectResponse(
                url=f"/models/new?error=Model+%27{name}%27+already+exists",
                status_code=303,
            )
        new_model = {
            "name": name,
            "enabled": enabled == "on",
            "last_seen": None,
            "poll_start_time": poll_start_time,
            "poll_stop_time": poll_stop_time,
            "recording_time_limit": recording_time_limit or None,
            "recording_file_size_limit": recording_file_size_limit or None,
            "on_limit_reached": on_limit_reached,
            "rollover_max_files": int(rollover_max_files) if rollover_max_files.strip() else None,
            "cooldown_minutes": int(cooldown_minutes) if cooldown_minutes.strip() else None,
            "transcode_no_audio": transcode_no_audio == "on",
        }
        config.setdefault("models", []).append(new_model)
        _save_cfg(config)
    return RedirectResponse(url=f"/models?success=Model+%27{name}%27+added", status_code=303)


@app.get("/models/{name}", response_class=HTMLResponse)
async def model_edit_form(request: Request, name: str, success: str = "", error: str = ""):
    with _state.config_lock:
        config = _load_cfg()
    model = next((m for m in config.get("models", []) if m["name"] == name), None)
    if not model:
        return RedirectResponse(url="/models?error=Model+not+found", status_code=303)
    return templates.TemplateResponse(request, "model_edit.html", {
        "model": model,
        "is_new": False,
        "success": success,
        "error": error,
    })


@app.post("/models/{name}")
async def model_update(
    name: str,
    enabled: str = Form("off"),
    poll_start_time: str = Form(...),
    poll_stop_time: str = Form(...),
    recording_time_limit: str = Form(""),
    recording_file_size_limit: str = Form(""),
    on_limit_reached: str = Form("stop_for_day"),
    rollover_max_files: str = Form(""),
    cooldown_minutes: str = Form(""),
    transcode_no_audio: str = Form("off"),
):
    with _state.config_lock:
        config = _load_cfg()
        model = next((m for m in config.get("models", []) if m["name"] == name), None)
        if not model:
            return RedirectResponse(url="/models?error=Model+not+found", status_code=303)
        model["enabled"] = enabled == "on"
        model["poll_start_time"] = poll_start_time
        model["poll_stop_time"] = poll_stop_time
        model["recording_time_limit"] = recording_time_limit or None
        model["recording_file_size_limit"] = recording_file_size_limit or None
        model["on_limit_reached"] = on_limit_reached
        model["rollover_max_files"] = int(rollover_max_files) if rollover_max_files.strip() else None
        model["cooldown_minutes"] = int(cooldown_minutes) if cooldown_minutes.strip() else None
        model["transcode_no_audio"] = transcode_no_audio == "on"
        _save_cfg(config)
    return RedirectResponse(url=f"/models/{name}?success=Saved", status_code=303)


@app.post("/models/{name}/delete")
async def model_delete(name: str):
    with _state.config_lock:
        config = _load_cfg()
        config["models"] = [m for m in config.get("models", []) if m["name"] != name]
        _save_cfg(config)
    return RedirectResponse(url=f"/models?success=Model+%27{name}%27+deleted", status_code=303)


@app.post("/models/{name}/toggle")
async def model_toggle(name: str):
    with _state.config_lock:
        config = _load_cfg()
        for m in config.get("models", []):
            if m["name"] == name:
                m["enabled"] = not m.get("enabled", True)
                break
        _save_cfg(config)
    return RedirectResponse(url="/models", status_code=303)


@app.get("/api/status")
async def api_status():
    with _state.config_lock:
        config = _load_cfg()
    model_list = config.get("models", [])
    statuses = {}
    for m in model_list:
        status, label, badge = _model_status(m["name"], m)
        statuses[m["name"]] = {"status": status, "label": label, "badge": badge}
    recording = [n for n, t in _state.active_recordings.items() if t.is_alive()]
    cooldowns = {n: dt.isoformat() for n, dt in _state.resume_after.items()}
    return {"recording": recording, "cooldowns": cooldowns, "statuses": statuses}


def _parse_recording(path: Path) -> dict | None:
    m = _RECORDING_RE.match(path.name)
    if not m:
        return None
    model_name, ts_raw = m.group(1), m.group(2)
    try:
        recorded_at = datetime.strptime(ts_raw, "%Y%m%d_%H%M%S")
    except ValueError:
        recorded_at = None
    stat = path.stat()
    return {
        "filename": path.name,
        "model": model_name,
        "recorded_at": recorded_at,
        "size": stat.st_size,
        "size_mb": round(stat.st_size / (1024 * 1024), 1),
    }


@app.get("/recordings", response_class=HTMLResponse)
async def recordings_list(request: Request, model: str = "", success: str = "", error: str = ""):
    rdir = _monitor.RECORDINGS_DIR
    files = []
    if rdir.exists():
        for p in sorted(rdir.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
            info = _parse_recording(p)
            if info and (not model or info["model"] == model):
                files.append(info)
    models_grouped: dict[str, list] = {}
    for f in files:
        models_grouped.setdefault(f["model"], []).append(f)
    return templates.TemplateResponse(request, "recordings.html", {
        "files": files,
        "models_grouped": models_grouped,
        "filter_model": model,
        "success": success,
        "error": error,
    })


@app.get("/recordings/{filename}")
async def recording_serve(filename: str):
    path = _monitor.RECORDINGS_DIR / filename
    if not path.exists() or path.suffix != ".mp4":
        return RedirectResponse(url="/recordings?error=File+not+found", status_code=303)
    return FileResponse(path, media_type="video/mp4")


@app.post("/recordings/{filename}/delete")
async def recording_delete(filename: str):
    path = _monitor.RECORDINGS_DIR / filename
    if not path.exists() or path.suffix != ".mp4":
        return RedirectResponse(url="/recordings?error=File+not+found", status_code=303)
    m = _RECORDING_RE.match(filename)
    model = m.group(1) if m else ""
    path.unlink()
    redirect = f"/recordings?success={filename}+deleted"
    if model:
        redirect += f"&model={model}"
    return RedirectResponse(url=redirect, status_code=303)
