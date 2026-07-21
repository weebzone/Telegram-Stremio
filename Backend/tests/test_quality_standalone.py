"""
Standalone Quality Checker Test
Run directly without full Backend imports
"""

import sys
import re
from typing import Dict, Tuple


class QualityChecker:
    """Quality comparison system"""
    
    SOURCE_RANKINGS = {
        'bluray': 100, 'blu-ray': 100, 'brrip': 100, 'bdrip': 100,
        'uhd': 100, '4k': 100,
        'web-dl': 85, 'webdl': 85, 'web dl': 85, 'web.dl': 85,
        'webrip': 75, 'web-rip': 75, 'web.rip': 75,
        'dsnp': 85, 'nf': 85, 'amzn': 85,
        'atvp': 85, 'aptv': 85, 'hmax': 85, 'hbo': 85,
        'dvdrip': 60, 'dvd-rip': 60,
        'hdrip': 55, 'hd-rip': 55,
        'hdtv': 50, 'hdtvrip': 50,
        'dvdscr': 40, 'screener': 40,
        'r5': 35,
        'hdcam': 25, 'hd-cam': 25, 'hdts': 25, 'hd-ts': 25,
        'cam': 15, 'camrip': 15, 'cam-rip': 15,
        'ts': 15, 'telesync': 15, 'tc': 15, 'telecine': 15,
        'predvd': 10, 'workprint': 10, 'ppv': 10, 'vhsrip': 5
    }
    
    CODEC_RANKINGS = {
        'h265': 20, 'hevc': 20, 'x265': 20, 'h.265': 20,
        'av1': 18,
        'h264': 15, 'x264': 15, 'h.264': 15, 'avc': 15,
        'vp9': 12, 'xvid': 8, 'divx': 5, 'mpeg': 3
    }
    
    AUDIO_RANKINGS = {
        'atmos': 100, 'dolby atmos': 100,
        'truehd': 95, 'dts-hd': 95, 'dts-hd ma': 95,
        'ddp': 90, 'dd+': 90, 'eac3': 90,
        'dts': 85, 'dts-x': 88,
        'dd5.1': 80, 'dd 5.1': 80, 'ac3': 80, 'dolby digital': 80,
        'ddp5.1': 85, 'dd+5.1': 85,
        'aac5.1': 65, 'aac 5.1': 65,
        'opus': 60,
        'aac2.0': 50, 'aac 2.0': 50, 'aac': 50,
        'mp3': 40, 'stereo': 35, '2.0': 35, 'mono': 20
    }
    
    RESOLUTION_RANKINGS = {
        '2160p': 100, '4k': 100, 'uhd': 100,
        '1440p': 80, '2k': 80,
        '1080p': 70, 'fhd': 70,
        '720p': 50, 'hd': 50,
        '576p': 35, '480p': 30, 'sd': 30,
        '360p': 20, '240p': 10
    }
    
    HDR_RANKINGS = {
        'hdr10+': 15, 'hdr10plus': 15,
        'hdr10': 12, 'hdr': 12,
        'dolby vision': 18, 'dv': 18,
        'hlg': 10, 'sdr': 0
    }
    
    @staticmethod
    def parse_filename(filename: str) -> Dict:
        filename_lower = filename.lower()
        result = {
            'source': None, 'source_score': 0,
            'codec': None, 'codec_score': 0,
            'audio': None, 'audio_score': 0,
            'resolution': None, 'resolution_score': 0,
            'hdr': None, 'hdr_score': 0,
            'is_10bit': '10bit' in filename_lower or '10-bit' in filename_lower,
            'bitrate_bonus': 0
        }
        
        for source, score in QualityChecker.SOURCE_RANKINGS.items():
            if source in filename_lower:
                if score > result['source_score']:
                    result['source'] = source
                    result['source_score'] = score
        
        for codec, score in QualityChecker.CODEC_RANKINGS.items():
            if codec in filename_lower:
                if score > result['codec_score']:
                    result['codec'] = codec
                    result['codec_score'] = score
        
        for audio, score in QualityChecker.AUDIO_RANKINGS.items():
            if audio in filename_lower:
                if score > result['audio_score']:
                    result['audio'] = audio
                    result['audio_score'] = score
        
        for resolution, score in QualityChecker.RESOLUTION_RANKINGS.items():
            if resolution in filename_lower:
                if score > result['resolution_score']:
                    result['resolution'] = resolution
                    result['resolution_score'] = score
        
        for hdr, score in QualityChecker.HDR_RANKINGS.items():
            if hdr in filename_lower:
                if score > result['hdr_score']:
                    result['hdr'] = hdr
                    result['hdr_score'] = score
        
        if result['is_10bit']:
            result['bitrate_bonus'] = 5
        
        # Default audio assumption for high-quality sources without explicit audio info
        if result['audio_score'] == 0:
            if result['source'] in ['bluray', 'blu-ray', 'brrip', 'bdrip', 'uhd', '4k']:
                result['audio'] = 'assumed-dd5.1'
                result['audio_score'] = 80
            elif result['source'] in ['web-dl', 'webdl', 'web dl', 'web.dl', 'dsnp', 'nf', 'amzn', 'atvp', 'aptv', 'hmax', 'hbo']:
                result['audio'] = 'assumed-ddp'
                result['audio_score'] = 85
            elif result['source'] in ['webrip', 'web-rip', 'web.rip']:
                result['audio'] = 'assumed-stereo'
                result['audio_score'] = 45
        
        return result
    
    @staticmethod
    def calculate_total_score(parsed_quality: Dict) -> int:
        return (
            parsed_quality['source_score'] +
            parsed_quality['codec_score'] +
            parsed_quality['audio_score'] +
            parsed_quality['resolution_score'] +
            parsed_quality['hdr_score'] +
            parsed_quality['bitrate_bonus']
        )
    
    @staticmethod
    def parse_file_size(size_str: str) -> float:
        if not size_str:
            return 0.0
        size_str = size_str.upper().replace(' ', '')
        match = re.search(r'([\d.]+)', size_str)
        if not match:
            return 0.0
        size = float(match.group(1))
        if 'GB' in size_str:
            size *= 1024
        elif 'TB' in size_str:
            size *= 1024 * 1024
        elif 'KB' in size_str:
            size /= 1024
        return size
    
    @staticmethod
    def compare_quality(existing_filename: str, existing_size: str,
                       new_filename: str, new_size: str) -> Tuple[bool, str]:
        existing_quality = QualityChecker.parse_filename(existing_filename)
        new_quality = QualityChecker.parse_filename(new_filename)
        
        existing_score = QualityChecker.calculate_total_score(existing_quality)
        new_score = QualityChecker.calculate_total_score(new_quality)
        
        existing_size_mb = QualityChecker.parse_file_size(existing_size)
        new_size_mb = QualityChecker.parse_file_size(new_size)
        
        print(f"\n  Existing: {existing_filename}")
        print(f"    Score: {existing_score} (source:{existing_quality['source_score']}, "
              f"codec:{existing_quality['codec_score']}, audio:{existing_quality['audio_score']}, "
              f"res:{existing_quality['resolution_score']}, hdr:{existing_quality['hdr_score']})")
        print(f"    Size: {existing_size} ({existing_size_mb:.2f} MB)")
        print(f"  New: {new_filename}")
        print(f"    Score: {new_score} (source:{new_quality['source_score']}, "
              f"codec:{new_quality['codec_score']}, audio:{new_quality['audio_score']}, "
              f"res:{new_quality['resolution_score']}, hdr:{new_quality['hdr_score']})")
        print(f"    Size: {new_size} ({new_size_mb:.2f} MB)")
        
        if new_score > existing_score:
            reason = f"✅ REPLACE - Better quality (score: {new_score} > {existing_score})"
            return True, reason
        elif new_score == existing_score:
            if new_size_mb > 0 and existing_size_mb > 0:
                if new_size_mb < existing_size_mb:
                    size_diff = existing_size_mb - new_size_mb
                    reason = f"✅ REPLACE - Same quality, smaller file (saves {size_diff:.2f}MB)"
                    return True, reason
                else:
                    reason = f"⏭️ SKIP - Same quality, but larger file"
                    return False, reason
            else:
                reason = f"✅ REPLACE - Same quality, size unknown"
                return True, reason
        else:
            reason = f"❌ SKIP - Lower quality (score: {new_score} < {existing_score})"
            return False, reason


