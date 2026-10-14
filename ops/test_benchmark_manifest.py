import unittest

from benchmark_batch import batch_fields


class BenchmarkGroupingTests(unittest.TestCase):
    def test_unrelated_samples_do_not_share_cast_context(self):
        self.assertEqual(batch_fields([{}, {}, {}]), [(None, None, None)] * 3)

    def test_explicit_source_batches_keep_episode_order(self):
        self.assertEqual(batch_fields([
            {"batchId": "a", "batchIndex": 7, "batchTotal": 20},
            {"batchId": "b", "batchIndex": 1, "batchTotal": 10},
            {"batchId": "a", "batchIndex": 2, "batchTotal": 20},
            {},
        ]), [("benchmark-a", 7, 20), ("benchmark-b", 1, 10),
             ("benchmark-a", 2, 20), (None, None, None)])

    def test_invalid_and_duplicate_positions_are_rejected(self):
        for media in ([{"batchId": ""}], [{"batchId": "a", "batchIndex": True}],
                      [{"batchId": "a", "batchIndex": 2, "batchTotal": 1}],
                      [{"batchId": "a", "batchIndex": 1}] * 2):
            with self.subTest(media=media), self.assertRaises(ValueError):
                batch_fields(media)

    def test_interleaved_groups_get_group_local_default_positions(self):
        self.assertEqual(batch_fields([{"batchId": "a"}, {"batchId": "b"}, {"batchId": "a"}]),
                         [("benchmark-a", 1, 2), ("benchmark-b", 1, 1), ("benchmark-a", 2, 2)])


if __name__ == "__main__":
    unittest.main()
