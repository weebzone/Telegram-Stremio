import os
import unittest

os.environ.setdefault(
    "DATABASE",
    "mongodb://localhost:27017/matcher_tracking,mongodb://localhost:27017/matcher_storage",
)

from Backend.helper.metadata_matcher import (
    MatchCandidate,
    MatchIntent,
    build_title_variants,
    choose_best_candidate,
    normalize_title,
)


class MetadataMatcherTests(unittest.TestCase):
    def test_patriot_2026_rejects_the_patriot_2000(self):
        intent = MatchIntent(
            raw_title="Patriot",
            clean_title="patriot",
            year=2026,
            media_type="movie",
        )
        decision = choose_best_candidate(intent, [
            MatchCandidate("imdb", "The Patriot", 2000, "movie", imdb_id="tt0187393"),
            MatchCandidate("tmdb", "Patriot", 2026, "movie", imdb_id="tt33412884", tmdb_id=123),
        ])
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.candidate.imdb_id, "tt33412884")

    def test_wrong_year_candidate_is_rejected(self):
        intent = MatchIntent(
            raw_title="Patriot",
            clean_title="patriot",
            year=2026,
            media_type="movie",
        )
        decision = choose_best_candidate(intent, [
            MatchCandidate("imdb", "The Patriot", 2000, "movie", imdb_id="tt0187393"),
        ])
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "metadata_year_mismatch")

    def test_duplicate_imdb_and_tmdb_same_title_is_not_ambiguous(self):
        intent = MatchIntent(
            raw_title="F9 The Fast Saga",
            clean_title="f9 the fast saga",
            year=2021,
            media_type="movie",
        )
        decision = choose_best_candidate(intent, [
            MatchCandidate("imdb", "F9: The Fast Saga", 2021, "movie", imdb_id="tt5433138", tmdb_id=385128),
            MatchCandidate("tmdb", "F9", 2021, "movie", tmdb_id=385128),
        ])
        self.assertTrue(decision.accepted)

    def test_generic_title_without_year_goes_unmatched(self):
        intent = MatchIntent(
            raw_title="War",
            clean_title="war",
            year=None,
            media_type="movie",
        )
        decision = choose_best_candidate(intent, [
            MatchCandidate("imdb", "War", 2019, "movie", imdb_id="tt7430722"),
        ])
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "metadata_generic_title_needs_year")

    def test_normalize_title_removes_release_noise(self):
        self.assertEqual(
            normalize_title("Patriot.2026.Malayalam.1080p.ZEE5.WEB-DL.DD+5.1.H.264-JeRi.mkv"),
            "patriot jeri",
        )

    def test_release_site_prefix_builds_clean_title_variant(self):
        variants = build_title_variants(
            raw_title="www_1TamilMV_cards_Patriot_2026_TRUE_WEB_DL_1080p_AVC.mkv",
            parsed_title="www 1TamilMV cards Patriot",
            year=2026,
        )
        self.assertEqual(variants[0], "patriot")
        self.assertIn("patriot", variants)

    def test_clean_variant_accepts_correct_generic_title(self):
        intent = MatchIntent(
            raw_title="www 1TamilMV cards Patriot",
            clean_title="www 1tamilmv cards patriot",
            year=2026,
            media_type="movie",
            title_variants=["patriot", "www 1tamilmv cards patriot"],
        )
        decision = choose_best_candidate(intent, [
            MatchCandidate("imdb", "The Patriot", 2000, "movie", imdb_id="tt0187393"),
            MatchCandidate("database", "Patriot", 2026, "movie", imdb_id="tt33412884", tmdb_id=1300501),
        ])
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.candidate.imdb_id, "tt33412884")

    def test_generic_exact_title_beats_partial_title_with_same_year(self):
        intent = MatchIntent(
            raw_title="Patriot",
            clean_title="patriot",
            year=2026,
            media_type="movie",
            title_variants=["patriot"],
        )
        decision = choose_best_candidate(intent, [
            MatchCandidate("tmdb", "Patriot", 2026, "movie", tmdb_id=1300501),
            MatchCandidate("tmdb", "Antoni Patek, Patriot and Watchmaker", 2025, "movie", tmdb_id=1600775),
        ])
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.candidate.tmdb_id, 1300501)


if __name__ == "__main__":
    unittest.main()
