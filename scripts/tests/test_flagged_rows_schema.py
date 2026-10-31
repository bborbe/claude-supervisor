#!/usr/bin/env python3
"""The row contract: `scripts/flagged-rows.py` against `schema/flagged-rows.schema.json`.

Two directions, and both are needed:

- every row the resolver produces VALIDATES, so the schema describes what ships;
- a deliberately broken row FAILS, so the validator is not a rubber stamp. A validator
  that accepts everything passes the first half of this file while proving nothing —
  which is why each mutation case names the one field it breaks.

Stdlib only. The subset implemented is exactly what the schema uses; anything outside it
raises rather than passing silently, so widening the schema cannot quietly widen this.
"""

import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "schema", "flagged-rows.schema.json")
MODULE = os.path.join(os.path.dirname(HERE), "flagged-rows.py")

TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "boolean": bool,
    "null": type(None),
}
KEYWORDS = {"$schema", "$id", "title", "description", "type", "enum", "required",
            "properties", "additionalProperties", "items", "minLength", "minimum"}


def load_resolver():
    spec = importlib.util.spec_from_file_location("flagged_rows", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fr = load_resolver()


def load_schema():
    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _type_ok(value, name):
    if name not in TYPES:
        raise AssertionError("unsupported type in schema: %r" % name)
    if name == "integer" and isinstance(value, bool):
        return False  # JSON `true` is not an integer
    return isinstance(value, TYPES[name])


def validate(value, schema, path="row"):
    """Every violation, as `path: message`. Empty list means valid."""
    errors = []
    unknown = set(schema) - KEYWORDS
    if unknown:
        raise AssertionError("schema keyword not implemented: %s" % sorted(unknown))

    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_type_ok(value, name) for name in names):
            return ["%s: expected %s, got %r" % (path, declared, value)]

    if "enum" in schema and value not in schema["enum"]:
        errors.append("%s: %r is not one of %s" % (path, value, schema["enum"]))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append("%s: shorter than %d" % (path, schema["minLength"]))
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("%s: %r below minimum %r" % (path, value, schema["minimum"]))

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append("%s: missing required %r" % (path, name))
        for name, item in value.items():
            if name in properties:
                errors.extend(validate(item, properties[name], "%s.%s" % (path, name)))
            elif schema.get("additionalProperties") is False:
                errors.append("%s: unexpected field %r" % (path, name))

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(validate(item, schema["items"], "%s[%d]" % (path, index)))

    return errors


def fixture_row(**overrides):
    """A row the RESOLVER built, not a hand-written one — the point is agreement."""
    root = tempfile.mkdtemp()
    tasks = os.path.join(root, "25 Tasks")
    os.makedirs(tasks)
    page = os.path.join(tasks, "A Task.md")
    with open(page, "w", encoding="utf-8") as handle:
        handle.write("---\npage_type: task\nflag: true\nstatus: in_progress\n"
                     "role: agent\n---\n")
    cfg = {"tasks_dir": "25 Tasks", "path": root,
           "worker_launcher": "/opt/launchers/cc-v",
           "manager_launcher": "/opt/launchers/cc-m",
           "claude_script": "/opt/launchers/cc"}
    ctx = {"today": "2026-10-31", "role_map": {}, "template_root": None,
           "no_liveness": True, "managers": set(), "topic_map": {},
           "mode_default": "interactive", "normalised": [],
               "subjects": {}, "liveness_cache": {}}
    row, reason = fr.row_for("v", cfg, "task", page, ctx)
    assert reason is None, reason
    row.update(overrides)
    return row


class ResolverAgreesWithSchema(unittest.TestCase):
    """The resolver's own output validates — the schema describes what ships."""

    def test_the_fixture_row_is_valid(self):
        self.assertEqual(validate(fixture_row(), load_schema()), [])

    def test_the_schema_requires_exactly_the_resolvers_nineteen_fields(self):
        schema = load_schema()
        self.assertEqual(len(schema["required"]), 19)
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(set(schema["properties"]), set(fixture_row()))

    def test_the_nullable_set_is_exactly_the_four_documented_fields(self):
        # Anything else going null is a defect the consumer would have to guess around.
        schema = load_schema()
        nullable = {name for name, prop in schema["properties"].items()
                    if isinstance(prop.get("type"), list) and "null" in prop["type"]}
        self.assertEqual(nullable, {"owner_live", "session_id", "pane", "window_id"})

    def test_each_nullable_field_actually_accepts_null(self):
        for name in ("owner_live", "session_id", "pane", "window_id"):
            self.assertEqual(validate(fixture_row(**{name: None}), load_schema()), [], name)


