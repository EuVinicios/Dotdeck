#!/usr/bin/env python3
"""A stand-in OpenDots server with demo Dots, for screenshots and manual checks.

It serves a real OpenDots web client build (the ``dist/client`` folder of an OpenDots
checkout, after ``npm run build``) and answers the read-only API with fictional data,
so screenshots never show anyone's real Dots:

    python3 tools/demo_server.py --client ~/OpenDots/dist/client --port 4399 [--lang pt]

``--lang pt`` gives the Dots Portuguese names and jobs, for the pt_BR screenshots (the
OpenDots page itself only speaks English). Everything here is made up: the Dots, the
model settings (Claude Haiku 5.5 through OpenRouter) and the prices, which are demo
values and not a price list.

Chats need CopilotKit Intelligence and are not simulated: every Dot shows its empty
conversation, which is what a new user sees. Every change (POST, PUT…) answers 403.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = "demo-owner-token-0123456789abcdef"
NOW = int(time.time() * 1000)

# (name, job, instructions). The job is the first line of the instructions, which is
# where Dotdeck reads it from; the words in it also pick each Dot's pet.
DOTS = {
    "en": [
        ("Assistant", "plans the day and the other Dots",
         "Coordinate the other Dots, keep a short daily brief and make sure nothing falls through."),
        ("Research", "market and product research",
         "Find reliable sources, compare options and summarize what matters with links."),
        ("Writing", "posts, emails and documents",
         "Draft clear, friendly text in the owner's voice and keep it short."),
        ("Finance", "budgets, bills and investments",
         "Track spending against the budget, flag unusual charges and explain the numbers simply."),
        ("Upkeep", "backups and the home server",
         "Watch backups, updates and disk space, and fix small problems before they grow."),
        ("Lab", "code, scripts and automations",
         "Write and test small scripts and automations in the sandbox before anything runs."),
        ("Strategy", "strategy and second opinions",
         "Challenge plans with good questions and lay out trade-offs before decisions."),
        ("Study", "courses, languages and study plans",
         "Turn big topics into study plans, quiz the owner and keep track of progress."),
    ],
    "pt": [
        ("Assistente", "organiza o dia e os outros Dots",
         "Coordene os outros Dots, faça um resumo curto do dia e não deixe nada escapar."),
        ("Pesquisa", "pesquisa de mercado e de produtos",
         "Encontre fontes confiáveis, compare opções e resuma o que importa com os links."),
        ("Redação", "posts, e-mails e documentos",
         "Escreva textos claros e simpáticos, no tom do dono, e sempre curtos."),
        ("Finanças", "orçamento, contas e investimentos",
         "Acompanhe os gastos, avise sobre cobranças estranhas e explique os números."),
        ("Manutenção", "backups e o servidor de casa",
         "Cuide dos backups, das atualizações e do espaço em disco antes que virem problema."),
        ("Laboratório", "código, scripts e automações",
         "Escreva e teste scripts e automações no sandbox antes de rodar qualquer coisa."),
        ("Estratégia", "estratégia e segundas opiniões",
         "Questione os planos com boas perguntas e mostre os prós e contras antes de decidir."),
        ("Estudos", "cursos, idiomas e planos de estudo",
         "Transforme temas grandes em planos de estudo, faça revisões e acompanhe o progresso."),
    ],
}
SPACES = {
    "en": [("Everyday", "Personal errands and notes"), ("Product launch", "Launch plan and copy"),
           ("Home", "The house and the home server"), ("Research notes", "Sources and summaries")],
    "pt": [("Dia a dia", "Tarefas e anotações pessoais"),
           ("Lançamento", "Plano e textos do lançamento"),
           ("Casa", "A casa e o servidor de casa"), ("Pesquisas", "Fontes e resumos")],
}
TASKS = {"en": "Weekly market summary", "pt": "Resumo semanal do mercado"}
MEMORIES = {
    "en": ["Prefers short answers", "Likes examples", "Budget review on Fridays"],
    "pt": ["Prefere respostas curtas", "Gosta de exemplos", "Revisão do orçamento às sextas"],
}

OPENROUTER_URL = "https://openrouter.ai/api/v1"
ANTHROPIC_URL = "https://api.anthropic.com/v1/"


def _preset(model_id: str, name: str, provider: str, price, speed: str) -> dict:
    return {"id": model_id, "name": name, "provider": provider,
            "price": {"input": price[0], "output": price[1]} if price else None,
            "speed": speed, "note": ""}


# What GET /api/model-config answers on a server set up with Dotdeck 0.3.3 or newer.
MODEL_CONFIG = {
    "model": "anthropic/claude-haiku-5.5", "modelName": "Claude Haiku 5.5",
    "provider": "openrouter", "baseUrl": OPENROUTER_URL, "configured": True,
    "price": {"input": 0.10, "output": 0.50}, "hasApiKey": True,
    "keys": {"openrouter": True, "anthropic": False, "openai-compatible": False},
    "providers": [
        {"id": "openrouter", "name": "OpenRouter", "hasKey": True, "baseUrl": OPENROUTER_URL},
        {"id": "anthropic", "name": "Anthropic", "hasKey": False, "baseUrl": ANTHROPIC_URL},
        {"id": "openai-compatible", "name": "OpenAI-compatible", "hasKey": False,
         "baseUrl": None},
    ],
    "voiceModel": "openai/gpt-audio-mini", "voiceName": "marin", "hasVoiceKey": False,
    "voiceReady": True, "voiceGateway": True,
    "presets": [
        _preset("anthropic/claude-haiku-5.5", "Claude Haiku 5.5", "openrouter", (0.10, 0.50),
                "fast"),
        _preset("anthropic/claude-sonnet-5.5", "Claude Sonnet 5.5", "openrouter", (1.0, 5.0),
                "balanced"),
        _preset("claude-haiku-5-5", "Claude Haiku 5.5", "anthropic", (0.10, 0.50), "fast"),
        _preset("claude-sonnet-5-5", "Claude Sonnet 5.5", "anthropic", (1.0, 5.0), "balanced"),
        _preset("claude-opus-5-5", "Claude Opus 5.5", "anthropic", None, "deep"),
    ],
    "call": {"voiceModel": "openai/gpt-audio-mini",
             "transcribeModel": "google/gemini-2.5-flash-lite",
             "thinkingModel": "anthropic/claude-haiku-5.5", "gateway": True,
             "voiceName": "marin"},
}


def workspace(lang: str = "en") -> dict:
    spaces = [{"id": f"space-{i}", "name": name, "description": text, "createdAt": NOW - i}
              for i, (name, text) in enumerate(SPACES[lang])]
    dots = [{"id": f"dot-{i}", "spaceId": "space-0", "spaceIds": ["space-0"], "name": name,
             "instructions": f"# {name} — {job}\n\n{text}", "researchAllowed": True,
             "memoryAllowed": True, "createdAt": NOW - 10_000 + i, "learningContainerId": None,
             "skillDeliveryEnabled": False}
            for i, (name, job, text) in enumerate(DOTS[lang])]
    return {"spaces": spaces, "dots": dots, "conversations": [], "calls": [],
            "setup": {"intelligence": True, "model": True, "browser": True, "voice": True,
                      "slack": "not_configured", "missing": []},
            # What a Dotdeck-ready OpenDots adds: the model in use, for the sidebar footer.
            "activeModel": MODEL_CONFIG["model"], "activeProvider": MODEL_CONFIG["provider"]}


def state(lang: str = "en") -> dict:
    return {"settings": {"name": "OpenDots Demo", "paused": False, "researchAllowed": True,
                         "memoryAllowed": True},
            "tasks": [{"id": "task-1", "prompt": TASKS[lang], "status": "running",
                       "intervalSeconds": 604800, "nextRunAt": NOW + 3600_000, "createdAt": NOW}],
            "memories": [{"id": f"m{i}", "text": text, "createdAt": NOW - i}
                         for i, text in enumerate(MEMORIES[lang])],
            "mode": "live", "configured": True}


class Handler(BaseHTTPRequestHandler):
    client: Path
    lang: str = "en"

    def log_message(self, *_args) -> None:
        pass

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._json(401, {"error": "Enter your owner access token to continue."})
            elif path == "/api/state":
                self._json(200, state(self.lang))
            elif path == "/api/workspace":
                self._json(200, workspace(self.lang))
            elif path == "/api/model-config":
                self._json(200, MODEL_CONFIG)
            elif path.startswith("/api/copilotkit/threads"):
                self._json(200, {"threads": [], "nextCursor": None})
            else:
                self._json(404, {"error": "Not found."})
            return
        target = (self.client / path.lstrip("/")).resolve()
        if not str(target).startswith(str(self.client)) or not target.is_file():
            target = self.client / "index.html"
        kind = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), kind)

    def do_POST(self) -> None:  # noqa: N802
        self._json(403, {"error": "This is a read-only demo."})

    do_PUT = do_PATCH = do_DELETE = do_POST


def serve(client: Path, port: int = 0, lang: str = "en") -> ThreadingHTTPServer:
    """Start the demo on a thread; ``lang`` is "en" or "pt" (any "pt…" counts)."""
    lang = "pt" if lang.lower().startswith("pt") else "en"
    handler = type("DemoHandler", (Handler,), {"client": client.resolve(), "lang": lang})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client", type=Path, required=True)
    parser.add_argument("--port", type=int, default=4399)
    parser.add_argument("--lang", default="en", help="en (default) or pt")
    args = parser.parse_args()
    httpd = serve(args.client, args.port, args.lang)
    print(f"Demo OpenDots on http://127.0.0.1:{httpd.server_port} (token {TOKEN})")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
