import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server


class FamilyTreeApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path_patch = patch.object(
            server, "DATABASE_PATH", Path(self.temporary_directory.name) / "family.sqlite3"
        )
        self.database_path_patch.start()
        server.initialize_database()
        self.http_server = server.ThreadingHTTPServer(("127.0.0.1", 0), server.FamilyTreeHandler)
        self.server_thread = threading.Thread(target=self.http_server.serve_forever)
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.http_server.server_port}"

    def tearDown(self):
        self.http_server.shutdown()
        self.http_server.server_close()
        self.server_thread.join()
        self.database_path_patch.stop()
        self.temporary_directory.cleanup()

    def get_family(self):
        with urlopen(f"{self.base_url}/api/family") as response:
            return json.loads(response.read())

    def post_person(self, payload):
        request = Request(
            f"{self.base_url}/api/people",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urlopen(request)

    def test_seed_data_preserves_four_generations_and_partner_relationship(self):
        family = self.get_family()

        self.assertEqual(len(family["people"]), 11)
        self.assertTrue(all("tone" not in person for person in family["people"]))
        with server.connect_database() as connection:
            columns = {
                column["name"] for column in connection.execute("PRAGMA table_info(people)")
            }
        self.assertNotIn("tone", columns)
        self.assertEqual(
            {person["name"]: person["generation"] for person in family["people"]}[
                "Eleanor Stanton"
            ],
            2,
        )
        self.assertEqual(
            {person["name"]: person["generation"] for person in family["people"]}[
                "Daniel Carter"
            ],
            2,
        )
        self.assertIn(
            {
                "person_id": 7,
                "related_person_id": 8,
                "relationship_type": "partner",
            },
            family["relationships"],
        )

    def test_empty_family_returns_no_people_even_if_orphan_relationships_remain(self):
        connection = sqlite3.connect(server.DATABASE_PATH)
        try:
            connection.execute("DELETE FROM people")
            connection.commit()
        finally:
            connection.close()

        server.initialize_database()

        self.assertEqual(
            self.get_family(),
            {"people": [], "relationships": [], "orphaned_relationships": 13},
        )

    def test_initialize_database_preserves_a_cleared_existing_family(self):
        connection = sqlite3.connect(server.DATABASE_PATH)
        try:
            connection.execute("DELETE FROM relationships")
            connection.execute("DELETE FROM people")
            connection.commit()
        finally:
            connection.close()

        server.initialize_database()

        self.assertEqual(
            self.get_family(),
            {"people": [], "relationships": [], "orphaned_relationships": 0},
        )

    def test_deleting_person_cascades_to_relationships(self):
        with server.connect_database() as connection:
            connection.execute("DELETE FROM people WHERE id = 7")
            remaining_relationships = connection.execute(
                """
                SELECT COUNT(*) FROM relationships
                WHERE person_id = 7 OR related_person_id = 7
                """
            ).fetchone()[0]

        self.assertEqual(remaining_relationships, 0)

    def test_create_person_persists_relationship_and_updates_generation(self):
        with self.post_person(
            {
                "name": "Sam Carter",
                "birth_year": 2024,
                "note": "Met during the summer reunion.",
                "relationship_type": "son_of",
                "related_person_id": 7,
            }
        ) as response:
            created_id = json.loads(response.read())["person_id"]
            self.assertEqual(response.status, 201)

        family = self.get_family()
        added_person = next(person for person in family["people"] if person["id"] == created_id)
        self.assertNotIn("tone", added_person)
        self.assertEqual(added_person["generation"], 3)
        self.assertEqual(added_person["display_relation"], "Son of Eleanor Stanton")
        self.assertEqual(added_person["note"], "Met during the summer reunion.")
        self.assertIn(
            {
                "person_id": created_id,
                "related_person_id": 7,
                "relationship_type": "son_of",
            },
            family["relationships"],
        )

    def test_initialize_database_removes_legacy_tone_column_without_losing_people(self):
        with server.connect_database() as connection:
            connection.execute("ALTER TABLE people ADD COLUMN tone TEXT NOT NULL DEFAULT ''")
            connection.execute(
                "UPDATE people SET tone = 'blue' WHERE name = 'Arthur Stanton'"
            )

        server.initialize_database()

        with server.connect_database() as connection:
            columns = {
                column["name"] for column in connection.execute("PRAGMA table_info(people)")
            }
            person = connection.execute(
                "SELECT name FROM people WHERE name = 'Arthur Stanton'"
            ).fetchone()
        self.assertNotIn("tone", columns)
        self.assertEqual(person["name"], "Arthur Stanton")

    def test_rejects_unknown_target_without_adding_person(self):
        with self.assertRaises(HTTPError) as error:
            self.post_person(
                {
                    "name": "Jamie Carter",
                    "relationship_type": "daughter_of",
                    "related_person_id": 9999,
                }
            )

        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(len(self.get_family()["people"]), 11)

    def test_rejects_note_longer_than_limit_without_adding_person(self):
        with self.assertRaises(HTTPError) as error:
            self.post_person(
                {
                    "name": "Jamie Carter",
                    "note": "n" * 1001,
                    "relationship_type": "daughter_of",
                    "related_person_id": 7,
                }
            )

        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(len(self.get_family()["people"]), 11)

    def test_rejects_invalid_relationship_without_adding_person(self):
        with self.assertRaises(HTTPError) as error:
            self.post_person(
                {
                    "name": "Jamie Carter",
                    "relationship_type": "sibling_of",
                    "related_person_id": 7,
                }
            )

        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(len(self.get_family()["people"]), 11)

    def test_rejects_person_as_their_own_parent(self):
        with self.assertRaises(HTTPError) as error:
            self.post_person(
                {
                    "name": "Eleanor Stanton",
                    "relationship_type": "daughter_of",
                    "related_person_id": 7,
                }
            )

        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(len(self.get_family()["people"]), 11)


if __name__ == "__main__":
    unittest.main()
