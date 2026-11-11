"""Per-computer configuration and writable storage, independent of user names or drive letters."""
from __future__ import annotations
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile


def user_data_directory():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()/"AppData"/"Local")/"BeamDiagnostics"


def writable_directory(path, create=False):
    path = Path(path)
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path):
            return True
    except OSError:
        return False


def default_config_path(root):
    root = Path(root).resolve()
    local, personal = root/"config.toml", user_data_directory()/"config.toml"
    if os.environ.get("BEAMDIAG_CONFIG"):
        return Path(os.environ["BEAMDIAG_CONFIG"]).expanduser().resolve()
    if not writable_directory(root):
        return personal
    if local.exists():
        return local
    if personal.exists():
        return personal
    return local


def config_text(cfg):
    """Complete editable TOML, including every supported setting."""
    from .config import validate
    validate(cfg)
    data = cfg.to_dict()
    app = {k: v for k, v in data.items() if k not in ("cameras", "pid", "recording")}
    def literal(value):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    parts = []
    for section, values in (("app", app), ("recording", data["recording"]), ("pid", data["pid"])):
        parts += [f"[{section}]", *[f"{k} = {literal(v)}" for k, v in values.items()], ""]
    for camera in data["cameras"]:
        parts += ["[[cameras]]", *[f"{k} = {literal(v)}" for k, v in camera.items()], ""]
    return "\n".join(parts)


def save_config(cfg, path):
    """Atomic TOML serialization without an extra dependency or personal installation paths."""
    content = config_text(cfg)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", prefix=path.name+".", dir=path.parent,
                                         encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def ensure_config(path, root):
    from .config import AppConfig, load_config
    path = Path(path)
    if not path.exists():
        template = Path(root)/"config.example.toml"
        cfg = load_config(template) if template.exists() else AppConfig()
        save_config(cfg, path)
    return path


def resolve_recording_path(cfg, config_path, override=None):
    configured = Path(override) if override is not None else Path(cfg.recording.output_dir)
    output = configured.expanduser()
    if not output.is_absolute():
        output = Path(config_path).resolve().parent/output
    if not writable_directory(output, create=True):
        if override is None and cfg.recording.output_dir == "captures":
            output = user_data_directory()/"captures"
            if not writable_directory(output, create=True):
                raise PermissionError("No writable capture folder. Choose one with --output.")
        else:
            raise PermissionError(f"Capture folder is not writable: {output}. Choose another folder with --output.")
    return replace(cfg, recording=replace(cfg.recording, output_dir=str(output.resolve())))


def assign_cameras(cfg, mapping):
    from .config import ROLES, validate
    if set(mapping) != set(ROLES) or any(not isinstance(v, str) or not v.strip() for v in mapping.values()):
        raise ValueError("Assign a camera serial to each of Far Field, Dark Field and Near Field")
    if len(set(mapping.values())) != 3:
        raise ValueError("Each role must use a different camera")
    cameras = []
    for camera in cfg.cameras:
        serial = mapping[camera.role].strip()
        # Camera replacement cannot inherit the old sensor's optical confirmation/ROI/reference.
        changes = dict(serial=serial)
        if serial != camera.serial:
            changes.update(calibration_confirmed=False, reference_px=(), analysis_roi=(), background_file="")
        cameras.append(replace(camera, **changes))
    return validate(replace(cfg, cameras=tuple(cameras)))
