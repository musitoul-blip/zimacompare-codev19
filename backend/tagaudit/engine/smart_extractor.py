"""
engine/smart_extractor.py - ZimaTAG SmartExtractor
Extraction intelligente avec stratégies configurables via ProviderManager
"""
from pathlib import Path
from typing import Dict, Optional
import hashlib
from datetime import datetime
from providers import ProviderManager
from core import logger, config

class SmartExtractor:
    """Extracteur intelligent de métadonnées audio avec providers"""
    
    def __init__(self, strategy_file: Optional[Path] = None):
        self.provider_manager = ProviderManager(strategy_file)
    
    def extract(self, filepath: Path) -> Dict[str, str]:
        """Extrait les métadonnées d'un fichier audio"""
        result = self._init_result(filepath)
        
        try:
            ext = filepath.suffix.lower()
            if ext not in config.AUDIO_EXTENSIONS:
                result['error'] = f"Format non supporté: {ext}"
                return result
            
            # Calcul MD5 du fichier audio
            result['file_md5'] = self._compute_file_md5(filepath)
            
            # Extraction via ProviderManager
            extracted = self.provider_manager.extract(filepath)
            result.update(extracted)
            
            # Complète les infos de pochette si présente.
            # On extrait MD5 + format + taille + dimensions en UNE SEULE lecture
            # (évite de reparser la cover plusieurs fois).
            if result.get('has_cover') == 'Yes':
                need_md5 = not result.get('cover_md5')
                need_format = not result.get('cover_format')
                need_dims = (not result.get('cover_width')
                             or not result.get('cover_height'))
                need_valid = not result.get('cover_valid')
                if need_md5 or need_format or need_dims or need_valid:
                    cover_info = self._extract_cover_info(filepath)
                    if need_md5 and cover_info.get('cover_md5'):
                        result['cover_md5'] = cover_info['cover_md5']
                    if need_format and cover_info.get('cover_format'):
                        result['cover_format'] = cover_info['cover_format']
                    if need_dims:
                        if cover_info.get('cover_width'):
                            result['cover_width'] = cover_info['cover_width']
                        if cover_info.get('cover_height'):
                            result['cover_height'] = cover_info['cover_height']
                    # Complète aussi cover_size si absent
                    if not result.get('cover_size') and cover_info.get('cover_size'):
                        result['cover_size'] = cover_info['cover_size']
                    if need_valid:
                        result['cover_valid'] = cover_info.get('cover_valid', '')
                        result['cover_error'] = cover_info.get('cover_error', '')
                # F23(a) : nombre d'images embarquees dans le fichier (mutagen)
                result['cover_count'] = self._count_covers(filepath)
            
        except Exception as e:
            result['error'] = str(e)
            logger.error(f"Extraction error {filepath.name}: {e}")
        
        return result
    
    def _init_result(self, filepath: Path) -> Dict[str, str]:
        """Initialise le résultat avec les infos fichier"""
        stat = filepath.stat()
        return {
            'filepath': str(filepath),
            'filename': filepath.name,
            'extension': filepath.suffix.lower().replace('.', ''),
            'directory': str(filepath.parent),
            'parent_folder': filepath.parent.name,
            'size_mb': round(stat.st_size / (1024 * 1024), 2),
            'modified_date': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
            'file_md5': '',
            'title': '', 'artist': '', 'album': '', 'albumartist': '',
            'composer': '', 'genre': '', 'year': '', 'track': '',
            'tracktotal': '', 'disc': '', 'disctotal': '', 'encoder': '',
            'duration': '', 'duration_seconds': 0, 'bitrate': '',
            'samplerate': '', 'channels': '', 'bitdepth': '', 'codec': '',
            'id3_version': '', 'has_cover': 'No', 'cover_size': 0,
            'cover_format': '', 'cover_width': 0, 'cover_height': 0,
            'cover_md5': '', 'cover_valid': '', 'cover_error': '',
            'cover_count': 0, 'error': ''
        }
    
    def _compute_file_md5(self, filepath: Path) -> str:
        """Calcule MD5 partiel du fichier (8KB début + 8KB milieu + 8KB fin)"""
        try:
            h = hashlib.md5()
            file_size = filepath.stat().st_size
            chunk_size = 8192  # 8KB
            
            with open(filepath, 'rb') as f:
                # Lit les premiers 8KB
                h.update(f.read(chunk_size))
                
                # Si fichier > 24KB, lit aussi le milieu et la fin
                if file_size > chunk_size * 3:
                    # Milieu du fichier
                    f.seek(file_size // 2 - chunk_size // 2)
                    h.update(f.read(chunk_size))
                    
                    # Fin du fichier
                    f.seek(-chunk_size, 2)
                    h.update(f.read(chunk_size))
                elif file_size > chunk_size:
                    # Fichier petit: lit juste la fin
                    f.seek(-min(chunk_size, file_size - chunk_size), 2)
                    h.update(f.read(chunk_size))
            
            return h.hexdigest()
        except Exception:
            return ''
    
    def _count_covers(self, filepath: Path) -> int:
        """F23(a) - compte les images embarquees (mutagen).
        MP3 frames APIC ; FLAC pictures ; M4A/MP4 entrees 'covr'. 0 si indetermine."""
        ext = filepath.suffix.lower()
        try:
            if ext == '.mp3':
                from mutagen.id3 import ID3
                try:
                    return len(ID3(str(filepath)).getall('APIC'))
                except Exception:
                    return 0
            if ext == '.flac':
                from mutagen.flac import FLAC
                return len(FLAC(str(filepath)).pictures)
            if ext in ('.m4a', '.mp4'):
                from mutagen.mp4 import MP4
                tags = MP4(str(filepath)).tags
                covr = tags.get('covr') if tags else None
                return len(covr) if covr else 0
        except Exception as e:
            logger.debug(f"_count_covers({filepath.name}): {e}")
        return 0

    def _extract_cover_info(self, filepath: Path) -> Dict[str, object]:
        """Extrait les infos de pochette en UNE SEULE lecture du fichier.
        
        Retourne un dict contenant :
          - cover_md5     : hash MD5 de la pochette embarquée (str, '' si absent)
          - cover_format  : mime type détecté via magic bytes
                            ('image/jpeg', 'image/png', 'image/webp', ...)
          - cover_size    : taille en octets des données de pochette (int)
          - cover_width   : largeur en pixels (int, 0 si indéterminé)
          - cover_height  : hauteur en pixels (int, 0 si indéterminé)
        
        La détection du format repose sur les magic bytes de l'image.
        La détection des dimensions repose sur les headers propres à chaque
        format (PNG IHDR, JPEG SOFx, GIF LSD, BMP DIB, WEBP VP8/VP8L/VP8X).
        Aucune dépendance externe : tout est fait en stdlib.
        """
        info = {
            'cover_md5': '', 'cover_format': '', 'cover_size': 0,
            'cover_width': 0, 'cover_height': 0,
        }
        try:
            from parsers import MP3Parser, FLACParser, M4AParser
            ext = filepath.suffix.lower()
            parsers = {'.mp3': MP3Parser, '.flac': FLACParser,
                       '.m4a': M4AParser, '.mp4': M4AParser}
            parser_cls = parsers.get(ext)
            if parser_cls:
                parser = parser_cls(filepath)
                result = parser.parse()
                cover = result.get('cover_data')
                if cover:
                    info['cover_md5'] = hashlib.md5(cover).hexdigest()
                    info['cover_size'] = len(cover)
                    info['cover_format'] = self._detect_cover_format(cover)
                    # PIL primaire (dims fiables + validation en UNE ouverture) ;
                    # parser maison en fallback si PIL echoue/format non gere.
                    w, h = self._detect_cover_dimensions(cover)
                    try:
                        from io import BytesIO
                        from PIL import Image
                        _img = Image.open(BytesIO(cover))
                        _img.load()
                        if _img.size and _img.size[0] > 0 and _img.size[1] > 0:
                            w, h = _img.size
                        info['cover_valid'] = 'Yes'
                    except Exception as ce:
                        info['cover_valid'] = 'No'
                        info['cover_error'] = str(ce)[:200]
                    info['cover_width'] = w
                    info['cover_height'] = h
        except Exception as e:
            logger.debug(f"_extract_cover_info({filepath.name}): {e}")
        return info
    
    @staticmethod
    def _detect_cover_format(cover_data: bytes) -> str:
        """Détecte le format d'une image depuis ses magic bytes."""
        if not cover_data or len(cover_data) < 12:
            return ''
        if cover_data[:3] == b'\xff\xd8\xff':
            return 'image/jpeg'
        if cover_data[:8] == b'\x89PNG\r\n\x1a\n':
            return 'image/png'
        if cover_data[:4] == b'GIF8':
            return 'image/gif'
        if cover_data[:2] == b'BM':
            return 'image/bmp'
        if cover_data[:4] == b'RIFF' and cover_data[8:12] == b'WEBP':
            return 'image/webp'
        if cover_data[:4] in (b'II\x2a\x00', b'MM\x00\x2a'):
            return 'image/tiff'
        return 'image/unknown'
    
    @staticmethod
    def _detect_cover_dimensions(cover_data: bytes) -> tuple:
        """Retourne (width, height) d'une image depuis ses headers.
        
        Retourne (0, 0) si format inconnu ou données trop courtes.
        Utilise uniquement la stdlib (struct) — zéro dépendance externe.
        
        Formats supportés :
          - PNG  : via chunk IHDR (offset 16-24)
          - JPEG : via parsing des segments SOFx
          - GIF  : via Logical Screen Descriptor (offset 6-10)
          - BMP  : via DIB header (offset 18-26)
          - WEBP : via VP8 / VP8L / VP8X chunks
        """
        import struct
        data = cover_data
        if not data or len(data) < 10:
            return (0, 0)
        
        # --- PNG ---
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            if len(data) >= 24 and data[12:16] == b'IHDR':
                w = struct.unpack('>I', data[16:20])[0]
                h = struct.unpack('>I', data[20:24])[0]
                return (w, h)
            return (0, 0)
        
        # --- GIF ---
        if data[:6] in (b'GIF87a', b'GIF89a'):
            w = struct.unpack('<H', data[6:8])[0]
            h = struct.unpack('<H', data[8:10])[0]
            return (w, h)
        
        # --- BMP ---
        if data[:2] == b'BM' and len(data) >= 26:
            w = struct.unpack('<i', data[18:22])[0]
            h = struct.unpack('<i', data[22:26])[0]
            return (abs(w), abs(h))
        
        # --- WEBP ---
        if data[:4] == b'RIFF' and len(data) >= 16 and data[8:12] == b'WEBP':
            chunk_type = data[12:16]
            if chunk_type == b'VP8 ' and len(data) >= 30:
                w = struct.unpack('<H', data[26:28])[0] & 0x3FFF
                h = struct.unpack('<H', data[28:30])[0] & 0x3FFF
                return (w, h)
            elif chunk_type == b'VP8L' and len(data) >= 25:
                b = data[21:25]
                w = ((b[1] & 0x3F) << 8 | b[0]) + 1
                h = ((b[3] & 0x0F) << 10 | b[2] << 2 | (b[1] & 0xC0) >> 6) + 1
                return (w, h)
            elif chunk_type == b'VP8X' and len(data) >= 30:
                w = (data[24] | data[25] << 8 | data[26] << 16) + 1
                h = (data[27] | data[28] << 8 | data[29] << 16) + 1
                return (w, h)
            return (0, 0)
        
        # --- JPEG ---
        if data[:3] == b'\xff\xd8\xff':
            # Parsing des segments jusqu'au premier SOFx (Start Of Frame).
            # SOFx markers : C0-C3, C5-C7, C9-CB, CD-CF.
            # Format segment : FF + marker(1) + length(2 BE) + data
            # Format SOFx   : ... + precision(1) + height(2 BE) + width(2 BE)
            sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                           0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
            i, n = 2, len(data)
            while i < n - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                # Skip padding 0xFF
                while i < n and data[i] == 0xFF:
                    i += 1
                if i >= n:
                    break
                marker = data[i]
                i += 1
                if marker == 0x00:
                    continue  # FF 00 = octet 0xFF littéral, pas un marker
                if marker in sof_markers:
                    if i + 7 < n:
                        h = struct.unpack('>H', data[i+3:i+5])[0]
                        w = struct.unpack('>H', data[i+5:i+7])[0]
                        return (w, h)
                    return (0, 0)
                # Segment standard, on skip les (length) octets
                if i + 1 < n:
                    seg_len = struct.unpack('>H', data[i:i+2])[0]
                    i += seg_len
                else:
                    break
            return (0, 0)
        
        return (0, 0)
    
    def _compute_cover_md5(self, filepath: Path) -> str:
        """[Deprecated] Alias de _extract_cover_info conservé pour compatibilité.
        
        Préférer _extract_cover_info() qui retourne aussi le format et la taille
        sans coût supplémentaire.
        """
        return self._extract_cover_info(filepath).get('cover_md5', '')
