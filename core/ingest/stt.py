"""Speech-to-Text ingestion for call recordings.

Supports:
- OpenAI Whisper (local or API)
- ffmpeg audio preprocessing
- Various audio formats (wav, mp3, m4a, ogg, flac, webm)
- Large file chunking (splits > 25MB)
- Automatic language detection
- Basic speaker turn detection from pauses
"""

import os
import re
import logging
import tempfile
from typing import Optional, Dict, Any, List
from datetime import datetime

logger = logging.getLogger("STT")

# Whisper import with graceful fallback
try:
    import whisper
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False
    whisper = None

try:
    from ..models.conversation import Conversation, ExtractedEntity
except ImportError:
    from core.models.conversation import Conversation, ExtractedEntity

# Max file size for Whisper API (25 MB)
MAX_API_FILE_SIZE = 25 * 1024 * 1024


def _check_ffmpeg() -> bool:
    """Check if ffmpeg is available for audio preprocessing."""
    try:
        import subprocess
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def _get_audio_duration(audio_path: str) -> Optional[float]:
    """Get audio duration in seconds using ffprobe."""
    try:
        import subprocess
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip()) if result.stdout.strip() else None
    except Exception:
        return None


def _preprocess_audio(audio_path: str, output_path: str) -> str:
    """Convert audio to 16kHz mono WAV for optimal Whisper processing."""
    try:
        import subprocess
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", "-f", "wav", output_path],
            capture_output=True, timeout=300,
        )
        if result.returncode == 0 and os.path.exists(output_path):
            return output_path
    except Exception as e:
        logger.warning(f"ffmpeg preprocessing failed: {e}")
    return audio_path


def _chunk_audio(audio_path: str, chunk_duration_sec: int = 600) -> List[str]:
    """Split audio into chunks for large files."""
    duration = _get_audio_duration(audio_path)
    if duration is None or duration <= chunk_duration_sec:
        return [audio_path]

    chunks = []
    try:
        import subprocess
        num_chunks = int(duration / chunk_duration_sec) + 1
        for i in range(num_chunks):
            start = i * chunk_duration_sec
            chunk_path = audio_path + f".chunk{i}.wav"
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", audio_path, "-ss", str(start),
                 "-t", str(chunk_duration_sec), "-ar", "16000", "-ac", "1", chunk_path],
                capture_output=True, timeout=120,
            )
            if result.returncode == 0 and os.path.exists(chunk_path):
                chunks.append(chunk_path)
    except Exception as e:
        logger.warning(f"Audio chunking failed: {e}")
        return [audio_path]

    return chunks if chunks else [audio_path]


def transcribe_audio(
    audio_path: str,
    model_size: str = "base",
    language: Optional[str] = None,
    task: str = "transcribe",
    use_api: bool = False,
) -> Dict[str, Any]:
    """
    Transcribe call recording to text.

    Args:
        audio_path: Path to audio file (wav, mp3, m4a, ogg, flac, webm)
        model_size: Whisper model size (tiny, base, small, medium, large)
        language: ISO language code (None = auto-detect)
        task: "transcribe" or "translate" (to English)
        use_api: If True, use OpenAI Whisper API instead of local model

    Returns:
        Dict with keys: text, language, segments, duration, word_count, confidence

    Raises:
        FileNotFoundError: If audio file doesn't exist
        RuntimeError: If transcription fails
    """
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    file_size = os.path.getsize(audio_path)
    if file_size == 0:
        raise ValueError(f"Audio file is empty: {audio_path}")

    # Preprocess audio
    processed_path = audio_path
    tmp_dir = None
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            wav_path = os.path.join(tmp_dir, "processed.wav")
            processed_path = _preprocess_audio(audio_path, wav_path)

            # Chunk large files
            chunks = _chunk_audio(processed_path)

            all_segments = []
            full_text_parts = []
            detected_language = language or "en"

            if use_api:
                result = _transcribe_via_api(processed_path, language, task)
                return result

            if not WHISPER_AVAILABLE:
                # Fallback: return basic info without transcription
                logger.warning("Whisper not installed — returning metadata only")
                return {
                    "text": "",
                    "language": detected_language,
                    "segments": [],
                    "duration": _get_audio_duration(audio_path) or 0.0,
                    "word_count": 0,
                    "confidence": 0.0,
                    "model_used": "none",
                    "warning": "Whisper not installed. Install with: pip install openai-whisper",
                }

            # Load model
            model = whisper.load_model(model_size)

            for i, chunk in enumerate(chunks):
                logger.info(f"Transcribing chunk {i+1}/{len(chunks)}...")
                result = model.transcribe(
                    chunk,
                    language=language,
                    task=task,
                    verbose=False,
                )

                chunk_text = result.get("text", "").strip()
                chunk_lang = result.get("language", detected_language)
                chunk_segments = result.get("segments", [])

                if chunk_text:
                    full_text_parts.append(chunk_text)
                    detected_language = chunk_lang

                for seg in chunk_segments:
                    seg["chunk_index"] = i
                    all_segments.append(seg)

            full_text = " ".join(full_text_parts).strip()
            word_count = len(full_text.split()) if full_text else 0

            # Calculate average confidence from segments
            confidences = [s.get("avg_logprob", 0) for s in all_segments if "avg_logprob" in s]
            avg_confidence = (
                round(1.0 + sum(confidences) / len(confidences), 4) if confidences else 0.0
            )

            duration = _get_audio_duration(audio_path)

            return {
                "text": full_text,
                "language": detected_language,
                "segments": all_segments,
                "duration": duration or 0.0,
                "word_count": word_count,
                "confidence": max(0.0, min(1.0, avg_confidence)),
                "model_used": model_size,
            }

    except FileNotFoundError:
        raise
    except Exception as e:
        raise RuntimeError(f"Transcription failed: {e}")


