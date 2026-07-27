import unittest

from tpdb_client import (
    build_match_source,
    choose_candidate,
    largest_file,
    map_scene_record,
    scene_key,
    select_site,
    size_text_to_bytes,
)


class FileSelectionTests(unittest.TestCase):
    def test_size_units_are_normalised_to_bytes(self):
        self.assertEqual(size_text_to_bytes("34 Bytes"), 34)
        self.assertEqual(size_text_to_bytes("2.5 MB"), int(2.5 * 1024**2))
        self.assertEqual(size_text_to_bytes("1.25 GB"), int(1.25 * 1024**3))
        self.assertEqual(size_text_to_bytes("1,019.30 MB"), int(1019.30 * 1024**2))
        self.assertIsNone(size_text_to_bytes("12 MB trailing junk"))

    def test_largest_file_wins_even_when_it_is_not_first(self):
        files = [
            {"filename": "notice.nfo", "size_text": "34 Bytes"},
            {"filename": "scene.mp4", "size_text": "2.85 GB"},
            {"filename": "sample.mp4", "size_text": "25 MB"},
        ]
        self.assertEqual(
            largest_file(files),
            ("scene.mp4", int(2.85 * 1024**3)),
        )

    def test_video_wins_when_malformed_size_would_make_nfo_look_larger(self):
        files = [
            {
                "filename": "Torrent Downloaded From XXXClub.to .nfo",
                "size_text": "34 Bytes",
            },
            {
                "filename": "beautyangels.26.07.22.evelina.dark.mp4",
                "size_text": "not reported",
            },
        ]
        self.assertEqual(
            largest_file(files),
            ("beautyangels.26.07.22.evelina.dark.mp4", None),
        )


class MatchSourceTests(unittest.TestCase):
    def test_scene_key_collapses_resolution_variants(self):
        self.assertEqual(
            scene_key("site.26.07.22.performer.title.4k.mp4"),
            scene_key("site.26.07.22.performer.title.480p.mp4"),
        )

    def test_standard_release_date_and_content_are_extracted(self):
        source = build_match_source(
            "jayspov.26.07.22.stella.nash.sister.crush.4k.mp4",
            "JaysPOV 26 07 22 Stella Nash Sister Crush XXX 2160p",
        )
        self.assertEqual(str(source.release_date), "2026-07-22")
        self.assertEqual(source.site_label, "JaysPOV")
        self.assertIn("stella nash sister crush", source.queries)

    def test_wrapped_filename_uses_date_from_torrent_title(self):
        source = build_match_source(
            "23.07.2026_Yurievij_IHaveAWife_NaughtyAmerica_"
            "LaSirena69_Antonella_La_Sirena_Chad_White_seduces_married_boss_1080p.mp4",
            "IHaveAWife 26 11 2019 NaughtyAmerica LaSirena69 "
            "Antonella La Sirena seduces married boss 1080p",
        )
        self.assertEqual(str(source.release_date), "2019-11-26")
        self.assertTrue(
            any("seduces married boss" in query for query in source.queries)
        )

    def test_episode_tokens_and_generic_release_groups_are_removed(self):
        source = build_match_source(
            "site.26.07.22.performer.scene123.e004.vaccine.title.mp4",
            "Site 26 07 22 Performer Episode12 Vaccine Title 1080p",
        )
        self.assertNotIn("scene123", source.tokens)
        self.assertNotIn("e004", source.tokens)
        self.assertNotIn("episode12", source.tokens)
        self.assertNotIn("vaccine", source.tokens)
        self.assertIn("performer", source.tokens)
        self.assertIn("title", source.tokens)

    def test_camel_case_is_split_only_for_match_source_tokens(self):
        filename = "site.26.07.22.RosiVelez.TheLatinaNextDoor.mp4"
        source = build_match_source(
            filename,
            "Site 26 07 22 RosiVelez TheLatinaNextDoor 1080p",
        )
        self.assertTrue({"rosi", "velez", "latina", "next", "door"} <= source.tokens)
        self.assertIn("rosivelez", scene_key(filename))

    def test_vr_noise_is_scoped_to_vr_sources(self):
        filename = "site.26.07.22.performer.180.lr.tb.vr180.4096p.title.mp4"
        vr_source = build_match_source(
            filename,
            "Site 26 07 22 Performer 180 LR TB VR180 4096p Title",
            "VR/VirtualReality",
        )
        standard_source = build_match_source(
            filename,
            "Site 26 07 22 Performer 180 LR TB VR180 4096p Title",
            "2160p/UHD/4K",
        )
        self.assertFalse({"180", "lr", "tb", "vr180", "4096p"} & vr_source.tokens)
        self.assertTrue({"180", "lr", "tb", "vr180", "4096p"} <= standard_source.tokens)


