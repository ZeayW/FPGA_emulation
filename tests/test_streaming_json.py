import io
import unittest

from emuflow_tools.common.streaming_json import StreamingJSONObjectReader


class StreamingJSONTest(unittest.TestCase):
    def test_nested_object_walk_across_tiny_chunks(self) -> None:
        document = '{"skip":{"x":[1,{"q":"}\\\""}]},"keep":{"a":1,"b":[2,3]}}'
        for chunk_size in (1, 2, 3, 7):
            reader = StreamingJSONObjectReader(
                io.StringIO(document), chunk_size=chunk_size
            )
            observed = {}
            for key in reader.object_keys():
                if key == "skip":
                    reader.skip_value()
                else:
                    for nested_key in reader.object_keys():
                        observed[nested_key] = reader.read_value()
            reader.finish()
            self.assertEqual(observed, {"a": 1, "b": [2, 3]})

    def test_truncation_fails_closed(self) -> None:
        reader = StreamingJSONObjectReader(
            io.StringIO('{"value":{"unterminated":1'), chunk_size=2
        )
        with self.assertRaisesRegex(ValueError, "truncated"):
            for _ in reader.object_keys():
                reader.skip_value()


if __name__ == "__main__":
    unittest.main()