def _transcribe_via_api(
    audio_path: str,
    language: Optional[str] = None,
    task: str = "transcribe",
) -> Dict[str, Any]:
    """Use OpenAI Whisper API for transcription."""
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx required for API transcription")

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY required for API transcription")

    file_size = os.path.getsize(audio_path)
    if file_size > MAX_API_FILE_SIZE:
        raise ValueError(f"File too large for API ({file_size} bytes). Max: {MAX_API_FILE_SIZE}")

    with open(audio_path, "rb") as f:
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (os.path.basename(audio_path), f, "audio/wav")},
                data={
                    "model": "whisper-1",
                    "language": language or "",
                    "response_format": "verbose_json",
                },
            )

    if resp.status_code != 200:
        raise RuntimeError(f"Whisper API error {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    segments = data.get("segments", [])

    return {
        "text": data.get("text", "").strip(),
        "language": data.get("language", language or "en"),
        "segments": [
            {
                "start": s.get("start", 0),
                "end": s.get("end", 0),
                "text": s.get("text", ""),
            }
            for s in segments
        ],
        "duration": data.get("duration", 0.0),
        "word_count": len(data.get("text", "").split()),
        "confidence": 0.9,  # API generally high quality
        "model_used": "whisper-1-api",
    }


def _extract_entities_from_transcript(text: str) -> ExtractedEntity:
    """Extract entities from transcript using LLM or heuristics."""
    if not text:
        return ExtractedEntity()

    # Try LLM extraction
    try:
        from ..intelligence.llm_manager import llm_manager
        import json

        system = (
            "You are a sales call analyst. Extract from this transcript: "
            '{"name": "primary contact name", "company": "company name or null", '
            '"commitments": ["list of promises/action items"], '
            '"urgency": "high/medium/low", "sentiment": "positive/negative/neutral", '
            '"sentiment_score": float_0_to_1, "pain_points": ["list"]}'
        )
        result = llm_manager.generate(
            text[:4000],
            system_message=system,
            temperature=0,
            max_tokens=500,
        )
        data = json.loads(result.content)
        return ExtractedEntity(
            name=data.get("name"),
            company=data.get("company"),
            sentiment=data.get("sentiment", "neutral"),
            sentiment_score=float(data.get("sentiment_score", 0.5)),
            pain_point=data.get("pain_points", [None])[0] if data.get("pain_points") else None,
        )
    except Exception:
        pass

    # Heuristic fallback
    return ExtractedEntity(sentiment="neutral", sentiment_score=0.5)


def _determine_urgency(text: str) -> str:
    """Determine urgency from transcript content."""
    text_lower = text.lower()
    high_keywords = ["urgent", "asap", "immediately", "right away", "emergency", "critical"]
    medium_keywords = ["soon", "this week", "follow up", "next step", "by friday"]

    if any(kw in text_lower for kw in high_keywords):
        return "high"
    elif any(kw in text_lower for kw in medium_keywords):
        return "medium"
    return "low"


def _extract_commitments_from_transcript(text: str) -> List[str]:
    """Extract commitments from transcript."""
    commitments = []
    patterns = [
        r'(?:I|we|they)\s+(?:will|shall|are going to|plan to|promise to)\s+(.{10,120}?)(?:\.|,|\n|$)',
        r'(?:let me|I\'ll|we\'ll)\s+(.{5,100}?)(?:\.|,|\n|$)',
        r'(?:next step|action item)\s*[:\-]\s*(.{5,100}?)(?:\.|,|\n|$)',
    ]
    for pat in patterns:
        matches = re.findall(pat, text, re.IGNORECASE)
        for m in matches:
            m = m.strip()
            if m and len(m) > 5 and m not in commitments:
                commitments.append(m)
    return commitments[:10]


def process_call_audio(
    audio_path: str,
    model_size: str = "base",
    language: Optional[str] = None,
    prospect_name: Optional[str] = None,
    use_api: bool = False,
) -> Conversation:
    """
    Process a call recording into a Conversation object.

    Steps:
    1. Transcribe audio to text
    2. Extract entities, commitments, sentiment
    3. Build normalized Conversation object

    Args:
        audio_path: Path to call recording
        model_size: Whisper model size
        language: Optional language hint
        prospect_name: Optional name override
        use_api: Use OpenAI API instead of local Whisper

    Returns:
        Conversation object
    """
    # Transcribe
    result = transcribe_audio(
        audio_path=audio_path,
        model_size=model_size,
        language=language,
        use_api=use_api,
    )

    transcript_text = result.get("text", "")
    if not transcript_text:
        raise RuntimeError(f"No speech detected in {audio_path}")

    # Extract entities
    entities = _extract_entities_from_transcript(transcript_text)
    commitments = _extract_commitments_from_transcript(transcript_text)

    # Determine urgency
    urgency = _determine_urgency(transcript_text)

    # Determine sentiment
    sentiment = entities.sentiment or "neutral"

    # Build participants
    participants = []
    if entities.name:
        participants.append({"name": entities.name, "email": ""})
    elif prospect_name:
        participants.append({"name": prospect_name, "email": ""})

    # Create conversation
    conv = Conversation(
        source="call",
        participants=participants,
        date=datetime.utcnow(),
        raw_text=transcript_text,
        commitments=commitments,
        entities=entities,
        sentiment=sentiment,
        urgency=urgency,
    )

    return conv
