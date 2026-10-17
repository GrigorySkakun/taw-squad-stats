import unittest
from datetime import datetime

import server


class GroundCountingTests(unittest.TestCase):
    def test_official_ground_kills_fill_other_ground_residual(self):
        flight = {
            "url": "https://tacticalairwar.com/pilot_sortie.php?id=test",
            "date": "2026-10-17T13:00:00",
            "status": "LANDED",
            "mission": "Test mission",
            "groundKillsOfficial": 15,
        }
        events = [
            {
                "date": "17.10.2026 13:01:00",
                "event": "DESTROYED",
                "object": "PzKpfw IV Ausf.H",
                "type": "Tank",
                "enemy": "",
                "damage": "",
            },
            {
                "date": "17.10.2026 13:02:00",
                "event": "DESTROYED",
                "object": "Ship transport",
                "type": "Ship",
                "enemy": "",
                "damage": "",
            },
            {
                "date": "17.10.2026 13:03:00",
                "event": "DESTROYED",
                "object": "Unknown target",
                "type": "Object",
                "enemy": "",
                "damage": "",
            },
        ]

        result = server.analyze_flight(flight, events, official_ground_kills=15)

        self.assertEqual(result["ground"], 15)
        self.assertEqual(result["counts"]["mediumTank"], 1)
        self.assertEqual(result["counts"]["ships"], 1)
        self.assertAlmostEqual(result["scoreRaw"], 10.66)
        self.assertEqual(result["counts"]["otherGround"], 4)
        self.assertFalse(result["groundMismatch"])

    def test_ground_kills_parser_accepts_common_separators(self):
        self.assertEqual(server.parse_ground_kills_official(
            "<div>Air kills: 0</div><div>Ground kills: 15</div>"
        ), 15)
        self.assertEqual(server.parse_ground_kills_official(
            "<div>Ground Kills | 27</div>"
        ), 27)

    def test_legacy_aa_and_at_markers(self):
        self.assertEqual(server.classify_ground({"object": "Flak AA gun", "type": "Gun"}), "aa")
        self.assertEqual(server.classify_ground({"object": "85mm AT gun", "type": "Gun"}), "artillery")
        self.assertEqual(server.classify_ground({"object": "Unknown object", "type": "Unknown"}), None)

    def test_breaker_sorties_are_excluded_from_campaign(self):
        def item(date, score, breaker):
            counts = {
                "fighter": 0, "attack": 0, "bomber": 0,
                "heavyTank": 0, "mediumTank": 0, "lightTank": 0,
                "aa": 0, "groundedPlanes": 0, "trains": 0,
                "artillery": 0, "vehicles": 0, "ships": 0, "otherGround": 0,
            }
            analysis = {
                "flight": {"date": date, "url": "test"},
                "scoreRaw": float(score),
                "score": score,
                "sorties": 1,
                "planes": 0,
                "ground": 0,
                "wounds": 0,
                "counts": counts,
                "victories": [],
                "breaker": breaker,
            }
            return {"analysis": analysis, "flight": analysis["flight"]}

        flights = [
            item("2026-10-17T12:00:00", 20, False),
            item("2026-10-16T12:00:00", 99, True),
            item("2026-10-15T12:00:00", 10, False),
        ]
        result = server.calculate_modes(flights)

        self.assertEqual(result["campaign"]["sorties"], 2)
        self.assertEqual(result["campaign"]["score"], 30)
        self.assertEqual(result["current"]["score"], 20)
        self.assertEqual(result["best"]["score"], 20)

    def test_bailed_out_breaks_only_if_captured(self):
        bailed = {"status": "BAILED OUT"}
        self.assertFalse(server.is_breaker(bailed, [{"event": "BAILED OUT"}]))
        self.assertTrue(server.is_breaker(bailed, [{"event": "CAPTURED"}]))
        self.assertTrue(server.is_breaker({"status": "KILLED"}, []))

    def test_event_log_breaker_overrides_unhelpful_profile_status(self):
        self.assertTrue(server.is_breaker(
            {"status": "LANDED"},
            [{"event": "KILLED"}],
        ))
        self.assertTrue(server.is_breaker(
            {"status": "UNKNOWN"},
            [{"event": "DISCO"}],
        ))
        self.assertFalse(server.is_breaker(
            {"status": "LANDED"},
            [{"event": "DESTROYED"}],
        ))


if __name__ == "__main__":
    unittest.main()
