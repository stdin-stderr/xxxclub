import unittest

from tpdb_client import largest_file, map_scene_record, size_text_to_bytes


class FileSelectionTests(unittest.TestCase):
    def test_size_units_are_normalised_to_bytes(self):
        self.assertEqual(size_text_to_bytes("34 Bytes"), 34)
        self.assertEqual(size_text_to_bytes("2.5 MB"), int(2.5 * 1024**2))
        self.assertEqual(size_text_to_bytes("1.25 GB"), int(1.25 * 1024**3))

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
