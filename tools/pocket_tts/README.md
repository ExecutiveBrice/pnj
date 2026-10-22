# Pocket TTS : essai français en streaming local

Double-cliquer sur **Tester Pocket TTS.bat** à la racine du projet.
Prérequis : uv et Python 3.12 avec Tkinter. Les fichiers de configuration de
Pocket TTS nécessitent le mode UTF-8 sous Windows ; le lanceur l'active automatiquement.

Cette démo utilise Pocket TTS 3.3.0, son propre environnement Python, son propre
cache et PyTorch CPU. Le moteur intégré aux conversations NPC se trouve dans
`src/npc/engines/pocket.py`, en parallèle de Piper et Qwen. Cette interface conserve
les mesures, exports WAV et essais de clonage sans modifier les campagnes.

Les premiers essais sont consignés dans [BENCHMARK.md](BENCHMARK.md) : à chaud,
premier bloc disponible en 0,11 s et envoyé à PortAudio en 0,33 s sur ce PC.
Les voix prédéfinies Estelle et Marius ont été vérifiées. Le clonage d'un extrait
personnel n'est pas disponible avec les poids accessibles dans cette installation.

## Essayer

1. Conserver **Rapide — 6 couches**, **estelle** et le texte d'exemple.
2. Cliquer sur **Préparer** : téléchargement et chargement du modèle et de la voix.
3. Cliquer sur **Tester et écouter**, puis **Mesurer 3 essais** pour comparer à chaud.
4. Essayer une autre voix. Estelle utilise un échantillon français ; les autres
   références peuvent donner un accent différent.
5. **Qualité — 24 couches** charge un autre modèle, plus volumineux et potentiellement
   plus lent. Il n'est téléchargé qu'à la demande.

La lecture reçoit les vrais blocs de `generate_audio_stream` dès leur disponibilité,
sans attendre une phrase complète. Le modèle et les états vocaux sont réutilisés
entre les essais. Les fichiers WAV et JSON sont conservés dans `outputs/`.

Le bouton **Arrêter** interrompt la génération et la lecture. Pendant le chargement
ou le téléchargement, l'annulation prend effet après cette phase. Un essai interrompu
après production d'audio conserve un WAV partiel, marqué `cancelled` dans le JSON.

## Mesures

- **Premier bloc** : temps jusqu'au premier PCM produit, hors chargement et préparation de voix.
- **Envoi audio** : temps jusqu'au premier bloc envoyé à PortAudio. Le tampon de la
  carte son et le silence initial du signal ne sont pas une mesure acoustique du premier mot.
- **Génération** : temps de calcul jusqu'au dernier bloc, hors attente de fin de lecture.
- **Temps / audio** : génération divisée par durée audio ; inférieur à 1 est souhaitable
  pour maintenir la lecture en continu.
- **Sous-alimentations** : nombre de signalements PortAudio de manque de données.
  Le signalement initial à l'ouverture du flux peut être compté.

Les JSON enregistrent aussi le chargement, la préparation de voix, le périphérique
de sortie, l'annulation et le détail temporel des blocs. Le temps total de requête
inclut la fin de lecture lorsqu'elle est activée.

## Référence personnalisée

Choisir un court extrait français propre dans le champ facultatif. Il est prioritaire
sur la voix sélectionnée et traité localement. La disponibilité du clonage dépend des
poids accessibles sur Hugging Face. Si les poids de clonage demandent une authentification
ou l'acceptation de leurs conditions, Pocket TTS peut utiliser les poids sans clonage :
les voix prédéfinies fonctionnent alors, mais pas les extraits personnalisés. La démo
n'accepte aucune condition et ne configure aucun compte automatiquement.

## Commandes PowerShell

Depuis la racine du projet :

```powershell
uv run --project tools/pocket_tts --python 3.12 --locked python -X utf8 tools/pocket_tts/demo.py
```

Mesures sans interface, avec écoute progressive :

```powershell
uv run --project tools/pocket_tts --python 3.12 --locked python -X utf8 tools/pocket_tts/demo.py --text "Bonjour, voyageurs. Entrez donc !" --repeat 3 --play
```

Retirer `--play` pour mesurer la génération seule. Utiliser `--reference CHEMIN.wav`
pour un extrait personnel, ou `--language french_24l` pour le profil qualité.

Tests sans modèles ni matériel, avec l'environnement principal :

```powershell
.\.venv\Scripts\python.exe -m pytest tools/pocket_tts/tests -q
```

Sources : https://github.com/kyutai-labs/pocket-tts
API : https://kyutai-labs.github.io/pocket-tts/API%20Reference/python-api/
