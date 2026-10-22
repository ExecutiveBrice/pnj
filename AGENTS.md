# Repères de maintenance

## Lire et lancer le projet

- Lire `README.md` pour les commandes et `ARCHITECTURE.md` pour la carte du code.
- Sources dans `src/npc/`, ressources distribuées dans `templates/` et `static/`.
- Installer avec `uv sync --frozen --group dev`. Le verrou `uv.lock` est conservé.
- Vérifier avec `.venv/bin/ruff check .` et `.venv/bin/pytest` (ou `uv run`).
- Construire avec `uv build --no-sources` après une modification du packaging.
- Les tests `integration` sont exclus par défaut. Les tests Tk exigent un
  affichage et `NPC_TEST_GUI=1`.

## Contrats à préserver

- `NPCApp` sérialise les tours dans son worker. Les interfaces et callbacks
  réseau passent par ses méthodes publiques pour soumettre les actions.
- Tout état conversationnel reste dans le `CharacterSlot` de son PNJ.
- Les corps des secrets bloqués ne doivent jamais entrer dans un prompt.
- Le lore ne charge que les chemins explicitement prévus ; ne pas parcourir
  récursivement les scénarios MJ ou les autres fiches.
- Un cache de lecture du lore vit le temps d'un chargement ou d'un contrôle,
  jamais au-delà d'un `/reload`.
- Distinguer le protocole joueur et le canal mobile MJ ; le compagnon MJ est
  activé par défaut via `mobile.secret_approval`.
- Garder les imports des modèles lourds paresseux et le fonctionnement sans
  microphone, GPU ni serveur dans les tests ordinaires.
- Préserver l'annulation audio, les files bornées et les écritures atomiques.

## Fichiers et tests

- Les campagnes personnelles vivent hors des sources ; `campaigns/` est ignoré.
- Construire les données de test dans `tmp_path`, sans chemins personnels ni
  dépendance à un dépôt de campagne voisin.
- Conserver `src/npc/pocket.py` pour les imports de compatibilité.
- Les deux scripts de `scripts/` sondent les prompts avec un vrai LLM ; ils sont
  manuels et ne remplacent pas les tests unitaires.
- Les voix et histoires sont des données de campagne : `voix/` pour les profils
  et aperçus, `histoires/` pour les textes Markdown, WAV et métadonnées JSON.
  Les exemples TOML résident dans le dépôt CampagneBaston, hors du moteur.
  Conserver la lecture des anciennes bibliothèques `voice_studio/`.
- Les modèles ONNX, caches et réglages IDE sont ignorés.
- Adapter les documents existants quand le comportement change ; éviter de
  multiplier les bilans, roadmaps ou copies de configuration.
