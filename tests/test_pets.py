"""Pet roster and assignment: every Dot gets a fitting pet, never a duplicate while free."""

from __future__ import annotations

import unittest

from src import config
from src.core import pets


class PetTests(unittest.TestCase):
    def test_roster_is_complete_and_drawn(self):
        ids = [pet.id for pet in pets.PETS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 12)
        for pet_id in ids:
            path = config.pets_dir() / f"{pet_id}.png"
            self.assertTrue(path.is_file(), f"missing picture for {pet_id}: {path}")

    def test_owner_dots_get_fitting_pets(self):
        dots = [("1", "Dot", "Be thoughtful, practical, and concise."),
                ("2", "Chefe", "Coordena os outros Dots"),
                ("3", "Zelador", "Cuida do homelab e dos backups"),
                ("4", "Dinheiro", "Finanças e investimentos"),
                ("5", "Negócios", "Projetos e clientes"),
                ("6", "Oficina", "Sandbox de código"),
                ("7", "Consultor", "Consultoria regional"),
                ("8", "Acadêmico", "Pesquisa e dissertação")]
        result = pets.assign(dots, {})
        self.assertEqual(result, {"1": "sprout", "2": "sky", "3": "bolt", "4": "sunny",
                                  "5": "ember", "6": "plum", "7": "graphite", "8": "sage"})
        self.assertEqual(len(set(result.values())), len(dots))

    def test_owner_choice_wins_and_others_move_aside(self):
        dots = [("a", "Chefe", ""), ("b", "Assistente", "")]
        result = pets.assign(dots, {"b": "sky", "a": "not-a-pet"})
        self.assertEqual(result["b"], "sky")
        self.assertNotEqual(result["a"], "sky")

    def test_more_dots_than_pets_still_get_one_each(self):
        dots = [(str(i), f"Dot {i}", "") for i in range(len(pets.PETS) + 5)]
        result = pets.assign(dots, {})
        self.assertEqual(len(result), len(dots))
        self.assertEqual(len(set(result.values())), len(pets.PETS))
        self.assertTrue(all(pets.get(pet_id) for pet_id in result.values()))

    def test_stable_between_runs(self):
        dots = [("x", "Helper", ""), ("y", "Another", "")]
        self.assertEqual(pets.assign(dots, {}), pets.assign(dots, {}))


if __name__ == "__main__":
    unittest.main()
