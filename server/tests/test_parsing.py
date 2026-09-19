"""Stdout-parsing unit tests — stdlib only, no mcp import."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import new_task_id
from persistence import (
    find_last_result_line,
    parse_progress_line,
    parse_question_line,
    progress_note,
    strip_result_marker,
)


class TestQuestionLine(unittest.TestCase):
    def test_valid_question(self):
        line = 'QUESTION:{"id":"abc123","kind":"blocker","message":"tests keep failing","context":"tried x"}'
        parsed = parse_question_line(line)
        self.assertEqual(parsed["id"], "abc123")
        self.assertEqual(parsed["kind"], "blocker")
        self.assertEqual(parsed["message"], "tests keep failing")

    def test_non_question_line(self):
        self.assertIsNone(parse_question_line('PROGRESS:{"step":1}'))
        self.assertIsNone(parse_question_line("plain text"))

    def test_malformed_json(self):
        self.assertIsNone(parse_question_line("QUESTION:{not json"))
        self.assertIsNone(parse_question_line("QUESTION:[1,2]"))

    def test_missing_required_fields(self):
        self.assertIsNone(parse_question_line('QUESTION:{"id":"x"}'))
        self.assertIsNone(parse_question_line('QUESTION:{"message":"no id"}'))
        self.assertIsNone(parse_question_line('QUESTION:{"id":"","message":"y"}'))
        self.assertIsNone(parse_question_line('QUESTION:{"id":123,"message":"y"}'))


class TestResultLine(unittest.TestCase):
    def test_empty_stdout(self):
        self.assertIsNone(find_last_result_line(""))

    def test_no_result_line(self):
        self.assertIsNone(find_last_result_line("hello\nPROGRESS:{\"step\":1}\nbye"))

    def test_extracts_last_result_line(self):
        out = 'garbage\nRESULT_JSON:{"status":"failed"}\nnoise\nRESULT_JSON:{"status":"succeeded"}\n'
        self.assertEqual(find_last_result_line(out), 'RESULT_JSON:{"status":"succeeded"}')

    def test_crlf_line_endings(self):
        out = 'PROGRESS:{"step":1}\r\nRESULT_JSON:{"status":"succeeded"}\r\n'
        self.assertEqual(find_last_result_line(out), 'RESULT_JSON:{"status":"succeeded"}')

    def test_mixed_garbage_progress_and_result(self):
        out = "\n".join(
            [
                "uv resolving dependencies...",
                'PROGRESS:{"step":1,"node":"agent"}',
                "random warning: something",
                'PROGRESS:{"step":2,"node":"tools","note":"editing file"}',
                'RESULT_JSON:{"status":"succeeded","turns":5}',
            ]
        )
        line = find_last_result_line(out)
        self.assertIsNotNone(line)
        self.assertIn('"turns": 5'.replace(" ", ""), line.replace(" ", ""))

    def test_strip_marker(self):
        self.assertEqual(strip_result_marker('RESULT_JSON:{"a":1}'), '{"a":1}')
        self.assertEqual(strip_result_marker('{"a":1}'), '{"a":1}')


class TestProgressLine(unittest.TestCase):
    def test_non_progress_line(self):
        self.assertIsNone(parse_progress_line("hello world"))
        self.assertIsNone(parse_progress_line('RESULT_JSON:{"status":"x"}'))

    def test_well_formed(self):
        parsed = parse_progress_line('PROGRESS:{"step":3,"node":"agent","note":"thinking"}')
        self.assertEqual(parsed, {"step": 3, "node": "agent", "note": "thinking"})

    def test_malformed_json(self):
        self.assertIsNone(parse_progress_line("PROGRESS:{not json"))

    def test_non_object_payload(self):
        self.assertIsNone(parse_progress_line("PROGRESS:[1,2,3]"))
        self.assertIsNone(parse_progress_line('PROGRESS:"text"'))

    def test_note_preference(self):
        self.assertEqual(progress_note({"note": "editing", "node": "agent", "step": 2}), "editing")
        self.assertEqual(progress_note({"node": "agent", "step": 2}), "agent#2")
        self.assertEqual(progress_note({"step": 7}), "step 7")
        self.assertEqual(progress_note({}), "step ?")


class TestTaskIdFormat(unittest.TestCase):
    def test_shape(self):
        tid = new_task_id()
        parts = tid.split("_")
        self.assertEqual(parts[0], "mk")
        self.assertEqual(len(parts), 3)
        self.assertEqual(len(parts[2]), 6)
        # base36 alphabet only
        for ch in parts[1] + parts[2]:
            self.assertIn(ch, "0123456789abcdefghijklmnopqrstuvwxyz")

    def test_unique(self):
        self.assertNotEqual(new_task_id(), new_task_id())


if __name__ == "__main__":
    unittest.main()
