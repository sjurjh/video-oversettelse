"""
Local video-to-Norwegian-voice pipeline for Windows.

Install:
    pip install openai pydub tqdm python-dotenv

Requirements:
    - ffmpeg and ffprobe must be installed in PATH or in tools\\ffmpeg\\bin
      next to this script.
    - OPENAI_API_KEY must be set in your environment or in a local .env file.

Example:
    python dub_to_norwegian.py "C:\\Videos\\my_video.mkv"
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

if TYPE_CHECKING:
    from openai import OpenAI


TARGET_LANGUAGE = "Norwegian Bokmål"
TTS_VOICE = "coral"
MAX_CHUNK_MB = 24
OUTPUT_FORMAT = "mp3"
FORCE = False

STT_MODEL = "whisper-1"
TRANSLATION_MODEL = "gpt-4o-mini"
TTS_MODEL = "gpt-4o-mini-tts"
TRANSLATION_BATCH_SIZE = 20
TRANSLATION_RETRY_ATTEMPTS = 3
MAX_REASONABLE_SPEEDUP = 1.25
EXTRACTED_AUDIO_BITRATE = "64k"
EXTRACTED_AUDIO_RATE = "16000"
FINAL_AUDIO_BITRATE = "192k"
FINAL_SAMPLE_RATE = 44100
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov"}
SCRIPT_DIR = Path(__file__).resolve().parent
TOOL_PATHS: Dict[str, str] = {}

# Future speaker support can map speaker labels to voices here.
VOICE_BY_SPEAKER = {
    "default": TTS_VOICE,
}

TTS_INSTRUCTIONS = (
    "Speak only Norwegian Bokmal, not English. "
    "Use a natural Norwegian narrator voice with clear pronunciation suitable for children."
)


Segment = Dict[str, Any]


def load_environment() -> None:
    def load_env_file(path: Path) -> None:
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None

    local_env = SCRIPT_DIR / ".env"
    if load_dotenv is not None:
        load_dotenv(local_env, override=False)
    load_env_file(local_env)

    key_files: List[Path] = []
    configured_key_file = os.environ.get("OPENAI_KEYS_FILE")
    if configured_key_file:
        key_files.append(Path(configured_key_file).expanduser())

    appdata = os.environ.get("APPDATA")
    if appdata:
        key_files.append(Path(appdata) / "OpenAIKeys" / "openai.env")

    key_files.append(local_env)
    for key_file in key_files:
        if key_file.exists():
            if load_dotenv is not None:
                load_dotenv(key_file, override=False)
            load_env_file(key_file)


def progress(iterable: Iterable[Any], desc: str) -> Iterable[Any]:
    try:
        from tqdm import tqdm
    except ImportError:
        logging.warning("tqdm is not installed; continuing without a progress bar.")
        return iterable
    return tqdm(iterable, desc=desc)


def setup_logging(output_dir: Path, extra_handler: Optional[logging.Handler] = None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "process.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    if extra_handler is not None:
        extra_handler.setFormatter(formatter)
        root.addHandler(extra_handler)


def resolve_tool(name: str) -> Optional[str]:
    if name in TOOL_PATHS:
        return TOOL_PATHS[name]

    found = shutil.which(name)
    if found:
        TOOL_PATHS[name] = found
        return found

    exe_name = f"{name}.exe" if not name.lower().endswith(".exe") else name
    candidates = [
        SCRIPT_DIR / "tools" / "ffmpeg" / "bin" / exe_name,
        SCRIPT_DIR / "ffmpeg" / "bin" / exe_name,
        Path("C:/ffmpeg/bin") / exe_name,
    ]

    ffmpeg_bin = os.environ.get("FFMPEG_BIN")
    if ffmpeg_bin:
        candidates.insert(0, Path(ffmpeg_bin) / exe_name)

    for candidate in candidates:
        if candidate.exists():
            TOOL_PATHS[name] = str(candidate)
            return str(candidate)

    return None


def run_command(command: List[str], description: str) -> subprocess.CompletedProcess:
    if command and command[0] in {"ffmpeg", "ffprobe"}:
        resolved = resolve_tool(command[0])
        if resolved:
            command = [resolved, *command[1:]]

    logging.info("%s", description)
    logging.debug("Command: %s", " ".join(command))

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        message = (
            f"{description} failed with exit code {result.returncode}.\n"
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        )
        logging.error(message)
        raise RuntimeError(message)

    if result.stderr.strip():
        logging.debug(result.stderr.strip())
    return result


def require_tool(name: str) -> None:
    if resolve_tool(name) is None:
        raise RuntimeError(
            f"Could not find '{name}'. Install FFmpeg for Windows and make sure "
            "ffmpeg.exe and ffprobe.exe are either in PATH, in C:\\ffmpeg\\bin, "
            "or in this project folder under tools\\ffmpeg\\bin. You can run "
            "install_ffmpeg.bat from this folder to install a local copy."
        )
    logging.info("Using %s: %s", name, resolve_tool(name))


def get_openai_client() -> OpenAI:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "Missing Python package 'openai'. Install dependencies with: "
            "pip install openai pydub tqdm python-dotenv"
        ) from exc

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        load_environment()
        api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        default_key_file = Path(os.environ.get("APPDATA", str(SCRIPT_DIR))) / "OpenAIKeys" / "openai.env"
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Set it in Windows environment variables "
            f"or place it in {default_key_file}."
        )
    return OpenAI(api_key=api_key)


def get_media_duration(path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = run_command(command, f"Reading duration for {path.name}")
    text = result.stdout.strip()
    try:
        return float(text)
    except ValueError as exc:
        raise RuntimeError(f"Could not read duration from ffprobe output: {text}") from exc


def extract_audio(video_path: Path, output_dir: Path, force: bool = FORCE) -> Path:
    audio_path = output_dir / f"audio.{OUTPUT_FORMAT}"
    if audio_path.exists() and not force:
        logging.info("Using existing extracted audio: %s", audio_path)
        return audio_path

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        EXTRACTED_AUDIO_RATE,
        "-b:a",
        EXTRACTED_AUDIO_BITRATE,
        str(audio_path),
    ]
    run_command(command, "Extracting compressed audio from video")
    return audio_path


def object_to_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "model_dump_json"):
        return json.loads(value.model_dump_json())
    raise TypeError(f"Cannot convert OpenAI response to dict: {type(value)!r}")


def transcribe_one_file(
    client: OpenAI,
    audio_path: Path,
    model: str,
    offset_seconds: float,
    fallback_duration: Optional[float] = None,
) -> List[Segment]:
    with audio_path.open("rb") as audio_file:
        try:
            response = client.audio.transcriptions.create(
                model=model,
                file=audio_file,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
        except TypeError:
            audio_file.seek(0)
            response = client.audio.transcriptions.create(
                model=model,
                file=audio_file,
                response_format="verbose_json",
            )
        except Exception as exc:
            if "timestamp" not in str(exc).lower():
                raise
            audio_file.seek(0)
            response = client.audio.transcriptions.create(
                model=model,
                file=audio_file,
                response_format="verbose_json",
            )

    data = object_to_dict(response)
    raw_segments = data.get("segments") or []

    if not raw_segments and data.get("text"):
        raw_segments = [
            {
                "start": 0.0,
                "end": fallback_duration or 0.0,
                "text": data["text"],
            }
        ]

    normalized: List[Segment] = []
    for item in raw_segments:
        start = float(item.get("start", 0.0)) + offset_seconds
        end = float(item.get("end", start)) + offset_seconds
        text = str(item.get("text", "")).strip()
        normalized.append(
            {
                "start": round(start, 3),
                "end": round(max(end, start), 3),
                "text": text,
            }
        )
    return normalized


def split_audio_into_chunks(
    audio_path: Path,
    output_dir: Path,
    max_chunk_mb: int,
    force: bool,
) -> List[Dict[str, Any]]:
    chunks_dir = output_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    duration = get_media_duration(audio_path)
    file_size = audio_path.stat().st_size
    max_bytes = max_chunk_mb * 1024 * 1024
    bytes_per_second = max(file_size / max(duration, 1.0), 1.0)
    chunk_seconds = int((max_bytes * 0.90) / bytes_per_second)
    chunk_seconds = max(30, min(chunk_seconds, 600))

    chunk_count = int(math.ceil(duration / chunk_seconds))
    logging.info(
        "Audio is %.1f MB, splitting into %s chunk(s) of about %s seconds",
        file_size / (1024 * 1024),
        chunk_count,
        chunk_seconds,
    )

    chunks: List[Dict[str, Any]] = []
    for index in range(chunk_count):
        start = index * chunk_seconds
        length = min(chunk_seconds, max(duration - start, 0.0))
        chunk_path = chunks_dir / f"chunk_{index:04d}.{OUTPUT_FORMAT}"

        if not chunk_path.exists() or force:
            command = [
                "ffmpeg",
                "-y",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{length:.3f}",
                "-i",
                str(audio_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                EXTRACTED_AUDIO_RATE,
                "-b:a",
                EXTRACTED_AUDIO_BITRATE,
                str(chunk_path),
            ]
            run_command(command, f"Creating audio chunk {index + 1}/{chunk_count}")

        chunks.append({"path": chunk_path, "offset": float(start), "duration": float(length)})

    return chunks


def transcribe_audio(
    audio_path: Path,
    output_dir: Path,
    client: OpenAI,
    max_chunk_mb: int = MAX_CHUNK_MB,
    force: bool = FORCE,
    model: str = STT_MODEL,
) -> List[Segment]:
    transcript_path = output_dir / "transcript_original.json"
    srt_path = output_dir / "transcript_original.srt"

    if transcript_path.exists() and not force:
        logging.info("Using existing original transcript: %s", transcript_path)
        return load_json(transcript_path)

    max_bytes = max_chunk_mb * 1024 * 1024
    audio_duration = get_media_duration(audio_path)

    if audio_path.stat().st_size <= max_bytes:
        logging.info("Transcribing audio as one file")
        segments = transcribe_one_file(
            client,
            audio_path,
            model=model,
            offset_seconds=0.0,
            fallback_duration=audio_duration,
        )
    else:
        chunks = split_audio_into_chunks(audio_path, output_dir, max_chunk_mb, force)
        segments = []
        for chunk in progress(chunks, desc="Transcribing chunks"):
            segments.extend(
                transcribe_one_file(
                    client,
                    chunk["path"],
                    model=model,
                    offset_seconds=chunk["offset"],
                    fallback_duration=chunk["duration"],
                )
            )

    segments.sort(key=lambda segment: (segment["start"], segment["end"]))
    for index, segment in enumerate(segments, start=1):
        segment["index"] = index

    save_json(transcript_path, segments)
    write_srt(segments, srt_path, text_key="text")
    logging.info("Saved original transcript: %s", transcript_path)
    return segments


def strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def call_translation_api(client: OpenAI, model: str, payload: Dict[str, Any]) -> Any:
    system_prompt = (
        f"You translate video transcript segments into {TARGET_LANGUAGE}. "
        "Make the Norwegian easy for children to understand, but do not change facts, "
        "meaning, names, numbers, order, or tone more than necessary. "
        "Keep each translation concise so it can fit the same time span. "
        "Do not add explanations. Return only valid JSON with this shape: "
        '{"segments":[{"index":1,"norwegian_text":"..."}]}.'
    )

    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    try:
        response = client.chat.completions.create(**kwargs)
    except TypeError:
        kwargs.pop("response_format", None)
        response = client.chat.completions.create(**kwargs)

    content = response.choices[0].message.content or ""
    return json.loads(strip_json_fences(content))


def translation_payload_for_segments(segments: List[Segment]) -> Dict[str, Any]:
    return {
        "segments": [
            {
                "index": int(segment["index"]),
                "start": segment["start"],
                "end": segment["end"],
                "duration": round(float(segment["end"]) - float(segment["start"]), 3),
                "text": segment.get("text", ""),
            }
            for segment in segments
        ]
    }


def normalize_translation_items(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        raw_segments = data
    elif isinstance(data, dict):
        raw_segments = data.get("segments")
        if raw_segments is None and "index" in data:
            raw_segments = [data]
    else:
        raw_segments = []

    if isinstance(raw_segments, dict):
        return [
            {"index": key, "norwegian_text": value}
            for key, value in raw_segments.items()
        ]

    if not isinstance(raw_segments, list):
        return []

    return [item for item in raw_segments if isinstance(item, dict)]


def extract_translations(data: Any) -> Dict[int, str]:
    translations: Dict[int, str] = {}
    for item in normalize_translation_items(data):
        try:
            index = int(item["index"])
        except (KeyError, TypeError, ValueError):
            logging.warning("Ignoring translation item without a valid index: %s", item)
            continue

        text = item.get("norwegian_text")
        if text is None:
            text = item.get("translated_text")
        if text is None:
            text = item.get("translation")
        if text is None:
            text = item.get("text")
        if text is None:
            logging.warning("Ignoring translation item %s without translated text", index)
            continue

        translations[index] = str(text).strip()

    return translations


def load_partial_translations(path: Path, original_segments: List[Segment]) -> Dict[int, str]:
    if not path.exists():
        return {}

    expected_text = {
        int(segment["index"]): str(segment.get("text", "")).strip()
        for segment in original_segments
    }

    try:
        data = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Could not read partial Norwegian transcript %s: %s", path, exc)
        return {}

    if isinstance(data, dict):
        if isinstance(data.get("segments"), list):
            records = data["segments"]
        else:
            records = [
                {"index": key, "norwegian_text": value}
                for key, value in data.items()
            ]
    elif isinstance(data, list):
        records = data
    else:
        logging.warning("Ignoring partial Norwegian transcript with unexpected shape: %s", path)
        return {}

    translations: Dict[int, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            index = int(record["index"])
        except (KeyError, TypeError, ValueError):
            continue

        if index not in expected_text:
            continue

        original_text = record.get("original_text")
        if original_text is not None and str(original_text).strip() != expected_text[index]:
            continue

        translated_text = record.get("norwegian_text")
        if translated_text is None:
            continue

        translations[index] = str(translated_text).strip()

    if translations:
        logging.info("Loaded %s partial Norwegian translation(s)", len(translations))
    return translations


def save_partial_translations(
    path: Path,
    original_segments: List[Segment],
    translated_by_index: Dict[int, str],
) -> None:
    segments_by_index = {int(segment["index"]): segment for segment in original_segments}
    records = []
    for index in sorted(translated_by_index):
        segment = segments_by_index.get(index)
        if segment is None:
            continue
        records.append(
            {
                "index": index,
                "original_text": str(segment.get("text", "")).strip(),
                "norwegian_text": translated_by_index[index],
            }
        )
    save_json(path, records)


def request_translations_for_segments(
    segments: List[Segment],
    client: OpenAI,
    model: str,
) -> Dict[int, str]:
    expected_indexes = {int(segment["index"]) for segment in segments}
    last_error: Optional[Exception] = None

    for attempt in range(1, TRANSLATION_RETRY_ATTEMPTS + 1):
        try:
            data = call_translation_api(
                client,
                model,
                translation_payload_for_segments(segments),
            )
            translations = extract_translations(data)
        except json.JSONDecodeError as exc:
            last_error = exc
            logging.warning(
                "Translation API returned invalid JSON on attempt %s/%s: %s",
                attempt,
                TRANSLATION_RETRY_ATTEMPTS,
                exc,
            )
            continue

        unexpected_indexes = sorted(set(translations) - expected_indexes)
        if unexpected_indexes:
            logging.warning(
                "Translation API returned unexpected segment index(es): %s",
                unexpected_indexes,
            )

        return {
            index: text
            for index, text in translations.items()
            if index in expected_indexes
        }

    raise RuntimeError(f"Translation API returned invalid JSON repeatedly: {last_error}")


def translate_segments(
    original_segments: List[Segment],
    output_dir: Path,
    client: OpenAI,
    force: bool = FORCE,
    target_language: str = TARGET_LANGUAGE,
    model: str = TRANSLATION_MODEL,
) -> List[Segment]:
    del target_language  # Kept in the signature so the target is easy to override later.

    transcript_path = output_dir / "transcript_no.json"
    partial_transcript_path = output_dir / "transcript_no.partial.json"
    srt_path = output_dir / "transcript_no.srt"

    if transcript_path.exists() and not force:
        logging.info("Using existing Norwegian transcript: %s", transcript_path)
        return load_json(transcript_path)

    translated_by_index: Dict[int, str] = {}
    if not force:
        translated_by_index.update(
            load_partial_translations(partial_transcript_path, original_segments)
        )

    batches = [
        original_segments[index : index + TRANSLATION_BATCH_SIZE]
        for index in range(0, len(original_segments), TRANSLATION_BATCH_SIZE)
    ]

    for batch in progress(batches, desc="Translating"):
        untranslated_batch = [
            segment
            for segment in batch
            if int(segment["index"]) not in translated_by_index
        ]
        if not untranslated_batch:
            continue

        translated_by_index.update(
            request_translations_for_segments(untranslated_batch, client, model)
        )

        missing = [
            int(segment["index"])
            for segment in batch
            if int(segment["index"]) not in translated_by_index
        ]
        if missing:
            logging.warning(
                "Translation API did not return segment(s) %s. Retrying one by one.",
                missing,
            )
            for segment in batch:
                index = int(segment["index"])
                if index not in missing:
                    continue

                for attempt in range(1, TRANSLATION_RETRY_ATTEMPTS + 1):
                    logging.info(
                        "Retrying translation for segment %s (%s/%s)",
                        index,
                        attempt,
                        TRANSLATION_RETRY_ATTEMPTS,
                    )
                    translated_by_index.update(
                        request_translations_for_segments([segment], client, model)
                    )
                    if index in translated_by_index:
                        break

        missing = [
            int(segment["index"])
            for segment in batch
            if int(segment["index"]) not in translated_by_index
        ]
        save_partial_translations(partial_transcript_path, original_segments, translated_by_index)
        if missing:
            raise RuntimeError(f"Translation API did not return segment(s): {missing}")

    norwegian_segments: List[Segment] = []
    for segment in original_segments:
        index = int(segment["index"])
        norwegian_segments.append(
            {
                "index": index,
                "start": float(segment["start"]),
                "end": float(segment["end"]),
                "original_text": str(segment.get("text", "")).strip(),
                "norwegian_text": translated_by_index[index],
            }
        )

    save_json(transcript_path, norwegian_segments)
    write_srt(norwegian_segments, srt_path, text_key="norwegian_text")
    if partial_transcript_path.exists():
        partial_transcript_path.unlink()
    logging.info("Saved Norwegian transcript: %s", transcript_path)
    return norwegian_segments


def choose_voice_for_segment(segment: Segment, default_voice: str) -> str:
    speaker = str(segment.get("speaker", "default"))
    return VOICE_BY_SPEAKER.get(speaker, default_voice)


def is_speakable_text(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    lower = cleaned.lower()
    noise_markers = {
        "[music]",
        "(music)",
        "[applause]",
        "(applause)",
        "[laughter]",
        "(laughter)",
        "[noise]",
        "(noise)",
        "...",
    }
    if lower in noise_markers:
        return False
    return any(char.isalnum() for char in cleaned)


def get_audio_segment_class():
    for tool_name in ("ffmpeg", "ffprobe"):
        tool_path = resolve_tool(tool_name)
        if not tool_path:
            continue
        tool_dir = str(Path(tool_path).parent)
        path_parts = os.environ.get("PATH", "").split(os.pathsep)
        if tool_dir.lower() not in {part.lower() for part in path_parts if part}:
            os.environ["PATH"] = tool_dir + os.pathsep + os.environ.get("PATH", "")

    try:
        from pydub import AudioSegment
    except ModuleNotFoundError as exc:
        missing_name = exc.name or ""
        if missing_name in {"audioop", "pyaudioop"}:
            raise RuntimeError(
                "Missing audio compatibility package for Python 3.13. "
                "Run install_dependencies.bat, or install it manually with: "
                "python -m pip install audioop-lts"
            ) from exc
        raise
    ffmpeg_path = resolve_tool("ffmpeg")
    if ffmpeg_path:
        AudioSegment.converter = ffmpeg_path
        AudioSegment.ffmpeg = ffmpeg_path
    ffprobe_path = resolve_tool("ffprobe")
    if ffprobe_path and hasattr(AudioSegment, "ffprobe"):
        AudioSegment.ffprobe = ffprobe_path
    return AudioSegment


def synthesize_one_segment(
    client: OpenAI,
    text: str,
    output_path: Path,
    model: str,
    voice: str,
    output_format: str,
) -> None:
    kwargs: Dict[str, Any] = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": output_format,
    }

    if model.startswith("gpt-4o"):
        kwargs["instructions"] = TTS_INSTRUCTIONS

    try:
        with client.audio.speech.with_streaming_response.create(**kwargs) as response:
            response.stream_to_file(output_path)
    except TypeError:
        kwargs.pop("instructions", None)
        with client.audio.speech.with_streaming_response.create(**kwargs) as response:
            response.stream_to_file(output_path)
    except AttributeError:
        kwargs.pop("instructions", None)
        response = client.audio.speech.create(**kwargs)
        response.stream_to_file(output_path)


def synthesize_segments(
    norwegian_segments: List[Segment],
    output_dir: Path,
    client: Optional[OpenAI],
    force_tts: bool = False,
    output_format: str = OUTPUT_FORMAT,
    model: str = TTS_MODEL,
    voice: str = TTS_VOICE,
) -> List[Segment]:
    segments_dir = output_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    result: List[Segment] = []
    for segment in progress(norwegian_segments, desc="Creating Norwegian speech"):
        segment_copy = dict(segment)
        text = str(segment_copy.get("norwegian_text", "")).strip()
        segment_index = int(segment_copy["index"])
        segment_audio = segments_dir / f"segment_{segment_index:04d}.{output_format}"

        if not is_speakable_text(text):
            logging.info("Skipping empty/noise segment %s", segment_index)
            segment_copy["audio_path"] = None
            result.append(segment_copy)
            continue

        if segment_audio.exists() and not force_tts:
            logging.info("Using existing TTS segment: %s", segment_audio.name)
        else:
            if client is None:
                client = get_openai_client()
            selected_voice = choose_voice_for_segment(segment_copy, voice)
            logging.info("Synthesizing segment %s with voice %s", segment_index, selected_voice)
            synthesize_one_segment(
                client=client,
                text=text,
                output_path=segment_audio,
                model=model,
                voice=selected_voice,
                output_format=output_format,
            )

        segment_copy["audio_path"] = str(segment_audio)
        result.append(segment_copy)

    return result


def load_audio_segment(path: Path):
    AudioSegment = get_audio_segment_class()
    return AudioSegment.from_file(path)


def fit_tts_to_segment_duration(
    audio_path: Path,
    fitted_path: Path,
    desired_seconds: float,
    force: bool,
) -> Path:
    AudioSegment = get_audio_segment_class()

    if fitted_path.exists() and not force:
        return fitted_path

    audio = load_audio_segment(audio_path)
    desired_ms = max(1, int(round(desired_seconds * 1000)))
    actual_ms = len(audio)

    if actual_ms <= desired_ms:
        silence = AudioSegment.silent(duration=desired_ms - actual_ms, frame_rate=FINAL_SAMPLE_RATE)
        fitted = audio + silence
        fitted.export(fitted_path, format="wav")
        return fitted_path

    ratio = actual_ms / desired_ms
    if ratio <= MAX_REASONABLE_SPEEDUP:
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(audio_path),
            "-filter:a",
            f"atempo={ratio:.5f}",
            "-ac",
            "2",
            "-ar",
            str(FINAL_SAMPLE_RATE),
            str(fitted_path),
        ]
        run_command(command, f"Speeding up {audio_path.name} by {ratio:.2f}x")

        fitted = load_audio_segment(fitted_path)
        if len(fitted) > desired_ms:
            fitted = fitted[:desired_ms]
        elif len(fitted) < desired_ms:
            fitted += AudioSegment.silent(
                duration=desired_ms - len(fitted), frame_rate=FINAL_SAMPLE_RATE
            )
        fitted.export(fitted_path, format="wav")
        return fitted_path

    logging.warning(
        "%s is much longer than its slot (%.2fs vs %.2fs). Leaving it unchanged.",
        audio_path.name,
        actual_ms / 1000,
        desired_seconds,
    )
    audio.export(fitted_path, format="wav")
    return fitted_path


def build_synced_audio(
    norwegian_segments: List[Segment],
    output_dir: Path,
    video_duration: float,
    force: bool = FORCE,
) -> Dict[str, Path]:
    AudioSegment = get_audio_segment_class()

    wav_path = output_dir / "norwegian_voice.wav"
    mp3_path = output_dir / "norwegian_voice.mp3"

    if wav_path.exists() and mp3_path.exists() and not force:
        logging.info("Using existing final audio files")
        return {"wav": wav_path, "mp3": mp3_path}

    fitted_dir = output_dir / "fitted_segments"
    fitted_dir.mkdir(parents=True, exist_ok=True)

    total_ms = max(1, int(math.ceil(video_duration * 1000)))
    base = AudioSegment.silent(duration=total_ms, frame_rate=FINAL_SAMPLE_RATE).set_channels(2)

    for segment in progress(norwegian_segments, desc="Syncing speech"):
        audio_value = segment.get("audio_path")
        if not audio_value:
            continue

        audio_path = Path(str(audio_value))
        if not audio_path.exists():
            logging.warning("Missing segment audio, skipping: %s", audio_path)
            continue

        start = float(segment["start"])
        end = float(segment["end"])
        desired_seconds = max(0.05, end - start)
        fitted_path = fitted_dir / f"fitted_{int(segment['index']):04d}.wav"

        fitted_path = fit_tts_to_segment_duration(
            audio_path=audio_path,
            fitted_path=fitted_path,
            desired_seconds=desired_seconds,
            force=force,
        )

        fitted_audio = load_audio_segment(fitted_path).set_frame_rate(FINAL_SAMPLE_RATE).set_channels(2)
        start_ms = max(0, int(round(start * 1000)))
        base = base.overlay(fitted_audio, position=start_ms)

    if len(base) > total_ms:
        base = base[:total_ms]

    logging.info("Exporting final WAV: %s", wav_path)
    base.export(wav_path, format="wav")

    logging.info("Exporting final MP3: %s", mp3_path)
    base.export(mp3_path, format="mp3", bitrate=FINAL_AUDIO_BITRATE)

    return {"wav": wav_path, "mp3": mp3_path}


def has_audio_stream(path: Path) -> bool:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ],
        f"Checking audio streams for {path.name}",
    )
    return bool(result.stdout.strip())


def mux_video_with_norwegian_audio(
    video_path: Path,
    norwegian_mp3: Path,
    output_dir: Path,
    force: bool = FORCE,
) -> Path:
    output_suffix = ".mp4" if video_path.suffix.lower() in {".mp4", ".mov"} else ".mkv"
    output_path = output_dir / f"{video_path.stem}_with_norwegian_audio{output_suffix}"

    if output_path.exists() and not force:
        logging.info("Using existing muxed video: %s", output_path)
        return output_path

    original_has_audio = has_audio_stream(video_path)
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(norwegian_mp3),
        "-map",
        "0:v:0",
    ]

    if original_has_audio:
        command.extend(["-map", "0:a:0"])
    command.extend(["-map", "1:a:0", "-c:v", "copy"])

    norwegian_audio_index = 1 if original_has_audio else 0
    if original_has_audio:
        command.extend(
            [
                "-c:a:0",
                "copy",
                "-metadata:s:a:0",
                "language=eng",
                "-metadata:s:a:0",
                "title=Original audio",
                "-disposition:a:0",
                "default",
            ]
        )

    command.extend(
        [
            f"-c:a:{norwegian_audio_index}",
            "aac",
            f"-b:a:{norwegian_audio_index}",
            FINAL_AUDIO_BITRATE,
            f"-metadata:s:a:{norwegian_audio_index}",
            "language=nor",
            f"-metadata:s:a:{norwegian_audio_index}",
            "title=Norwegian AI voice",
            f"-disposition:a:{norwegian_audio_index}",
            "0",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )

    run_command(command, "Adding Norwegian audio track to video")
    return output_path


def format_srt_time(seconds: float) -> str:
    milliseconds_total = int(round(max(seconds, 0.0) * 1000))
    hours, remainder = divmod(milliseconds_total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(segments: List[Segment], path: Path, text_key: str = "text") -> None:
    lines: List[str] = []
    for fallback_index, segment in enumerate(segments, start=1):
        index = int(segment.get("index", fallback_index))
        start = format_srt_time(float(segment.get("start", 0.0)))
        end = format_srt_time(float(segment.get("end", segment.get("start", 0.0))))
        text = str(segment.get(text_key, "")).strip()
        text = re.sub(r"\s+", " ", text)

        lines.append(str(index))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_video_path(video_path: Path) -> Path:
    resolved = video_path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Video file does not exist: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"Path is not a file: {resolved}")
    if resolved.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_VIDEO_EXTENSIONS))
        raise ValueError(f"Unsupported video format '{resolved.suffix}'. Supported: {supported}")
    return resolved


def output_dir_for_video(video_path: Path) -> Path:
    return video_path.parent / f"{video_path.stem}_norwegian_voice"


def vlc_command(video_path: Path, norwegian_mp3: Path) -> str:
    return f'vlc "{video_path}" --input-slave="{norwegian_mp3}"'


def print_vlc_instructions(
    video_path: Path,
    norwegian_mp3: Path,
    muxed_video: Optional[Path] = None,
) -> None:
    command = vlc_command(video_path, norwegian_mp3)
    print("\nDone.")
    if muxed_video is not None:
        print("\nVideo with extra Norwegian audio track:")
        print(str(muxed_video))
    print("\nVLC command:")
    print(command)
    print("\nManual VLC method:")
    print("VLC -> Media -> Open Multiple Files -> Add video -> Show more options ->")
    print("Play another media synchronously -> Browse norsk lydfil -> Play")


def resolve_output_dir(video_path: Path, output_dir: Optional[str]) -> Path:
    if output_dir and output_dir.strip():
        return Path(output_dir.strip().strip('"')).expanduser().resolve()
    return output_dir_for_video(video_path)


def run_pipeline(
    video_input: str,
    args: argparse.Namespace,
    output_dir_input: Optional[str] = None,
    extra_log_handler: Optional[logging.Handler] = None,
    print_instructions: bool = True,
) -> Dict[str, Path]:
    video_path = validate_video_path(Path(video_input.strip().strip('"')))
    output_dir = resolve_output_dir(video_path, output_dir_input)
    setup_logging(output_dir, extra_handler=extra_log_handler)

    logging.info("Video: %s", video_path)
    logging.info("Output folder: %s", output_dir)

    require_tool("ffmpeg")
    require_tool("ffprobe")

    transcript_no_path = output_dir / "transcript_no.json"
    client: Optional[OpenAI] = None

    video_duration = get_media_duration(video_path)
    logging.info("Video duration: %.2f seconds", video_duration)

    if transcript_no_path.exists() and not args.force:
        logging.info("Skipping transcription and translation because transcript_no.json exists")
        norwegian_segments = load_json(transcript_no_path)
    else:
        client = get_openai_client()
        audio_path = extract_audio(video_path, output_dir, force=args.force)
        original_segments = transcribe_audio(
            audio_path=audio_path,
            output_dir=output_dir,
            client=client,
            max_chunk_mb=args.max_chunk_mb,
            force=args.force,
            model=args.stt_model,
        )
        norwegian_segments = translate_segments(
            original_segments=original_segments,
            output_dir=output_dir,
            client=client,
            force=args.force,
            model=args.translation_model,
        )

    norwegian_segments = synthesize_segments(
        norwegian_segments=norwegian_segments,
        output_dir=output_dir,
        client=client,
        force_tts=args.force_tts,
        output_format=OUTPUT_FORMAT,
        model=args.tts_model,
        voice=args.tts_voice,
    )

    final_audio = build_synced_audio(
        norwegian_segments=norwegian_segments,
        output_dir=output_dir,
        video_duration=video_duration,
        force=args.force or args.force_tts,
    )
    muxed_video = mux_video_with_norwegian_audio(
        video_path=video_path,
        norwegian_mp3=final_audio["mp3"],
        output_dir=output_dir,
        force=args.force or args.force_tts,
    )
    final_audio["muxed_video"] = muxed_video

    if print_instructions:
        print_vlc_instructions(video_path, final_audio["mp3"], muxed_video=muxed_video)
    else:
        logging.info("VLC command: %s", vlc_command(video_path, final_audio["mp3"]))
        logging.info("Video with extra Norwegian audio track: %s", muxed_video)

    return final_audio


class QueueLogHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put(self.format(record))
        except Exception:
            self.handleError(record)


def launch_gui(default_args: argparse.Namespace) -> None:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except Exception as exc:
        raise RuntimeError(
            "Could not start the UI. Tkinter is normally included with Python on Windows."
        ) from exc

    root = tk.Tk()
    root.title("Norsk tale til video")
    root.geometry("820x560")
    root.minsize(760, 480)

    log_queue: queue.Queue = queue.Queue()
    worker_thread: Optional[threading.Thread] = None

    video_var = tk.StringVar(value=default_args.video or "")
    output_var = tk.StringVar(value=default_args.output_dir or "")
    force_var = tk.BooleanVar(value=default_args.force)
    force_tts_var = tk.BooleanVar(value=default_args.force_tts)
    max_chunk_var = tk.StringVar(value=str(default_args.max_chunk_mb))
    voice_var = tk.StringVar(value=default_args.tts_voice)
    status_var = tk.StringVar(value="Klar")

    def set_default_output_from_video() -> None:
        raw_path = video_var.get().strip().strip('"')
        if raw_path and not output_var.get().strip():
            try:
                output_var.set(str(output_dir_for_video(Path(raw_path).expanduser().resolve())))
            except Exception:
                pass

    def browse_video() -> None:
        selected = filedialog.askopenfilename(
            title="Velg video",
            filetypes=[
                ("Video files", "*.mp4 *.mkv *.webm *.mov"),
                ("All files", "*.*"),
            ],
        )
        if selected:
            video_var.set(selected)
            set_default_output_from_video()

    def browse_output() -> None:
        selected = filedialog.askdirectory(title="Velg output-mappe")
        if selected:
            output_var.set(selected)

    def append_log(message: str) -> None:
        log_text.configure(state="normal")
        log_text.insert("end", message + "\n")
        log_text.see("end")
        log_text.configure(state="disabled")

    def build_args_for_run() -> argparse.Namespace:
        try:
            max_chunk_mb = int(max_chunk_var.get().strip())
        except ValueError as exc:
            raise ValueError("Max chunk MB må være et heltall.") from exc

        if max_chunk_mb < 1:
            raise ValueError("Max chunk MB må være minst 1.")

        return argparse.Namespace(
            video=video_var.get().strip(),
            output_dir=output_var.get().strip(),
            force=force_var.get(),
            force_tts=force_tts_var.get(),
            max_chunk_mb=max_chunk_mb,
            stt_model=default_args.stt_model,
            translation_model=default_args.translation_model,
            tts_model=default_args.tts_model,
            tts_voice=voice_var.get().strip() or TTS_VOICE,
            ui=True,
            no_ui=False,
        )

    def worker(run_args: argparse.Namespace) -> None:
        handler = QueueLogHandler(log_queue)
        try:
            final_audio = run_pipeline(
                video_input=run_args.video,
                args=run_args,
                output_dir_input=run_args.output_dir,
                extra_log_handler=handler,
                print_instructions=False,
            )
            done_payload = {
                "mp3": str(final_audio["mp3"]),
                "muxed_video": str(final_audio.get("muxed_video", "")),
            }
            log_queue.put("__DONE__" + json.dumps(done_payload, ensure_ascii=False))
        except Exception as exc:
            logging.exception("Failed: %s", exc)
            log_queue.put(f"__ERROR__{exc}")

    def start() -> None:
        nonlocal worker_thread
        if worker_thread and worker_thread.is_alive():
            return

        try:
            run_args = build_args_for_run()
            if not run_args.video:
                raise ValueError("Velg en videofil først.")
            set_default_output_from_video()
            if not run_args.output_dir:
                run_args.output_dir = output_var.get().strip()
        except Exception as exc:
            messagebox.showerror("Kan ikke starte", str(exc))
            return

        log_text.configure(state="normal")
        log_text.delete("1.0", "end")
        log_text.configure(state="disabled")

        start_button.configure(state="disabled")
        status_var.set("Kjører...")
        append_log("Starter prosess. Dette kan ta en stund for lange videoer.")
        worker_thread = threading.Thread(target=worker, args=(run_args,), daemon=True)
        worker_thread.start()

    def pump_log_queue() -> None:
        nonlocal worker_thread
        try:
            while True:
                message = log_queue.get_nowait()
                if isinstance(message, str) and message.startswith("__DONE__"):
                    done_raw = message.replace("__DONE__", "", 1)
                    try:
                        done_data = json.loads(done_raw)
                    except json.JSONDecodeError:
                        done_data = {"mp3": done_raw, "muxed_video": ""}
                    mp3_path = str(done_data.get("mp3", ""))
                    muxed_video_path = str(done_data.get("muxed_video", ""))
                    status_var.set("Ferdig")
                    start_button.configure(state="normal")
                    append_log("")
                    if muxed_video_path:
                        append_log(f"Ferdig. Video med norsk lydspor: {muxed_video_path}")
                        append_log(f"Norsk lydfil: {mp3_path}")
                        messagebox.showinfo(
                            "Ferdig",
                            "Video med norsk lydspor er klar:\n"
                            f"{muxed_video_path}\n\n"
                            "Separat norsk lydfil:\n"
                            f"{mp3_path}",
                        )
                    else:
                        append_log(f"Ferdig. Norsk lydfil: {mp3_path}")
                        messagebox.showinfo("Ferdig", f"Norsk lydfil er klar:\n{mp3_path}")
                elif isinstance(message, str) and message.startswith("__ERROR__"):
                    error = message.replace("__ERROR__", "", 1)
                    status_var.set("Feil")
                    start_button.configure(state="normal")
                    append_log("")
                    append_log(f"Feil: {error}")
                    messagebox.showerror("Feil", error)
                else:
                    append_log(str(message))
        except queue.Empty:
            pass
        root.after(150, pump_log_queue)

    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    frame = ttk.Frame(root, padding=16)
    frame.grid(row=0, column=0, sticky="nsew")
    frame.columnconfigure(1, weight=1)
    frame.rowconfigure(7, weight=1)

    ttk.Label(frame, text="Videofil").grid(row=0, column=0, sticky="w", pady=(0, 6))
    ttk.Entry(frame, textvariable=video_var).grid(row=0, column=1, sticky="ew", padx=(10, 8), pady=(0, 6))
    ttk.Button(frame, text="Velg...", command=browse_video).grid(row=0, column=2, sticky="ew", pady=(0, 6))

    ttk.Label(frame, text="Output-mappe").grid(row=1, column=0, sticky="w", pady=(0, 6))
    ttk.Entry(frame, textvariable=output_var).grid(row=1, column=1, sticky="ew", padx=(10, 8), pady=(0, 6))
    ttk.Button(frame, text="Velg...", command=browse_output).grid(row=1, column=2, sticky="ew", pady=(0, 6))

    ttk.Label(frame, text="TTS-stemme").grid(row=2, column=0, sticky="w", pady=(0, 6))
    ttk.Entry(frame, textvariable=voice_var, width=20).grid(row=2, column=1, sticky="w", padx=(10, 8), pady=(0, 6))

    ttk.Label(frame, text="Max chunk MB").grid(row=3, column=0, sticky="w", pady=(0, 6))
    ttk.Entry(frame, textvariable=max_chunk_var, width=10).grid(row=3, column=1, sticky="w", padx=(10, 8), pady=(0, 6))

    ttk.Checkbutton(frame, text="Lag transkripsjon/oversettelse på nytt", variable=force_var).grid(
        row=4, column=1, sticky="w", padx=(10, 8), pady=(4, 2)
    )
    ttk.Checkbutton(frame, text="Lag TTS-segmenter på nytt", variable=force_tts_var).grid(
        row=5, column=1, sticky="w", padx=(10, 8), pady=(0, 8)
    )

    start_button = ttk.Button(frame, text="Start", command=start)
    start_button.grid(row=6, column=0, sticky="w", pady=(4, 10))
    ttk.Label(frame, textvariable=status_var).grid(row=6, column=1, sticky="w", padx=(10, 8), pady=(4, 10))

    log_text = tk.Text(frame, height=16, wrap="word", state="disabled")
    log_text.grid(row=7, column=0, columnspan=3, sticky="nsew")

    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=log_text.yview)
    scrollbar.grid(row=7, column=3, sticky="ns")
    log_text.configure(yscrollcommand=scrollbar.set)

    if video_var.get().strip():
        set_default_output_from_video()

    pump_log_queue()
    root.mainloop()


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a separate Norwegian AI voice track for a local video file."
    )
    parser.add_argument("video", nargs="?", help="Path to local video file")
    parser.add_argument("--ui", action="store_true", help="Open a small Windows UI")
    parser.add_argument("--no-ui", action="store_true", help="Use terminal prompt when no video path is given")
    parser.add_argument("--output-dir", help="Folder where output files should be saved")
    parser.add_argument("--force", action="store_true", help="Recreate audio extraction and transcripts")
    parser.add_argument("--force-tts", action="store_true", help="Recreate per-segment TTS audio")
    parser.add_argument("--max-chunk-mb", type=int, default=MAX_CHUNK_MB, help="Max audio chunk size for STT")
    parser.add_argument("--stt-model", default=STT_MODEL, help="OpenAI speech-to-text model")
    parser.add_argument("--translation-model", default=TRANSLATION_MODEL, help="OpenAI translation model")
    parser.add_argument("--tts-model", default=TTS_MODEL, help="OpenAI TTS model")
    parser.add_argument("--tts-voice", default=TTS_VOICE, help="OpenAI TTS voice")
    return parser.parse_args(argv)


def main() -> None:
    load_environment()
    args = parse_args()

    if args.ui or (not args.video and not args.no_ui):
        launch_gui(args)
        return

    video_input = args.video or input("Sti til videofil: ").strip().strip('"')
    if not video_input:
        raise ValueError("No video file path provided.")

    run_pipeline(
        video_input=video_input,
        args=args,
        output_dir_input=args.output_dir,
        print_instructions=True,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        logging.exception("Failed: %s", exc)
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
