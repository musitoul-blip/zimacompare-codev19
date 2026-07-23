"""
providers/strategy_manager.py - ZimaTAG Strategy Manager
Gestion des stratégies d'extraction par tag et format

Corrections appliquées :
  [17] Warnings au chargement des stratégies :
       - Si une stratégie chargée depuis le fichier référence un provider
         inconnu (typo dans table_provider.tsv), un WARNING est loggé.
       - Si une stratégie référence une extension non supportée par le
         provider concerné, un WARNING est loggé.
       - Si une stratégie a un Provider_Key vide ou un Tag_Common vide,
         WARNING également.
       - Si une ligne du fichier est mal formée (colonnes manquantes,
         priorité non parsable), WARNING + ligne ignorée.
       
       La validation est non-bloquante : les stratégies invalides sont
       écartées, les valides sont chargées normalement. Cela permet de
       diagnostiquer rapidement une typo dans table_provider.tsv (ex:
       'mp3_natif' au lieu de 'mp3_native') sans planter au démarrage.
       
       Liste des providers connus définie dans KNOWN_PROVIDERS — à
       compléter si de nouveaux providers sont ajoutés.
"""
import pandas as pd
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from core import logger, config


@dataclass
class TagStrategy:
    """Stratégie d'extraction pour un tag"""
    tag_common: str
    extension: str
    provider: str
    provider_key: str
    priority: int


