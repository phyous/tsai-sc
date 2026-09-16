"""Render game-only recordings with the model's recorded action probabilities.

Usage: python -m tsai_sc.render RUN_DIR --output demo.mp4 --speed 4 --fps 30
Requires Pillow and ffmpeg. No desktop, browser chrome, microphone, or credentials
are captured. The input is the runner's trace.jsonl plus 640x480 PNG game frames.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

from PIL import Image, ImageDraw, ImageFont


WIDTH, HEIGHT = 1600, 900
BACKGROUND = "#080f14"
PANEL = "#101c23"
BORDER = "#23343d"
TEXT = "#e7f1ee"
MUTED = "#8caaa9"
GREEN = "#a4f0bb"
DIM_GREEN = "#355e51"
GOLD = "#e8c270"
RED = "#ef9e92"
MAX_TRACE_BYTES = 100_000_000


class RenderError(ValueError):
    """A safe recording or rendering error, without raw trace contents."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RenderError("A trace record contains duplicate JSON keys.")
        result[key] = value
    return result


def _finite(value: Any, low: float = 0, high: float = float("inf")) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and low <= value <= high
    except OverflowError:
        return False


def safe_text(value: Any, limit: int = 160) -> str:
    """Keep overlay text legible and remove credential-shaped strings."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return "—"
    text = str(value)
    text = re.sub(r"(?i)\b(?:Bearer\s+\S+|(?:TYPESAFE_API_KEY|api[_ -]?key|token|password)\s*[:=]\s*\S+)", "[redacted]", text)
    text = re.sub(r"\b(?:sk-|ts_|tsai_|typesafe_|apikey_)[A-Za-z0-9_\-]{8,}\b", "[redacted]", text)
    text = re.sub(r"\b[A-Za-z0-9_\-]{40,}\b", "[redacted]", text)
    text = " ".join("".join(char if char.isprintable() else " " for char in text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def resolve_frame(run_dir: Path, value: Any) -> Path:
    """Constrain screenshot reads to regular PNG files inside the run directory."""
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise RenderError("A trace frame must be a relative PNG path inside the run directory.")
    root = run_dir.resolve()
    target = (root / value).resolve()
    if not target.is_relative_to(root) or target.suffix.lower() != ".png" or not target.is_file():
        raise RenderError("A trace frame is missing or outside the run directory.")
    return target


def choice_groups(record: dict) -> list[tuple[str, dict]]:
    """Read real Choice distributions; malformed distributions abort rendering."""
    decision = record.get("decision")
    if decision is None:
        return []
    if not isinstance(decision, dict) or not isinstance(decision.get("answers"), dict):
        raise RenderError("A recorded model decision is malformed.")
    groups = []
    for name, answer in decision["answers"].items():
        if not isinstance(name, str) or not isinstance(answer, dict):
            raise RenderError("A recorded answer is malformed.")
        if answer.get("type") != "choice":
            continue
        probabilities = answer.get("probabilities")
        selected = answer.get("choice")
        if (
            not isinstance(probabilities, dict)
            or not 2 <= len(probabilities) <= 255
            or not all(isinstance(option, str) and option for option in probabilities)
            or not all(_finite(value, high=1) for value in probabilities.values())
            or not math.isclose(math.fsum(probabilities.values()), 1, abs_tol=1e-6, rel_tol=0)
            or not isinstance(selected, str)
            or selected not in probabilities
            or not math.isclose(probabilities[selected], max(probabilities.values()), abs_tol=1e-9, rel_tol=0)
            or not _finite(answer.get("confidence"), high=1)
        ):
            raise RenderError("A recorded Choice distribution is invalid; probabilities cannot be reconstructed.")
        groups.append((name, answer))
    # Action comes first, followed by the API's recorded question order.
    groups.sort(key=lambda pair: pair[0] != "action")
    return groups


def load_trace(run_dir: str | os.PathLike[str]) -> list[dict]:
    root = Path(run_dir).resolve()
    trace = root / "trace.jsonl"
    if not trace.resolve().is_relative_to(root) or not trace.is_file() or trace.stat().st_size > MAX_TRACE_BYTES:
        raise RenderError("The run requires a nonempty trace.jsonl within the trace size limit.")
    records = []
    previous = -1.0
    try:
        with trace.open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                record = json.loads(line, object_pairs_hook=_unique_object)
                if (
                    not isinstance(record, dict)
                    or not _finite(record.get("t"))
                    or record["t"] < previous
                    or not isinstance(record.get("state"), dict)
                    or not isinstance(record.get("status"), str)
                    or record.get("status") not in {"running", "victory", "defeat"}
                    or not isinstance(record.get("action", {}), dict)
                ):
                    raise RenderError("A trace record has invalid timing, state, action, or status.")
                choice_groups(record)
                resolve_frame(root, record.get("frame"))
                records.append(record)
                previous = record["t"]
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise RenderError("Unable to read valid recording data from trace.jsonl.") from None
    if not records:
        raise RenderError("The recording trace is empty.")
    if any(record["status"] != "running" for record in records[:-1]):
        raise RenderError("A terminal mission result must be the final trace record.")
    return records


def timeline(records: list[dict], speed: float, final_hold: float = 3.0) -> list[tuple[dict, float]]:
    """Use actual elapsed wall-clock intervals, divided by the playback speed.

    The final frame is held for final_hold output seconds. Simultaneous events
    have no visible duration; the most recent event supplies the next interval.
    """
    if not _finite(speed, low=0.1, high=100) or not _finite(final_hold, low=0.1, high=30):
        raise RenderError("Playback speed or final hold is outside the supported range.")
    if not records:
        raise RenderError("The recording trace is empty.")
    if not all(isinstance(record, dict) and _finite(record.get("t")) for record in records):
        raise RenderError("A trace timestamp is invalid.")
    segments = []
    for index, record in enumerate(records):
        if not _finite(record.get("t")):
            raise RenderError("A trace timestamp is invalid.")
        duration = final_hold if index == len(records) - 1 else (records[index + 1]["t"] - record["t"]) / speed
        if not _finite(duration):
            raise RenderError("Trace timestamps must be monotonic.")
        if duration > 0:
            segments.append((record, duration))
    return segments


_FONTS: dict[tuple[int, bool], Any] = {}


def _font(size: int, mono: bool = False):
    key = (size, mono)
    if key not in _FONTS:
        candidates = (
            ["/System/Library/Fonts/SFNSMono.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
            if mono else
            ["/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
        )
        _FONTS[key] = next((ImageFont.truetype(path, size) for path in candidates if Path(path).is_file()), None)
        if _FONTS[key] is None:
            _FONTS[key] = ImageFont.load_default(size=size)
    return _FONTS[key]


def _text(draw, position, value, size=20, color=TEXT, mono=False, limit=160):
    draw.text(position, safe_text(value, limit), fill=color, font=_font(size, mono))


def _fit(draw, text, width, size=18, mono=False):
    value = safe_text(text)
    while value and draw.textlength(value, font=_font(size, mono)) > width:
        value = value[:-2].rstrip("…") + "…"
    return value


def _wrap(draw, text, width, size=20, max_lines=2):
    words = safe_text(text).split()
    lines = []
    line = ""
    for word in words:
        candidate = (line + " " + word).strip()
        if line and draw.textlength(candidate, font=_font(size)) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _fit(draw, lines[-1] + "…", width, size)
    return [_fit(draw, line, width, size) for line in lines]


def _resource(state, key):
    value = state.get(key)
    if _finite(value):
        return str(int(value)) if value == int(value) else f"{value:g}"
    return "—"


def _supply(state: dict) -> str:
    supply = state.get("supply")
    if isinstance(supply, dict):
        return f"{_resource(supply, 'used')}/{_resource(supply, 'available')}"
    return f"{_resource(state, 'supply_used')}/{_resource(state, 'supply_total')}"


def _model_order(action: dict) -> str:
    label = safe_text(action.get("label", "No model order recorded"))
    if action.get("accepted") is False or action.get("acceptedFalse") is True:
        return "Not accepted — " + label
    return label


def _objective(state: dict) -> str:
    # Only the explicit human-readable objective is shown; arbitrary state fields
    # and nested command payloads never enter the exported video.
    if isinstance(state.get("objective_summary"), str) and state["objective_summary"]:
        return safe_text(state["objective_summary"])
    progress = state.get("objective_progress")
    if isinstance(progress, str) and progress:
        return safe_text(progress)
    if isinstance(progress, dict):
        parts = []
        for key in ("supply_depots", "refineries", "gas", "barracks", "marines"):
            value = progress.get(key)
            if _finite(value):
                parts.append(f"{key.replace('_', ' ')}: {value:g}")
            elif isinstance(value, dict) and _finite(value.get("current")) and _finite(value.get("target")):
                parts.append(f"{key.replace('_', ' ')}: {value['current']:g}/{value['target']:g}")
        if parts:
            return "  ·  ".join(parts)
    for key in ("objective", "mission"):
        if isinstance(state.get(key), str) and state[key]:
            return safe_text(state[key])
    return "Mission objective recorded in the game"


def compose_frame(record: dict, game: Image.Image, *, speed: float = 4, test_only: bool = False) -> Image.Image:
    """Compose one video frame from a genuine game image and recorded telemetry."""
    if game.size != (640, 480):
        raise RenderError("Recording screenshots must be game-only 640×480 PNG frames.")
    groups = choice_groups(record)
    state = record["state"]
    decision = record.get("decision") or {}
    metadata = decision.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise RenderError("Recorded decision metadata must be an object.")
    canvas = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, WIDTH, 5), fill=GREEN)
    _text(draw, (32, 26), "JEV × STARCRAFT", 34)
    mission = safe_text(state.get('mission', 'StarCraft'), 32).upper()
    _text(draw, (34, 72), f"{mission}  /  SHAREWARE MISSION  /  PAUSED FOR DECISIONS", 15, MUTED, True)
    status = record.get("status", "running")
    status_text = {"running": "MISSION IN PROGRESS", "victory": "VICTORY RECORDED", "defeat": "DEFEAT RECORDED"}[status]
    status_color = RED if status == "defeat" else GREEN
    if test_only:
        status_text, status_color = "TEST — SYNTHETIC PREVIEW", GOLD
    _text(draw, (1024, 29), status_text, 21, status_color)
    model = decision.get("model")
    model_label = model if isinstance(model, str) and re.fullmatch(r"jev[a-zA-Z0-9._-]*", model) else "No model result"
    clock = record.get("t", 0)
    elapsed = f"{int(clock) // 60:02d}:{int(clock) % 60:02d}" if _finite(clock) else "—"
    _text(draw, (1024, 66), f"{model_label}   ·   {speed:g}× playback   ·   {elapsed} elapsed", 16, MUTED, True)

    # Exact 4:3 framing; no crop and no surrounding desktop capture.
    canvas.paste(game.convert("RGB").resize((960, 720), Image.Resampling.NEAREST), (32, 112))
    draw.rectangle((31, 111, 992, 832), outline=BORDER, width=2)
    if test_only:
        draw.rectangle((48, 128, 257, 175), fill=BACKGROUND)
        _text(draw, (64, 137), "TEST FRAME", 24, GOLD, True)
    draw.rounded_rectangle((1024, 112, 1568, 832), radius=12, fill=PANEL, outline=BORDER)

    latency = metadata.get("latency_ms")
    latency_text = f"{latency:.0f} ms" if _finite(latency) else "—"
    metrics = [("MINERALS", _resource(state, "minerals")), ("GAS", _resource(state, "gas")),
               ("SUPPLY", _supply(state)), ("API LATENCY", latency_text)]
    for index, (label, value) in enumerate(metrics):
        x = 1048 + index * 128
        _text(draw, (x, 132), label, 12, MUTED, True)
        _text(draw, (x, 154), value, 25, TEXT, True)
    draw.line((1048, 199, 1544, 199), fill=BORDER, width=1)
    _text(draw, (1048, 218), "ACTION PROBABILITIES", 15, GREEN, True)
    observed_frame = state.get("frame")
    frame_text = str(observed_frame) if type(observed_frame) is int and observed_frame >= 0 else "—"
    decision_caption = f"Last Jev decision · observed frame {frame_text}" if groups else f"Observed frame {frame_text} · no Jev decision yet"
    _text(draw, (1048, 242), decision_caption, 14, MUTED)
    _text(draw, (1048, 265), "Action choices, not a prediction of winning", 12, MUTED)

    shown = groups[:3]
    area_top, area_height = 301, 363
    if not shown:
        _text(draw, (1048, 314), "No model decision recorded", 22, MUTED)
        _text(draw, (1048, 352), "Probabilities appear after an API response.", 17, MUTED)
    for index, (name, answer) in enumerate(shown):
        group_height = area_height // len(shown)
        y = area_top + index * group_height
        heading = _fit(draw, name.replace("_", " ").upper(), 330, 16)
        _text(draw, (1048, y), heading, 16)
        _text(draw, (1400, y + 1), f"conf {answer['confidence']:.2f}", 14, MUTED, True)
        max_options = max(2, min(5, (group_height - 40) // 31))
        options = sorted(answer["probabilities"].items(), key=lambda item: (-item[1], item[0]))
        for option_index, (option, probability) in enumerate(options[:max_options]):
            bar_y = y + 29 + option_index * 31
            selected = option == answer["choice"]
            color = GREEN if selected else MUTED
            label = _fit(draw, option.replace("_", " "), 207, 15)
            _text(draw, (1048, bar_y - 2), label, 15, color)
            draw.rounded_rectangle((1267, bar_y, 1480, bar_y + 14), radius=3, fill=BORDER)
            if probability > 0:
                draw.rounded_rectangle((1267, bar_y, 1267 + max(1, 213 * probability), bar_y + 14), radius=3, fill=GREEN if selected else DIM_GREEN)
            _text(draw, (1491, bar_y - 2), f"{probability:.0%}", 14, color, True)
        if len(options) > max_options:
            _text(draw, (1048, y + group_height - 18), f"Top {max_options} shown · {len(options) - max_options} more options in trace", 12, MUTED)
    combat = state.get('combat')
    if len(shown) == 1 and isinstance(combat, dict):
        _text(draw, (1048, 523), "OBSERVED FORCE", 13, MUTED, True)
        _text(draw, (1048, 548), f"{_resource(combat, 'own_marines')} Marines  ·  {_resource(combat, 'own_firebats')} Firebats  ·  {_resource(combat, 'own_ghosts')} Ghosts", 18)
        _text(draw, (1048, 585), f"Visible hostile units: {_resource(combat, 'visible_enemy_units')}", 17, MUTED)
    if len(groups) > 3:
        _text(draw, (1048, 671), f"{len(groups) - 3} additional question(s) preserved in trace", 12, MUTED)

    draw.line((1048, 697, 1544, 697), fill=BORDER, width=1)
    calls = metadata.get("request_count")
    call_text = str(calls) if type(calls) is int and calls >= 0 else "—"
    _text(draw, (1048, 714), "MODEL ORDER", 13, GOLD, True)
    _text(draw, (1426, 715), f"calls {call_text}", 13, MUTED, True)
    action = record.get("action") or {}
    order = _model_order(action)
    for index, line in enumerate(_wrap(draw, order, 496, size=21, max_lines=2)):
        _text(draw, (1048, 744 + index * 27), line, 21)

    _text(draw, (32, 851), "OBJECTIVE", 12, MUTED, True)
    _text(draw, (145, 846), _fit(draw, _objective(state), 847, 18), 18)
    for index, label in enumerate(("STATE", "JEV", "ORDERS")):
        x = 1024 + index * 190
        draw.rounded_rectangle((x, 850, x + 156, 883), radius=5, outline=GREEN if index == 1 else BORDER)
        _text(draw, (x + 18, 857), label, 14, GREEN if index == 1 else MUTED, True)
        if index < 2:
            _text(draw, (x + 165, 855), "→", 18, MUTED)
    return canvas


def render(run_dir: str | os.PathLike[str], output: str | os.PathLike[str], *, speed: float = 4, fps: int = 30, ffmpeg: str = "ffmpeg") -> dict:
    if type(fps) is not int or not 1 <= fps <= 60:
        raise RenderError("Video frame rate must be an integer between 1 and 60.")
    root = Path(run_dir).resolve()
    records = load_trace(root)
    segments = timeline(records, speed)
    destination = Path(output).expanduser().resolve()
    if destination.suffix.lower() != ".mp4":
        raise RenderError("Choose an MP4 output filename.")
    executable = shutil.which(ffmpeg)
    if executable is None:
        raise RenderError("ffmpeg is required to export a video.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    duration = math.fsum(seconds for _, seconds in segments)
    with tempfile.TemporaryDirectory(prefix="tsai-sc-render-") as temporary:
        work = Path(temporary)
        manifest = []
        for index, (record, seconds) in enumerate(segments):
            path = resolve_frame(root, record["frame"])
            try:
                with Image.open(path) as game:
                    if game.format != "PNG":
                        raise RenderError("A recording frame is not a PNG image.")
                    image = compose_frame(record, game, speed=speed, test_only=record.get("test") is True)
                    filename = f"composed-{index:06d}.png"
                    image.save(work / filename)
            except (OSError, Image.DecompressionBombError):
                raise RenderError("Unable to read a valid game recording frame.") from None
            # Names are generated here, so trace paths cannot inject concat syntax.
            manifest.extend((f"file '{filename}'", f"duration {seconds:.9f}"))
        manifest.append(f"file 'composed-{len(segments) - 1:06d}.png'")
        (work / "frames.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
        rendered = work / "video.mp4"
        command = [
            executable, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "1", "-i", str(work / "frames.txt"),
            "-an", "-vf", f"fps={fps}", "-t", f"{duration:.9f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(rendered),
        ]
        try:
            process = subprocess.run(command, capture_output=True, check=False, timeout=max(120, duration * 10))
        except (OSError, subprocess.TimeoutExpired):
            raise RenderError("Video encoding failed or exceeded its time limit.") from None
        if process.returncode or not rendered.is_file() or not rendered.stat().st_size:
            raise RenderError("ffmpeg could not encode the recording.")
        # Replace only after successful encoding; an existing export survives errors.
        descriptor, temporary_output = tempfile.mkstemp(prefix=".tsai-sc-", suffix=".mp4", dir=destination.parent)
        os.close(descriptor)
        try:
            shutil.copyfile(rendered, temporary_output)
            os.replace(temporary_output, destination)
        finally:
            Path(temporary_output).unlink(missing_ok=True)
    return {"output": str(destination), "duration_seconds": duration, "records": len(records), "speed": speed, "fps": fps, "status": records[-1]["status"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--speed", default=4.0, type=float)
    parser.add_argument("--fps", default=30, type=int)
    args = parser.parse_args(argv)
    try:
        result = render(args.run_dir, args.output, speed=args.speed, fps=args.fps)
    except RenderError as error:
        parser.exit(1, f"Recording export failed: {error}\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