class CandidateScoringTests(unittest.TestCase):
    def setUp(self):
        self.source = build_match_source(
            "jayspov.26.07.22.stella.nash.sister.crush.mp4",
            "JaysPOV 26 07 22 Stella Nash Sister Crush XXX 1080p",
        )

    def test_unique_site_date_performer_match_is_accepted(self):
        candidate = {
            "id": "scene-1",
            "title": "Naughty Stella Nash Needs Step-Bro To Warm Her Up",
            "date": "2026-07-22",
            "site": {"id": 258, "name": "Jay's POV", "short_name": "jayspov"},
            "performers": [{"name": "Stella Nash"}],
        }
        decision = choose_candidate(
            [candidate],
            self.source,
            expected_site={"id": 258, "name": "Jay's POV"},
        )
        self.assertTrue(decision.accepted)
        self.assertIs(decision.candidate, candidate)

    def test_old_scene_with_same_performer_is_rejected(self):
        candidate = {
            "id": "scene-old",
            "title": "Stella Nash",
            "date": "2025-01-01",
            "site": {"id": 258, "name": "Jay's POV", "short_name": "jayspov"},
            "performers": [{"name": "Stella Nash"}],
        }
        decision = choose_candidate(
            [candidate],
            self.source,
            expected_site={"id": 258, "name": "Jay's POV"},
        )
        self.assertFalse(decision.accepted)

    def test_semantically_duplicate_candidates_do_not_create_ambiguity(self):
        candidates = [
            {
                "id": f"scene-{index}",
                "title": "Stella Nash Sister Crush",
                "date": "2026-07-22",
                "site": {"id": 258, "name": "Jay's POV"},
                "performers": [{"name": "Stella Nash"}],
            }
            for index in (1, 2)
        ]
        decision = choose_candidate(candidates, self.source)
        self.assertTrue(decision.accepted)

    def test_ambiguity_only_compares_individually_accepted_candidates(self):
        source = build_match_source(
            "narcos-fpv-23-07-03-baby-kitten-wakey-blinders-in-birmingham-"
            "vr180-4096p.mp4",
            "FuckPassVR 23 07 03 Baby Kitten Wakey Blinders in Birmingham "
            "XXX VR180 4096p MP4-Narcos [XC]",
            "VR/VirtualReality",
        )
        candidates = [
            {
                "id": "bf1607cb-6244-40eb-8154-f6127369ed50",
                "title": "Wakey Blinders in Birmingham",
                "date": "2023-08-21",
                "site": {"name": "FuckPassVR"},
            },
            {
                "id": "ff57e7ff-3928-451d-a594-3fb8a1cc3035",
                "title": "Wakey Blinders in Birmingham",
                "date": "2023-06-23",
                "site": {"name": "FuckPassVR"},
            },
            {
                "id": "836f0b50-ac27-4524-bdfe-87b00998e862",
                "title": "Reload 6on1 Double Anal Gang Bang, Baby Kitten ATM",
                "date": "2026-07-01",
                "site": {"name": "Giorgio Grandi"},
            },
            {
                "id": "86b8447f-6706-49da-804c-c607630cf2da",
                "title": "Behind the Pissing Scenes #189, Baby Kxtten",
                "date": "2024-02-05",
                "site": {"name": "XFREAXX"},
            },
            {
                "id": "b0e8a11a-e881-4571-8fbb-0553b25f171a",
                "title": "Baby Kxtten - Wunf 367",
                "date": "2022-10-18",
                "site": {"name": "Wake Up N Fuck"},
            },
        ]
        decision = choose_candidate(candidates, source)
        self.assertTrue(decision.accepted)
        self.assertEqual(
            decision.candidate["id"],
            "ff57e7ff-3928-451d-a594-3fb8a1cc3035",
        )
        selected = [item for item in decision.audit if item.get("selected")]
        self.assertEqual([item["id"] for item in selected], [decision.candidate["id"]])

    def test_two_individually_accepted_ties_remain_ambiguous(self):
        candidates = [
            {
                "id": f"scene-{index}",
                "title": f"Stella Nash Sister Crush Part {index}",
                "date": "2026-07-22",
                "site": {"id": 258, "name": "Jay's POV"},
                "performers": [{"name": "Stella Nash"}],
            }
            for index in (1, 2)
        ]
        decision = choose_candidate(candidates, self.source)
        self.assertFalse(decision.accepted)
        self.assertIn("ambiguous top candidates", decision.reason)
        self.assertFalse(any(item.get("selected") for item in decision.audit))

    def test_site_selection_requires_an_exact_alias_when_search_is_ambiguous(self):
        sites = [
            {"id": 258, "name": "Jay's POV", "short_name": "jayspov"},
            {"id": 73385, "name": "Jay's POV Movies", "short_name": "jayspovmovies"},
        ]
        self.assertEqual(select_site(sites, "JaysPOV")["id"], 258)


class ResponseMappingTests(unittest.TestCase):
    def test_scene_relationships_are_normalised(self):
        raw = {
            "id": "9f58d3b3-4edd-43c6-b4a5-bc685dface3d",
            "_id": 11271503,
            "title": "Infiltrate Proxy",
            "date": "2026-07-23",
            "background": {
                "full": "https://cdn.example/scene-background.jpg",
            },
            "site": {
                "id": 3370,
                "uuid": "1d9a41d9-5f14-452c-9602-e2f45d7f3e26",
                "name": "Example Site",
                "network_id": 20559,
                "network": {
                    "id": 20559,
                    "uuid": "916c4a19-8045-4f6c-bff1-7123d97e2401",
                    "name": "Example Network",
                },
            },
            "performers": [
                {
                    "id": "0b7fb34f-0e4d-46aa-9b99-70574952fc79",
                    "name": "Site Profile",
                    "parent": {
                        "id": "fa6f57c1-f7ce-4b1a-9e6e-6ca20d16864c",
                        "_id": 87765,
                        "name": "Canonical Performer",
                        "extras": {"birthday": "1989-05-15"},
                    },
                }
            ],
            "tags": [{"name": "POV"}],
        }

        bundle = map_scene_record(raw)
        self.assertEqual(bundle["scene"]["tpdb_id"], 11271503)
        self.assertEqual(bundle["site"]["site_id"], 3370)
        self.assertEqual(bundle["network"]["network_id"], 20559)
        self.assertEqual(bundle["performers"][0]["name"], "Canonical Performer")
        self.assertEqual(bundle["scene"]["tags"], ["POV"])
        self.assertEqual(
            bundle["scene"]["background_url"],
            "https://cdn.example/scene-background.jpg",
        )


if __name__ == "__main__":
    unittest.main()
