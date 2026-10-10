"""The pets: plush characters that give every Dot a face.

The roster is shared by the app and by tools/render_pets.py, which draws the PNGs in
src/pets. Each Dot gets a pet automatically (by its name and job, never two Dots with
the same pet on one server) and the owner can pick another one in the pet gallery.
"""

from __future__ import annotations

import unicodedata
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from ..i18n import N_


@dataclass(frozen=True)
class Pet:
    id: str
    name: str                       # translatable (N_), shown in the gallery
    fur: tuple[float, float, float]
    body: str = "round"             # round, pear, bean, wide
    ears: str | None = None         # bear, cat, fox, bunny, leaves
    top: str | None = None          # headset, crown, bow, beanie, cap, hardhat, antenna
    face: str | None = None         # glasses
    extra: str | None = None        # scarf
    eyes: str = "round"             # round, happy
    mouth: str = "smile"            # smile, open, cat
    blush: tuple[float, float, float] = (1.0, 0.5, 0.66)
    inner: tuple[float, float, float] | None = None
    detail: tuple[float, float, float] | None = None
    belly: tuple[float, float, float] | None = None
    keywords: tuple[str, ...] = field(default=(), compare=False)


PETS: tuple[Pet, ...] = (
    Pet("sky", N_("Sky"), (0.2, 0.53, 1.0), top="headset",
        keywords=("chief", "chefe", "jefe", "boss", "lead", "staff", "coordena", "assistant",
                  "assistente", "secretar", "call")),
    Pet("sprout", N_("Sprout"), (0.4, 0.82, 0.64), body="pear", ears="leaves", mouth="open",
        detail=(0.36, 0.74, 0.46),
        keywords=("dot", "everyday", "daily", "diario", "dia a dia", "garden", "jardim",
                  "health", "saude", "salud", "wellness", "habit")),
    Pet("ember", N_("Ember"), (1.0, 0.5, 0.18), ears="fox", mouth="cat", inner=(1.0, 0.93, 0.84),
        belly=(1.0, 0.93, 0.84),
        keywords=("business", "negocio", "sales", "venda", "marketing", "growth", "cliente",
                  "client", "startup", "empresa", "projeto", "project")),
    Pet("plum", N_("Plum"), (0.5, 0.32, 0.88), body="bean", top="antenna",
        detail=(0.55, 0.95, 1.0),
        keywords=("oficina", "workshop", "sandbox", "lab", "code", "codigo", "dev", "engineer",
                  "engenh", "tech", "automat", "build", "prototype", "taller")),
    Pet("sunny", N_("Sunny"), (1.0, 0.8, 0.22), top="crown", eyes="happy",
        blush=(1.0, 0.45, 0.4),
        keywords=("money", "dinheiro", "dinero", "financ", "invest", "budget", "orcamento",
                  "bank", "banco", "tax", "imposto", "accounting", "contab")),
    Pet("rosy", N_("Rosy"), (1.0, 0.56, 0.7), top="bow", blush=(0.92, 0.3, 0.45),
        detail=(0.92, 0.2, 0.36),
        keywords=("writ", "escrit", "escrev", "content", "conteudo", "social", "copy", "design",
                  "blog", "post", "story", "historia", "redat")),
    Pet("graphite", N_("Graphite"), (0.46, 0.49, 0.56), body="bean", face="glasses",
        keywords=("consult", "advis", "conselh", "analyst", "analis", "legal", "juridic",
                  "lawyer", "advog", "review", "audit", "strategy", "estrateg")),
    Pet("cocoa", N_("Cocoa"), (0.56, 0.36, 0.23), body="wide", ears="bear", mouth="open",
        inner=(0.86, 0.66, 0.5), belly=(0.9, 0.74, 0.58),
        keywords=("home", "casa", "hogar", "family", "familia", "kids", "filho", "pet",
                  "cozinha", "kitchen", "receita", "recipe", "shopping", "compras")),
    Pet("snow", N_("Snow"), (0.95, 0.94, 0.91), top="beanie", blush=(1.0, 0.52, 0.6),
        detail=(0.86, 0.22, 0.26),
        keywords=("travel", "viage", "viaje", "trip", "weather", "clima", "tempo", "agenda",
                  "calendar", "calendario", "event", "evento")),
    Pet("lilac", N_("Lilac"), (0.74, 0.68, 1.0), body="pear", ears="bunny",
        inner=(1.0, 0.76, 0.84), eyes="happy",
        keywords=("learn", "aprend", "study", "estud", "english", "ingles", "language", "idioma",
                  "teach", "profess", "school", "escola", "course", "curso", "tutor")),
    Pet("sage", N_("Sage"), (0.13, 0.62, 0.66), top="cap",
        keywords=("academ", "research", "pesquis", "investiga", "university", "universidad",
                  "phd", "doutor", "thesis", "tese", "dissert", "paper", "artigo", "science",
                  "ciencia")),
    Pet("bolt", N_("Bolt"), (0.92, 0.3, 0.27), body="wide", top="hardhat", blush=(1.0, 0.7, 0.6),
        keywords=("zelador", "caretaker", "janitor", "infra", "homelab", "server", "servidor",
                  "ops", "devops", "maint", "manuten", "backup", "monitor", "repair", "fix")),
)

BY_ID = {pet.id: pet for pet in PETS}
DEFAULT = PETS[0]


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").casefold()
    return "".join(c for c in text if not unicodedata.combining(c))


def get(pet_id: str | None) -> Pet | None:
    return BY_ID.get(pet_id or "")


def image_path(pet_id: str) -> Path:
    from .. import config
    return config.pets_dir() / f"{pet_id}.png"


def suggest(name: str, job: str = "", taken: set[str] | frozenset[str] = frozenset()) -> Pet:
    """The best free pet for a Dot: its name first, then its job, then a stable pick."""
    plain_name, plain_job = _plain(name), _plain(job)
    for text, weight in ((plain_name, 3), (plain_job, 1)):
        best, score = None, 0
        for pet in PETS:
            if pet.id in taken:
                continue
            # Longer matches are more specific: "homelab" beats the "lab" of a workshop.
            hits = sum(weight * len(word) for word in pet.keywords if word in text)
            if hits > score:
                best, score = pet, hits
        if best:
            return best
    free = [pet for pet in PETS if pet.id not in taken] or list(PETS)
    return free[zlib.crc32(plain_name.encode()) % len(free)]


def assign(dots: list[tuple[str, str, str]], chosen: dict[str, str]) -> dict[str, str]:
    """Pets for every Dot: ``dots`` is [(id, name, job)], ``chosen`` the owner's picks.

    Owner picks always win; the rest are spread so that, while there are free pets,
    no two Dots look the same. The order of ``dots`` (the server's) keeps it stable.
    """
    result = {dot_id: chosen[dot_id] for dot_id, _n, _j in dots if chosen.get(dot_id) in BY_ID}
    taken = set(result.values())
    for dot_id, name, job in dots:
        if dot_id in result:
            continue
        pet = suggest(name, job, taken if len(taken) < len(PETS) else frozenset())
        result[dot_id] = pet.id
        taken.add(pet.id)
    return result
