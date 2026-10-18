import unittest

from infofilm.planning import max_points, script_errors


class ScriptPointLimitTests(unittest.TestCase):
    def test_longer_films_may_have_more_points(self):
        self.assertEqual(max_points(), 25)
        self.assertEqual(max_points(285), 25)
        self.assertEqual(max_points(450), 33)     # Rebels and the Raj: 29-30 points were refused at a flat 25

    def test_the_error_names_the_limit(self):
        evidence = {'facts': [{'id': 'F1'}]}
        points = [{'id': f'P{i}', 'narration': 'x', 'fact_ids': ['F1']} for i in range(30)]
        self.assertFalse(any('point sequence' in e for e in script_errors({'points': points}, evidence, duration=450)))
        errors = [e for e in script_errors({'points': points}, evidence, duration=285) if 'point sequence' in e]
        self.assertIn('at most 25', errors[0])


if __name__ == '__main__':
    unittest.main()
