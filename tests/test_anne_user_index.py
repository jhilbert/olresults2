import unittest

from ingest.anne_user_index import normalise_user


class AnneUserIndexTests(unittest.TestCase):
    def test_preserves_explicit_championship_eligibility(self):
        user = normalise_user({"id": 17, "championshipEligibility": True})
        ineligible = normalise_user({"id": 18, "championshipEligibility": False})

        self.assertIs(user["championship_eligibility"], True)
        self.assertIs(user["championship_eligibility_reported"], True)
        self.assertIs(ineligible["championship_eligibility"], False)
        self.assertIs(ineligible["championship_eligibility_reported"], True)

    def test_distinguishes_null_from_omitted_eligibility(self):
        explicit_null = normalise_user({"id": 17, "championshipEligibility": None})
        omitted = normalise_user({"id": 18})

        self.assertIsNone(explicit_null["championship_eligibility"])
        self.assertIs(explicit_null["championship_eligibility_reported"], True)
        self.assertIsNone(omitted["championship_eligibility"])
        self.assertIs(omitted["championship_eligibility_reported"], False)


if __name__ == "__main__":
    unittest.main()
