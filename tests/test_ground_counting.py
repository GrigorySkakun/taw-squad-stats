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

    def test_breaker_sortie_is_excluded_from_campaign_and_streaks(self):
        def item(date, score, breaker, planes):
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
                "planes": planes,
                "ground": 0,
                "wounds": 0,
                "counts": counts,
                "victories": [],
                "breaker": breaker,
            }
            return {"analysis": analysis, "flight": analysis["flight"]}

        # Input is newest to oldest, matching TAW's profile page.
        flights = [
            item("2026-10-17T12:00:00", 20, False, 1),
            item("2026-10-16T12:00:00", 99, True, 3),
            item("2026-10-15T12:00:00", 10, False, 2),
        ]
        result = server.calculate_modes(flights)

        self.assertEqual(result["campaign"]["sorties"], 2)
        self.assertEqual(result["campaign"]["score"], 30)
        self.assertEqual(result["campaign"]["planes"], 3)
        self.assertEqual(result["current"]["score"], 20)
        self.assertEqual(result["current"]["planes"], 1)
        self.assertEqual(result["best"]["score"], 10)
        self.assertEqual(result["best"]["planes"], 2)
    def test_air_kill_requires_exact_event_plane_type_and_pilot(self):
        valid = {
            "date": "09.10.2026 12:10:22",
            "event": "SHOT DOWN",
            "object": "Spitfire Mk.IXe",
            "type": "Plane",
            "enemy": "FTC_KasperKJ",
        }
        self.assertTrue(server.is_air_kill(valid))
        self.assertFalse(server.is_air_kill({**valid, "event": "WAS SHOT DOWN"}))
        self.assertFalse(server.is_air_kill({**valid, "event": "SHOT DOWN BY AAA"}))
        self.assertFalse(server.is_air_kill({**valid, "type": "Tank"}))
        self.assertFalse(server.is_air_kill({**valid, "object": ""}))
        self.assertFalse(server.is_air_kill({**valid, "enemy": ""}))

    def test_event_log_parser_reads_aircraft_type_and_pilot(self):
        html = """
        <table>
          <tr><th>Date</th><th>Event</th><th>Damage</th><th>Object</th><th>Type</th><th>Enemy</th></tr>
          <tr><td>09.10.2026 12:10:22</td><td>SHOT DOWN</td><td></td>
              <td>Spitfire Mk.IXe</td><td>Plane</td>
              <td><a href="pilot_sortie.php?id=29623&amp;name=FTC_KasperKJ">FTC_KasperKJ</a></td></tr>
        </table>
        """
        events = server.parse_event_log(html)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "SHOT DOWN")
        self.assertEqual(events[0]["object"], "Spitfire Mk.IXe")
        self.assertEqual(events[0]["type"], "Plane")
        self.assertEqual(events[0]["enemy"], "FTC_KasperKJ")
        self.assertTrue(server.is_air_kill(events[0]))

    def test_only_sortie_list_status_breaks_streak(self):
        self.assertFalse(server.is_breaker({"status": "BAILED OUT"}, [{"event": "BAILED OUT"}]))
        self.assertFalse(server.is_breaker({"status": "BAILED OUT"}, [{"event": "CAPTURED"}]))
        self.assertTrue(server.is_breaker({"status": "KILLED"}, []))
        self.assertTrue(server.is_breaker({"status": "CAPTURED"}, []))
        self.assertTrue(server.is_breaker({"status": "DISCO"}, []))
        self.assertFalse(server.is_breaker({"status": "LANDED"}, [{"event": "WAS KILLED"}]))

    def test_pilot_exit_does_not_break_streak_but_is_detected(self):
        self.assertTrue(server.is_pilot_exit(
            {"status": "UNKNOWN", "remarks": "PILOT EXIT"}, []
        ))
        self.assertTrue(server.is_pilot_exit(
            {"status": "LANDED"}, [{"event": "PILOT EXIT"}]
        ))
        self.assertFalse(server.is_breaker(
            {"status": "LANDED", "remarks": "PILOT EXIT"}, []
        ))

    def test_pilot_exit_results_are_excluded_without_splitting_streak(self):
        def item(date, score, pilot_exit=False, breaker=False):
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
                "planes": 0,
                "ground": 0,
                "wounds": 0,
                "counts": counts,
                "victories": [],
                "breaker": breaker,
                "pilotExit": pilot_exit,
            }
            return {"analysis": analysis, "flight": analysis["flight"]}

        # Newest to oldest: PILOT EXIT lies between two valid sorties.
        result = server.calculate_modes([
            item("2026-10-17T12:00:00", 20),
            item("2026-10-16T12:00:00", 99, pilot_exit=True),
            item("2026-10-15T12:00:00", 10),
        ])
        for mode in ("current", "best", "campaign"):
            self.assertEqual(result[mode]["score"], 30)
            self.assertEqual(result[mode]["sorties"], 2)

    def test_sortie_list_status_not_event_log_breaks_streak(self):
        # Exact status from the profile's sortie row is authoritative.
        for status in ("KILLED", "CAPTURED", "DISCO"):
            self.assertTrue(server.is_breaker({"status": status}, []))
        # Event log labels alone must not break the streak.
        for event_name in ("WAS KILLED", "WAS CAPTURED", "DISCO", "KILLED", "Enemy pilot was killed"):
            self.assertFalse(server.is_breaker(
                {"status": "LANDED"},
                [{"event": event_name}],
            ))


if __name__ == "__main__":
    unittest.main()
