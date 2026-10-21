# Lancer NPC sur Windows 11 ou macOS

L’interface de bureau lance directement le moteur Python local. Elle permet
de choisir une campagne, préparer les modèles, discuter au clavier ou au
micro, puis terminer la session avec un résumé mémorisé. WSL n’est pas requis.

## Première installation

1. Installer **Python 3.12** depuis <https://www.python.org/downloads/>.
   Conserver l’installation de **Tcl/Tk et IDLE**. Sous Windows, activer
   également **Add python.exe to PATH**.
2. Installer **uv** selon <https://docs.astral.sh/uv/getting-started/installation/>.
   Fermer et rouvrir le terminal après son installation.
3. Installer **Ollama** depuis <https://ollama.com/download>, puis lancer
   l’application. Le serveur doit être disponible à `http://localhost:11434`,
   sauf configuration personnalisée dans le `config.toml` de la campagne.
4. Récupérer le dossier complet du projet sur l’ordinateur.

Depuis un terminal ouvert dans le dossier du projet :

```bash
uv run --python 3.12 --frozen npc gui
```

Pour utiliser Pocket TTS depuis le terminal, ajouter `--extra pocket` avant
`npc`. Sous Windows, **Lancer NPC.bat** installe déjà cette option et utilise
PyTorch sur CPU ; les dépendances CUDA de Whisper sont activées si une carte
NVIDIA est détectée.

Le premier lancement installe les dépendances Python. Il ne télécharge pas
encore les modèles de conversation ou de parole.

Pour vérifier que le Python choisi dispose de Tkinter :

```bash
uv run --python 3.12 --frozen python -m tkinter
```

Si Tcl/Tk est absent, installer un Python qui l’inclut puis recréer
l’environnement `.venv` avec cet interpréteur. On peut sélectionner explicitement
son chemin avec `uv sync --python CHEMIN_VERS_PYTHON --frozen` et lancer ensuite
`uv run --frozen npc gui`. Les lanceurs fournis sélectionnent Python 3.12.

## Lancement par double-clic

- **Windows 11** : ouvrir `Lancer NPC.bat`.
- **macOS** : ouvrir `Lancer NPC.command`. Si le droit d’exécution a été perdu
  pendant le transfert, utiliser `chmod +x "Lancer NPC.command"` depuis le dossier
  du projet.

Ces fichiers lancent le projet source avec uv. Ils nécessitent les prérequis
ci-dessus ; ce ne sont pas des applications autonomes `.exe` ou `.app`.

## Utilisation

Pour les voix Pocket TTS, choisir **Pocket TTS** dans le cadre **Voix**, puis
une voix (`estelle`, `marius`, `javert`, `jean`, `alba` ou `anna`). La sélection
dans **Langue des personnages** détermine si la voix est affectée à toute la
campagne par défaut ou à un personnage. **Enregistrer** sauvegarde ce choix ;
**Écouter cette voix** lit un extrait sans Ollama ni reconnaissance vocale.
Le premier essai peut télécharger des fichiers. Un essai recharge son moteur
à chaque lancement ; pendant une session, le modèle et les voix restent chargés.

Pocket utilise par défaut le petit modèle français `french`, sur CPU, et lit
les blocs audio dès leur génération. Les voix sont préparées et le modèle
préchauffé au démarrage de la session. Piper reste disponible ; changer de
moteur conserve les affectations pour retrouver ses voix en revenant dessus.
Le clonage vocal n’est pas activé dans cette intégration.

1. Cliquer sur **Parcourir…** pour ouvrir une campagne existante, ou **Créer…**
   pour générer les fichiers d’un personnage exemple. Les fichiers existants
   ne sont jamais remplacés. Le dossier `examples/rusty-lantern` permet aussi
   de découvrir une campagne prête à jouer.
2. Dans **Langue des personnages**, choisir **Campagne (par défaut)** ou un
   personnage, puis sa langue : **English**, **Français (France)**,
   **Français (Suisse)**, **Français (Canada / Québec)** ou **Français (Belgique)**.
   **Suivre la campagne** retire la préférence propre au personnage. Les choix
   sont enregistrés dans `config.toml`. Ils se modifient entre deux sessions.
   Cliquer sur **Préparer les modèles**. Cette action peut télécharger plusieurs
   gigaoctets : le modèle Ollama, Whisper et les modèles/voix du moteur choisi. Les
   résultats et erreurs s’affichent dans la fenêtre. Pour LM Studio, Jan ou un
   autre serveur compatible, charger le modèle depuis cette application.
3. Cocher **Activer le micro** si souhaité, puis cliquer sur **Démarrer**.
   En cas de problème audio ou de modèle vocal manquant, la saisie reste
   disponible tant que le serveur de conversation fonctionne.
4. Choisir **Parole du joueur** et saisir une phrase : elle est envoyée au
   personnage. Le mode **Note du MJ** donne une instruction hors personnage.
   Le mode **Commande** accepte `/npc nom`, `/save`, `/reload`, `/help`, etc.
5. Pour parler au micro, cliquer sur **Enregistrer**, parler, puis cliquer sur
   **Envoyer l’enregistrement**. Aucun raccourci clavier global n’est nécessaire.
6. Cliquer sur **Terminer et mémoriser** pour attendre les réponses en cours,
   écrire le résumé et arrêter le moteur. Fermer une session active déclenche
   également cette fin de session. La fenêtre reste réactive pendant l’attente.

Le bouton **Forcer l’arrêt…** permet d’interrompre un démarrage, une préparation
ou une fermeture bloquée. Il demande confirmation car le résumé final et les
téléchargements en cours peuvent rester incomplets. Les transcriptions déjà
écrites restent dans `sessions/`.

## Configuration et limites

- Éditer `config.toml` dans le dossier de campagne pour choisir le serveur,
  le modèle et la voix. Les valeurs par défaut utilisent
  `qwen2.5:7b-instruct`, Whisper `small` et Piper `en_GB-alba-medium`.
- Les personnages répondent dans la langue choisie. Le français utilise par défaut
  la voix Piper `fr_FR-siwis-medium`. Les voix personnalisées dans `[tts] voice`
  et `[tts.voices]` restent prioritaires ; choisir une voix adaptée à la langue.
- Les variantes suisses, canadiennes et belges guident le vocabulaire et les
  expressions. L’accent audio dépend du modèle vocal : une variante régionale
  ne transforme pas l’accent de la voix Piper.
- En configuration manuelle, `language = "fr"` en tête de fichier choisit le
  français pour la campagne. Sous `[character_languages]`, écrire par exemple
  `korval = "fr-CA"` pour `characters/korval.md`, ou `character = "fr-CH"` pour
  le personnage racine. Les autres codes sont `en`, `fr` et `fr-BE`.
- Autoriser l’accès au micro pour Python ou l’application de lancement dans
  les paramètres de confidentialité Windows/macOS. Vérifier également les
  périphériques d’entrée et de sortie par défaut du système.
- Le bouton de micro de l’interface utilise un démarrage/arrêt explicite,
  indépendamment du mode de raccourci configuré pour le terminal Linux.
- Sous macOS, Whisper fonctionne sur CPU en l’absence de CUDA ; le moteur de
  conversation Ollama choisit son accélération indépendamment.
- L’affichage OBS reste accessible via `npc run ... --overlay` en terminal.

Le téléchargement initial nécessite Internet. Les modèles par défaut et le
moteur peuvent ensuite fonctionner hors ligne avec un serveur local.
