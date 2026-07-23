"""
providers/provider_mutagen.py - ZimaTAG Mutagen Provider
Provider utilisant la bibliothèque mutagen (fallback)
"""
from pathlib import Path
from typing import Dict
from providers.base_provider import BaseProvider

class MutagenProvider(BaseProvider):
    """Provider utilisant mutagen (optionnel)"""
    
    def __init__(self):
        super().__init__("mutagen")
        self.supported_formats = {'.mp3', '.flac', '.m4a', '.mp4', '.ogg', '.opus'}
        self._mutagen_available = self._check_mutagen()
    
    def _check_mutagen(self) -> bool:
        """Vérifie si mutagen est disponible"""
        try:
            import mutagen
            return True
        except ImportError:
            return False
    
    def extract_tags(self, filepath: Path) -> Dict[str, str]:
        """Extrait tags avec mutagen"""
        if not self._mutagen_available:
            return {}
        
        result = {}
        ext = filepath.suffix.lower()
        
        try:
            import mutagen
            from mutagen.easyid3 import EasyID3
            from mutagen.flac import FLAC
            from mutagen.mp4 import MP4
            
            if ext == '.mp3':
                result = self._extract_mp3(filepath)
            elif ext == '.flac':
                result = self._extract_flac(filepath)
            elif ext in ('.m4a', '.mp4'):
                result = self._extract_m4a(filepath)
            
        except Exception:
            pass
        
        return result
    
    def _extract_mp3(self, filepath: Path) -> Dict[str, str]:
        """Extrait tags MP3 via mutagen"""
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3
        
        result = {}
        audio = MP3(filepath)
        
        if audio.info:
            result['duration_seconds'] = round(audio.info.length, 2)
            result['duration'] = self.format_duration(audio.info.length)
            result['bitrate'] = str(audio.info.bitrate // 1000)
            result['samplerate'] = str(audio.info.sample_rate)
            result['channels'] = str(audio.info.channels)
        
        result['codec'] = 'MP3'
        return result
    
    def _extract_flac(self, filepath: Path) -> Dict[str, str]:
        """Extrait tags FLAC via mutagen"""
        from mutagen.flac import FLAC
        
        result = {}
        audio = FLAC(filepath)
        
        if audio.info:
            result['duration_seconds'] = round(audio.info.length, 2)
            result['duration'] = self.format_duration(audio.info.length)
            result['samplerate'] = str(audio.info.sample_rate)
            result['channels'] = str(audio.info.channels)
            result['bits_per_sample'] = str(audio.info.bits_per_sample)
            result['bitdepth'] = str(audio.info.bits_per_sample)
        
        # Tags Vorbis
        tag_map = {
            'title': 'title', 'artist': 'artist', 'album': 'album',
            'albumartist': 'albumartist', 'genre': 'genre', 'date': 'year'
        }
        for src, dst in tag_map.items():
            if src in audio:
                result[dst] = self.clean_value(audio[src][0])
        
        result['codec'] = 'FLAC'
        return result
    
    def _extract_m4a(self, filepath: Path) -> Dict[str, str]:
        """Extrait tags M4A via mutagen"""
        from mutagen.mp4 import MP4
        
        result = {}
        audio = MP4(filepath)
        
        if audio.info:
            result['duration_seconds'] = round(audio.info.length, 2)
            result['duration'] = self.format_duration(audio.info.length)
            result['samplerate'] = str(audio.info.sample_rate)
            result['channels'] = str(audio.info.channels)
            result['bitrate'] = str(audio.info.bitrate // 1000)
            if hasattr(audio.info, 'bits_per_sample'):
                result['bits_per_sample'] = str(audio.info.bits_per_sample)
                result['bitdepth'] = str(audio.info.bits_per_sample)
        
        # Tags iTunes
        tag_map = {
            '\xa9nam': 'title', '\xa9ART': 'artist', '\xa9alb': 'album',
            'aART': 'albumartist', '\xa9gen': 'genre', '\xa9day': 'year',
            '\xa9too': 'encoder'
        }
        for src, dst in tag_map.items():
            if src in audio:
                result[dst] = self.clean_value(audio[src][0])
        
        result['codec'] = 'AAC'
        return result
