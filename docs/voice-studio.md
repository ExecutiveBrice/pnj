# Studio voix et histoires

Le serveur mobile sert l’interface web sur `/` depuis la console et l’application
PC. Activer `[mobile] enabled = true` puis démarrer la session. Pour un téléphone,
utiliser l’adresse LAN du PC et la configuration réseau décrite dans [mobile.md](mobile.md).
Les contrôles d’accès `authorize` existants couvrent aussi la page et les WAV.

## Installation et moteurs

```powershell
uv sync --python 3.12 --locked --extra pocket --extra qwen
```

Ajouter `--extra cuda` pour conserver les bibliothèques CUDA de Whisper si utilisées.
Piper reste le moteur par défaut. `src/npc/engines/` contient les adaptateurs
Piper, Pocket et Qwen ; `npc.tts.SpeakerFactory` partage le lecteur audio et met
les voix en cache. `npc.pocket` conserve les imports de compatibilité. Les outils
Pocket (mesures, références audio, exports, interface Tk) sont dans `tools/pocket_tts/`,
avec leur environnement et cache indépendants conservés après le déplacement.

Pour les dialogues PNJ, choisir Qwen dans le sélecteur PC ou configurer :

```toml
[tts]
backend = "qwen"
voice = "Une voix masculine grave, calme, légèrement rocailleuse."
qwen_model = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
qwen_device = "auto"
qwen_language = "French"
```

Le studio utilise ces paramètres `qwen_model` et `qwen_device` même quand
`backend = "piper"` ou `"pocket"`. Il ne change pas les voix des PNJ. Le switch
PC conserve les affectations des trois moteurs dans `tts.voice_presets`.
Qwen est une dépendance optionnelle pour Python 3.11–3.13 ; les lanceurs utilisent
Python 3.12. Le premier usage charge/télécharge les poids ; un chemin de modèle
local dans `qwen_model` permet de préparer une installation hors ligne.

## Extension du protocole mobile 1

`hello.capabilities.voice_design` et `story_generation` indiquent la présence du
paquet `qwen_tts`, sans charger les poids. Les erreurs d’import, de téléchargement,
de mémoire ou du LLM sont rapportées par `studio_error`, avec détails dans le journal PC.
Ces capacités sont indépendantes de `audio_output`, qui concerne la voix des PNJ.
Les anciens clients peuvent ignorer les champs supplémentaires.

| Commande | Champs | Résultat |
| --- | --- | --- |
| `studio_list` | aucun | `studio_catalog` : `available`, `languages`, `voices`, `stories` |
| `voice_design` | `title`, `description`, `text` (aperçu, facultatif), `language` | Un profil vocal et son aperçu audio |
| `narrate_text` | `title`, `text`, `language`, `voice_id` ou `description` | Lecture exacte du récit fourni |
| `generate_story` | `title`, `prompt`, `language`, `voice_id` ou `description` | Rédaction par le LLM, puis narration Qwen |
| `studio_cancel` | aucun | Annule le travail de cette connexion |

Exemple :

```json
{"type":"voice_design","title":"La conteuse","description":"Une voix féminine grave, chaleureuse, au débit lent.","text":"Bienvenue au coin du feu.","language":"French"}
```

La connexion demandeuse reçoit `studio_started`, puis `studio_progress`
(`message`, `stage`, et `current`/`total` pendant la synthèse), puis :

```json
{"type":"studio_result","category":"voices","item":{"id":"<32 caractères hexadécimaux>","title":"La conteuse","text":"Bienvenue au coin du feu.","description":"Une voix féminine grave, chaleureuse, au débit lent.","language":"French","created_at":"<date ISO UTC>","duration":2.4,"audio_url":"/studio/audio/<id>.wav"}}
```

Réutiliser `item.id` comme `voice_id` dans `narrate_text` ou `generate_story`.
`category` vaut `stories` pour ces deux commandes. L’élément contient le texte
final et, si utilisé, `voice_id`. `studio_list` retrouve les mêmes éléments après
reconnexion. Lire `audio_url` avec un lecteur WAV natif ou HTML `<audio controls>` ;
le serveur accepte les plages HTTP (`Range`) pour la navigation et la réécoute.
Les nouveaux audios du studio ne sont pas diffusés sur le flux PCM des dialogues
PNJ : seul le demandeur reçoit leur résultat, puis ils sont dans la bibliothèque
partagée de la campagne. Il n’y a pas de lecture forcée sur les haut-parleurs PC.

Une erreur de validation ou un studio occupé renvoie `error`. Une panne de génération
renvoie `studio_error`; une annulation renvoie `studio_cancelled`. Un seul travail
de studio peut s’exécuter à la fois pour ce serveur ; les demandes supplémentaires
sont rejetées sans file illimitée. Déconnecter le demandeur ou arrêter le serveur
annule aussi sa génération. Le réseau reste réactif pendant l’inférence.

Limites : titre 120 caractères, description 1 000, aperçu 500, récit 12 000,
idée 2 000. `language` utilise les noms Qwen (`French`, `English`, `Auto`, etc.).
Les messages JSON restent limités à 64 Kio. La rédaction reçoit uniquement l’idée
et la langue : aucun secret, fiche, instruction MJ ou historique de campagne.

## Conservation et limites du modèle

Les fichiers vivent dans `<campagne>/voice_studio/` : profils `voices/*.json`,
récits `stories/*.json`, audio `audio/*.wav`. Écritures temporaires puis renommage ;
une erreur/annulation supprime le WAV incomplet et ne crée pas d’entrée de bibliothèque.
Le studio reste consultable sans installer Qwen si des créations existent déjà.

L’API officielle `generate_voice_design(text, language, instruct)` renvoie un
signal complet. Les longs récits sont divisés en passages de 400 caractères maximum.
Il ne s’agit pas d’un streaming audio natif. L’annulation attend le chargement
ou l’inférence du passage courant ; l’audio de ce passage est ensuite abandonné.
La description de voix est réutilisée, mais VoiceDesign ne fixe pas une identité
vocale stable. Une telle garantie demanderait aussi le modèle Base et un flux de
clonage, distinct de cette intégration.

Le dépôt privilégie PyTorch CPU, avec torchaudio de même version (2.6). Un PyTorch
CUDA compatible est nécessaire pour `qwen_device = "cuda:0"`. FlashAttention
n’est pas requis : l’adaptateur utilise SDPA. Les avertissements SoX/FlashAttention
du paquet peuvent apparaître à l’import ; le chemin VoiceDesign utilisé ici
n’appelle pas de transformation SoX.

Référence : [Qwen3-TTS, API VoiceDesign officielle](https://github.com/QwenLM/Qwen3-TTS#voice-design).
