import os
import re
import json

CONFIG_FILE = "bot_config.json"

ALL_MODELS = [
    "gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts", "gemma-4-26b-a4b-it",
    "gemma-4-31b-it", "gemini-flash-latest", "gemini-flash-lite-latest",
    "gemini-pro-latest", "gemini-2.5-flash-image", "gemini-3-flash-preview",
    "gemini-3.1-pro-preview", "gemini-3.1-pro-preview-customtools",
    "gemini-3.1-flash-lite-preview", "gemini-3.1-flash-lite", "gemini-3-pro-image-preview",
    "gemini-3-pro-image", "nano-banana-pro-preview", "gemini-3.1-flash-image-preview",
    "gemini-3.1-flash-image", "gemini-3.1-flash-lite-image", "gemini-3.5-flash",
    "gemini-3.5-flash-lite", "gemini-omni-flash-preview", "gemini-omni-1.1-flash",
    "gemini-3.5-transcribe", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash",
    "lyria-3-clip-preview", "lyria-3-pro-preview", "lyria-3.5",
    "gemini-3.1-flash-tts-preview", "gemini-robotics-er-2-preview",
    "gemini-2.5-computer-use-preview-10-2025", "antigravity-preview-05-2026",
    "deep-research-max-preview-04-2026", "deep-research-preview-04-2026",
    "deep-research-pro-preview-12-2025", "gemini-embedding-001", "gemini-embedding-2-preview",
    "gemini-embedding-2", "gemini-3.5-transcribe-live", "gemini-2.5-flash-native-audio-latest",
    "gemini-2.5-flash-native-audio-preview-09-2025", "gemini-2.5-flash-native-audio-preview-12-2025",
    "gemini-3.1-flash-live-preview", "gemini-robotics-er-2-streaming-preview",
    "gemini-3.5-live-translate-preview"
]

config = {
    "current_model": "gemini-3.6-flash",
    "chat_channel_id": None,    # Single exclusive chat channel
    "log_channel_id": None,
    "summary_channel_id": None,
    "status_message_id": None,
    "checkpoint": None,         # {"id": int, "url": str, "timestamp": str}
    "summary_messages": [],     # Both bot and user messages since checkpoint
    "past_summaries": []
}

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config.update(json.load(f))
        except Exception as e:
            print(f"Error loading {CONFIG_FILE}: {e}")

def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving {CONFIG_FILE}: {e}")

def get_buffer_total_chars() -> int:
    return sum(m.get("length", len(m.get("content", ""))) for m in config["summary_messages"])

def generate_config_text() -> str:
    """Generates machine-readable text placed above the embed for Discord-based state persistence."""
    return (
        "```ini\n"
        "[GEMINI-CONFIG]\n"
        f"MODEL = {config['current_model']}\n"
        f"CHAT_CHANNEL = {config['chat_channel_id'] or 'None'}\n"
        f"LOG_CHANNEL = {config['log_channel_id'] or 'None'}\n"
        f"SUMMARY_CHANNEL = {config['summary_channel_id'] or 'None'}\n"
        f"CHECKPOINT_ID = {config['checkpoint']['id'] if config.get('checkpoint') else 'None'}\n"
        "```"
    )

def parse_config_text(text: str) -> bool:
    """Parses settings from a saved status message on Discord."""
    if "[GEMINI-CONFIG]" not in text:
        return False
    try:
        model = re.search(r"MODEL\s*=\s*(.+)", text)
        chat = re.search(r"CHAT_CHANNEL\s*=\s*(\d+)", text)
        log = re.search(r"LOG_CHANNEL\s*=\s*(\d+)", text)
        summary = re.search(r"SUMMARY_CHANNEL\s*=\s*(\d+)", text)

        if model:
            val = model.group(1).strip()
            if val in ALL_MODELS:
                config["current_model"] = val
        if chat:
            config["chat_channel_id"] = int(chat.group(1))
        if log:
            config["log_channel_id"] = int(log.group(1))
        if summary:
            config["summary_channel_id"] = int(summary.group(1))

        save_config()
        return True
    except Exception as e:
        print(f"Failed parsing status config text: {e}")
        return False

load_config()
