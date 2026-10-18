import json
import sqlite3
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "kinfolk.sqlite3"
MAX_REQUEST_SIZE = 64 * 1024

SEED_PEOPLE = (
    ("Arthur Stanton", 1938, 2008, "Paternal grandfather", ""),
    ("Evelyn Stanton", 1941, 2019, "Paternal grandmother", ""),
    ("Walter Hayes", 1940, 2014, "Maternal grandfather", ""),
    ("June Hayes", 1943, 2021, "Maternal grandmother", ""),
    ("William Stanton", 1963, None, "Father", ""),
    ("Claire Hayes", 1965, None, "Mother", ""),
    ("Eleanor Stanton", 1988, None, "You · at the heart of this family", "“The best stories are the ones we share.”"),
    ("Daniel Carter", 1986, None, "Partner", ""),
    ("Oliver Carter", 2014, None, "Son", ""),
    ("Maya Carter", 2017, None, "Daughter", ""),
    ("Leo Carter", 2020, None, "Son", ""),
)

SEED_RELATIONSHIPS = (
    (5, 1, "son_of"),
    (5, 2, "son_of"),
    (6, 3, "daughter_of"),
    (6, 4, "daughter_of"),
    (7, 5, "daughter_of"),
    (7, 6, "daughter_of"),
    (7, 8, "partner"),
    (9, 7, "son_of"),
    (9, 8, "son_of"),
    (10, 7, "daughter_of"),
    (10, 8, "daughter_of"),
    (11, 7, "son_of"),
    (11, 8, "son_of"),
)


