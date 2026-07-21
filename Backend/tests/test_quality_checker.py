"""
Test cases for Quality Hierarchy System
Run with: uv run python -m pytest Backend/tests/test_quality_checker.py -v
"""

import pytest
from Backend.helper.quality_checker import QualityChecker


class TestQualityChecker:
    """Test Quality Hierarchy System"""
    
    def test_bluray_vs_hdcam_same_resolution(self):
        """BluRay should always beat HDCam even at same resolution"""
        should_replace, reason = QualityChecker.compare_quality(
            existing_filename="Movie.2023.1080p.BluRay.DD5.1.mkv",
            existing_size="3.2GB",
            new_filename="Movie.2023.1080p.HDCam.AAC.2.0.mkv",
            new_size="1.5GB"
        )
        assert should_replace == False, "Should NOT replace BluRay with HDCam"
        assert "Lower quality" in reason
    
    def test_hdcam_to_bluray_upgrade(self):
        """HDCam should be replaced by BluRay"""
        should_replace, reason = QualityChecker.compare_quality(
            existing_filename="Movie.2023.1080p.HDCam.AAC.2.0.mkv",
            existing_size="1.5GB",
            new_filename="Movie.2023.1080p.BluRay.DD5.1.mkv",
            new_size="3.2GB"
        )
        assert should_replace == True, "Should replace HDCam with BluRay"
        assert "Better quality" in reason
    
    def test_web_dl_vs_webrip(self):
        """WEB-DL should be preferred over WEBRip"""
        should_replace, reason = QualityChecker.compare_quality(
            existing_filename="Movie.2023.1080p.WEB-DL.DD5.1.mkv",
            existing_size="2.8GB",
            new_filename="Movie.2023.1080p.WEBRip.AAC.2.0.mkv",
            new_size="2.0GB"
        )
        assert should_replace == False, "Should NOT replace WEB-DL with WEBRip"
    
    def test_same_quality_prefer_smaller_size(self):
        """Same quality - prefer HEVC smaller file"""
        should_replace, reason = QualityChecker.compare_quality(
            existing_filename="Movie.2023.1080p.BluRay.x264.DD5.1.mkv",
            existing_size="3.5GB",
            new_filename="Movie.2023.1080p.BluRay.x265.HEVC.DD5.1.mkv",
            new_size="2.1GB"
        )
        assert should_replace == True, "Should replace with smaller file"
        assert "smaller file" in reason.lower()
    
    def test_same_quality_reject_larger_size(self):
        """Same quality - reject larger file"""
        should_replace, reason = QualityChecker.compare_quality(
            existing_filename="Movie.2023.1080p.BluRay.x265.HEVC.DD5.1.mkv",
            existing_size="2.1GB",
            new_filename="Movie.2023.1080p.BluRay.x264.DD5.1.mkv",
            new_size="3.5GB"
        )
        assert should_replace == False, "Should NOT replace with larger file"
        assert "larger file" in reason.lower()
    
    def test_dd5_1_vs_aac(self):
        """DD5.1 audio should beat AAC 2.0"""
        should_replace, reason = QualityChecker.compare_quality(
            existing_filename="Movie.2023.1080p.BluRay.DD5.1.mkv",
            existing_size="3GB",
            new_filename="Movie.2023.1080p.BluRay.AAC.2.0.mkv",
            new_size="2.5GB"
        )
        assert should_replace == False, "Should NOT downgrade audio quality"
    
    def test_hdr_bonus(self):
        """HDR10 should add bonus points"""
        should_replace, reason = QualityChecker.compare_quality(
            existing_filename="Movie.2023.1080p.WEB-DL.AAC.mkv",
            existing_size="2.5GB",
            new_filename="Movie.2023.1080p.WEB-DL.HDR10.AAC.mkv",
            new_size="2.5GB"
        )
        assert should_replace == True, "HDR version should win"
    
    def test_10bit_bonus(self):
        """10-bit encoding should add bonus"""
        should_replace, reason = QualityChecker.compare_quality(
            existing_filename="Movie.2023.1080p.WEB-DL.x264.mkv",
            existing_size="2.5GB",
            new_filename="Movie.2023.1080p.WEB-DL.x264.10bit.mkv",
            new_size="2.5GB"
        )
        assert should_replace == True, "10-bit version should win"
    
    def test_different_resolution_always_replace(self):
        """Different resolutions should use default logic"""
        should_replace, reason = QualityChecker.should_replace_quality(
            existing_quality_label="720p",
            existing_quality_name="Movie.2023.720p.BluRay.mkv",
            existing_quality_size="2GB",
            new_quality_label="1080p",
            new_quality_name="Movie.2023.1080p.HDCam.mkv",
            new_quality_size="1.5GB"
        )
        assert should_replace == True, "Different resolutions use default logic"
        assert "Different resolution" in reason
    
    def test_filename_parsing_complex(self):
        """Test parsing complex real-world filename"""
        parsed = QualityChecker.parse_filename(
            "Avengers.Endgame.2019.1080p.BluRay.x265.10bit.DD5.1.HEVC-Galaxy"
        )
        assert parsed['source'] == 'bluray'
        assert parsed['codec'] in ['x265', 'hevc']
        assert parsed['resolution'] == '1080p'
        assert parsed['is_10bit'] == True
        assert 'dd5.1' in parsed['audio'] or 'dd 5.1' in parsed['audio']
    
    def test_size_parsing(self):
        """Test file size parsing"""
        assert QualityChecker.parse_file_size("2.5GB") == pytest.approx(2560, rel=1)
        assert QualityChecker.parse_file_size("1500MB") == pytest.approx(1500, rel=1)
        assert QualityChecker.parse_file_size("3.2 GB") == pytest.approx(3276.8, rel=1)
        assert QualityChecker.parse_file_size("500KB") == pytest.approx(0.488, rel=0.1)
    
    def test_real_world_scenario_1(self):
        """Test based on user's Avengers Endgame example"""
        # Good BluRay DD5.1 vs HDCam AAC
        should_replace, reason = QualityChecker.compare_quality(
            existing_filename="Avengers.Endgame.2019.1080p.BluRay.DD5.1.x265.10bit-Galaxy",
            existing_size="3.0GB",
            new_filename="Avengers.Endgame.2019.1080p.HDCam.AAC.2.0.mkv",
            new_size="2.1GB"
        )
        assert should_replace == False, "Must NOT replace BluRay with HDCam"
    
    def test_real_world_scenario_2(self):
        """Test upgrading from WEBRip to BluRay"""
        should_replace, reason = QualityChecker.compare_quality(
            existing_filename="Movie.2023.1080p.WEBRip.x264.AAC.mkv",
            existing_size="2.5GB",
            new_filename="Movie.2023.1080p.BluRay.x265.DD5.1.10bit.mkv",
            new_size="2.8GB"
        )
        assert should_replace == True, "Should upgrade WEBRip to BluRay"
    
    def test_platform_sources(self):
        """Test streaming platform sources (DSNP, AMZN, NF)"""
        should_replace, reason = QualityChecker.compare_quality(
            existing_filename="Movie.2023.1080p.DSNP.WEB-DL.DD5.1.mkv",
            existing_size="3GB",
            new_filename="Movie.2023.1080p.HDRip.AAC.mkv",
            new_size="2GB"
        )
        assert should_replace == False, "DSNP WEB-DL should beat HDRip"


