import os
import asyncio
from datetime import datetime, timezone
from google import genai
from google.genai.errors import APIError
from config import config, save_config

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

async def call_gemini_chat(history_text: str, user_prompt: str) -> str:
    """Uses Chat.send_message via an established chat session to avoid AFC SDK warnings."""
    def _run():
        chat = client.chats.create(model=config["current_model"])
        if history_text.strip():
            # Send history as background context
            chat.send_message(f"System Reference Context:\n{history_text}")
        response = chat.send_message(user_prompt)
        return response.text or "[Empty Response]"

    return await asyncio.to_thread(_run)

async def generate_summary(messages: list, checkpoint_data: dict) -> str:
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
        f"Summarize this conversation concisely. "
        f"STRICT LIMIT: Must be under {max_len} characters total.\n\n"
        f"Conversation:\n{transcript}"
    )

    def _run_summary():
        chat = client.chats.create(model=config["current_model"])
        res = chat.send_message(prompt)
        return res.text or "No summary generated."

    summary_raw = await asyncio.to_thread(_run_summary)
    summary_text = summary_raw.strip()
    total_post = header + summary_text
    if len(total_post) > 500:
        total_post = total_post[:497] + "..."

    config.setdefault("past_summaries", []).append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": total_post
    })
    config["past_summaries"] = config["past_summaries"][-10:]
    save_config()

    return total_post