# Test Cases
def run_tests():
    tests = [
        {
            'name': 'BluRay vs HDCam (same resolution)',
            'existing': ('Movie.2023.1080p.BluRay.DD5.1.mkv', '3.2GB'),
            'new': ('Movie.2023.1080p.HDCam.AAC.2.0.mkv', '1.5GB'),
            'expected': False,
            'reason': 'Must NOT replace BluRay with HDCam'
        },
        {
            'name': 'HDCam to BluRay upgrade',
            'existing': ('Movie.2023.1080p.HDCam.AAC.2.0.mkv', '1.5GB'),
            'new': ('Movie.2023.1080p.BluRay.DD5.1.mkv', '3.2GB'),
            'expected': True,
            'reason': 'Should upgrade from HDCam to BluRay'
        },
        {
            'name': 'Same quality, prefer smaller HEVC',
            'existing': ('Movie.2023.1080p.BluRay.x264.DD5.1.mkv', '3.5GB'),
            'new': ('Movie.2023.1080p.BluRay.x265.HEVC.DD5.1.mkv', '2.1GB'),
            'expected': True,
            'reason': 'Should prefer smaller file with same quality'
        },
        {
            'name': 'Same quality, reject larger file',
            'existing': ('Movie.2023.1080p.BluRay.x265.HEVC.DD5.1.mkv', '2.1GB'),
            'new': ('Movie.2023.1080p.BluRay.x264.DD5.1.mkv', '3.5GB'),
            'expected': False,
            'reason': 'Should reject larger file with same quality'
        },
        {
            'name': 'WEB-DL vs WEBRip',
            'existing': ('Movie.2023.1080p.WEB-DL.DD5.1.mkv', '2.8GB'),
            'new': ('Movie.2023.1080p.WEBRip.AAC.2.0.mkv', '2.0GB'),
            'expected': False,
            'reason': 'WEB-DL should beat WEBRip'
        },
        {
            'name': 'Real-world: Avengers BluRay vs HDCam',
            'existing': ('Avengers.Endgame.2019.1080p.BluRay.DD5.1.x265.10bit-Galaxy', '3.0GB'),
            'new': ('Avengers.Endgame.2019.1080p.HDCam.AAC.2.0.mkv', '2.1GB'),
            'expected': False,
            'reason': 'Must protect high-quality BluRay from HDCam replacement'
        },
        {
            'name': 'Real-world: WEBRip DDP5.1 vs WEB-DL (no audio in filename)',
            'existing': ('Mission.Impossible.The.Final.Reckoning.2025.1080p.WEBRip.DDP5.1.x265-NeoNoir.mkv', '2.80GB'),
            'new': ('Mission Impossible The Final Reckoning 2025 IMAX 1080p WEB-DL HEVC x265-RMTeam.mkv', '2.87GB'),
            'expected': True,
            'reason': 'WEB-DL should replace WEBRip even without explicit audio (assumes DD5.1)'
        },
        {
            'name': 'Real-world: WEB-DL vs AMZN WEB-DL (dot separator)',
            'existing': ('The.Conjuring.Last.Rites.2025.WebDl.Rip.1080p.H265.AC3.mkv', '2.35GB'),
            'new': ('The.Conjuring.Last.Rites.2025.1080p.HEVC.AMZN.WEB.DL.Multi.H.265.mkv', '3.25GB'),
            'expected': True,
            'reason': 'AMZN WEB.DL (with dots) should be detected and replace WebDl Rip'
        },
        {
            'name': 'Real-world: WEBRip vs web.dl (lowercase with dots)',
            'existing': ('Dangerous.Animals.2025.1080p.WEBRip.DDP5.1.x265-NeoNoir.mkv', '1.62GB'),
            'new': ('dangerous.animals.2025.1080p.web.dl.hevc.x265.rmteam.mkv', '1.20GB'),
            'expected': True,
            'reason': 'web.dl (lowercase with dots) should be detected as WEB-DL and replace WEBRip'
        },
    ]
    
    print("\n" + "="*70)
    print("QUALITY HIERARCHY SYSTEM - TEST RESULTS")
    print("="*70)
    
    passed = 0
    failed = 0
    
    for test in tests:
        print(f"\n🧪 Test: {test['name']}")
        print(f"Expected: {'REPLACE' if test['expected'] else 'SKIP'} - {test['reason']}")
        
        should_replace, reason = QualityChecker.compare_quality(
            test['existing'][0], test['existing'][1],
            test['new'][0], test['new'][1]
        )
        
        print(f"  Result: {reason}")
        
        if should_replace == test['expected']:
            print(f"  ✅ PASS")
            passed += 1
        else:
            print(f"  ❌ FAIL - Expected {test['expected']}, got {should_replace}")
            failed += 1
    
    print("\n" + "="*70)
    print(f"FINAL RESULTS: {passed} PASSED, {failed} FAILED")
    print("="*70 + "\n")
    
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