if __name__ == "__main__":
    # Run tests manually
    test = TestQualityChecker()
    
    print("Running Quality Hierarchy Tests...\n")
    
    tests = [
        ("BluRay vs HDCam", test.test_bluray_vs_hdcam_same_resolution),
        ("HDCam to BluRay upgrade", test.test_hdcam_to_bluray_upgrade),
        ("WEB-DL vs WEBRip", test.test_web_dl_vs_webrip),
        ("Same quality prefer smaller", test.test_same_quality_prefer_smaller_size),
        ("Same quality reject larger", test.test_same_quality_reject_larger_size),
        ("DD5.1 vs AAC", test.test_dd5_1_vs_aac),
        ("HDR bonus", test.test_hdr_bonus),
        ("10-bit bonus", test.test_10bit_bonus),
        ("Different resolution", test.test_different_resolution_always_replace),
        ("Complex filename parsing", test.test_filename_parsing_complex),
        ("Size parsing", test.test_size_parsing),
        ("Real-world scenario 1", test.test_real_world_scenario_1),
        ("Real-world scenario 2", test.test_real_world_scenario_2),
        ("Platform sources", test.test_platform_sources),
    ]
    
    passed = 0
    failed = 0
    
    for name, test_func in tests:
        try:
            test_func()
            print(f"✅ PASS: {name}")
            passed += 1
        except AssertionError as e:
            print(f"❌ FAIL: {name}")
            print(f"   Error: {e}\n")
            failed += 1
        except Exception as e:
            print(f"❌ ERROR: {name}")
            print(f"   Error: {e}\n")
            failed += 1
    
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*50}")
