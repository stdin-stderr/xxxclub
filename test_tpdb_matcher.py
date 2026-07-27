import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.modules.setdefault(
    "asyncpg",
    types.SimpleNamespace(Pool=object, Record=object),
)

import db
from tpdb_client import CandidateDecision, build_match_source
from tpdb_matcher import _audit_for_storage, match_one, search_with_fallbacks


class FakeAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeTransaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return FakeAcquire(self.connection)


class ReuseDataAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_shadow_ids_can_recheck_existing_matches(self):
        connection = AsyncMock()
        connection.fetch.return_value = []

        await db.fetch_tpdb_shadow_candidates(
            FakePool(connection),
            2,
            torrent_ids=[443936, 451346],
        )

        sql, categories, limit, torrent_ids = connection.fetch.await_args.args
        self.assertIn("$3::bigint[] IS NOT NULL", sql)
        self.assertIn("t.torrent_id = ANY($3::bigint[])", sql)
        self.assertEqual(categories, list(db.TPDB_CATEGORIES))
        self.assertEqual(limit, 2)
        self.assertEqual(torrent_ids, [443936, 451346])

    async def test_reuse_lookup_only_selects_independently_scored_roots(self):
        connection = AsyncMock()
        connection.fetchrow.return_value = {
            "scene_id": "scene-id",
            "source_torrent_id": 41,
            "match_score": 0.9,
        }
        row = await db.find_reusable_tpdb_match(FakePool(connection), "key", 42)

        sql = connection.fetchrow.await_args.args[0]
        self.assertIn(
            "a.method NOT IN ('resolution_sibling', 'parse_filename_first')",
            sql,
        )
        self.assertIn("a.match_score IS NOT NULL", sql)
        self.assertEqual(row["match_score"], 0.9)

    async def test_reused_match_persists_the_root_score(self):
        connection = AsyncMock()
        await db.save_reused_tpdb_match(
            FakePool(connection),
            torrent_id=42,
            scene_id="scene-id",
            filename="scene.mp4",
            file_size_bytes=123,
            scene_key="scene",
            source_torrent_id=41,
            match_score=0.83,
        )

        args = connection.execute.await_args.args
        self.assertEqual(args[6], 0.83)
        self.assertIn("match_score=EXCLUDED.match_score", args[0])

    async def test_v2_data_migration_reports_cleanup_and_backfill_counts(self):
        connection = AsyncMock()
        connection.transaction = lambda: FakeTransaction()
        connection.execute.side_effect = ["DELETE 98", "UPDATE 4044"]

        result = await db.migrate_tpdb_matcher_v2(FakePool(connection))

        self.assertEqual(
            result,
            {"legacy_deleted": 98, "siblings_backfilled": 4044},
        )
        delete_sql = connection.execute.await_args_list[0].args[0]
        backfill_sql = connection.execute.await_args_list[1].args[0]
        self.assertIn("WITH RECURSIVE legacy_closure", delete_sql)
        self.assertIn("WITH RECURSIVE sibling_lineage", backfill_sql)
        self.assertIn(
            "jsonb_build_object('source_torrent_id', roots.root_id)",
            backfill_sql,
        )


class AuditTests(unittest.TestCase):
    def test_selected_audit_entry_survives_storage_limit_without_resorting(self):
        audit = [{"id": index} for index in range(30)]
        audit[27]["selected"] = True

        stored = _audit_for_storage(audit, limit=25)

        self.assertEqual([item["id"] for item in stored[:24]], list(range(24)))
        self.assertEqual(stored[24]["id"], 27)
        self.assertTrue(stored[24]["selected"])


class FakeFallbackClient:
    async def search_filename(self, filename):
        return ([{"id": "rejected", "title": "Nothing", "date": "2020-01-01"}], 1)

    async def search_sites(self, label):
        return ([], 0)

    async def search_scenes(self, query):
        return (
            [
                {
                    "id": "accepted",
                    "title": "Stella Nash Sister Crush",
                    "date": "2026-07-22",
                    "site": {"name": "JaysPOV"},
                    "performers": [{"name": "Stella Nash"}],
                }
            ],
            1,
        )


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_fallback_marks_the_actual_selected_audit_entry(self):
        source = build_match_source(
            "jayspov.26.07.22.stella.nash.sister.crush.mp4",
            "JaysPOV 26 07 22 Stella Nash Sister Crush XXX 1080p",
        )

        result = await search_with_fallbacks(
            FakeFallbackClient(),
            source,
            "jayspov.26.07.22.stella.nash.sister.crush.mp4",
        )

        self.assertEqual(result["candidate"]["id"], "accepted")
        selected = [item for item in result["audit"] if item.get("selected")]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["id"], "accepted")
        self.assertEqual(selected[0]["method"], "scene_query")
        self.assertNotEqual(result["audit"][0]["id"], "accepted")

    async def test_match_one_threads_category_and_root_score(self):
        row = {
            "torrent_id": 42,
            "title": "Example 26 07 22 Performer 180 Title",
            "category": "VR/VirtualReality",
            "files": [
                {
                    "filename": "example.26.07.22.performer.180.title.mp4",
                    "size_text": "1 GB",
                }
            ],
            "attempts": 0,
        }
        reusable = {
            "scene_id": "scene-id",
            "source_torrent_id": 41,
            "match_score": 0.83,
        }
        with (
            patch("tpdb_matcher.db.find_reusable_tpdb_match", AsyncMock(return_value=reusable)),
            patch("tpdb_matcher.db.save_reused_tpdb_match", AsyncMock()) as save,
        ):
            outcome = await match_one(object(), object(), row)

        self.assertEqual(outcome, "matched")
        self.assertEqual(save.await_args.kwargs["match_score"], 0.83)


if __name__ == "__main__":
    unittest.main()