class StrategyManager:
    """Gestionnaire de stratégies d'extraction"""
    
    # [17] Liste des providers reconnus. Toute valeur hors de cette liste
    # dans la colonne Provider du fichier de stratégies déclenche un warning.
    # Centralisée ici pour avoir un seul endroit à mettre à jour quand on
    # ajoute un nouveau provider.
    KNOWN_PROVIDERS = {'custom', 'mp3_native', 'mutagen'}
    
    # [17] Extensions audio reconnues (cohérent avec config.AUDIO_EXTENSIONS).
    # Les extensions du fichier de stratégies hors de cet ensemble déclenchent
    # un warning.
    KNOWN_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.mp4'}
    
    DEFAULT_STRATEGIES = [
        # MP3
        ('album', '.mp3', 'custom', 'Album', 1),
        ('albumartist', '.mp3', 'custom', 'Album Artist', 1),
        ('artist', '.mp3', 'custom', 'Artist', 1),
        ('bitrate', '.mp3', 'custom', 'Bitrate (kbps)', 1),
        ('channels', '.mp3', 'custom', 'Channels', 1),
        ('codec', '.mp3', 'custom', 'Codec', 1),
        ('disc', '.mp3', 'custom', 'Disc', 1),
        ('disctotal', '.mp3', 'custom', 'Total Discs', 1),
        ('duration', '.mp3', 'custom', 'Duration (s)', 1),
        ('encoder', '.mp3', 'custom', 'Encoder', 1),
        ('genre', '.mp3', 'custom', 'Genre', 1),
        ('id3_version', '.mp3', 'mp3_native', 'id3_version', 1),
        ('samplerate', '.mp3', 'custom', 'Samplerate (Hz)', 1),
        ('title', '.mp3', 'custom', 'Title', 1),
        ('track', '.mp3', 'custom', 'Track', 1),
        ('tracktotal', '.mp3', 'custom', 'Total Tracks on Disc', 1),
        ('year', '.mp3', 'custom', 'Year', 1),
        # M4A
        ('album', '.m4a', 'custom', 'Album', 1),
        ('albumartist', '.m4a', 'custom', 'Album Artist', 1),
        ('artist', '.m4a', 'custom', 'Artist', 1),
        ('bitdepth', '.m4a', 'mutagen', 'bits_per_sample', 1),
        ('bitrate', '.m4a', 'mutagen', 'bitrate', 1),
        ('channels', '.m4a', 'custom', 'Channels', 1),
        ('codec', '.m4a', 'custom', 'Codec', 1),
        ('disc', '.m4a', 'custom', 'Disc', 1),
        ('disctotal', '.m4a', 'custom', 'Total Discs', 1),
        ('duration', '.m4a', 'custom', 'Duration (s)', 1),
        ('encoder', '.m4a', 'custom', 'Encoder', 1),
        ('genre', '.m4a', 'mutagen', 'genre', 1),
        ('genre', '.m4a', 'custom', 'Genre', 2),
        ('samplerate', '.m4a', 'custom', 'Samplerate (Hz)', 1),
        ('title', '.m4a', 'custom', 'Title', 1),
        ('track', '.m4a', 'custom', 'Track', 1),
        ('tracktotal', '.m4a', 'custom', 'Total Tracks on Disc', 1),
        ('year', '.m4a', 'custom', 'Year', 1),
        # FLAC
        ('album', '.flac', 'custom', 'Album', 1),
        ('albumartist', '.flac', 'custom', 'Album Artist', 1),
        ('artist', '.flac', 'custom', 'Artist', 1),
        ('bitrate', '.flac', 'custom', 'Bitrate (kbps)', 1),
        ('channels', '.flac', 'custom', 'Channels', 1),
        ('codec', '.flac', 'custom', 'Codec', 1),
        ('disc', '.flac', 'custom', 'Disc', 1),
        ('disctotal', '.flac', 'custom', 'Total Discs', 1),
        ('duration', '.flac', 'custom', 'Duration (s)', 1),
        ('encoder', '.flac', 'custom', 'Encoder', 1),
        ('genre', '.flac', 'custom', 'Genre', 1),
        ('samplerate', '.flac', 'custom', 'Samplerate (Hz)', 1),
        ('title', '.flac', 'custom', 'Title', 1),
        ('track', '.flac', 'custom', 'Track', 1),
        ('tracktotal', '.flac', 'custom', 'Total Tracks on Disc', 1),
        ('year', '.flac', 'custom', 'Year', 1),
    ]
    
    def __init__(self, strategy_file: Optional[Path] = None):
        self.strategy_file = strategy_file or config.strategy_path
        self.strategies: Dict[Tuple[str, str], List[TagStrategy]] = {}
        self._load_strategies()
    
    def _load_strategies(self):
        """Charge les stratégies depuis fichier ou défaut"""
        if self.strategy_file.exists():
            self._load_from_file()
        else:
            self._load_defaults()
            self._save_to_file()
    
    # ------------------------------------------------------------------
    # Validation des stratégies (correction [17])
    # ------------------------------------------------------------------
    @staticmethod
    def _is_blank(v) -> bool:
        """[17] Retourne True si la valeur est vide au sens du chargement
        des stratégies. Couvre :
          - None
          - chaîne vide ou ne contenant que des espaces
          - NaN pandas (cas typique d'une cellule TSV vide lue par
            pandas.read_csv : la valeur est np.nan, pas '')
        """
        if v is None:
            return True
        # Détection NaN sans dépendre de pandas dans la signature publique
        try:
            # pd.isna couvre NaN/NaT/None ; True/False/0 retournent False
            import pandas as _pd
            if _pd.isna(v):
                return True
        except Exception:
            pass
        if isinstance(v, str) and not v.strip():
            return True
        return False
    
    def _validate_strategy(
        self,
        tag_common: str,
        extension: str,
        provider: str,
        provider_key: str,
        priority,
        source_idx: int,
    ) -> Optional[Tuple[str, str, str, str, int]]:
        """Valide une stratégie. Retourne le tuple normalisé ou None.
        
        [17] Loggue un WARNING précis pour chaque problème détecté afin
        d'aider l'utilisateur à diagnostiquer une typo dans le fichier
        de stratégies. Retourne None si la stratégie est invalide
        (la stratégie est alors écartée, les autres restent chargées).
        
        Cas vérifiés :
          1. Tag_Common vide ou non-string  -> rejet
          2. Extension vide ou non-string   -> rejet
          3. Extension hors KNOWN_EXTENSIONS -> warning (mais on garde,
             pour permettre l'ajout futur d'extensions sans bloquer)
          4. Provider hors KNOWN_PROVIDERS  -> rejet (sinon le provider
             ne sera jamais résolu et le tag sera silencieusement vide)
          5. Provider_Key vide              -> rejet
          6. Priorité non parsable          -> rejet
        """
        prefix = f"[StrategyManager] ligne {source_idx}"
        
        # 1. Tag_Common
        if self._is_blank(tag_common):
            logger.warning(f"{prefix}: Tag_Common vide ou invalide — stratégie ignorée")
            return None
        tag_common = str(tag_common).strip()
        
        # 2. Extension
        if self._is_blank(extension):
            logger.warning(
                f"{prefix} (tag={tag_common!r}): Extension vide — stratégie ignorée"
            )
            return None
        extension = str(extension).strip().lower()
        if not extension.startswith('.'):
            extension = '.' + extension
        
        # 3. Extension non standard : warning mais on garde (permet l'ajout
        # futur de formats sans avoir à toucher ce module).
        if extension not in self.KNOWN_EXTENSIONS:
            logger.warning(
                f"{prefix} (tag={tag_common!r}): extension {extension!r} non "
                f"reconnue (connues : {sorted(self.KNOWN_EXTENSIONS)}). "
                f"La stratégie est conservée mais ne sera utilisée que "
                f"si un fichier avec cette extension est scanné."
            )
        
        # 4. Provider
        if self._is_blank(provider):
            logger.warning(
                f"{prefix} (tag={tag_common!r}, ext={extension}): "
                f"Provider vide — stratégie ignorée"
            )
            return None
        provider = str(provider).strip()
        if provider not in self.KNOWN_PROVIDERS:
            logger.warning(
                f"{prefix} (tag={tag_common!r}, ext={extension}): "
                f"provider {provider!r} inconnu (connus : "
                f"{sorted(self.KNOWN_PROVIDERS)}). Le tag ne pourrait "
                f"pas être extrait — stratégie ignorée."
            )
            return None
        
        # 5. Provider_Key
        if self._is_blank(provider_key):
            logger.warning(
                f"{prefix} (tag={tag_common!r}, ext={extension}, "
                f"provider={provider}): Provider_Key vide — stratégie ignorée"
            )
            return None
        provider_key = str(provider_key).strip()
        
        # 6. Priorité
        try:
            prio = int(priority) if priority is not None else 1
        except (TypeError, ValueError):
            logger.warning(
                f"{prefix} (tag={tag_common!r}, ext={extension}): "
                f"priorité {priority!r} non parsable, valeur 1 utilisée "
                f"par défaut"
            )
            prio = 1
        
        return (tag_common, extension, provider, provider_key, prio)
    
    def _load_from_file(self):
        """Charge depuis fichier Excel/CSV.
        
        [17] Chaque ligne est validée individuellement. Les lignes
        invalides sont loggées (warning) et ignorées, les autres sont
        chargées normalement. En cas d'erreur globale (fichier illisible,
        colonnes manquantes), on retombe sur les stratégies par défaut.
        """
        try:
            if self.strategy_file.suffix == '.xlsx':
                df = pd.read_excel(self.strategy_file)
            else:
                df = pd.read_csv(self.strategy_file, sep='\t')
            
            # [17] Vérification globale des colonnes attendues
            required_cols = {'Tag_Common', 'Extension', 'Provider', 'Provider_Key'}
            missing_cols = required_cols - set(df.columns)
            if missing_cols:
                logger.error(
                    f"[StrategyManager] {self.strategy_file} : colonnes "
                    f"obligatoires manquantes {sorted(missing_cols)} — "
                    f"chargement des stratégies par défaut"
                )
                self._load_defaults()
                return
            
            n_loaded = 0
            n_skipped = 0
            for idx, row in df.iterrows():
                # idx peut ne pas être 0-based si le DataFrame a un index
                # personnalisé, mais c'est suffisant pour les warnings.
                # On ajoute +2 pour donner un numéro de ligne "humain"
                # compatible avec un éditeur (header en ligne 1).
                line_no = int(idx) + 2 if isinstance(idx, (int, float)) else idx
                
                priority_val = row.get('Priorité', row.get('Priority', 1))
                
                validated = self._validate_strategy(
                    tag_common=row.get('Tag_Common', ''),
                    extension=row.get('Extension', ''),
                    provider=row.get('Provider', ''),
                    provider_key=row.get('Provider_Key', ''),
                    priority=priority_val,
                    source_idx=line_no,
                )
                if validated is None:
                    n_skipped += 1
                    continue
                
                tc, ext, prov, pkey, prio = validated
                strat = TagStrategy(tc, ext, prov, pkey, prio)
                key = (tc, ext)
                if key not in self.strategies:
                    self.strategies[key] = []
                self.strategies[key].append(strat)
                n_loaded += 1
            
            # Tri par priorité
            for key in self.strategies:
                self.strategies[key].sort(key=lambda x: x.priority)
            
            if n_skipped > 0:
                logger.warning(
                    f"[StrategyManager] {n_loaded} stratégie(s) chargée(s), "
                    f"{n_skipped} ignorée(s) pour cause d'erreur de validation. "
                    f"Voir warnings ci-dessus pour détail."
                )
            else:
                logger.info(f"Stratégies chargées: {n_loaded} règles ({len(self.strategies)} clés)")
            
            # Si aucune stratégie valide n'a été chargée, on retombe sur les
            # défauts pour que l'application reste fonctionnelle.
            if n_loaded == 0:
                logger.error(
                    "[StrategyManager] aucune stratégie valide dans le "
                    "fichier — chargement des stratégies par défaut"
                )
                self._load_defaults()
            
        except Exception as e:
            logger.error(f"Erreur chargement stratégies: {e}")
            self._load_defaults()
    
    def _load_defaults(self):
        """Charge stratégies par défaut"""
        for tag, ext, prov, key, prio in self.DEFAULT_STRATEGIES:
            strat = TagStrategy(tag, ext, prov, key, prio)
            k = (tag, ext)
            if k not in self.strategies:
                self.strategies[k] = []
            self.strategies[k].append(strat)
        
        logger.info("Stratégies par défaut chargées")
    
    def _save_to_file(self):
        """Sauvegarde stratégies vers fichier"""
        try:
            rows = []
            for strats in self.strategies.values():
                for s in strats:
                    rows.append({
                        'Tag_Common': s.tag_common,
                        'Extension': s.extension,
                        'Provider': s.provider,
                        'Provider_Key': s.provider_key,
                        'Priorité': s.priority
                    })
            
            df = pd.DataFrame(rows)
            self.strategy_file.parent.mkdir(parents=True, exist_ok=True)
            
            if self.strategy_file.suffix == '.xlsx':
                df.to_excel(self.strategy_file, index=False)
            else:
                df.to_csv(self.strategy_file, sep='\t', index=False)
            
            logger.info(f"Stratégies sauvegardées: {self.strategy_file}")
            
        except Exception as e:
            logger.error(f"Erreur sauvegarde stratégies: {e}")
    
    def get_strategy(self, tag: str, extension: str) -> Optional[TagStrategy]:
        """Récupère la stratégie pour un tag et format"""
        key = (tag, extension)
        strats = self.strategies.get(key, [])
        return strats[0] if strats else None
    
    def get_all_strategies(self, extension: str) -> List[TagStrategy]:
        """Récupère toutes les stratégies pour un format"""
        result = []
        for (tag, ext), strats in self.strategies.items():
            if ext == extension:
                result.extend(strats)
        return result
    
    def get_tags_for_extension(self, extension: str) -> List[str]:
        """Liste les tags configurés pour un format"""
        tags = set()
        for (tag, ext) in self.strategies.keys():
            if ext == extension:
                tags.add(tag)
        return sorted(tags)
