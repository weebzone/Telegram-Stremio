"""
Quality Hierarchy System for comparing video qualities
Prevents accidental replacement of high-quality videos with lower quality
"""

import re
from typing import Dict, Optional, Tuple
from Backend.logger import LOGGER


class QualityChecker:
    """
    Checks and compares video quality based on source, resolution, codec, and audio.
    Based on real-world torrent quality patterns.
    """

    # Video Source Quality Rankings (higher = better)
    SOURCE_RANKINGS = {
        # Best Quality
        'bluray': 100, 'blu-ray': 100, 'brrip': 100, 'bdrip': 100,
        'uhd': 100, '4k': 100,

        # Excellent Quality
        'web-dl': 85, 'webdl': 85, 'web dl': 85, 'web.dl': 85,  # Added dot variant
        'webrip': 75, 'web-rip': 75, 'web.rip': 75,  # Added dot variant

        # Streaming Platform WEB-DLs (same quality as generic WEB-DL)
        'dsnp': 85, 'nf': 85, 'amzn': 85,  # Disney+, Netflix, Amazon (all WEB-DL quality)
        'atvp': 85, 'aptv': 85,  # Apple TV+
        'hmax': 85, 'hbo': 85,  # HBO Max

        # Good Quality
        'dvdrip': 60, 'dvd-rip': 60,
        'hdrip': 55, 'hd-rip': 55,

        # Decent Quality
        'hdtv': 50, 'hdtvrip': 50,

        # Lower Quality
        'dvdscr': 40, 'screener': 40,
        'r5': 35,

        # Poor Quality
        'hdcam': 25, 'hd-cam': 25, 'hdts': 25, 'hd-ts': 25,
        'cam': 15, 'camrip': 15, 'cam-rip': 15,
        'ts': 15, 'telesync': 15, 'tc': 15, 'telecine': 15,

        # Worst Quality
        'predvd': 10, 'workprint': 10, 'ppv': 10, 'vhsrip': 5
    }

    # Video Codec Rankings (higher = better)
    CODEC_RANKINGS = {
        'h265': 20, 'hevc': 20, 'x265': 20, 'h.265': 20,
        'av1': 18,
        'h264': 15, 'x264': 15, 'h.264': 15, 'avc': 15,
        'vp9': 12,
        'xvid': 8,
        'divx': 5,
        'mpeg': 3
    }

    # Audio Quality Rankings (higher = better)
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
        'mp3': 40,
        'stereo': 35, '2.0': 35,
        'mono': 20
    }

    # Resolution Rankings (higher = better)
    RESOLUTION_RANKINGS = {
        '2160p': 100, '4k': 100, 'uhd': 100,
        '1440p': 80, '2k': 80,
        '1080p': 70, 'fhd': 70,
        '720p': 50, 'hd': 50,
        '576p': 35,
        '480p': 30, 'sd': 30,
        '360p': 20,
        '240p': 10
    }

    # HDR/Color Rankings (bonus points)
    HDR_RANKINGS = {
        'hdr10+': 15, 'hdr10plus': 15,
        'hdr10': 12, 'hdr': 12,
        'dolby vision': 18, 'dv': 18,
        'hlg': 10,
        'sdr': 0
    }

    @staticmethod
    def parse_filename(filename: str) -> Dict[str, any]:
        """
        Parse filename to extract quality indicators.
        Returns dict with source, codec, audio, resolution, hdr, etc.
        """
        filename_lower = filename.lower()

        result = {
            'source': None,
            'source_score': 0,
            'codec': None,
            'codec_score': 0,
            'audio': None,
            'audio_score': 0,
            'resolution': None,
            'resolution_score': 0,
            'hdr': None,
            'hdr_score': 0,
            'is_10bit': '10bit' in filename_lower or '10-bit' in filename_lower,
            'bitrate_bonus': 0
        }

        # Find video source
        for source, score in QualityChecker.SOURCE_RANKINGS.items():
            if source in filename_lower:
                if score > result['source_score']:
                    result['source'] = source
                    result['source_score'] = score

        # Find codec
        for codec, score in QualityChecker.CODEC_RANKINGS.items():
            if codec in filename_lower:
                if score > result['codec_score']:
                    result['codec'] = codec
                    result['codec_score'] = score

        # Find audio
        for audio, score in QualityChecker.AUDIO_RANKINGS.items():
            if audio in filename_lower:
                if score > result['audio_score']:
                    result['audio'] = audio
                    result['audio_score'] = score

        # Find resolution
        for resolution, score in QualityChecker.RESOLUTION_RANKINGS.items():
            if resolution in filename_lower:
                if score > result['resolution_score']:
                    result['resolution'] = resolution
                    result['resolution_score'] = score

        # Find HDR
        for hdr, score in QualityChecker.HDR_RANKINGS.items():
            if hdr in filename_lower:
                if score > result['hdr_score']:
                    result['hdr'] = hdr
                    result['hdr_score'] = score

        # 10-bit bonus
        if result['is_10bit']:
            result['bitrate_bonus'] = 5

        # Default audio assumption for high-quality sources without explicit audio info
        # This prevents WEB-DL/BluRay from losing to lower sources just because audio isn't in filename
        if result['audio_score'] == 0:
            # High-quality sources typically have at least DD5.1 or better
            if result['source'] in ['bluray', 'blu-ray', 'brrip', 'bdrip', 'uhd', '4k']:
                result['audio'] = 'assumed-dd5.1'
                result['audio_score'] = 80  # Assume standard DD5.1 for BluRay
            elif result['source'] in ['web-dl', 'webdl', 'web dl', 'web.dl', 'dsnp', 'nf', 'amzn', 'atvp', 'aptv', 'hmax', 'hbo']:
                result['audio'] = 'assumed-ddp'
                result['audio_score'] = 85  # Streaming platforms typically use DD+ (EAC3)
            elif result['source'] in ['webrip', 'web-rip', 'web.rip']:
                result['audio'] = 'assumed-stereo'
                result['audio_score'] = 45  # Conservative assumption for WEBRip

        return result

    @staticmethod
    def calculate_total_score(parsed_quality: Dict) -> int:
        """
        Calculate total quality score from parsed quality data.
        """
        total = (
            parsed_quality['source_score'] +
            parsed_quality['codec_score'] +
            parsed_quality['audio_score'] +
            parsed_quality['resolution_score'] +
            parsed_quality['hdr_score'] +
            parsed_quality['bitrate_bonus']
        )
        return total

    @staticmethod
    def parse_file_size(size_str: str) -> float:
        """
        Parse file size string to MB.
        Examples: "2.5GB", "1500MB", "1.5 GB"
        """
        if not size_str:
            return 0.0

        size_str = size_str.upper().replace(' ', '')

        # Extract number
        match = re.search(r'([\d.]+)', size_str)
        if not match:
            return 0.0

        size = float(match.group(1))

        # Convert to MB
        if 'GB' in size_str:
            size *= 1024
        elif 'TB' in size_str:
            size *= 1024 * 1024
        elif 'KB' in size_str:
            size /= 1024

        return size

    @staticmethod
    def compare_quality(
        existing_filename: str,
        existing_size: str,
        new_filename: str,
        new_size: str
    ) -> Tuple[bool, str]:
        """
        Compare two video qualities.

        Returns:
            (should_replace: bool, reason: str)

        Logic:
        1. If new quality score > existing: REPLACE (better quality)
        2. If scores equal but new size < existing: REPLACE (same quality, smaller file)
        3. If new quality score < existing: DON'T REPLACE (worse quality)
        """
        # Parse both files
        existing_quality = QualityChecker.parse_filename(existing_filename)
        new_quality = QualityChecker.parse_filename(new_filename)

        # Calculate scores
        existing_score = QualityChecker.calculate_total_score(existing_quality)
        new_score = QualityChecker.calculate_total_score(new_quality)

        # Parse file sizes
        existing_size_mb = QualityChecker.parse_file_size(existing_size)
        new_size_mb = QualityChecker.parse_file_size(new_size)

        # Log comparison
        LOGGER.info(f"Quality Comparison:")
        LOGGER.info(f"  Existing: {existing_filename}")
        LOGGER.info(f"    Score: {existing_score} (source:{existing_quality['source_score']}, "
                   f"codec:{existing_quality['codec_score']}, audio:{existing_quality['audio_score']}, "
                   f"res:{existing_quality['resolution_score']}, hdr:{existing_quality['hdr_score']})")
        LOGGER.info(f"    Size: {existing_size} ({existing_size_mb:.2f} MB)")
        LOGGER.info(f"  New: {new_filename}")
        LOGGER.info(f"    Score: {new_score} (source:{new_quality['source_score']}, "
                   f"codec:{new_quality['codec_score']}, audio:{new_quality['audio_score']}, "
                   f"res:{new_quality['resolution_score']}, hdr:{new_quality['hdr_score']})")
        LOGGER.info(f"    Size: {new_size} ({new_size_mb:.2f} MB)")

        # Decision logic
        if new_score > existing_score:
            reason = (f"✅ REPLACE - Better quality "
                     f"(score: {new_score} > {existing_score})")
            LOGGER.info(f"  Decision: {reason}")
            return True, reason

        elif new_score == existing_score:
            # Same quality - prefer smaller file
            if new_size_mb > 0 and existing_size_mb > 0:
                if new_size_mb < existing_size_mb:
                    size_diff = existing_size_mb - new_size_mb
                    reason = (f"✅ REPLACE - Same quality, smaller file "
                             f"({new_size_mb:.2f}MB < {existing_size_mb:.2f}MB, saves {size_diff:.2f}MB)")
                    LOGGER.info(f"  Decision: {reason}")
                    return True, reason
                else:
                    reason = (f"⏭️ SKIP - Same quality, but larger file "
                             f"({new_size_mb:.2f}MB > {existing_size_mb:.2f}MB)")
                    LOGGER.info(f"  Decision: {reason}")
                    return False, reason
            else:
                # Can't compare size, allow replacement
                reason = f"✅ REPLACE - Same quality, size unknown"
                LOGGER.info(f"  Decision: {reason}")
                return True, reason

        else:  # new_score < existing_score
            reason = (f"❌ SKIP - Lower quality detected! "
                     f"(score: {new_score} < {existing_score})\n"
                     f"Existing: {existing_quality['source'] or 'unknown'} "
                     f"{existing_quality['resolution'] or ''} "
                     f"{existing_quality['codec'] or ''} "
                     f"{existing_quality['audio'] or ''}\n"
                     f"New: {new_quality['source'] or 'unknown'} "
                     f"{new_quality['resolution'] or ''} "
                     f"{new_quality['codec'] or ''} "
                     f"{new_quality['audio'] or ''}")
            LOGGER.warning(f"  Decision: {reason}")
            return False, reason

    @staticmethod
    def should_replace_quality(
        existing_quality_label: str,
        existing_quality_name: str,
        existing_quality_size: str,
        new_quality_label: str,
        new_quality_name: str,
        new_quality_size: str
    ) -> Tuple[bool, str]:
        """
        Main entry point for quality comparison.

        Args:
            existing_quality_label: Resolution label like "1080p", "720p"
            existing_quality_name: Full filename
            existing_quality_size: File size string like "2.5GB"
            new_quality_label: Resolution label
            new_quality_name: Full filename
            new_quality_size: File size string

        Returns:
            (should_replace: bool, reason: str)
        """
        # If different resolution labels, use old logic (always replace)
        if existing_quality_label != new_quality_label:
            reason = f"Different resolution ({existing_quality_label} vs {new_quality_label}) - using default replacement"
            LOGGER.info(reason)
            return True, reason

        # Same resolution label - use quality hierarchy
        return QualityChecker.compare_quality(
            existing_quality_name,
            existing_quality_size,
            new_quality_name,
            new_quality_size
        )
