"""Real-time speech translation in ~100 lines, with AssemblyAI.

Speak into your mic in one language and hear yourself in another, live:

    mic --> streaming speech-to-text (Universal-3.6 Pro)
              + LLM Gateway translates each finished turn, on the same WebSocket
        --> text-to-speech --> speakers

Run it (use headphones, so the translation isn't picked up by your mic):

    pip install websockets sounddevice numpy
    export ASSEMBLYAI_API_KEY=your-key          # https://www.assemblyai.com/dashboard/api-keys
    python realtime_translate.py --to Spanish --voice aai-antonioluis

This is the core of Natter (https://github.com/dan-ince-aai/natter), without the app around it.
"""
import argparse
import asyncio
import base64
import json
import os
import urllib.parse

import numpy as np
import sounddevice as sd
import websockets

API_KEY = os.environ["ASSEMBLYAI_API_KEY"]
STT_URL = "wss://streaming.assemblyai.com/v3/ws"
TTS_URL = "wss://tts.us.assemblyai.com/v1/ws"
USER_AGENT = "natter-example/0.1 (+https://github.com/dan-ince-aai/natter)"
MIC_RATE, TTS_RATE = 16000, 24000

PROMPT = ("You are a live interpreter. Translate what the speaker just said into natural, spoken {target}. "
          "Keep names and numbers as they are. Reply with only the translation.\n\n"
          "Recent conversation, for context only: {{{{history_5}}}}\n\nTranslate this: {{{{turn}}}}")


def connect(url, **params):
    """Open an AssemblyAI WebSocket. Works with old (extra_headers) and new (additional_headers) websockets."""
    url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": API_KEY}
    try:
        from websockets.asyncio.client import connect as new_connect
        return new_connect(url, additional_headers=headers, user_agent_header=USER_AGENT, max_size=None)
    except ImportError:
        return websockets.connect(url, extra_headers=headers, user_agent_header=USER_AGENT, max_size=None)


async def main(target, voice):
    loop = asyncio.get_running_loop()
    mic = asyncio.Queue()
    speaker = sd.RawOutputStream(samplerate=TTS_RATE, channels=1, dtype="int16")
    speaker.start()

    # 1. One streaming session does speech-to-text AND translation: the llm_gateway parameter runs your prompt on
    #    every finished turn and sends the result back as an LLMGatewayResponse message.
    stt = connect(STT_URL, sample_rate=MIC_RATE, encoding="pcm_s16le", speech_model="universal-3-6-pro",
                  format_turns="true", mode="min_latency",
                  llm_gateway=json.dumps({"model": "gemini-2.5-flash-lite", "max_tokens": 300,
                                          "prompt": PROMPT.format(target=target)}))
    # 2. One text-to-speech session, kept open, speaks each translation as soon as it arrives.
    tts = connect(TTS_URL, voice_id=voice, language=target.lower(), sample_rate=TTS_RATE, encoding="pcm_s16le")

    async with stt as stt_ws, tts as tts_ws:
        await tts_ws.recv()  # the session's configuration message

        def on_mic(data, frames, time, status):  # runs on the audio thread: hand the bytes to the event loop
            loop.call_soon_threadsafe(mic.put_nowait, bytes(data))

        async def send_mic():
            with sd.RawInputStream(samplerate=MIC_RATE, channels=1, dtype="int16", blocksize=800, callback=on_mic):
                print(f"Listening... speak and you'll hear yourself in {target}. Ctrl+C to stop.")
                while True:
                    await stt_ws.send(await mic.get())  # 50 ms chunks of 16 kHz PCM

        async def read_stt():
            async for raw in stt_ws:
                msg = json.loads(raw)
                if msg.get("type") == "Turn" and msg.get("end_of_turn"):
                    print(f"\nyou: {msg['transcript']}")
                elif msg.get("type") == "LLMGatewayResponse":
                    text = msg["data"]["choices"][0]["message"]["content"].strip()
                    if text:
                        print(f"  -> {text}")
                        await tts_ws.send(json.dumps({"type": "Generate", "text": text}))
                        await tts_ws.send(json.dumps({"type": "Flush"}))

        async def play_tts():
            async for raw in tts_ws:
                msg = json.loads(raw)
                if msg.get("type") == "Audio":
                    pcm = base64.b64decode(msg["audio"])
                    await asyncio.to_thread(speaker.write, np.frombuffer(pcm, "<i2"))  # blocking write, off the loop

        async def keep_tts_alive():  # TTS sessions close after 2 idle minutes
            while True:
                await asyncio.sleep(60)
                await tts_ws.send(json.dumps({"type": "KeepAlive"}))

        await asyncio.gather(send_mic(), read_stt(), play_tts(), keep_tts_alive())


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Speak in one language, hear yourself in another.")
    ap.add_argument("--to", default="Spanish", help="English, Spanish, French, German, Italian or Portuguese")
    ap.add_argument("--voice", default="aai-antonioluis", help="a TTS voice id, e.g. aai-kyla (English), aai-donatien (French)")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.to, args.voice))
    except KeyboardInterrupt:
        print("\nbye")
