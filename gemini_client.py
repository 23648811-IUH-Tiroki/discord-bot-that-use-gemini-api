import os
import asyncio
from datetime import datetime, timezone
from google import genai
from google.genai.errors import APIError
from config import config, save_config

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

async def call_gemini(prompt: str) -> str:
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=config["current_model"],
        contents=prompt
    )
    return response.text or "[Empty Response]"

async def generate_summary(messages: list, checkpoint_data: dict) -> str:
    """Generates a summary <= 500 characters including jump links and timestamps."""
    first_msg = checkpoint_data if checkpoint_data else messages[0]
    last_msg = messages[-1]

    start_time = first_msg.get("timestamp", "")[:16].replace("T", " ")
    end_time = last_msg.get("timestamp", "")[:16].replace("T", " ")

    header = f"🔗 [Start]({first_msg['url']}) ({start_time}) ➔ [End]({last_msg['url']}) ({end_time})\n"
    max_len = 500 - len(header) - 10
    if max_len < 50:
        max_len = 50

    transcript = "\n".join([f"{m['author']}: {m['content']}" for m in messages])
    prompt = (
        f"Provide a concise summary of this conversation. "
        f"STRICT LIMIT: Must be under {max_len} characters total.\n\n"
        f"Conversation:\n{transcript}"
    )

    summary_raw = await call_gemini(prompt)
    summary_text = summary_raw.strip()
    total_post = header + summary_text
    if len(total_post) > 500:
        total_post = total_post[:497] + "..."

    # Store in memory for macro-history recall ("What did I do last 2 days?")
    config.setdefault("past_summaries", []).append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": total_post
    })
    # Keep last 10 summaries in memory
    config["past_summaries"] = config["past_summaries"][-10:]
    save_config()

    return total_post