@contextmanager
def connect_database():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database():
    with connect_database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 100),
                birth_year INTEGER CHECK(birth_year IS NULL OR birth_year BETWEEN 1000 AND 2100),
                death_year INTEGER CHECK(death_year IS NULL OR death_year BETWEEN 1000 AND 2100),
                display_relation TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS relationships (
                person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
                related_person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
                relationship_type TEXT NOT NULL
                    CHECK(relationship_type IN ('son_of', 'daughter_of', 'partner')),
                PRIMARY KEY(person_id, related_person_id, relationship_type),
                CHECK(person_id <> related_person_id)
            );
            CREATE INDEX IF NOT EXISTS relationships_related_person
                ON relationships(related_person_id, relationship_type);
            """
        )
        people_columns = {
            column["name"] for column in connection.execute("PRAGMA table_info(people)")
        }
        if "tone" in people_columns:
            connection.execute("ALTER TABLE people DROP COLUMN tone")

        people_count = connection.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        relationship_count = connection.execute(
            "SELECT COUNT(*) FROM relationships"
        ).fetchone()[0]
        previous_people_sequence = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'people'"
        ).fetchone()
        if (
            people_count == 0
            and relationship_count == 0
            and previous_people_sequence is None
        ):
            connection.executemany(
                """
                INSERT INTO people
                    (name, birth_year, death_year, display_relation, note)
                VALUES (?, ?, ?, ?, ?)
                """,
                SEED_PEOPLE,
            )
            connection.executemany(
                """
                INSERT INTO relationships (person_id, related_person_id, relationship_type)
                VALUES (?, ?, ?)
                """,
                SEED_RELATIONSHIPS,
            )


def family_data():
    with connect_database() as connection:
        people = [
            dict(person)
            for person in connection.execute(
                """
                SELECT id, name, birth_year, death_year, display_relation, note
                FROM people ORDER BY id
                """
            )
        ]
        if not people:
            orphaned_relationships = connection.execute(
                "SELECT COUNT(*) FROM relationships"
            ).fetchone()[0]
            return {
                "people": [],
                "relationships": [],
                "orphaned_relationships": orphaned_relationships,
            }

        relationships = [
            dict(relationship)
            for relationship in connection.execute(
                """
                SELECT person_id, related_person_id, relationship_type
                FROM relationships ORDER BY rowid
                """
            )
        ]

    people_by_id = {person["id"]: person for person in people}
    for relationship in relationships:
        if relationship["person_id"] not in people_by_id or relationship["related_person_id"] not in people_by_id:
            raise RuntimeError("A relationship points to a missing family member.")
    parent_relationships = {}
    partner_parent = {person_id: person_id for person_id in people_by_id}

    def find_partner_group(person_id):
        while partner_parent[person_id] != person_id:
            partner_parent[person_id] = partner_parent[partner_parent[person_id]]
            person_id = partner_parent[person_id]
        return person_id

    for relationship in relationships:
        if relationship["relationship_type"] == "partner":
            first = find_partner_group(relationship["person_id"])
            second = find_partner_group(relationship["related_person_id"])
            partner_parent[second] = first
        else:
            parent_relationships.setdefault(relationship["person_id"], []).append(relationship)

    grouped_people = {}
    for person_id in people_by_id:
        grouped_people.setdefault(find_partner_group(person_id), []).append(person_id)
    group_parents = {group_id: set() for group_id in grouped_people}
    for child_id, parent_edges in parent_relationships.items():
        child_group = find_partner_group(child_id)
        for relationship in parent_edges:
            parent_group = find_partner_group(relationship["related_person_id"])
            if parent_group != child_group:
                group_parents[child_group].add(parent_group)

    pending = set(grouped_people)
    generations = {}
    while pending:
        ready = [
            group_id for group_id in pending
            if group_parents[group_id].isdisjoint(pending)
        ]
        if not ready:
            raise RuntimeError("The family relationships contain a parent cycle.")
        for group_id in ready:
            generations[group_id] = (
                max(generations[parent_group] for parent_group in group_parents[group_id]) + 1
                if group_parents[group_id] else 0
            )
            pending.remove(group_id)
    for person_id, person in people_by_id.items():
        person["generation"] = generations[find_partner_group(person_id)]

    return {"people": people, "relationships": relationships, "orphaned_relationships": 0}


class FamilyTreeHandler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/family":
            try:
                self.send_json(200, family_data())
            except (sqlite3.Error, RuntimeError) as error:
                self.send_json(500, {"error": f"Could not read the family database: {error}"})
            return
        if path not in ("/", "/index.html"):
            self.send_json(404, {"error": "Not found."})
            return

        try:
            content = (ROOT / "index.html").read_bytes()
        except OSError as error:
            self.send_json(500, {"error": f"Could not read the app page: {error}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        if urlparse(self.path).path != "/api/people":
            self.send_json(404, {"error": "Not found."})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Invalid request size."})
            return
        if length <= 0 or length > MAX_REQUEST_SIZE:
            self.send_json(400, {"error": "Request must contain a small JSON body."})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "Request body must be valid JSON."})
            return
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "Request body must be a JSON object."})
            return

        name = payload.get("name")
        relationship_type = payload.get("relationship_type")
        related_person_id = payload.get("related_person_id")
        birth_year = payload.get("birth_year")
        note = payload.get("note", "")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            self.send_json(400, {"error": "Name must contain between 1 and 100 characters."})
            return
        if relationship_type not in ("son_of", "daughter_of"):
            self.send_json(400, {"error": "Relationship must be Son of or Daughter of."})
            return
        if isinstance(related_person_id, bool) or not isinstance(related_person_id, int):
            self.send_json(400, {"error": "Select a person from the family tree."})
            return
        if birth_year is not None and (
            isinstance(birth_year, bool)
            or not isinstance(birth_year, int)
            or not 1000 <= birth_year <= 2100
        ):
            self.send_json(400, {"error": "Birth year must be between 1000 and 2100."})
            return
        if not isinstance(note, str) or len(note) > 1000:
            self.send_json(400, {"error": "Note must be 1000 characters or fewer."})
            return

        relationship_label = "Son" if relationship_type == "son_of" else "Daughter"
        try:
            with connect_database() as connection:
                related_person = connection.execute(
                    "SELECT id, name FROM people WHERE id = ?", (related_person_id,)
                ).fetchone()
                if related_person is None:
                    self.send_json(400, {"error": "The selected family member no longer exists."})
                    return
                if name.strip().casefold() == related_person["name"].casefold():
                    self.send_json(400, {"error": "A person cannot be their own parent."})
                    return
                cursor = connection.execute(
                    """
                    INSERT INTO people (name, birth_year, display_relation, note)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        name.strip(),
                        birth_year,
                        f"{relationship_label} of {related_person['name']}",
                        note.strip(),
                    ),
                )
                person_id = cursor.lastrowid
                connection.execute(
                    """
                    INSERT INTO relationships (person_id, related_person_id, relationship_type)
                    VALUES (?, ?, ?)
                    """,
                    (person_id, related_person_id, relationship_type),
                )
        except sqlite3.Error as error:
            self.send_json(500, {"error": f"Could not save the family member: {error}"})
            return
        self.send_json(201, {"person_id": person_id})

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")


if __name__ == "__main__":
    initialize_database()
    server = ThreadingHTTPServer(("127.0.0.1", 8000), FamilyTreeHandler)
    print("Family tree app is available at http://127.0.0.1:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nFamily tree server stopped.")
    finally:
        server.server_close()