class BrokenRowsAreRejected(unittest.TestCase):
    """The validator discriminates — each case breaks exactly one thing."""

    def assert_rejected(self, fragment, **overrides):
        errors = validate(fixture_row(**overrides), load_schema())
        self.assertTrue(errors, "accepted a row it should have rejected: %r" % overrides)
        self.assertTrue(any(fragment in error for error in errors),
                        "rejected for the wrong reason: %r" % errors)

    def test_launcher_null_is_rejected(self):
        # The live hazard: `launcher or None` used to emit null for every task with no
        # `launcher:` frontmatter, while `launcher_source` still claimed a default won.
        self.assert_rejected("launcher", launcher=None)

    def test_owner_null_is_rejected(self):
        self.assert_rejected("owner", owner=None)

    def test_mode_null_is_rejected(self):
        self.assert_rejected("mode", mode=None)

    def test_an_unknown_kind_is_rejected(self):
        self.assert_rejected("kind", kind="objective")

    def test_an_unknown_branch_is_rejected(self):
        self.assert_rejected("branch", branch="spawn")

    def test_an_unknown_liveness_is_rejected(self):
        self.assert_rejected("liveness", liveness="DEAD")

    def test_an_unknown_launcher_source_is_rejected(self):
        self.assert_rejected("launcher_source", launcher_source="goal")

    def test_a_string_window_id_is_rejected(self):
        self.assert_rejected("window_id", window_id="4")

    def test_a_negative_window_id_is_rejected(self):
        self.assert_rejected("window_id", window_id=-1)

    def test_a_missing_field_is_rejected(self):
        row = fixture_row()
        del row["refused"]
        self.assertTrue(any("missing required" in e
                            for e in validate(row, load_schema())))

    def test_an_extra_field_is_rejected(self):
        self.assert_rejected("unexpected field", extra="surprise")

    def test_a_blank_name_is_rejected(self):
        self.assert_rejected("name", name="")

    def test_a_non_string_blocker_is_rejected(self):
        self.assert_rejected("blocked_by", blocked_by=[1])

    def test_the_validator_itself_refuses_a_keyword_it_does_not_implement(self):
        # Otherwise a widened schema would pass while nothing checked the new keyword.
        with self.assertRaises(AssertionError):
            validate({}, {"type": "object", "oneOf": []})


class EnumsMatchTheirProducers(unittest.TestCase):
    """Each enum is checked against the module that produces it, not retyped here."""

    def test_launcher_source_is_the_union_of_both_arms(self):
        schema = load_schema()
        self.assertEqual(set(schema["properties"]["launcher_source"]["enum"]),
                         set(fr.LAUNCHER_SOURCES))

    def test_window_and_chip_come_from_the_role_table(self):
        schema = load_schema()
        self.assertEqual(set(schema["properties"]["window"]["enum"]),
                         set(fr.ROLE_WINDOW.values()))
        self.assertEqual(set(schema["properties"]["chip"]["enum"]),
                         set(fr.ROLE_CHIP.values()) | {fr.MISSING_MAP_CHIP})

    def test_role_is_the_three_role_table(self):
        self.assertEqual(set(load_schema()["properties"]["role"]["enum"]), set(fr.ROLES))

    def test_mode_is_the_servers_spawn_modes(self):
        self.assertEqual(set(load_schema()["properties"]["mode"]["enum"]),
                         set(fr.SPAWN_MODES))

    def test_liveness_is_the_exit_code_map_plus_none(self):
        self.assertEqual(set(load_schema()["properties"]["liveness"]["enum"]),
                         set(fr.LIVENESS_EXIT.values()) | {"none"})

    def test_kind_matches_the_sort_table(self):
        self.assertEqual(set(load_schema()["properties"]["kind"]["enum"]),
                         set(fr.KIND_ORDER))


if __name__ == "__main__":
    unittest.main()
