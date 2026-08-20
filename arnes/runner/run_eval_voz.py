"""Runner del caso de voz (T4) — habla por el WebSocket real de Retell.

A diferencia de los casos de texto, este no puede ir por el chat web: lo que se
examina es precisamente la forma de hablar en una llamada (frases cortas, una
pregunta cada vez, sin listas ni enlaces, repetir los datos para confirmarlos).

Registra una llamada web en Retell para tener un `call_id` autentico —el
WebSocket es fail-closed y rechaza cualquier otro— y despues conversa. No hay
audio: se manda la transcripcion como haria Retell tras el ASR.

Corre en el HOST (necesita salir a la API de Retell y al tunel):

    RV_RETELL=key_... RV_WSBASE=wss://<tunel>/api/v1/voice/retell/llm-ws \
        python arnes/runner/run_eval_voz.py

No juzga: deja la evidencia en `arnes/evidencia/T4.json`.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import time
from datetime import datetime, timezone

import httpx
import websockets

RETELL_KEY = os.environ["RV_RETELL"]
WS_BASE = os.environ["RV_WSBASE"]
AGENT_ID = os.environ.get("RV_AGENT", "")
SALIDA = pathlib.Path(os.environ.get("RV_SALIDA", "arnes/evidencia"))

GUION = [
    "Hola, buenas, llamaba para preguntar por la ortodoncia invisible",
    "Vale. Y me podriais dar cita para una revision?",
    "Me llamo Rocio Bermudez",
    "Mi telefono es el seis uno dos, tres cuatro cinco, seis siete ocho",
    "El jueves por la tarde me vendria bien",
]


def _registrar_llamada() -> str:
    r = httpx.post(
        "https://api.retellai.com/v2/create-web-call",
        headers={"Authorization": f"Bearer {RETELL_KEY}"},
        json={"agent_id": AGENT_ID},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["call_id"]


async def main() -> None:
    call_id = _registrar_llamada()
    registro = {
        "caso": "T4",
        "canal": "retell_voice",
        "call_id": call_id,
        "momento": datetime.now(timezone.utc).isoformat(),
        "turnos": [],
    }
    async with websockets.connect(f"{WS_BASE}/{call_id}", open_timeout=30) as ws:
        transcript: list[dict] = []
        for _ in range(2):  # config + saludo
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if m.get("response_type") == "response":
                registro["saludo"] = m["content"]
                transcript.append({"role": "agent", "content": m["content"]})
        for i, frase in enumerate(GUION, start=1):
            transcript.append({"role": "user", "content": frase})
            t0 = time.perf_counter()
            await ws.send(
                json.dumps(
                    {
                        "interaction_type": "response_required",
                        "response_id": i,
                        "transcript": transcript,
                    }
                )
            )
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                if m.get("response_type") != "ping_pong":
                    break
            texto = m.get("content", "")
            transcript.append({"role": "agent", "content": texto})
            registro["turnos"].append(
                {
                    "paciente": frase,
                    "agente": texto,
                    "latencia_ms": round((time.perf_counter() - t0) * 1000),
                    "end_call": m.get("end_call"),
                }
            )
            print(f"paciente: {frase}")
            print(f"agente  : {texto}\n")
    SALIDA.mkdir(parents=True, exist_ok=True)
    (SALIDA / "T4.json").write_text(
        json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"evidencia -> {SALIDA / 'T4.json'}")


if __name__ == "__main__":
    asyncio.run(main())
